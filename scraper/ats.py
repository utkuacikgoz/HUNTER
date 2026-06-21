"""ATS catalog sources — Greenhouse, Lever, and Ashby public job-board APIs.

Each is an `ApiSource` with `accepts_query = False`: it lists every role on its
configured company boards in one pass, then keeps only titles matching
ROLE_MATCH_KEYWORDS. The board token doubles as the company name — the seeded
tokens are the sponsor-friendly companies in SPONSOR_FRIENDLY_COMPANIES, so the
downstream filter recognizes them as allowlisted.
"""
import html
import logging
import re
from abc import abstractmethod
from datetime import date

from config.settings import (
    ASHBY_BOARDS,
    GREENHOUSE_BOARDS,
    LEVER_BOARDS,
    RECRUITEE_BOARDS,
    ROLE_MATCH_KEYWORDS,
    SMARTRECRUITERS_COMPANIES,
)
from scraper.base import ApiSource

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str:
    """HTML -> plain text: unescape entities first (ATS content is often escaped),
    then drop tags and collapse whitespace."""
    unescaped = html.unescape(text or "")
    return " ".join(_TAG_RE.sub(" ", unescaped).split())


class AtsSource(ApiSource):
    """Base for ATS board sources. Subclasses implement `_fetch_board`."""

    accepts_query = False

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless)
        self.boards = list(boards) if boards is not None else []

    def _title_matches(self, title: str) -> bool:
        t = (title or "").lower()
        return any(kw in t for kw in ROLE_MATCH_KEYWORDS)

    async def scrape(self, query: str = "", location: str = "", max_results: int = 10) -> list[dict]:
        jobs: list[dict] = []
        # The loop stops once max_results is hit, so a fixed board order would
        # starve the tail of the list. Rotate by a daily offset so every board
        # gets its turn near the front over a span of days.
        boards = self.boards
        if boards:
            offset = date.today().toordinal() % len(boards)
            boards = boards[offset:] + boards[:offset]
        scanned = 0
        for board in boards:
            try:
                board_jobs = await self._fetch_board(board)
            except Exception as e:
                logger.warning(f"{self.platform_name}: board {board!r} failed: {e}")
                continue
            scanned += 1
            jobs.extend(board_jobs)
            if len(jobs) >= max_results:
                break
        logger.info(
            f"{self.platform_name}: {len(jobs)} matching role(s) from {scanned}/{len(self.boards)} board(s)"
        )
        return jobs[:max_results]

    @abstractmethod
    async def _fetch_board(self, board: str) -> list[dict]:
        ...


class GreenhouseSource(AtsSource):
    platform_name = "greenhouse"

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless, GREENHOUSE_BOARDS if boards is None else boards)

    async def _fetch_board(self, board: str) -> list[dict]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        data = await self._get_json(url)
        if not isinstance(data, dict):
            return []
        out = []
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not self._title_matches(title):
                continue
            loc = (j.get("location") or {}).get("name", "")
            url_j = j.get("absolute_url", "")
            desc = _strip_html(j.get("content", ""))
            if title and url_j:
                out.append(self._normalize_job(title, board, loc, "", url_j, desc[:500]))
        return out


class LeverSource(AtsSource):
    platform_name = "lever"

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless, LEVER_BOARDS if boards is None else boards)

    async def _fetch_board(self, board: str) -> list[dict]:
        url = f"https://api.lever.co/v0/postings/{board}?mode=json"
        data = await self._get_json(url)
        if not isinstance(data, list):
            return []
        out = []
        for j in data:
            title = j.get("text", "")
            if not self._title_matches(title):
                continue
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ""
            url_j = j.get("hostedUrl", "") or j.get("applyUrl", "")
            desc = j.get("descriptionPlain", "") or ""
            is_remote = str(j.get("workplaceType", "")).lower() == "remote"
            if title and url_j:
                out.append(self._normalize_job(title, board, loc, "", url_j, desc[:500], is_remote))
        return out


class RecruiteeSource(AtsSource):
    platform_name = "recruitee"

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless, RECRUITEE_BOARDS if boards is None else boards)

    async def _fetch_board(self, board: str) -> list[dict]:
        url = f"https://{board}.recruitee.com/api/offers/"
        data = await self._get_json(url)
        if not isinstance(data, dict):
            return []
        out = []
        for j in data.get("offers", []):
            title = j.get("title", "")
            if not self._title_matches(title):
                continue
            loc = j.get("location") or ", ".join(
                p for p in (j.get("city"), j.get("country")) if p
            )
            url_j = j.get("careers_url") or j.get("careers_apply_url", "")
            desc = _strip_html(j.get("description", ""))
            is_remote = bool(j.get("remote"))
            if title and url_j:
                out.append(self._normalize_job(title, board, loc, "", url_j, desc[:500], is_remote))
        return out


class SmartRecruitersSource(AtsSource):
    platform_name = "smartrecruiters"

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless, SMARTRECRUITERS_COMPANIES if boards is None else boards)

    async def _fetch_board(self, board: str) -> list[dict]:
        url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100"
        data = await self._get_json(url)
        if not isinstance(data, dict):
            return []
        out = []
        for j in data.get("content", []):
            title = j.get("name", "")
            if not self._title_matches(title):
                continue
            loc_obj = j.get("location") or {}
            parts = [loc_obj.get("city"), loc_obj.get("country")]
            loc = loc_obj.get("fullLocation") or ", ".join(p for p in parts if p)
            is_remote = bool(loc_obj.get("remote"))
            if is_remote:
                loc = f"Remote - {loc}" if loc else "Remote"
            # The list API omits the public apply URL; build it from the posting id.
            posting_id = j.get("id") or (j.get("ref", "").rstrip("/").rsplit("/", 1)[-1])
            url_j = f"https://jobs.smartrecruiters.com/{board}/{posting_id}" if posting_id else ""
            if title and url_j:
                out.append(self._normalize_job(title, board, loc, "", url_j, "", is_remote))
        return out


class AshbySource(AtsSource):
    platform_name = "ashby"

    def __init__(self, headless: bool = True, boards: list[str] | None = None):
        super().__init__(headless, ASHBY_BOARDS if boards is None else boards)

    async def _fetch_board(self, board: str) -> list[dict]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        data = await self._get_json(url)
        if not isinstance(data, dict):
            return []
        out = []
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not self._title_matches(title):
                continue
            loc = j.get("location", "") or ""
            url_j = j.get("jobUrl", "") or j.get("applyUrl", "")
            desc = j.get("descriptionPlain", "") or _strip_html(j.get("descriptionHtml", ""))
            is_remote = bool(j.get("isRemote")) or str(j.get("workplaceType", "")).lower() == "remote"
            if title and url_j:
                out.append(self._normalize_job(title, board, loc, "", url_j, desc[:500], is_remote))
        return out
