"""Wellfound (AngelList) job scraper."""
import logging
from urllib.parse import quote_plus

from config.settings import WELLFOUND_SELECTORS
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)


async def _find_cards(page, selectors: list[str]):
    """Try each selector in order; return cards from the first that matches.

    Logs hit-counts for every selector so DOM drift is visible in the logs.
    """
    hits: list[tuple[str, int]] = []
    chosen: list = []
    for sel in selectors:
        try:
            found = await page.query_selector_all(sel)
        except Exception as e:
            logger.debug(f"wellfound selector {sel!r} errored: {e}")
            found = []
        hits.append((sel, len(found)))
        if found and not chosen:
            chosen = found
    summary = ", ".join(f"{sel}={n}" for sel, n in hits)
    logger.info(f"wellfound selectors: {summary}")
    return chosen


class WellfoundScraper(BaseScraper):
    platform_name = "wellfound"

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs = []
        try:
            page, context = await self.new_page()
            try:
                encoded_query = quote_plus(query)
                url = f"https://wellfound.com/role/r/{encoded_query.lower().replace('+', '-')}"

                logger.info(f"Wellfound: scraping {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self.delay()

                # Scroll to load
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await self.delay()

                cards = await _find_cards(page, WELLFOUND_SELECTORS)

                for card in cards[:max_results]:
                    try:
                        title_el = await card.query_selector(
                            "a[class*='jobTitle'], h2 a, a[data-test='job-title']"
                        )
                        company_el = await card.query_selector(
                            "a[class*='companyName'], h2 + a, a[class*='company']"
                        )
                        location_el = await card.query_selector(
                            "span[class*='location'], div[class*='location']"
                        )
                        salary_el = await card.query_selector(
                            "span[class*='compensation'], div[class*='salary']"
                        )

                        title = await title_el.inner_text() if title_el else ""
                        company = await company_el.inner_text() if company_el else ""
                        loc = await location_el.inner_text() if location_el else ""
                        salary = await salary_el.inner_text() if salary_el else ""

                        href = ""
                        if title_el:
                            href = await title_el.get_attribute("href") or ""
                        if href and not href.startswith("http"):
                            href = f"https://wellfound.com{href}"

                        if title and href:
                            jobs.append(self._normalize_job(
                                title=title,
                                company=company,
                                location=loc,
                                salary=salary,
                                url=href,
                            ))
                    except Exception as e:
                        logger.debug(f"Wellfound: error parsing card: {e}")
                        continue

                logger.info(f"Wellfound: found {len(jobs)} jobs for '{query}'")
            finally:
                await page.close()
                await context.close()

        except Exception as e:
            logger.error(f"Wellfound scraper error: {e}")

        return jobs
