"""Tests for scraper/ats.py — Greenhouse/Lever/Ashby parsing, role filtering, normalization."""
from scraper.ats import (
    AshbySource,
    AtsSource,
    GreenhouseSource,
    LeverSource,
    _strip_html,
)
from scraper.base import ApiSource


def _patch_get_json(monkeypatch, src, payload):
    async def fake(url, **kwargs):
        return payload
    monkeypatch.setattr(src, "_get_json", fake)


class TestStripHtml:
    def test_unescapes_then_strips_tags(self):
        assert _strip_html("&lt;p&gt;Hello &amp; welcome&lt;/p&gt;") == "Hello & welcome"

    def test_plain_text_passthrough(self):
        assert _strip_html("just text") == "just text"

    def test_none(self):
        assert _strip_html(None) == ""


class TestAtsCommon:
    def test_is_api_source(self):
        assert issubclass(AtsSource, ApiSource)

    def test_catalog_source_does_not_accept_query(self):
        assert GreenhouseSource().accepts_query is False

    def test_seeded_boards_by_default(self):
        assert "stripe" in GreenhouseSource().boards
        assert "spotify" in LeverSource().boards
        assert "deel" in AshbySource().boards

    def test_tier23_boards_listed_before_tier1(self):
        # Tier-2/3 scale-ups must come first so they fill the per-run cap before giants.
        gh = GreenhouseSource().boards
        assert "gocardless" in gh and gh.index("gocardless") < gh.index("stripe")
        ash = AshbySource().boards
        assert "pleo" in ash and ash.index("pleo") < ash.index("notion")

    def test_title_match_keeps_product_drops_other(self):
        s = GreenhouseSource(boards=[])
        assert s._title_matches("Senior Product Manager")
        assert s._title_matches("Head of Product")
        assert s._title_matches("Group Product Manager - Messaging")
        assert s._title_matches("Product Lead, AI")
        assert not s._title_matches("Staff Software Engineer")
        # Precision: non-PM "product" roles must not match (caught in live smoke).
        assert not s._title_matches("Staff Product Designer")
        assert not s._title_matches("Senior Product Designer, Design Systems")
        assert not s._title_matches("Product Marketing Manager")

    async def test_board_failure_is_skipped(self, monkeypatch):
        src = GreenhouseSource(boards=["a", "b"])

        async def boom(url, **kwargs):
            raise RuntimeError("ats down")

        monkeypatch.setattr(src, "_get_json", boom)
        assert await src.scrape() == []  # errors swallowed per-board


class TestGreenhouse:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = GreenhouseSource(boards=["acme"])
        payload = {"jobs": [
            {"title": "Senior Product Manager", "location": {"name": "Remote - EU"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "content": "&lt;p&gt;Own the roadmap&lt;/p&gt;"},
            {"title": "Staff Backend Engineer", "location": {"name": "NYC"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "content": "x"},
        ]}
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert len(jobs) == 1
        j = jobs[0]
        assert j["title"] == "Senior Product Manager"
        assert j["company"] == "acme"
        assert j["platform"] == "greenhouse"
        assert j["location"] == "Remote - EU"
        assert j["url"].endswith("/acme/jobs/1")
        assert j["description"] == "Own the roadmap"  # html stripped

    async def test_non_dict_payload_yields_nothing(self, monkeypatch):
        src = GreenhouseSource(boards=["acme"])
        _patch_get_json(monkeypatch, src, None)
        assert await src.scrape() == []


class TestLever:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = LeverSource(boards=["acme"])
        payload = [
            {"text": "Group Product Manager", "categories": {"location": "London, UK"},
             "hostedUrl": "https://jobs.lever.co/acme/1", "descriptionPlain": "Lead products"},
            {"text": "Account Executive", "categories": {"location": "NYC"},
             "hostedUrl": "https://jobs.lever.co/acme/2", "descriptionPlain": "Sell"},
        ]
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert [j["title"] for j in jobs] == ["Group Product Manager"]
        assert jobs[0]["platform"] == "lever"
        assert jobs[0]["company"] == "acme"
        assert jobs[0]["url"] == "https://jobs.lever.co/acme/1"
        assert jobs[0]["location"] == "London, UK"


class TestAshby:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = AshbySource(boards=["acme"])
        payload = {"jobs": [
            {"title": "Principal Product Manager", "location": "Remote",
             "jobUrl": "https://jobs.ashbyhq.com/acme/1", "descriptionPlain": "Strategy"},
            {"title": "Technical Recruiter", "location": "Remote",
             "jobUrl": "https://jobs.ashbyhq.com/acme/2", "descriptionPlain": "Hire"},
        ]}
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Principal Product Manager"
        assert jobs[0]["platform"] == "ashby"
        assert jobs[0]["url"] == "https://jobs.ashbyhq.com/acme/1"
