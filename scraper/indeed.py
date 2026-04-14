"""Indeed job scraper."""
import logging
from urllib.parse import quote_plus
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)


class IndeedScraper(BaseScraper):
    platform_name = "indeed"

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs = []
        try:
            page, context = await self.new_page()
            encoded_query = quote_plus(query)
            location_param = f"&l={quote_plus(location)}" if location else ""
            url = f"https://www.indeed.com/jobs?q={encoded_query}{location_param}&sort=date&fromage=7"

            logger.info(f"Indeed: scraping {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.delay()

            cards = await page.query_selector_all("div.job_seen_beacon") or \
                    await page.query_selector_all("div.jobsearch-ResultsList > div") or \
                    await page.query_selector_all("td.resultContent")

            for card in cards[:max_results]:
                try:
                    title_el = await card.query_selector(
                        "h2.jobTitle a, a.jcs-JobTitle, span[id^='jobTitle']"
                    )
                    company_el = await card.query_selector(
                        "span[data-testid='company-name'], span.companyName"
                    )
                    location_el = await card.query_selector(
                        "div[data-testid='text-location'], div.companyLocation"
                    )
                    salary_el = await card.query_selector(
                        "div.salary-snippet-container, div.metadata.salary-snippet-container"
                    )

                    title = await title_el.inner_text() if title_el else ""
                    company = await company_el.inner_text() if company_el else ""
                    loc = await location_el.inner_text() if location_el else ""
                    salary = await salary_el.inner_text() if salary_el else ""

                    href = ""
                    if title_el:
                        href = await title_el.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = f"https://www.indeed.com{href}"

                    # Try to get job key for a cleaner URL
                    job_key = await card.get_attribute("data-jk") if not href else ""
                    if job_key and not href:
                        href = f"https://www.indeed.com/viewjob?jk={job_key}"

                    if title and href:
                        jobs.append(self._normalize_job(
                            title=title,
                            company=company,
                            location=loc,
                            salary=salary,
                            url=href,
                        ))
                except Exception as e:
                    logger.debug(f"Indeed: error parsing card: {e}")
                    continue

            await page.close()
            await context.close()
            logger.info(f"Indeed: found {len(jobs)} jobs for '{query}'")

        except Exception as e:
            logger.error(f"Indeed scraper error: {e}")

        return jobs
