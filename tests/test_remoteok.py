"""Tests for scraper/remoteok.py — JSON API parsing, title filter, field mapping."""
import scraper.remoteok as remoteok_mod
from scraper.base import ApiSource
from scraper.remoteok import RemoteOKScraper

# RemoteOK's API returns a list whose first element is metadata, then job objects.
API = [
    {"legal": "RemoteOK metadata — skipped"},
    {
        "position": "Senior Product Manager", "slug": "acme-spm", "company": "Acme",
        "location": "United States", "salary_min": 120000, "salary_max": 160000,
        "description": "Own the roadmap.",
    },
    {  # non-PM → filtered out by the role title filter
        "position": "Staff Software Engineer", "slug": "beta-eng", "company": "Beta",
        "location": "Worldwide",
    },
    {  # junior → filtered out by the seniority exclusion
        "position": "Junior Product Manager", "slug": "gamma-jpm", "company": "Gamma",
        "location": "Worldwide",
    },
    {  # PM with no residence restriction → plain "Remote"
        "position": "Group Product Manager", "slug": "delta-gpm", "company": "Delta",
        "location": "", "salary_min": 90000,
    },
]


def _patch(monkeypatch, body, tags=("product",)):
    """Single tag so _get_json is hit once; return the canned body for it."""
    monkeypatch.setattr(remoteok_mod, "REMOTEOK_TAGS", list(tags))

    async def fake_get_json(self, url, **kwargs):
        return body

    monkeypatch.setattr(RemoteOKScraper, "_get_json", fake_get_json)


class TestRemoteOK:
    def test_is_catalog_api_source(self):
        assert issubclass(RemoteOKScraper, ApiSource)
        assert RemoteOKScraper().accepts_query is False

    async def test_parses_filters_and_maps(self, monkeypatch):
        _patch(monkeypatch, API)
        jobs = await RemoteOKScraper().scrape(max_results=10)

        # Engineer + junior roles dropped; only senior PM roles survive.
        assert [j["title"] for j in jobs] == ["Senior Product Manager", "Group Product Manager"]

        spm = jobs[0]
        assert spm["company"] == "Acme"
        assert spm["location"] == "Remote - United States"   # residence prefixed for the lock filter
        assert spm["salary"] == "$120,000 - $160,000"
        assert spm["url"] == "https://remoteok.com/remote-jobs/acme-spm"
        assert spm["description"] == "Own the roadmap."
        assert spm["is_remote"] is True
        assert spm["platform"] == "remoteok"

        gpm = jobs[1]
        assert gpm["location"] == "Remote"          # no residence restriction
        assert gpm["salary"] == "$90,000+"          # salary_min only

    async def test_max_results_caps_output(self, monkeypatch):
        _patch(monkeypatch, API)
        jobs = await RemoteOKScraper().scrape(max_results=1)
        assert len(jobs) == 1

    async def test_non_list_response_yields_nothing(self, monkeypatch):
        _patch(monkeypatch, {"error": "rate limited"})
        assert await RemoteOKScraper().scrape() == []

    async def test_none_response_yields_nothing(self, monkeypatch):
        _patch(monkeypatch, None)
        assert await RemoteOKScraper().scrape() == []
