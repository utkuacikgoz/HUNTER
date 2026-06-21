"""We Work Remotely job source — public remote-only RSS feed (no browser).

Catalog source: fetches the Product category RSS once per run. Every posting is
remote; the feed's `<region>` ("Anywhere in the World", "Berlin", "USA Only", …) is
mapped into the location field for the region / country-lock filter. The category is
broad (engineering/sales leak in), so the PM title filter is load-bearing.
"""
import html
import logging
import re
import xml.etree.ElementTree as ET

from config.settings import ROLE_MATCH_KEYWORDS, WEWORKREMOTELY_FEEDS
from scraper.base import ApiSource

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
    return " ".join(_TAG_RE.sub(" ", html.unescape(text or "")).split())


class WeWorkRemotelySource(ApiSource):
    platform_name = "weworkremotely"
    accepts_query = False

    def _title_matches(self, title: str) -> bool:
        t = (title or "").lower()
        return any(kw in t for kw in ROLE_MATCH_KEYWORDS)

    async def scrape(self, query: str = "", location: str = "", max_results: int = 10) -> list[dict]:
        jobs: list[dict] = []
        seen: set[str] = set()
        for feed in WEWORKREMOTELY_FEEDS:
            try:
                body = await self._get_text(feed)
                if not body:
                    continue
                root = ET.fromstring(body)
            except Exception as e:
                logger.warning(f"WeWorkRemotely: feed {feed!r} failed: {e}")
                continue
            for item in root.findall(".//item"):
                # Title is "Company: Role"; the role half drives the PM filter.
                raw_title = (item.findtext("title") or "").strip()
                company, _, role = raw_title.partition(":")
                role = role.strip() or raw_title
                company = company.strip() if role != raw_title else ""
                if not self._title_matches(role):
                    continue
                url_j = (item.findtext("link") or "").strip()
                if not (role and url_j) or url_j in seen:
                    continue
                seen.add(url_j)
                region = (item.findtext("region") or "").strip()
                loc = f"Remote - {region}" if region else "Remote"
                desc = _strip_html(item.findtext("description"))
                jobs.append(self._normalize_job(
                    title=role,
                    company=company,
                    location=loc,
                    salary="",
                    url=url_j,
                    description=desc[:500],
                    is_remote=True,
                ))
                if len(jobs) >= max_results:
                    break
            if len(jobs) >= max_results:
                break
        logger.info(f"WeWorkRemotely: found {len(jobs)} matching role(s) from {len(WEWORKREMOTELY_FEEDS)} feed(s)")
        return jobs[:max_results]
