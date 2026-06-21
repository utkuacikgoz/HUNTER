"""RemoteOK job source — uses their public JSON API (no browser).

Catalog source: RemoteOK's API is tag-driven (/api?tag=<tag>), so it iterates the
configured REMOTEOK_TAGS once per run rather than once per free-text SEARCH_QUERY
(multi-word queries produce dead tags). Every RemoteOK posting is remote, and the
PM title filter trims the broad "product" tag down to manager-level roles.
"""
import logging

from config.settings import REMOTEOK_TAGS, ROLE_MATCH_KEYWORDS
from scraper.base import ApiSource

logger = logging.getLogger(__name__)


class RemoteOKScraper(ApiSource):
    platform_name = "remoteok"
    accepts_query = False

    def _title_matches(self, title: str) -> bool:
        t = (title or "").lower()
        return any(kw in t for kw in ROLE_MATCH_KEYWORDS)

    async def scrape(self, query: str = "", location: str = "", max_results: int = 10) -> list[dict]:
        jobs: list[dict] = []
        seen: set[str] = set()
        for tag in REMOTEOK_TAGS:
            try:
                api_url = f"https://remoteok.com/api?tag={tag}"
                logger.info(f"RemoteOK: fetching {api_url}")
                data = await self._get_json(api_url)
                if not isinstance(data, list):
                    if data is not None:
                        logger.warning(f"RemoteOK: unexpected response type {type(data)}")
                    continue
                # First element is metadata, skip it.
                for item in data[1:]:
                    try:
                        title = item.get("position", "")
                        if not self._title_matches(title):
                            continue
                        slug = item.get("slug", "")
                        url = f"https://remoteok.com/remote-jobs/{slug}" if slug else item.get("url", "")
                        if not (title and url) or url in seen:
                            continue
                        seen.add(url)

                        salary_min = item.get("salary_min", "")
                        salary_max = item.get("salary_max", "")
                        salary = ""
                        try:
                            if salary_min and salary_max:
                                salary = f"${int(salary_min):,} - ${int(salary_max):,}"
                            elif salary_min:
                                salary = f"${int(salary_min):,}+"
                        except (ValueError, TypeError):
                            salary = str(salary_min) if salary_min else ""
                        # RemoteOK postings are all remote; its "location" field is a
                        # residence restriction (e.g. "United States", "Worldwide").
                        # Prefix "Remote - " so the country-lock filter can read it.
                        raw_loc = (item.get("location") or "").strip()
                        loc = f"Remote - {raw_loc}" if raw_loc else "Remote"
                        description = item.get("description", "")

                        jobs.append(self._normalize_job(
                            title=title,
                            company=item.get("company", ""),
                            location=loc,
                            salary=salary,
                            url=url,
                            description=description[:500],
                            is_remote=True,
                        ))
                        if len(jobs) >= max_results:
                            break
                    except Exception as e:
                        logger.debug(f"RemoteOK: error parsing item: {e}")
                        continue
            except Exception as e:
                logger.error(f"RemoteOK scraper error for tag {tag!r}: {e}")
            if len(jobs) >= max_results:
                break

        logger.info(f"RemoteOK: found {len(jobs)} matching role(s) from {len(REMOTEOK_TAGS)} tag(s)")
        return jobs[:max_results]
