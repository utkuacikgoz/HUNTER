"""Glassdoor job scraper."""
import logging
from urllib.parse import quote_plus
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)


class GlassdoorScraper(BaseScraper):
    platform_name = "glassdoor"

    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        jobs = []
        try:
            page, context = await self.new_page()
            encoded_query = quote_plus(query)
            url = f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={encoded_query}&fromAge=7"

            logger.info(f"Glassdoor: scraping {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.delay()

            cards = await page.query_selector_all("li.JobsList_jobListItem__wjTHv") or \
                    await page.query_selector_all("li[data-test='jobListing']") or \
                    await page.query_selector_all("ul.JobsList_jobsList__lqjTr > li")

            for card in cards[:max_results]:
                try:
                    title_el = await card.query_selector(
                        "a.JobCard_jobTitle__GLyJ1, a[data-test='job-title']"
                    )
                    company_el = await card.query_selector(
                        "span.EmployerProfile_compactEmployerName__9MGcV, "
                        "div.EmployerProfile_employerName__Xemli"
                    )
                    location_el = await card.query_selector(
                        "div.JobCard_location__N_iYE, div[data-test='emp-location']"
                    )
                    salary_el = await card.query_selector(
                        "div.JobCard_salaryEstimate__QpbTW, div[data-test='detailSalary']"
                    )

                    title = await title_el.inner_text() if title_el else ""
                    company = await company_el.inner_text() if company_el else ""
                    loc = await location_el.inner_text() if location_el else ""
                    salary = await salary_el.inner_text() if salary_el else ""

                    href = ""
                    if title_el:
                        href = await title_el.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = f"https://www.glassdoor.com{href}"

                    if title and href:
                        jobs.append(self._normalize_job(
                            title=title,
                            company=company,
                            location=loc,
                            salary=salary,
                            url=href,
                        ))
                except Exception as e:
                    logger.debug(f"Glassdoor: error parsing card: {e}")
                    continue

            await page.close()
            await context.close()
            logger.info(f"Glassdoor: found {len(jobs)} jobs for '{query}'")

        except Exception as e:
            logger.error(f"Glassdoor scraper error: {e}")

        return jobs
