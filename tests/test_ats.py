"""Tests for scraper/ats.py — ATS parsing, role filtering, normalization."""
from scraper.ats import (
    AshbySource,
    AtsSource,
    GreenhouseSource,
    LeverSource,
    RecruiteeSource,
    SmartRecruitersSource,
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
        assert "notion" in AshbySource().boards

    def test_tier23_boards_listed_before_tier1(self):
        # Tier-2/3 scale-ups must come first so they fill the per-run cap before giants.
        gh = GreenhouseSource().boards
        assert "gocardless" in gh and gh.index("gocardless") < gh.index("stripe")
        ash = AshbySource().boards
        assert "pleo" in ash and ash.index("pleo") < ash.index("notion")

    def test_title_match_keeps_target_drops_other(self):
        # The target is deliberately narrow: Head of Product + Senior Product
        # Manager only (see ROLE_MATCH_KEYWORDS).
        s = GreenhouseSource(boards=[])
        assert s._title_matches("Senior Product Manager")
        assert s._title_matches("Senior Product Manager, Payments")
        assert s._title_matches("Sr. Product Manager")
        assert s._title_matches("Head of Product")
        # Widened senior-plus titles (2026-08-31).
        assert s._title_matches("Lead Product Manager, Growth")
        assert s._title_matches("Principal Product Manager")
        assert s._title_matches("Director of Product")
        assert not s._title_matches("Staff Software Engineer")
        # Adjacent PM-shaped titles are still out of target.
        assert not s._title_matches("Group Product Manager - Messaging")
        assert not s._title_matches("Product Lead, AI")
        assert not s._title_matches("Product Manager")
        # Precision: non-PM "product" roles must not match (caught in live smoke).
        assert not s._title_matches("Staff Product Designer")
        assert not s._title_matches("Senior Product Designer, Design Systems")
        assert not s._title_matches("Product Marketing Manager")
        assert not s._title_matches("Head of Product Design")
        assert not s._title_matches("Head of Product Marketing")
        assert not s._title_matches("Director of Product Design")
        assert not s._title_matches("Director of Product Marketing")

    def test_title_match_drops_junior_seniority(self):
        s = GreenhouseSource(boards=[])
        assert not s._title_matches("Junior Product Manager")
        assert not s._title_matches("Associate Product Manager")
        assert not s._title_matches("Product Management Intern")
        assert not s._title_matches("Graduate Product Manager")
        # The intern-substring guard: "International" must survive.
        assert s._title_matches("Senior Product Manager, International")
        assert s._title_matches("Senior Product Manager")

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
        # Greenhouse has no structured remote flag → unknown (text fallback in filter).
        assert j["is_remote"] is None

    async def test_non_dict_payload_yields_nothing(self, monkeypatch):
        src = GreenhouseSource(boards=["acme"])
        _patch_get_json(monkeypatch, src, None)
        assert await src.scrape() == []


class TestLever:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = LeverSource(boards=["acme"])
        payload = [
            {"text": "Senior Product Manager", "categories": {"location": "London, UK"},
             "workplaceType": "remote",
             "hostedUrl": "https://jobs.lever.co/acme/1", "descriptionPlain": "Lead products"},
            {"text": "Account Executive", "categories": {"location": "NYC"},
             "hostedUrl": "https://jobs.lever.co/acme/2", "descriptionPlain": "Sell"},
        ]
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert [j["title"] for j in jobs] == ["Senior Product Manager"]
        assert jobs[0]["platform"] == "lever"
        assert jobs[0]["company"] == "acme"
        assert jobs[0]["url"] == "https://jobs.lever.co/acme/1"
        assert jobs[0]["location"] == "London, UK"
        assert jobs[0]["is_remote"] is True  # from workplaceType "remote"

    async def test_workplace_type_hybrid_is_not_remote(self, monkeypatch):
        src = LeverSource(boards=["acme"])
        payload = [
            {"text": "Senior Product Manager", "categories": {"location": "Berlin"},
             "workplaceType": "hybrid", "hostedUrl": "https://jobs.lever.co/acme/3"},
        ]
        _patch_get_json(monkeypatch, src, payload)
        jobs = await src.scrape()
        assert jobs[0]["is_remote"] is False


class TestAshby:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = AshbySource(boards=["acme"])
        payload = {"jobs": [
            {"title": "Head of Product", "location": "Berlin",
             "isRemote": True, "workplaceType": "Remote",
             "jobUrl": "https://jobs.ashbyhq.com/acme/1", "descriptionPlain": "Strategy"},
            {"title": "Technical Recruiter", "location": "Remote",
             "jobUrl": "https://jobs.ashbyhq.com/acme/2", "descriptionPlain": "Hire"},
        ]}
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Head of Product"
        assert jobs[0]["platform"] == "ashby"
        assert jobs[0]["url"] == "https://jobs.ashbyhq.com/acme/1"
        # Structured isRemote flag survives even when location is a bare city.
        assert jobs[0]["is_remote"] is True

    async def test_non_remote_workplace_type(self, monkeypatch):
        src = AshbySource(boards=["acme"])
        payload = {"jobs": [
            {"title": "Senior Product Manager", "location": "Paris", "isRemote": False,
             "workplaceType": "Hybrid", "jobUrl": "https://jobs.ashbyhq.com/acme/3"},
        ]}
        _patch_get_json(monkeypatch, src, payload)
        jobs = await src.scrape()
        assert jobs[0]["is_remote"] is False


class TestRecruitee:
    async def test_parses_filters_normalizes(self, monkeypatch):
        src = RecruiteeSource(boards=["acme"])
        payload = {"offers": [
            {"title": "Senior Product Manager", "location": "Amsterdam, Netherlands",
             "remote": True,
             "careers_url": "https://acme.recruitee.com/o/spm", "description": "<p>Own it</p>"},
            {"title": "AML Analyst", "location": "Bucharest, Romania",
             "careers_url": "https://acme.recruitee.com/o/aml"},
        ]}
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert [j["title"] for j in jobs] == ["Senior Product Manager"]
        assert jobs[0]["platform"] == "recruitee"
        assert jobs[0]["company"] == "acme"
        assert jobs[0]["url"] == "https://acme.recruitee.com/o/spm"
        assert jobs[0]["location"] == "Amsterdam, Netherlands"
        assert jobs[0]["description"] == "Own it"  # html stripped
        assert jobs[0]["is_remote"] is True  # from structured remote flag

    async def test_location_falls_back_to_city_country(self, monkeypatch):
        src = RecruiteeSource(boards=["acme"])
        payload = {"offers": [
            {"title": "Senior Product Manager", "city": "Berlin", "country": "Germany",
             "careers_url": "https://acme.recruitee.com/o/pm"},
        ]}
        _patch_get_json(monkeypatch, src, payload)
        jobs = await src.scrape()
        assert jobs[0]["location"] == "Berlin, Germany"


class TestSmartRecruiters:
    async def test_parses_filters_builds_public_url(self, monkeypatch):
        src = SmartRecruitersSource(boards=["Acme"])
        payload = {"content": [
            {"name": "Senior Product Manager", "id": "123",
             "location": {"city": "Paris", "country": "fr", "remote": False,
                          "fullLocation": "Paris, France"}},
            {"name": "Sales Lead", "id": "456",
             "location": {"city": "NYC", "country": "us"}},
        ]}
        _patch_get_json(monkeypatch, src, payload)

        jobs = await src.scrape()
        assert [j["title"] for j in jobs] == ["Senior Product Manager"]
        assert jobs[0]["platform"] == "smartrecruiters"
        assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/Acme/123"
        assert jobs[0]["location"] == "Paris, France"

    async def test_remote_flag_prefixes_location(self, monkeypatch):
        src = SmartRecruitersSource(boards=["Acme"])
        payload = {"content": [
            {"name": "Senior Product Manager", "id": "789",
             "location": {"city": "London", "country": "uk", "remote": True,
                          "fullLocation": "London, UK"}},
        ]}
        _patch_get_json(monkeypatch, src, payload)
        jobs = await src.scrape()
        assert jobs[0]["location"] == "Remote - London, UK"
        assert jobs[0]["is_remote"] is True

    async def test_id_falls_back_to_ref_tail(self, monkeypatch):
        src = SmartRecruitersSource(boards=["Acme"])
        payload = {"content": [
            {"name": "Head of Product", "location": {"fullLocation": "Berlin"},
             "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/999"},
        ]}
        _patch_get_json(monkeypatch, src, payload)
        jobs = await src.scrape()
        assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/Acme/999"


class TestBoardTiering:
    """Priority (EU/global) boards are scanned before the US-only-remote tail."""

    def test_greenhouse_combines_tiers_with_priority_count(self):
        from config.settings import GREENHOUSE_BOARDS, GREENHOUSE_US_BOARDS
        src = GreenhouseSource()
        assert src.priority_count == len(GREENHOUSE_BOARDS)
        assert src.boards == GREENHOUSE_BOARDS + GREENHOUSE_US_BOARDS
        # A US-only giant sits in the deprioritized tail, not the priority tier.
        assert "databricks" in GREENHOUSE_US_BOARDS
        assert src.boards.index("databricks") >= src.priority_count

    def test_ashby_us_tier_is_deprioritized(self):
        from config.settings import ASHBY_BOARDS, ASHBY_US_BOARDS
        src = AshbySource()
        assert src.priority_count == len(ASHBY_BOARDS)
        # OpenAI is blocklisted (no ChatGPT jobs), so it's gone from the US tier.
        assert "openai" not in ASHBY_US_BOARDS
        assert "perplexity" in ASHBY_US_BOARDS
        assert src.boards.index("perplexity") >= src.priority_count

    def test_explicit_boards_default_to_all_priority(self):
        src = GreenhouseSource(boards=["a", "b"])
        assert src.priority_count == 2  # no US tier when boards passed explicitly

    def test_ordered_boards_priority_first_rest_fixed(self):
        src = GreenhouseSource(boards=["p1", "p2", "us1", "us2"])
        src.priority_count = 2
        ordered = src._ordered_boards()
        assert set(ordered[:2]) == {"p1", "p2"}   # priority tier (daily-rotated)
        assert ordered[2:] == ["us1", "us2"]      # deprioritized tier, fixed order

    async def test_deprioritized_tier_skipped_when_cap_filled(self, monkeypatch):
        src = GreenhouseSource(boards=["p1", "p2", "us1"])
        src.priority_count = 2
        fetched: list[str] = []

        async def fake_fetch(board):
            fetched.append(board)
            return [{"title": "Product Manager", "company": board, "location": "Remote",
                     "url": f"https://x/{board}", "platform": "greenhouse", "is_remote": True}]

        monkeypatch.setattr(src, "_fetch_board", fake_fetch)
        jobs = await src.scrape(max_results=2)
        # The 2 priority boards fill the cap, so the US board is never fetched.
        assert "us1" not in fetched
        assert len(jobs) == 2
