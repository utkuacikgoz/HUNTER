"""LinkedIn job scraper using session cookie auth."""
import logging
import asyncio
from urllib.parse import quote_plus
from scraper.base import BaseScraper
from config.settings import LINKEDIN_SESSION_COOKIE

logger = logging.getLogger(__name__)


class LinkedInScraper(BaseScraper):
    platform_name = "linkedin"

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs = []
        try:
            cookies = []
            if LINKEDIN_SESSION_COOKIE:
                cookies = [
                    {
                        "name": "li_at",
                        "value": LINKEDIN_SESSION_COOKIE,
                        "domain": ".linkedin.com",
                        "path": "/",
                    }
                ]

            page, context = await self.new_page(cookies=cookies)
            encoded_query = quote_plus(query)
            location_param = f"&location={quote_plus(location)}" if location else ""
            url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_query}{location_param}&f_TPR=r604800&sortBy=DD"

            logger.info(f"LinkedIn: scraping {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.delay()

            # Scroll to load more results
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.delay()

            # Try authenticated selectors first, then public
            cards = await page.query_selector_all("div.job-card-container") or \
                    await page.query_selector_all("div.base-card") or \
                    await page.query_selector_all("li.jobs-search-results__list-item")

            for card in cards[:max_results]:
                try:
                    title_el = await card.query_selector(
                        "a.job-card-list__title, "
                        "h3.base-search-card__title, "
                        "a.job-card-container__link"
                    )
                    company_el = await card.query_selector(
                        "span.job-card-container__primary-description, "
                        "h4.base-search-card__subtitle, "
                        "a.job-card-container__company-name"
                    )
                    location_el = await card.query_selector(
                        "li.job-card-container__metadata-item, "
                        "span.job-search-card__location"
                    )
                    link_el = await card.query_selector(
                        "a.job-card-list__title, "
                        "a.base-card__full-link, "
                        "a.job-card-container__link"
                    )

                    title = await title_el.inner_text() if title_el else ""
                    company = await company_el.inner_text() if company_el else ""
                    loc = await location_el.inner_text() if location_el else ""
                    href = await link_el.get_attribute("href") if link_el else ""

                    if title and href:
                        # Clean up URL
                        if "?" in href:
                            href = href.split("?")[0]
                        if not href.startswith("http"):
                            href = f"https://www.linkedin.com{href}"

                        jobs.append(self._normalize_job(
                            title=title,
                            company=company,
                            location=loc,
                            salary="",
                            url=href,
                        ))
                except Exception as e:
                    logger.debug(f"LinkedIn: error parsing card: {e}")
                    continue

            await page.close()
            await context.close()
            logger.info(f"LinkedIn: found {len(jobs)} jobs for '{query}'")

        except Exception as e:
            logger.error(f"LinkedIn scraper error: {e}")

        return jobs
