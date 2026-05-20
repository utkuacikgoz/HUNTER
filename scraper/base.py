"""Base scraper with shared Playwright logic."""
import asyncio
import logging
import random
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, async_playwright

from config.settings import (
    PROXY_URL,
    SCRAPE_DELAY_MAX,
    SCRAPE_DELAY_MIN,
    USER_AGENTS,
)

logger = logging.getLogger(__name__)

_BLOCKED_PROXY_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "metadata.google.internal"}


class BaseScraper(ABC):
    """Abstract base for all platform scrapers."""

    platform_name: str = "base"

    def __init__(self, headless=True):
        self.headless = headless
        self._browser: Browser | None = None
        self._playwright = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if PROXY_URL:
            parsed = urlparse(PROXY_URL)
            if parsed.scheme not in ("http", "https", "socks5"):
                raise ValueError(f"Invalid proxy scheme: {parsed.scheme}")
            if parsed.hostname and parsed.hostname.lower() in _BLOCKED_PROXY_HOSTS:
                raise ValueError("Proxy to internal/metadata networks is blocked")
            launch_kwargs["proxy"] = {"server": PROXY_URL}
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        return self

    async def __aexit__(self, *args):
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"{self.platform_name}: browser.close failed: {e}")
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"{self.platform_name}: playwright.stop failed: {e}")

    async def new_page(self, cookies=None) -> tuple[Page, object]:
        ua = random.choice(USER_AGENTS)
        context = await self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
        )
        try:
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            return page, context
        except Exception:
            await context.close()
            raise

    async def delay(self):
        """Random delay between requests for anti-detection."""
        wait = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
        await asyncio.sleep(wait)

    @abstractmethod
    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        """Return list of job dicts: {title, company, location, salary, url, description}"""
        ...

    def _normalize_job(self, title, company, location, salary, url, description=""):
        return {
            "title": (title or "").strip(),
            "company": (company or "").strip(),
            "location": (location or "").strip(),
            "salary": (salary or "").strip(),
            "url": (url or "").strip(),
            "platform": self.platform_name,
            "description": (description or "").strip(),
        }
