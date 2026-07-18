"""Source interfaces shared by all job sources.

`JobSource` is the common contract (platform_name, scrape, _normalize_job, delay,
and a no-op async context manager). `BrowserSource` adds Playwright — a headless
Chromium per `async with`. `ApiSource` adds a shared aiohttp session with a
retried JSON GET and never launches a browser. `BaseScraper` is kept as an alias
of `BrowserSource` for backward compatibility with existing scrapers and tests.
"""
import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import aiohttp
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import (
    CHROMIUM_LAUNCH_ARGS,
    PROXY_URL,
    ROLE_EXCLUDE_KEYWORDS,
    ROLE_MATCH_KEYWORDS,
    SCRAPE_DELAY_MAX,
    SCRAPE_DELAY_MIN,
    USER_AGENTS,
)

logger = logging.getLogger(__name__)

_BLOCKED_PROXY_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "metadata.google.internal"}  # nosec B104 — SSRF blocklist of hosts to reject, not a socket bind address

_TITLE_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def matches_role_title(title: str) -> bool:
    """Keep a posting only if its title contains a target role keyword AND no seniority
    exclusion. Includes are plain substrings (ROLE_MATCH_KEYWORDS holds multi-word phrases);
    excludes are word-boundary matched so "intern" doesn't drop "International Product Manager".
    """
    t = (title or "").lower()
    if not any(kw in t for kw in ROLE_MATCH_KEYWORDS):
        return False
    norm = f" {_TITLE_NON_ALNUM.sub(' ', t).strip()} "
    return not any(f" {bad} " in norm for bad in ROLE_EXCLUDE_KEYWORDS)


class JobSource(ABC):
    """Common contract for every job source.

    Subclass `BrowserSource` for DOM-scraped sites or `ApiSource` for HTTP/JSON
    APIs. `accepts_query` tells the orchestrator how to drive the source:
    True  = call scrape() once per configured search query (site search);
    False = catalog source — call scrape() once and let it filter its own
            catalog against the configured queries internally.
    """

    platform_name: str = "base"
    accepts_query: bool = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def delay(self) -> None:
        """Random delay between requests for anti-detection / politeness."""
        await asyncio.sleep(random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX))

    @abstractmethod
    async def scrape(self, query: str, location: str = "", max_results: int = 10) -> list[dict]:
        """Return list of job dicts: {title, company, location, salary, url, description}"""
        ...

    def _normalize_job(self, title, company, location, salary, url, description="", is_remote=None):
        """Normalize a scraped job into the common dict shape.

        `is_remote` carries a structured remote flag straight from a source API
        (Ashby `isRemote`, Lever `workplaceType`, Recruitee `remote`, …) when the
        source knows it. `None` means "unknown" — the filter then falls back to
        keyword-matching the location/description text.
        """
        return {
            "title": (title or "").strip(),
            "company": (company or "").strip(),
            "location": (location or "").strip(),
            "salary": (salary or "").strip(),
            "url": (url or "").strip(),
            "platform": self.platform_name,
            "description": (description or "").strip(),
            "is_remote": is_remote,
        }


class BrowserSource(JobSource):
    """Job source backed by a headless Playwright Chromium (one per `async with`)."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._browser: Browser | None = None
        self._playwright = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            "args": CHROMIUM_LAUNCH_ARGS,
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

    async def new_page(self, cookies=None) -> tuple[Page, BrowserContext]:
        assert self._browser is not None, "browser not started — use 'async with scraper:'"
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


# Backward-compat alias: existing scrapers and tests import `BaseScraper`.
BaseScraper = BrowserSource


class ApiSource(JobSource):
    """Job source backed by an HTTP/JSON API — shared aiohttp session, no browser."""

    def __init__(self, headless: bool = True):
        # `headless` is accepted for a uniform constructor with BrowserSource but
        # is meaningless here (there is no browser).
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self._session:
            try:
                await self._session.close()
            except Exception as e:
                logger.warning(f"{self.platform_name}: session.close failed: {e}")
        return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
        reraise=True,
    )
    async def _get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None):
        """GET and parse JSON, retrying transient transport errors / 429s.

        Returns parsed JSON, or None on a non-200 / unparseable response. Raises
        only after repeated transport failures (tenacity reraise).
        """
        assert self._session is not None, "session not started — use 'async with source:'"
        hdrs = {"User-Agent": random.choice(USER_AGENTS)}
        if headers:
            hdrs.update(headers)
        async with self._session.get(
            url, params=params, headers=hdrs, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 429:
                logger.warning(f"{self.platform_name}: rate limited (429), retrying...")
                raise aiohttp.ClientError("Rate limited")
            if resp.status != 200:
                logger.warning(f"{self.platform_name}: status {resp.status} for {url}")
                return None
            try:
                return await resp.json(content_type=None)
            except Exception as e:
                logger.error(f"{self.platform_name}: invalid JSON from {url}: {e}")
                return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
        reraise=True,
    )
    async def _get_text(self, url: str, *, params: dict | None = None, headers: dict | None = None):
        """GET and return the response body as text (e.g. RSS/XML feeds).

        Returns the body string, or None on a non-200 response. Raises only after
        repeated transport failures (tenacity reraise).
        """
        assert self._session is not None, "session not started — use 'async with source:'"
        hdrs = {"User-Agent": random.choice(USER_AGENTS)}
        if headers:
            hdrs.update(headers)
        async with self._session.get(
            url, params=params, headers=hdrs, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 429:
                logger.warning(f"{self.platform_name}: rate limited (429), retrying...")
                raise aiohttp.ClientError("Rate limited")
            if resp.status != 200:
                logger.warning(f"{self.platform_name}: status {resp.status} for {url}")
                return None
            return await resp.text()
