"""LinkedIn job scraper using public guest search + optional session cookie."""
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
            # Try authenticated search first, fall back to public guest search
            if LINKEDIN_SESSION_COOKIE:
                jobs = await self._scrape_authenticated(query, location, max_results)
            if not jobs:
                jobs = await self._scrape_guest(query, location, max_results)
        except Exception as e:
            logger.error(f"LinkedIn scraper error: {e}")
        return jobs

    async def _scrape_authenticated(self, query: str, location: str, max_results: int) -> list[dict]:
        """Scrape using li_at session cookie (logged-in view)."""
        jobs = []
        cookies = [{
            "name": "li_at",
            "value": LINKEDIN_SESSION_COOKIE,
            "domain": ".linkedin.com",
            "path": "/",
        }]

        page, context = await self.new_page(cookies=cookies)
        try:
            encoded_query = quote_plus(query)
            location_param = f"&location={quote_plus(location)}" if location else ""
            url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_query}{location_param}&f_TPR=r604800&sortBy=DD"

            logger.info(f"LinkedIn (auth): scraping {url}")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Detect auth failure: redirect to login or captcha
            final_url = page.url
            if "/login" in final_url or "/authwall" in final_url or "/checkpoint" in final_url:
                logger.warning(f"LinkedIn: cookie expired/invalid, redirected to {final_url}")
                return []

            if response and response.status >= 400:
                logger.warning(f"LinkedIn (auth): HTTP {response.status} for {url}")
                return []

            await self.delay()

            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.delay()

            cards = await page.query_selector_all("div.job-card-container") or \
                    await page.query_selector_all("li.jobs-search-results__list-item")

            if not cards:
                logger.warning(f"LinkedIn (auth): no job cards found, page title: {await page.title()}")
                return []

            for card in cards[:max_results]:
                job = await self._parse_auth_card(card)
                if job:
                    jobs.append(job)

            logger.info(f"LinkedIn (auth): found {len(jobs)} jobs for '{query}'")
        except Exception as e:
            if "ERR_TOO_MANY_REDIRECTS" in str(e):
                logger.warning("LinkedIn: cookie caused redirect loop, will try guest search")
            else:
                logger.error(f"LinkedIn (auth) error: {e}")
        finally:
            await page.close()
            await context.close()

        return jobs

    async def _scrape_guest(self, query: str, location: str, max_results: int) -> list[dict]:
        """Scrape using LinkedIn's public guest job search (no login needed)."""
        jobs = []
        page, context = await self.new_page()
        try:
            encoded_query = quote_plus(query)
            location_param = f"&location={quote_plus(location)}" if location else ""
            url = f"https://www.linkedin.com/jobs/search?keywords={encoded_query}{location_param}&f_TPR=r604800&sortBy=DD&position=1&pageNum=0"

            logger.info(f"LinkedIn (guest): scraping {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.delay()

            # Guest search might redirect to jobs-guest
            final_url = page.url
            if "/authwall" in final_url or "/login" in final_url:
                logger.warning(f"LinkedIn (guest): blocked, redirected to {final_url}")
                return []

            # Scroll to load lazy content
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.delay()

            # Guest page uses base-card class
            cards = await page.query_selector_all("div.base-card, div.base-search-card, li.base-card")

            if not cards:
                # Try alternative: job result cards in the ul list
                cards = await page.query_selector_all("ul.jobs-search__results-list > li")

            if not cards:
                page_title = await page.title()
                body_text = await page.evaluate("document.body.innerText.substring(0, 500)")
                logger.warning(f"LinkedIn (guest): no cards found. Title: {page_title}, Body preview: {body_text[:200]}")
                return []

            for card in cards[:max_results]:
                job = await self._parse_guest_card(card)
                if job:
                    jobs.append(job)

            logger.info(f"LinkedIn (guest): found {len(jobs)} jobs for '{query}'")
        except Exception as e:
            logger.error(f"LinkedIn (guest) error: {e}")
        finally:
            await page.close()
            await context.close()

        return jobs

    async def _parse_auth_card(self, card) -> dict | None:
        """Parse a job card from authenticated LinkedIn view."""
        try:
            title_el = await card.query_selector(
                "a.job-card-list__title, a.job-card-container__link"
            )
            company_el = await card.query_selector(
                "span.job-card-container__primary-description, "
                "a.job-card-container__company-name"
            )
            location_el = await card.query_selector(
                "li.job-card-container__metadata-item"
            )

            title = await title_el.inner_text() if title_el else ""
            company = await company_el.inner_text() if company_el else ""
            loc = await location_el.inner_text() if location_el else ""
            href = await title_el.get_attribute("href") if title_el else ""

            if title and href:
                return self._normalize_job(
                    title=title, company=company, location=loc,
                    salary="", url=self._clean_linkedin_url(href),
                )
        except Exception as e:
            logger.debug(f"LinkedIn: auth card parse error: {e}")
        return None

    async def _parse_guest_card(self, card) -> dict | None:
        """Parse a job card from LinkedIn's public guest search."""
        try:
            title_el = await card.query_selector(
                "h3.base-search-card__title"
            )
            company_el = await card.query_selector(
                "h4.base-search-card__subtitle, a.hidden-nested-link"
            )
            location_el = await card.query_selector(
                "span.job-search-card__location"
            )
            link_el = await card.query_selector(
                "a.base-card__full-link"
            )

            title = await title_el.inner_text() if title_el else ""
            company = await company_el.inner_text() if company_el else ""
            loc = await location_el.inner_text() if location_el else ""
            href = await link_el.get_attribute("href") if link_el else ""

            if title and href:
                return self._normalize_job(
                    title=title, company=company, location=loc,
                    salary="", url=self._clean_linkedin_url(href),
                )
        except Exception as e:
            logger.debug(f"LinkedIn: guest card parse error: {e}")
        return None

    @staticmethod
    def _clean_linkedin_url(href: str) -> str:
        """Clean LinkedIn job URL."""
        if not href:
            return ""
        if "?" in href:
            href = href.split("?")[0]
        if not href.startswith("http"):
            href = f"https://www.linkedin.com{href}"
        return href
