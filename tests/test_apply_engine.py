"""Tests for applicant/engine.py structured apply: field resolution, LLM answers,
and the submit-and-confirm gate (only marks success on a real confirmation)."""
import applicant.engine as engine_mod
from applicant.engine import AutoApplicant


class FakeElement:
    def __init__(self, *, value="", tag="input", attrs=None):
        self._value = value
        self._tag = tag
        self._attrs = attrs or {}
        self.filled = None
        self.clicked = False
        self.uploaded = None

    async def fill(self, v):
        self.filled = v
        self._value = v

    async def click(self):
        self.clicked = True

    async def set_input_files(self, p):
        self.uploaded = p

    async def input_value(self):
        return self._value

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def evaluate(self, script):
        if "tagName" in script:
            return self._tag
        return self._attrs.get("label", "")


class FakePage:
    """Minimal Playwright Page stand-in: maps selectors -> element(s)."""
    def __init__(self, mapping=None, url="https://example.com/apply", all_fields=None):
        self._mapping = mapping or {}
        self.url = url
        self._all = all_fields or []
        self.screenshotted = False

    async def query_selector(self, sel):
        return self._mapping.get(sel)

    async def query_selector_all(self, sel):
        return self._all

    async def screenshot(self, path=None):
        self.screenshotted = True


class TestResolveFieldValue:
    async def test_static_match_wins(self):
        app = AutoApplicant()
        v = await app._resolve_field_value("Email address", "CL", is_freetext=False)
        from prompts.generator import COMMON_ANSWERS
        assert v == COMMON_ANSWERS["email"]

    async def test_freetext_falls_back_to_llm_and_caches(self, monkeypatch):
        engine_mod._ANSWER_CACHE.clear()
        calls = []

        def fake_llm(question, job_title="", company=""):
            calls.append(question)
            return "Generated answer."

        monkeypatch.setattr(engine_mod, "generate_form_answer", fake_llm)
        app = AutoApplicant()
        q = "Why do you want to work here specifically?"
        v1 = await app._resolve_field_value(q, "CL", is_freetext=True)
        v2 = await app._resolve_field_value(q, "CL", is_freetext=True)
        assert v1 == "Generated answer."
        assert v2 == "Generated answer."
        assert calls == [q]  # cached: LLM called once

    async def test_non_freetext_unmatched_returns_empty(self):
        app = AutoApplicant()
        v = await app._resolve_field_value("some unknown short label", "CL", is_freetext=False)
        assert v == ""


class TestSubmitAndConfirm:
    async def _run(self, page, monkeypatch, dry_run):
        monkeypatch.setattr(engine_mod, "APPLY_DRY_RUN", dry_run)
        app = AutoApplicant()
        return await app._submit_and_confirm(
            page, {"id": 1}, "greenhouse",
            submit_selectors=["#submit"],
            confirm_selectors=["#done"],
            confirm_url_substrings=["thank"],
        )

    async def test_dry_run_does_not_submit(self, monkeypatch):
        btn = FakeElement()
        page = FakePage({"#submit": btn})
        res = await self._run(page, monkeypatch, dry_run=True)
        assert res.success is False
        assert res.method == "form_filled"
        assert btn.clicked is False

    async def test_confirmed_by_url(self, monkeypatch):
        btn = FakeElement()
        page = FakePage({"#submit": btn}, url="https://boards.greenhouse.io/x/thank-you")
        res = await self._run(page, monkeypatch, dry_run=False)
        assert btn.clicked is True
        assert res.success is True
        assert res.method == "submitted"

    async def test_confirmed_by_element(self, monkeypatch):
        page = FakePage({"#submit": FakeElement(), "#done": FakeElement()},
                        url="https://x/apply")
        res = await self._run(page, monkeypatch, dry_run=False)
        assert res.success is True

    async def test_submitted_but_unconfirmed_is_not_success(self, monkeypatch):
        page = FakePage({"#submit": FakeElement()}, url="https://x/apply")
        res = await self._run(page, monkeypatch, dry_run=False)
        assert res.success is False
        assert res.method == "form_filled"

    async def test_no_submit_button_is_not_success(self, monkeypatch):
        page = FakePage({}, url="https://x/apply")
        res = await self._run(page, monkeypatch, dry_run=False)
        assert res.success is False
        assert res.method == "form_filled"


class TestSelectTarget:
    """Dropdown answers must disambiguate authorization vs sponsorship correctly."""

    def setup_method(self):
        self.app = AutoApplicant()

    def test_requires_sponsorship_yes(self):
        assert self.app._select_target("Will you now or in future require visa sponsorship?") == "yes"

    def test_authorized_without_sponsorship_is_no(self):
        # Mentions 'sponsorship' but is an authorization question → must be "no".
        assert self.app._select_target("Are you legally authorized to work without sponsorship?") == "no"

    def test_authorized_in_country_is_no(self):
        assert self.app._select_target("Are you authorized to work in the United States?") == "no"

    def test_demographic_declines(self):
        assert self.app._select_target("Gender") == "decline"
        assert self.app._select_target("Veteran status") == "decline"

    def test_referral_source(self):
        assert self.app._select_target("How did you hear about us?") == "linkedin"

    def test_unknown_label_skipped(self):
        assert self.app._select_target("Favourite colour") is None


class TestApplyCap:
    async def test_caps_per_run_and_buckets_manual(self, monkeypatch):
        import applicant.engine as eng
        monkeypatch.setattr(eng, "MAX_APPLIES_PER_RUN", 2)

        calls = []

        class FakeApplicant:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def apply_to_job(self, job):
                calls.append(job["id"])
                return eng.ApplyResult(success=False, method="manual_handoff", message="x")

        monkeypatch.setattr(eng, "AutoApplicant", lambda headless=True: FakeApplicant())

        async def no_sleep(*a, **k):
            return None

        monkeypatch.setattr(eng.asyncio, "sleep", no_sleep)

        jobs = [{"id": i, "title": "PM", "company": "c", "url": "u"} for i in range(5)]
        res = await eng.apply_to_approved_jobs(jobs)
        assert calls == [0, 1]                 # only 2 attempted
        assert res["total"] == 2
        assert res["skipped_over_cap"] == 3
        assert res["needs_manual"] == 2        # manual_handoff bucketed correctly


class TestSetFirst:
    async def test_fills_first_present_selector(self):
        target = FakeElement()
        page = FakePage({"#b": target})
        app = AutoApplicant()
        ok = await app._set_first(page, ["#a", "#b"], "value")
        assert ok is True
        assert target.filled == "value"

    async def test_empty_value_is_noop(self):
        app = AutoApplicant()
        assert await app._set_first(FakePage({}), ["#a"], "") is False
