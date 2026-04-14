"""Wellfound (AngelList) job scraper."""
import logging
import asyncio
from urllib.parse import quote_plus
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)


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

                cards = await page.query_selector_all(
                    "div.styles_jobListing__aFBtk"
                ) or await page.query_selector_all(
                    "div[class*='jobListing']"
                ) or await page.query_selector_all(
                    "div.mb-6"
                )

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
