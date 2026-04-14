"""RemoteOK job scraper - uses their JSON API."""
import logging
import random
import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from scraper.base import BaseScraper
from config.settings import USER_AGENTS

logger = logging.getLogger(__name__)


class RemoteOKScraper(BaseScraper):
    platform_name = "remoteok"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
        reraise=True,
    )
    async def _fetch_api(self, api_url: str):
        """Fetch RemoteOK JSON API with retry on transient failures."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers={"User-Agent": random.choice(USER_AGENTS)},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    logger.warning("RemoteOK: rate limited (429), retrying...")
                    raise aiohttp.ClientError("Rate limited")
                if resp.status != 200:
                    logger.warning(f"RemoteOK: status {resp.status}")
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception as e:
                    logger.error(f"RemoteOK: invalid JSON response: {e}")
                    return None

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs = []
        try:
            tag = query.lower().replace(" ", "-")
            api_url = f"https://remoteok.com/api?tag={tag}"

            logger.info(f"RemoteOK: fetching {api_url}")

            data = await self._fetch_api(api_url)

            if data is None:
                return jobs

            if not isinstance(data, list):
                logger.warning(f"RemoteOK: unexpected response type {type(data)}")
                return jobs

            # First element is metadata, skip it
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
