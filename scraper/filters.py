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


_US_TOKENS = {
    "united states", "usa", "u.s.", "u.s.a", " us ",
    "remote - us", "remote us", "us-remote", "us only", "us-based",
}
# Common US state names and abbreviations that imply a US-only location when standalone.
_US_STATE_HINTS = {
    "california", "new york", "texas", "florida", "washington", "massachusetts",
    "illinois", "georgia", "colorado", "oregon", "ohio", "virginia", "michigan",
    "north carolina", "pennsylvania", "san francisco", "seattle", "boston", "austin",
    "chicago", "denver", "los angeles", "nyc",
}
_EU_TOKENS = {
    "european union", "europe", "eu ", "eu-", "eu/", " eu)", "eea",
    "germany", "france", "spain", "italy", "netherlands", "ireland", "portugal",
    "poland", "sweden", "denmark", "finland", "norway", "belgium", "austria",
    "czech", "romania", "greece", "hungary", "estonia", "lithuania", "latvia",
    "berlin", "paris", "amsterdam", "madrid", "barcelona", "dublin", "lisbon",
    "stockholm", "copenhagen", "munich", "warsaw",
}
_EMEA_TOKENS = {
    "emea", "middle east", "africa", "mena",
    "turkey", "türkiye", "istanbul", "ankara", "izmir",
    "uae", "united arab emirates", "dubai", "abu dhabi",
    "saudi arabia", "ksa", "riyadh",
    "qatar", "doha", "bahrain", "kuwait", "oman", "jordan", "israel", "tel aviv",
    "egypt", "cairo", "morocco", "south africa", "johannesburg", "cape town",
    "london", "uk", "united kingdom", "britain", "england", "scotland",
}
_REMOTE_TOKENS = {"remote", "work from home", "wfh", "anywhere", "distributed", "fully remote"}

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
    """Best-effort region classification from title + location + description."""
    location = (job.get("location") or "").lower()
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()

    # Prefer matching against the structured location first, fall back to broader text.
    loc_hay = f" {location} "
    full_hay = f" {location} {title} {description} "

    emea_hit = any(t in loc_hay for t in _EMEA_TOKENS) or any(t in full_hay for t in _EMEA_TOKENS)
    eu_hit = any(t in loc_hay for t in _EU_TOKENS) or any(t in full_hay for t in _EU_TOKENS)
    us_hit = (
        any(t in loc_hay for t in _US_TOKENS)
        or any(t in full_hay for t in _US_TOKENS)
        or any(s in loc_hay for s in _US_STATE_HINTS)
    )

    # EMEA includes UK + ME + Africa; prefer it over plain EU if both match.
    if emea_hit:
        return "emea"
    if eu_hit:
        return "eu"
    if us_hit:
        return "us"

    # Plain "Remote" with no country signal: unknown (let it be flagged, not dropped).
    if any(t in loc_hay for t in _REMOTE_TOKENS) or not location.strip():
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
