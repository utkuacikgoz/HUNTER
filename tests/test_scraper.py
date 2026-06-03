"""Tests for scraper/base.py and scraper/linkedin.py."""
from scraper.base import ApiSource, BaseScraper, BrowserSource, JobSource
from scraper.linkedin import LinkedInScraper
from scraper.remoteok import RemoteOKScraper


class ConcreteScraper(BaseScraper):
    """Concrete implementation for testing abstract base."""
    platform_name = "test"

    async def scrape(self, query, location="", max_results=10):
        return []


class TestNormalizeJob:
    def setup_method(self):
        self.scraper = ConcreteScraper()

    def test_strips_whitespace(self):
        job = self.scraper._normalize_job(
            "  Senior PM  ", "  Acme Corp  ", "  NYC  ", "  $100k  ",
            "  https://example.com  ", "  Great job  "
        )
        assert job["title"] == "Senior PM"
        assert job["company"] == "Acme Corp"
        assert job["location"] == "NYC"
        assert job["salary"] == "$100k"
        assert job["url"] == "https://example.com"
        assert job["description"] == "Great job"

    def test_handles_none_values(self):
        job = self.scraper._normalize_job(None, None, None, None, None, None)
        assert job["title"] == ""
        assert job["company"] == ""
        assert job["location"] == ""
        assert job["salary"] == ""
        assert job["url"] == ""
        assert job["description"] == ""

    def test_sets_platform(self):
        job = self.scraper._normalize_job("PM", "Co", "", "", "https://x.com", "")
        assert job["platform"] == "test"

    def test_all_keys_present(self):
        job = self.scraper._normalize_job("PM", "Co", "NYC", "$100k", "https://x.com", "desc")
        expected_keys = {"title", "company", "location", "salary", "url", "platform", "description"}
        assert set(job.keys()) == expected_keys


class TestSourceHierarchy:
    """The interface split: BrowserSource vs ApiSource, with BaseScraper alias."""

    def test_basescraper_is_browsersource_alias(self):
        assert BaseScraper is BrowserSource

    def test_browser_and_api_share_jobsource(self):
        assert issubclass(BrowserSource, JobSource)
        assert issubclass(ApiSource, JobSource)

    def test_remoteok_is_api_source_not_browser(self):
        # RemoteOK is a pure JSON-API source and must never pull in the browser.
        assert issubclass(RemoteOKScraper, ApiSource)
        assert not issubclass(RemoteOKScraper, BrowserSource)
        assert not hasattr(RemoteOKScraper(), "_browser")

    def test_default_accepts_query(self):
        assert RemoteOKScraper().accepts_query is True


class _FakeResp:
    def __init__(self, status, payload=None, raise_json=False):
        self.status = status
        self._payload = payload
        self._raise_json = raise_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        if self._raise_json:
            raise ValueError("bad json")
        return self._payload


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, *a, **k):
        return self._resp

    async def close(self):
        pass


class _DummyApi(ApiSource):
    platform_name = "dummy"

    async def scrape(self, query, location="", max_results=10):
        return []


class TestApiSourceGetJson:
    async def test_200_returns_parsed_json(self):
        src = _DummyApi()
        src._session = _FakeSession(_FakeResp(200, {"ok": 1}))
        assert await src._get_json("http://x") == {"ok": 1}

    async def test_non_200_returns_none(self):
        src = _DummyApi()
        src._session = _FakeSession(_FakeResp(404))
        assert await src._get_json("http://x") is None

    async def test_unparseable_json_returns_none(self):
        src = _DummyApi()
        src._session = _FakeSession(_FakeResp(200, raise_json=True))
        assert await src._get_json("http://x") is None

    async def test_context_manager_opens_and_closes_session(self):
        src = _DummyApi()
        assert src._session is None
        async with src as s:
            assert s is src
            assert s._session is not None  # real aiohttp session, no network
        assert src._session.closed


class TestLinkedInCleanUrl:
    def test_strips_query_params(self):
        url = "https://www.linkedin.com/jobs/view/12345?trk=abc&refId=xyz"
        assert LinkedInScraper._clean_linkedin_url(url) == "https://www.linkedin.com/jobs/view/12345"

    def test_adds_base_url_for_relative(self):
        url = "/jobs/view/12345"
        assert LinkedInScraper._clean_linkedin_url(url) == "https://www.linkedin.com/jobs/view/12345"

    def test_absolute_url_unchanged(self):
        url = "https://www.linkedin.com/jobs/view/12345"
        assert LinkedInScraper._clean_linkedin_url(url) == url

    def test_relative_with_query_params(self):
        url = "/jobs/view/99999?utm_source=test"
        assert LinkedInScraper._clean_linkedin_url(url) == "https://www.linkedin.com/jobs/view/99999"

    def test_empty_url(self):
        assert LinkedInScraper._clean_linkedin_url("") == ""


class TestLinkedInScraperStructure:
    def test_has_guest_and_auth_methods(self):
        """LinkedIn scraper should have both auth and guest scrape paths."""
        assert hasattr(LinkedInScraper, '_scrape_authenticated')
        assert hasattr(LinkedInScraper, '_scrape_guest')

    def test_platform_name(self):
        assert LinkedInScraper.platform_name == "linkedin"
