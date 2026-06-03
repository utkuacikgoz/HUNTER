"""RemoteOK job source — uses their public JSON API (no browser)."""
import logging

from scraper.base import ApiSource

logger = logging.getLogger(__name__)


class RemoteOKScraper(ApiSource):
    platform_name = "remoteok"

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs: list[dict] = []
        try:
            tag = query.lower().replace(" ", "-")
            api_url = f"https://remoteok.com/api?tag={tag}"
            logger.info(f"RemoteOK: fetching {api_url}")

            data = await self._get_json(api_url)
            if data is None:
                return jobs
            if not isinstance(data, list):
                logger.warning(f"RemoteOK: unexpected response type {type(data)}")
                return jobs

            # First element is metadata, skip it.
            listings = data[1:] if len(data) > 1 else []
            for item in listings[:max_results]:
                try:
                    title = item.get("position", "")
                    company = item.get("company", "")
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
                    loc = ", ".join(item.get("location", "Remote").split()) if item.get("location") else "Remote"
                    slug = item.get("slug", "")
                    url = f"https://remoteok.com/remote-jobs/{slug}" if slug else item.get("url", "")
                    description = item.get("description", "")

                    if title and url:
                        jobs.append(self._normalize_job(
                            title=title,
                            company=company,
                            location=loc,
                            salary=salary,
                            url=url,
                            description=description[:500],
                        ))
                except Exception as e:
                    logger.debug(f"RemoteOK: error parsing item: {e}")
                    continue

            logger.info(f"RemoteOK: found {len(jobs)} jobs for '{query}'")

        except Exception as e:
            logger.error(f"RemoteOK scraper error: {e}")

        return jobs
