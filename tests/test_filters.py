"""Tests for scraper/filters.py — the job-classification pipeline."""
import pytest

import scraper.filters as filters
from scraper.filters import (
    JobVerdict,
    check_sponsor_allowlist,
    classify_region,
    detect_country_locked_remote,
    detect_freelance,
    detect_remote,
    detect_us_only_blocker,
    evaluate_job,
    evaluate_job_async,
)


class TestDetectUsOnlyBlocker:
    def test_us_citizens_only(self):
        job = {"description": "We accept US Citizens only."}
        assert detect_us_only_blocker(job) is not None

    def test_must_be_based_in_us(self):
        job = {"description": "Must be located in the United States to apply."}
        assert detect_us_only_blocker(job) is not None

    def test_no_sponsorship(self):
        job = {"description": "Great team. No visa sponsorship available."}
        assert detect_us_only_blocker(job) is not None

    def test_sponsorship_not_provided(self):
        job = {"description": "Sponsorship is not provided for this role."}
        assert detect_us_only_blocker(job) is not None

    def test_clean_description(self):
        job = {"description": "Join our distributed team in Europe and beyond."}
        assert detect_us_only_blocker(job) is None

    def test_empty(self):
        assert detect_us_only_blocker({"description": ""}) is None
        assert detect_us_only_blocker({}) is None


class TestDetectRemote:
    def test_remote_in_location(self):
        assert detect_remote({"title": "PM", "location": "Remote", "description": ""}) is True

    def test_wfh_in_description(self):
        assert detect_remote({"title": "PM", "location": "", "description": "WFH role"}) is True

    def test_fully_remote(self):
        assert detect_remote({"title": "Fully Remote PM", "location": "", "description": ""}) is True

    def test_onsite_only(self):
        assert detect_remote({"title": "PM", "location": "San Francisco, CA", "description": ""}) is False

    def test_structured_flag_true_trusted_over_bare_city(self):
        # The core bug fix: an ATS role flagged remote with only a city location
        # (no "remote" keyword) must still read as remote.
        assert detect_remote(
            {"title": "PM", "location": "Berlin", "description": "", "is_remote": True}
        ) is True

    def test_structured_flag_false_trusted(self):
        # A "remote" keyword in prose must not override an explicit not-remote flag.
        assert detect_remote(
            {"title": "PM", "location": "Remote-friendly office", "description": "",
             "is_remote": False}
        ) is False

    def test_none_flag_falls_back_to_text(self):
        assert detect_remote(
            {"title": "PM", "location": "Remote", "description": "", "is_remote": None}
        ) is True
        assert detect_remote(
            {"title": "PM", "location": "Berlin", "description": "", "is_remote": None}
        ) is False


class TestClassifyRegion:
    def test_emea_explicit(self):
        assert classify_region({"location": "EMEA", "title": "", "description": ""}) == "emea"

    def test_turkey(self):
        assert classify_region({"location": "Istanbul, Turkey", "title": "", "description": ""}) == "emea"

    def test_uk_london(self):
        assert classify_region({"location": "London, UK", "title": "", "description": ""}) == "emea"

    def test_germany_berlin(self):
        assert classify_region({"location": "Berlin, Germany", "title": "", "description": ""}) == "eu"

    def test_us_city(self):
        assert classify_region({"location": "San Francisco, CA", "title": "", "description": ""}) == "us"

    def test_remote_us(self):
        assert classify_region({"location": "Remote - US", "title": "", "description": ""}) == "us"

    def test_plain_remote_unknown(self):
        assert classify_region({"location": "Remote", "title": "PM", "description": ""}) == "unknown"

    def test_empty_location_unknown(self):
        assert classify_region({"location": "", "title": "PM", "description": ""}) == "unknown"


class TestClassifyRegionBoundaries:
    """Word-boundary regressions: substring matching used to misclassify these."""

    # (location, title, description, expected_region)
    CASES = [
        # "uk" must NOT match inside "Milwaukee" (was wrongly EMEA); an
        # unrecognized concrete location falls to "other", not a bogus region.
        ("Milwaukee, WI", "", "", "other"),
        # Pronoun "us" in prose must NOT read as USA when location is generic.
        ("Remote", "PM", "We'd love for you to join us!", "unknown"),
        # Legit signals still classify correctly.
        ("London, UK", "", "", "emea"),
        ("Remote — EMEA", "", "", "emea"),
        ("Remote (EU)", "", "", "eu"),
        ("Dublin, Ireland", "", "", "eu"),
        ("Dubai, UAE", "", "", "emea"),
        ("Remote - US", "", "", "us"),
        ("Austin, TX", "", "", "us"),
    ]

    @pytest.mark.parametrize("location,title,description,expected", CASES)
    def test_region(self, location, title, description, expected):
        job = {"location": location, "title": title, "description": description}
        assert classify_region(job) == expected


class TestDetectCountryLockedRemote:
    """US/Canada-locked remote roles: candidate is EMEA, so these should drop."""

    # The three real examples from production (form3 / mercury / paxos).
    LOCKED = [
        "100% Remote (US/Canada*)",
        "San Francisco, CA, New York, NY, Portland, OR, or Remote within Canada or United States",
        "Remote - United States",
        "Remote - US",
        "Remote U.S.",               # punctuated form (Ashby's default)
        "Remote (USA only)",
        "Remote, Canada",
    ]
    # Open to the candidate or global → not a lock.
    NOT_LOCKED = [
        "Remote - EMEA",
        "Remote (EU)",
        "Remote - US, UK",          # also opens to UK (EMEA)
        "Remote - Worldwide",
        "Remote (Anywhere)",
        "Remote",                    # no country at all
        "London, UK",               # not even US/Canada
    ]

    @pytest.mark.parametrize("location", LOCKED)
    def test_locked(self, location):
        assert detect_country_locked_remote({"location": location}) is not None

    @pytest.mark.parametrize("location", NOT_LOCKED)
    def test_not_locked(self, location):
        assert detect_country_locked_remote({"location": location}) is None

    def test_onsite_us_is_not_locked_remote(self):
        # On-site (no remote token, no is_remote flag) isn't a "locked remote" — handled
        # by the not-remote drop instead.
        assert detect_country_locked_remote({"location": "San Francisco, CA"}) is None


class TestDetectFreelance:
    """Employment type is inferred from text — no source exposes a structured field."""

    # (title, description) — bare words count in the title, phrases count anywhere.
    FREELANCE = [
        ("Marketing Manager (6-month contract)", ""),
        ("Freelance Marketing Consultant", ""),
        ("Interim Head of Growth", ""),
        ("Fractional CMO", ""),
        ("Growth Marketer", "12-month fixed-term contract, Berlin office."),
        ("Growth Marketer", "We need someone on a freelance basis."),
        ("Growth Marketer", "This is a project-based engagement."),
        ("Growth Marketer", "6 months contract, extendable."),
    ]
    # The same words in a JD body mean something else entirely — title-only scoping is
    # what saves these.
    NOT_FREELANCE = [
        ("Growth Marketer", "You will own contract negotiation and vendor contract management."),
        ("Growth Marketer", "Our consulting clients span EMEA."),
        # The fintech-board false positive: on fintech boards (monzo/n26/bitpanda),
        # "fractional shares" is a literal product feature.
        ("Growth Marketer", "We offer fractional shares and equity."),
        ("Growth Marketer", "In the interim, you will report to the CEO."),
        # A duty ("manage freelancers"), not the role's own employment type.
        ("Growth Marketer", "Manage freelance copywriters and designers."),
        ("Growth Marketer", "Temporary access to our tools is provided."),
        ("Growth Marketer", "Permanent, full-time, on-site in Istanbul."),
        # Part-time / hourly is a schedule, not a contract type — deliberately excluded.
        ("Part-time Marketing Manager", "Hourly rate."),
    ]

    @pytest.mark.parametrize(("title", "description"), FREELANCE)
    def test_freelance(self, title, description):
        assert detect_freelance({"title": title, "location": "", "description": description}) is True

    @pytest.mark.parametrize(("title", "description"), NOT_FREELANCE)
    def test_not_freelance(self, title, description):
        assert detect_freelance({"title": title, "location": "", "description": description}) is False

    def test_word_boundary_safety(self):
        # "monthly contract value" is not " month contract "; padding keeps them apart.
        assert detect_freelance(
            {"title": "Growth Marketer", "description": "Grow monthly contracted revenue."}
        ) is False

    def test_structured_remote_us_cities_is_locked(self):
        # The Figma case: structured is_remote=True with a bare US-cities location (no
        # literal "remote" word) is still US-locked for an overseas candidate.
        job = {
            "location": "San Francisco, CA, New York, NY, United States",
            "is_remote": True,
        }
        assert detect_country_locked_remote(job) is not None

    def test_us_based_in_title_is_locked(self):
        # The Toptal case: "US-Based" in the title with a non-US-locked location.
        job = {"title": "VP of Product - US-Based", "location": "Remote"}
        assert detect_country_locked_remote(job) is not None

    def test_us_only_in_title_is_locked(self):
        assert detect_country_locked_remote(
            {"title": "Senior PM (US only)", "location": "Remote"}
        ) is not None


class TestCheckSponsorAllowlist:
    def test_known_friendly_company(self):
        # Stripe is in the default SPONSOR_FRIENDLY_COMPANIES.
        assert check_sponsor_allowlist("Stripe") == "allowlist"

    def test_case_insensitive(self):
        assert check_sponsor_allowlist("gitlab") == "allowlist"
        assert check_sponsor_allowlist("GITLAB") == "allowlist"

    def test_unknown_company(self):
        assert check_sponsor_allowlist("NeverHeardOfThemCo") == "unknown"

    def test_empty_company(self):
        assert check_sponsor_allowlist("") == "unknown"
        assert check_sponsor_allowlist(None) == "unknown"  # type: ignore[arg-type]

    def test_blocklisted_company(self, monkeypatch):
        monkeypatch.setattr(filters, "SPONSOR_BLOCKLIST_COMPANIES", {"blockedco"})
        assert check_sponsor_allowlist("BlockedCo") == "blocklist"

    def test_blocklist_beats_allowlist(self, monkeypatch):
        # A company on both lists is blocked — the blocklist is checked first.
        monkeypatch.setattr(filters, "SPONSOR_BLOCKLIST_COMPANIES", {"stripe"})
        assert check_sponsor_allowlist("Stripe") == "blocklist"

    def test_default_blocks_canonical_and_openai(self):
        # Point-6 requirement: never surface Canonical or OpenAI/ChatGPT roles.
        assert check_sponsor_allowlist("Canonical") == "blocklist"
        assert check_sponsor_allowlist("OpenAI") == "blocklist"
        assert check_sponsor_allowlist("Open AI") == "blocklist"


class TestEvaluateJob:
    def test_allowlisted_emea_remote_includes(self):
        v = evaluate_job({
            "title": "Senior PM",
            "company": "Stripe",
            "location": "Remote, Europe",
            "description": "Join us building",
        })
        assert v.verdict == "include"
        assert v.region == "eu"
        assert v.is_remote is True
        assert v.sponsor_status == "allowlist"

    def test_us_only_blocker_drops(self):
        v = evaluate_job({
            "title": "PM",
            "company": "Acme",
            "location": "Remote",
            "description": "Must be located in the United States. No visa sponsorship.",
        })
        assert v.verdict == "drop"
        assert "us-only language" in v.reasons[0]

    def test_unknown_sponsor_eu_remote_flags(self):
        v = evaluate_job({
            "title": "PM",
            "company": "FooCorp",
            "location": "Berlin, Germany - Remote",
            "description": "Awesome team in EU.",
        })
        assert v.verdict == "flag"
        assert v.region == "eu"

    def test_not_remote_drops(self):
        v = evaluate_job({
            "title": "PM",
            "company": "BarCo",
            "location": "San Francisco, CA",
            "description": "On-site only.",
        })
        assert v.verdict == "drop"
        assert v.is_remote is False

    def test_structured_remote_flag_rescues_bare_city_role(self):
        # The core bug fix end-to-end: an ATS role with a bare-city location but a
        # structured is_remote=True flag must NOT drop as "not marked remote".
        v = evaluate_job({
            "title": "Senior PM",
            "company": "FooCorp",
            "location": "Berlin",
            "description": "EU product team.",
            "is_remote": True,
        })
        assert v.verdict == "flag"
        assert v.is_remote is True
        assert v.region == "eu"

    def test_ashby_remote_us_role_drops_as_us_locked(self):
        # Ashby's "Remote U.S." + isRemote=True: now recognized as US-locked so an
        # overseas candidate isn't flooded with roles they can't take.
        v = evaluate_job({
            "title": "Staff Product Manager",
            "company": "Vanta",
            "location": "Remote U.S.",
            "description": "",
            "is_remote": True,
        })
        assert v.verdict == "drop"
        assert "locked to US/Canada" in v.reasons[0]

    def test_allowlist_overrides_unknown_region(self):
        v = evaluate_job({
            "title": "PM",
            "company": "GitLab",
            "location": "Remote",
            "description": "All-remote company.",
        })
        assert v.verdict == "include"
        assert v.sponsor_status == "allowlist"

    def test_unknown_sponsor_unknown_region_flags(self):
        v = evaluate_job({
            "title": "PM",
            "company": "NewCo",
            "location": "Remote",
            "description": "",
        })
        assert v.verdict == "flag"
        assert v.region == "unknown"

    def test_us_locked_remote_drops_even_for_allowlist_company(self):
        # Cloudflare is sponsor-friendly, but a US-locked posting still won't take
        # an overseas candidate — the country lock takes precedence over allowlist.
        v = evaluate_job({
            "title": "PM",
            "company": "Cloudflare",
            "location": "Remote - US",
            "description": "",
        })
        assert v.verdict == "drop"
        assert "locked to US/Canada" in v.reasons[0]

    def test_structured_us_remote_drops_even_for_allowlist_company(self):
        # The Figma card from production: sponsor-friendly company, structured
        # is_remote=True, but the location is US cities only → US-remote, which an
        # overseas candidate can't take. Must drop despite the allowlist.
        v = evaluate_job({
            "title": "Product Manager, CMS",
            "company": "figma",
            "location": "San Francisco, CA, New York, NY, United States",
            "description": "",
            "is_remote": True,
        })
        assert v.verdict == "drop"
        assert "locked to US/Canada" in v.reasons[0]

    def test_us_canada_locked_remote_drops(self):
        # The form3 production case: unknown sponsor, "100% Remote (US/Canada)".
        v = evaluate_job({
            "title": "Product Owner - US Payments",
            "company": "form3",
            "location": "100% Remote (US/Canada*)",
            "description": "Join our payments team.",
        })
        assert v.verdict == "drop"
        assert "locked to US/Canada" in v.reasons[0]

    def test_emea_remote_unknown_sponsor_still_flags(self):
        # The lock check must not over-reach: EMEA remote stays a flag, not a drop.
        v = evaluate_job({
            "title": "Senior PM",
            "company": "SomeFintech",
            "location": "Remote - EMEA",
            "description": "Pan-European team.",
        })
        assert v.verdict == "flag"
        assert v.region == "emea"

    def test_blocklisted_company_drops(self, monkeypatch):
        monkeypatch.setattr(filters, "SPONSOR_BLOCKLIST_COMPANIES", {"blockedco"})
        v = evaluate_job({
            "title": "Senior PM",
            "company": "BlockedCo",
            "location": "Remote, Europe",
            "description": "Great EU remote role.",
        })
        assert v.verdict == "drop"
        assert v.sponsor_status == "blocklist"
        assert "blocklist" in v.reasons[0]

    def test_verdict_dataclass_fields(self):
        v = JobVerdict(
            region="eu", is_remote=True, sponsor_status="allowlist",
            verdict="include", is_freelance=True, reasons=["test"],
        )
        assert v.region == "eu"
        assert v.reasons == ["test"]
        assert v.is_freelance is True

    def test_verdict_is_freelance_defaults_false(self):
        v = JobVerdict(region="eu", is_remote=True, sponsor_status="unknown", verdict="flag")
        assert v.is_freelance is False


class TestAllowOnsiteFreelance:
    """ALLOW_ONSITE_FREELANCE exempts freelance/contract roles from the on-site drop,
    making the feed read "remote OR freelance" (a remote-or-freelance profile). Only the
    remote gate is exempted — region and sponsor checks still apply.
    """

    ONSITE_FREELANCE = {
        "title": "Freelance Marketing Consultant",
        "company": "FooCorp",
        "location": "Istanbul, Turkey",
        "description": "On-site in our Istanbul office.",
    }

    def test_onsite_freelance_survives_when_allowed(self, monkeypatch):
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", True)
        v = evaluate_job(self.ONSITE_FREELANCE)
        assert v.verdict == "flag"
        assert v.is_remote is False
        assert v.is_freelance is True
        assert v.region == "emea"
        assert any("freelance/contract — allowed" in r for r in v.reasons)

    def test_onsite_permanent_still_drops_when_freelance_allowed(self, monkeypatch):
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", True)
        v = evaluate_job({
            "title": "Marketing Manager",
            "company": "FooCorp",
            "location": "Berlin, Germany",
            "description": "Permanent role, on-site.",
        })
        assert v.verdict == "drop"
        assert v.is_freelance is False
        assert "no freelance/contract signal" in v.reasons[0]

    def test_onsite_freelance_drops_when_not_allowed(self, monkeypatch):
        # Default-behavior regression guard: with the toggle off (the default, and what
        # the PM bot runs), an on-site freelance role drops exactly as it always did.
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", False)
        v = evaluate_job(self.ONSITE_FREELANCE)
        assert v.verdict == "drop"
        assert v.reasons[0] == "not marked remote"

    def test_onsite_us_freelance_drops_on_region(self, monkeypatch):
        # The exemption reopens the on-site door, so REGION_ALLOWLIST becomes the only
        # thing keeping US on-site freelance out. Pins that against future refactors.
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", True)
        monkeypatch.setattr(filters, "REGION_ALLOWLIST", {"eu", "emea"})
        v = evaluate_job({
            "title": "Freelance Marketing Consultant",
            "company": "FooCorp",
            "location": "New York, NY",
            "description": "On-site in our NYC office.",
        })
        assert v.verdict == "drop"
        assert v.is_freelance is True
        assert "not in REGION_ALLOWLIST" in v.reasons[0]

    def test_remote_permanent_unaffected(self, monkeypatch):
        # The exemption widens the gate; it must not narrow it for plain remote roles.
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", True)
        v = evaluate_job({
            "title": "Growth Marketing Manager",
            "company": "Stripe",
            "location": "Remote, Europe",
            "description": "Permanent role.",
        })
        assert v.verdict == "include"
        assert v.is_remote is True
        assert v.is_freelance is False

    def test_us_only_blocker_beats_freelance_exemption(self, monkeypatch):
        # The exemption is scoped to the remote gate only — it must not rescue a job
        # that says outright it won't sponsor.
        monkeypatch.setattr(filters, "REMOTE_REQUIRED", True)
        monkeypatch.setattr(filters, "ALLOW_ONSITE_FREELANCE", True)
        v = evaluate_job({
            "title": "Freelance Marketing Consultant",
            "company": "FooCorp",
            "location": "Istanbul, Turkey",
            "description": "No visa sponsorship.",
        })
        assert v.verdict == "drop"
        assert "us-only language" in v.reasons[0]


class TestEvaluateJobAsync:
    """The LLM sponsor-scoring layer: only runs on flag+unknown jobs and can flip the
    verdict to include (yes) or drop (no), or leave it flagged (unclear / error)."""

    # An unknown-sponsor EU-remote job → sync verdict is flag/unknown, so the LLM runs.
    FLAG_JOB = {
        "title": "PM",
        "company": "FooCorp",
        "location": "Berlin, Germany - Remote",
        "description": "Awesome team in EU.",
    }

    def _scorer(self, verdict, *, recorder=None):
        async def score(company, description):
            if recorder is not None:
                recorder.append((company, description))
            return {"verdict": verdict, "reasons": f"signal:{verdict}"}
        return score

    async def test_llm_yes_promotes_to_include(self):
        v = await evaluate_job_async(dict(self.FLAG_JOB), llm_score=self._scorer("yes"))
        assert v.verdict == "include"
        assert v.sponsor_status == "llm_yes"

    async def test_llm_no_demotes_to_drop(self):
        v = await evaluate_job_async(dict(self.FLAG_JOB), llm_score=self._scorer("no"))
        assert v.verdict == "drop"
        assert v.sponsor_status == "llm_no"

    async def test_llm_unclear_stays_flag(self):
        v = await evaluate_job_async(dict(self.FLAG_JOB), llm_score=self._scorer("unclear"))
        assert v.verdict == "flag"
        assert v.sponsor_status == "unknown"
        assert any("unclear" in r for r in v.reasons)

    async def test_llm_exception_falls_back_to_sync_verdict(self):
        async def boom(company, description):
            raise RuntimeError("API down")
        v = await evaluate_job_async(dict(self.FLAG_JOB), llm_score=boom)
        assert v.verdict == "flag"  # unchanged; the error is swallowed

    async def test_none_scorer_returns_sync_verdict(self):
        v = await evaluate_job_async(dict(self.FLAG_JOB), llm_score=None)
        assert v.verdict == "flag"

    async def test_llm_not_called_for_hard_drop(self):
        # US-only language already drops in pure Python — don't spend tokens on it.
        calls: list = []
        job = {
            "title": "PM", "company": "Acme", "location": "Remote",
            "description": "Must be located in the United States. No visa sponsorship.",
        }
        v = await evaluate_job_async(job, llm_score=self._scorer("yes", recorder=calls))
        assert v.verdict == "drop"
        assert calls == []  # LLM never invoked

    async def test_llm_not_called_for_allowlist_include(self):
        # Allowlisted company is already a confident include — skip the LLM.
        calls: list = []
        job = {
            "title": "Senior PM", "company": "Stripe",
            "location": "Remote, Europe", "description": "EU remote.",
        }
        v = await evaluate_job_async(job, llm_score=self._scorer("no", recorder=calls))
        assert v.verdict == "include"
        assert calls == []
