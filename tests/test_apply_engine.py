"""Tests for applicant/engine.py structured apply: field resolution, LLM answers,
and the submit-and-confirm gate (only marks success on a real confirmation)."""
import asyncio
import time

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
        self.on_click = None  # optional coroutine: simulate the page navigating

    async def fill(self, v):
        self.filled = v
        self._value = v

    async def click(self):
        self.clicked = True
        if self.on_click is not None:
            await self.on_click()

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


class _FakeKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class FakePage:
    """Minimal Playwright Page stand-in: maps selectors -> element(s)."""
    def __init__(self, mapping=None, url="https://example.com/apply", all_fields=None,
                 missing_required=None):
        self._mapping = mapping or {}
        self.url = url
        self._all = all_fields or []
        self.screenshotted = False
        # What _missing_required_fields' page.evaluate() returns.
        self.missing_required = missing_required or []
        self.keyboard = _FakeKeyboard()

    async def evaluate(self, script, *args):
        return self.missing_required

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
        page = FakePage({"#submit": btn}, url="https://x/apply")

        async def click_navigates():
            page.url = "https://boards.greenhouse.io/x/thank-you"

        btn.on_click = click_navigates
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
        monkeypatch.setattr(engine_mod, "CONFIRM_TIMEOUT_S", 0.2)
        page = FakePage({"#submit": FakeElement()}, url="https://x/apply")
        res = await self._run(page, monkeypatch, dry_run=False)
        assert res.success is False
        # Its own method: the caller records it and refuses to auto-resubmit.
        assert res.method == "submitted_unconfirmed"

    async def test_required_gap_blocks_submit(self, monkeypatch):
        # An incomplete form is never submitted: the ATS would reject it, and a
        # half-filled application under the candidate's name is worse than none.
        btn = FakeElement()
        page = FakePage({"#submit": btn}, url="https://x/apply",
                        missing_required=["Work Authorisation Status", "Country"])
        res = await self._run(page, monkeypatch, dry_run=False)
        assert btn.clicked is False
        assert res.success is False
        assert res.method == "form_filled"
        assert "Work Authorisation Status" in res.message

    async def test_dry_run_reports_required_gaps(self, monkeypatch):
        page = FakePage({"#submit": FakeElement()}, missing_required=["Country"])
        res = await self._run(page, monkeypatch, dry_run=True)
        assert "Country" in res.message

    async def test_no_submit_button_is_not_success(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "CONFIRM_TIMEOUT_S", 0.2)
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


class FakeRadioPage:
    def __init__(self, groups):
        self._groups = groups
        self.clicked = []

    async def evaluate(self, script):
        return self._groups

    async def click(self, sel):
        self.clicked.append(sel)


class TestFillRadios:
    """Ashby uses radio groups for visa/EEO questions — pick the right option."""

    async def test_picks_correct_radio_per_question(self):
        app = AutoApplicant()
        groups = {
            "g1": {"question": "Will you require visa sponsorship?",
                   "options": [{"id": "y1", "label": "Yes"}, {"id": "n1", "label": "No"}]},
            "g2": {"question": "Are you authorized to work without sponsorship?",
                   "options": [{"id": "y2", "label": "Yes"}, {"id": "n2", "label": "No"}]},
            "g3": {"question": "Gender",
                   "options": [{"id": "m", "label": "Male"},
                               {"id": "d", "label": "Decline to self-identify"}]},
        }
        page = FakeRadioPage(groups)
        await app._fill_radios(page)
        assert 'label[for="y1"]' in page.clicked   # sponsorship -> Yes
        assert 'label[for="n2"]' in page.clicked    # authorized-without-sponsorship -> No
        assert 'label[for="d"]' in page.clicked     # gender -> Decline
        assert 'label[for="y2"]' not in page.clicked  # didn't wrongly pick Yes for auth

    async def test_unknown_question_skipped(self):
        app = AutoApplicant()
        page = FakeRadioPage({"g": {"question": "Pineapple on pizza?",
                                    "options": [{"id": "y", "label": "Yes"}]}})
        await app._fill_radios(page)
        assert page.clicked == []


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


class TestEventLoopIsNotBlocked:
    """apply_to_job runs on the bot's event loop (via the apply worker), so the
    blocking Anthropic cover-letter call must go to a thread — otherwise Telegram
    polling and the scheduler stall for the whole request."""

    async def test_cover_letter_call_does_not_block_the_loop(self, monkeypatch):
        def slow_generate(title, company, description=""):
            time.sleep(0.2)  # stand-in for the real, blocking Anthropic request
            return "Cover letter."

        monkeypatch.setattr(engine_mod, "generate_cover_letter", slow_generate)
        monkeypatch.setattr(engine_mod, "get_job_by_id", lambda job_id: None)
        monkeypatch.setattr(engine_mod, "set_cover_letter", lambda job_id, text: None)
        monkeypatch.setattr(engine_mod, "log_action", lambda *a, **k: None)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            # Recruitee has no auto-submit path, so this stops after the cover
            # letter — no browser is launched.
            result = await AutoApplicant().apply_to_job(
                {"id": 1, "title": "PM", "company": "Acme",
                 "url": "https://example.com/job", "platform": "recruitee"}
            )
        finally:
            task.cancel()

        assert result.method == "manual_handoff"
        assert ticks >= 5, f"event loop was blocked during generation (only {ticks} ticks)"
