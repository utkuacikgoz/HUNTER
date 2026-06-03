"""Post-scrape filter: classify jobs by region, remote, and sponsor-friendliness.

Verdicts:
- include: confidently a fit (remote + allowed region + sponsor-friendly)
- flag:    plausible fit but at least one unknown signal (yellow flag in Telegram)
- drop:    clearly disqualified (US-only language, wrong region, on-site, blocklisted)
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from config.settings import (
    REGION_ALLOWLIST,
    REMOTE_REQUIRED,
    SPONSOR_BLOCKLIST_COMPANIES,
    SPONSOR_FRIENDLY_COMPANIES,
)

logger = logging.getLogger(__name__)

Region = Literal["us", "eu", "emea", "other", "unknown"]
SponsorStatus = Literal["allowlist", "blocklist", "llm_yes", "llm_no", "unknown"]
Verdict = Literal["include", "flag", "drop"]


@dataclass
class JobVerdict:
    region: Region
    is_remote: bool
    sponsor_status: SponsorStatus
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)


# Region tokens are matched word-boundary-safe via _normalize/_has below, so
# "uk" no longer hits inside "Milwaukee" and the pronoun "us" no longer reads as
# the USA. Tokens are stored already normalized (lowercase, no punctuation).

# Unambiguous tokens — safe to match anywhere (location + title + description).
_US_TOKENS = {
    "united states", "usa", "u s a", "us only", "us based",
    "us remote", "remote us",
}
_EU_TOKENS = {
    "european union", "europe", "eu", "eea",
    "germany", "france", "spain", "italy", "netherlands", "ireland", "portugal",
    "poland", "sweden", "denmark", "finland", "norway", "belgium", "austria",
    "czech", "romania", "greece", "hungary", "estonia", "lithuania", "latvia",
    "berlin", "paris", "amsterdam", "madrid", "barcelona", "dublin", "lisbon",
    "stockholm", "copenhagen", "munich", "warsaw",
}
_EMEA_TOKENS = {
    "emea", "middle east", "africa", "mena",
    "turkey", "turkiye", "istanbul", "ankara", "izmir",
    "uae", "united arab emirates", "dubai", "abu dhabi",
    "saudi arabia", "ksa", "riyadh",
    "qatar", "doha", "bahrain", "kuwait", "oman", "jordan", "israel", "tel aviv",
    "egypt", "cairo", "morocco", "south africa", "johannesburg", "cape town",
    "london", "uk", "united kingdom", "britain", "england", "scotland",
}
# Short / ambiguous tokens — only trusted in the structured location field, never
# free-text description (avoids "join us" -> USA, state names appearing in prose).
_US_LOC_ONLY = {"us"}
_US_STATE_HINTS = {
    "california", "new york", "texas", "florida", "washington", "massachusetts",
    "illinois", "georgia", "colorado", "oregon", "ohio", "virginia", "michigan",
    "north carolina", "pennsylvania", "san francisco", "seattle", "boston", "austin",
    "chicago", "denver", "los angeles", "nyc",
}
_REMOTE_TOKENS = {"remote", "work from home", "wfh", "anywhere", "distributed", "fully remote"}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str | None) -> str:
    """Lowercase, collapse punctuation to spaces, pad — makes token checks
    word-boundary safe (a token matches only when surrounded by spaces)."""
    return f" {_NON_ALNUM.sub(' ', (text or '').lower()).strip()} "


def _has(normalized: str, tokens: set[str]) -> bool:
    return any(f" {tok} " in normalized for tok in tokens)

_US_ONLY_BLOCKERS = re.compile(
    r"(?i)("
    r"u\.?s\.? citizens?\s+only"
    r"|must be (located|based) in the (u\.?s\.?|united states)"
    r"|work authorization in the u\.?s\.?\s+required"
    r"|authorized to work in the (u\.?s\.?|united states)\s+(without|with no) sponsorship"
    r"|no visa sponsorship"
    r"|sponsorship (is )?not (available|provided|offered)"
    r"|unable to (provide|offer) (visa )?sponsorship"
    r"|green card holders? only"
    r"|u\.?s\.?-only"
    r")"
)


def _haystack(job: dict, *, include_description: bool = True) -> str:
    parts = [job.get("title") or "", job.get("location") or ""]
    if include_description:
        parts.append(job.get("description") or "")
    return " ".join(parts).lower()


def detect_us_only_blocker(job: dict) -> str | None:
    """Return the matched phrase, or None."""
    text = job.get("description") or ""
    m = _US_ONLY_BLOCKERS.search(text)
    return m.group(0) if m else None


def detect_remote(job: dict) -> bool:
    hay = _haystack(job)
    return any(token in hay for token in _REMOTE_TOKENS)


def classify_region(job: dict) -> Region:
    """Best-effort region from location (primary) + title + description (secondary).

    Strong tokens (countries, cities, multi-word) are matched everywhere; short
    ambiguous tokens ("us", US state names) only against the structured location
    field. EMEA wins over EU (it subsumes UK/Middle East/Africa), EU over US.
    """
    location = _normalize(job.get("location"))
    full = _normalize(
        f"{job.get('location') or ''} {job.get('title') or ''} {job.get('description') or ''}"
    )

    if _has(full, _EMEA_TOKENS):
        return "emea"
    if _has(full, _EU_TOKENS):
        return "eu"
    if _has(full, _US_TOKENS) or _has(location, _US_LOC_ONLY) or _has(location, _US_STATE_HINTS):
        return "us"

    # Plain "Remote" with no country signal, or no location at all: unknown
    # (so it gets flagged for human review, not silently dropped).
    if _has(location, _REMOTE_TOKENS) or not location.strip():
        return "unknown"
    return "other"


def check_sponsor_allowlist(company: str) -> SponsorStatus:
    name = (company or "").strip().lower()
    if not name:
        return "unknown"
    if name in SPONSOR_BLOCKLIST_COMPANIES:
        return "blocklist"
    if name in SPONSOR_FRIENDLY_COMPANIES:
        return "allowlist"
    return "unknown"


def evaluate_job(
    job: dict,
    *,
    llm_score: Callable[[str, str], Awaitable[dict]] | None = None,
) -> JobVerdict:
    """Synchronous classifier. Pass `llm_score=None` to keep it pure-Python.

    For LLM-assisted scoring use `evaluate_job_async` instead.
    """
    return _evaluate_sync(job)


async def evaluate_job_async(
    job: dict,
    *,
    llm_score: Callable[[str, str], Awaitable[dict]] | None = None,
) -> JobVerdict:
    verdict = _evaluate_sync(job)
    # Only invoke the LLM when we'd otherwise flag for unknown sponsor.
    if llm_score is None or verdict.sponsor_status != "unknown" or verdict.verdict == "drop":
        return verdict
    try:
        score = await llm_score(job.get("company", ""), job.get("description", ""))
        decision = (score or {}).get("verdict", "unclear")
        reason = (score or {}).get("reasons", "")
    except Exception as e:
        logger.warning(f"LLM sponsor scoring failed for {job.get('company')!r}: {e}")
        return verdict

    if decision == "yes":
        verdict.sponsor_status = "llm_yes"
        verdict.verdict = "include"
        verdict.reasons.append(f"llm: {reason}".strip())
    elif decision == "no":
        verdict.sponsor_status = "llm_no"
        verdict.verdict = "drop"
        verdict.reasons.append(f"llm: {reason}".strip())
    else:
        verdict.reasons.append(f"llm unclear: {reason}".strip())
    return verdict


def _evaluate_sync(job: dict) -> JobVerdict:
    reasons: list[str] = []

    blocker = detect_us_only_blocker(job)
    if blocker:
        return JobVerdict(
            region="us",
            is_remote=detect_remote(job),
            sponsor_status="blocklist",
            verdict="drop",
            reasons=[f"us-only language: {blocker!r}"],
        )

    sponsor = check_sponsor_allowlist(job.get("company", ""))
    if sponsor == "blocklist":
        return JobVerdict(
            region=classify_region(job),
            is_remote=detect_remote(job),
            sponsor_status="blocklist",
            verdict="drop",
            reasons=[f"company in blocklist: {job.get('company')!r}"],
        )

    is_remote = detect_remote(job)
    if REMOTE_REQUIRED and not is_remote:
        return JobVerdict(
            region=classify_region(job),
            is_remote=False,
            sponsor_status=sponsor,
            verdict="drop",
            reasons=["not marked remote"],
        )

    region = classify_region(job)
    if region == "other":
        return JobVerdict(
            region=region,
            is_remote=is_remote,
            sponsor_status=sponsor,
            verdict="drop",
            reasons=["region outside allowlist"],
        )
    if region != "unknown" and region not in REGION_ALLOWLIST:
        return JobVerdict(
            region=region,
            is_remote=is_remote,
            sponsor_status=sponsor,
            verdict="drop",
            reasons=[f"region {region} not in REGION_ALLOWLIST"],
        )

    if sponsor == "allowlist":
        if region == "unknown":
            reasons.append("unknown region but sponsor-friendly company")
        return JobVerdict(
            region=region,
            is_remote=is_remote,
            sponsor_status="allowlist",
            verdict="include",
            reasons=reasons or ["allowlist match"],
        )

    # Unknown sponsor + good region/remote -> flag for human review.
    reasons.append("unknown sponsor — needs review")
    if region == "unknown":
        reasons.append("region not detected from text")
    return JobVerdict(
        region=region,
        is_remote=is_remote,
        sponsor_status=sponsor,
        verdict="flag",
        reasons=reasons,
    )
