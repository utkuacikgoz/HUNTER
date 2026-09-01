"""Tests for scraper/weworkremotely.py — RSS parsing, title split, filtering."""
from scraper.base import ApiSource
from scraper.weworkremotely import WeWorkRemotelySource, _strip_html

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Acme: Senior Product Manager</title>
    <region>Europe</region>
    <link>https://weworkremotely.com/remote-jobs/acme-spm</link>
    <description>&lt;p&gt;Own it&lt;/p&gt;</description>
  </item>
  <item>
    <title>Beta: Staff Software Engineer</title>
    <region>Anywhere in the World</region>
    <link>https://weworkremotely.com/remote-jobs/beta-eng</link>
    <description>x</description>
  </item>
  <item>
    <title>Gamma: Head of Product (Remote)</title>
    <region>USA Only</region>
    <link>https://weworkremotely.com/remote-jobs/gamma-pl</link>
    <description>y</description>
  </item>
</channel></rss>"""


def _patch_get_text(monkeypatch, src, body):
    async def fake(url, **kwargs):
        return body
    monkeypatch.setattr(src, "_get_text", fake)


class TestWeWorkRemotely:
    def test_is_catalog_api_source(self):
        assert issubclass(WeWorkRemotelySource, ApiSource)
        assert WeWorkRemotelySource().accepts_query is False

    async def test_parses_filters_and_maps(self, monkeypatch):
        src = WeWorkRemotelySource()
        _patch_get_text(monkeypatch, src, RSS)

        jobs = await src.scrape()
        # Engineer role filtered out by the role title filter.
        assert [j["title"] for j in jobs] == ["Senior Product Manager", "Head of Product (Remote)"]
        assert jobs[0]["company"] == "Acme"            # split on "Company: Role"
        assert jobs[0]["location"] == "Remote - Europe"
        assert jobs[0]["url"].endswith("/acme-spm")
        assert jobs[0]["description"] == "Own it"      # html unescaped + stripped
        assert all(j["is_remote"] is True for j in jobs)
        assert all(j["platform"] == "weworkremotely" for j in jobs)

    async def test_empty_body_yields_nothing(self, monkeypatch):
        src = WeWorkRemotelySource()
        _patch_get_text(monkeypatch, src, None)
        assert await src.scrape() == []

    async def test_malformed_xml_is_skipped(self, monkeypatch):
        src = WeWorkRemotelySource()
        _patch_get_text(monkeypatch, src, "<rss><channel><item>broken")
        assert await src.scrape() == []


def test_strip_html_unescapes_and_strips():
    assert _strip_html("&lt;p&gt;Hello &amp; bye&lt;/p&gt;") == "Hello & bye"
    assert _strip_html(None) == ""
