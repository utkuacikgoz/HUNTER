"""Tests for scraper/filters.py — the job-classification pipeline."""
import pytest

from scraper.filters import (
    JobVerdict,
    check_sponsor_allowlist,
    classify_region,
    detect_country_locked_remote,
    detect_remote,
    detect_us_only_blocker,
    evaluate_job,
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
        # On-site (no remote token) isn't a "locked remote" — handled by the
        # not-remote drop instead.
        assert detect_country_locked_remote({"location": "San Francisco, CA"}) is None

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

    def test_verdict_dataclass_fields(self):
        v = JobVerdict(
            region="eu", is_remote=True, sponsor_status="allowlist",
            verdict="include", reasons=["test"],
        )
        assert v.region == "eu"
        assert v.reasons == ["test"]
