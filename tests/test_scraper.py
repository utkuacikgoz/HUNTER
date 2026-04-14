"""Tests for scraper/base.py and scraper/linkedin.py."""
import pytest
from scraper.base import BaseScraper
from scraper.linkedin import LinkedInScraper


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
