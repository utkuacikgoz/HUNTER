"""Post-scrape filter: classify jobs by region, remote, and sponsor-friendliness.

Verdicts:
- include: confidently a fit (remote + allowed region + sponsor-friendly)
- flag:    plausible fit but at least one unknown signal (yellow flag in Telegram)
- drop:    clearly disqualified (US-only language, wrong region, on-site, blocklisted)

ALLOW_ONSITE_FREELANCE exempts freelance/contract roles from the on-site drop, turning
the feed into "remote OR freelance". Only that gate is exempted — region, country-lock
and sponsor checks still apply.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from config.settings import (
    ALLOW_ONSITE_FREELANCE,
    CANDIDATE_WORK_REGIONS,
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
    # Defaulted, so it sits after the required fields rather than next to is_remote.
    is_freelance: bool = False
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
# "u s" catches the punctuated "U.S." form (e.g. Ashby's "Remote U.S."), which
# _normalize splits into two single-letter tokens.
_US_LOC_ONLY = {"us", "u s"}
_US_STATE_HINTS = {
    "california", "new york", "texas", "florida", "washington", "massachusetts",
    "illinois", "georgia", "colorado", "oregon", "ohio", "virginia", "michigan",
    "north carolina", "pennsylvania", "san francisco", "seattle", "boston", "austin",
    "chicago", "denver", "los angeles", "nyc",
}
_REMOTE_TOKENS = {"remote", "work from home", "wfh", "anywhere", "distributed", "fully remote"}
_CANADA_TOKENS = {"canada", "toronto", "vancouver", "montreal", "ontario", "quebec"}
# Explicit US-residence markers that appear in role TITLES (e.g. "VP Product - US-Based").
_TITLE_US_LOCK_TOKENS = {
    "us based", "u s based", "us only", "u s only", "usa only", "united states only",
}
# Tokens that signal a remote role is open beyond a single country — so a US/Canada
# mention alongside one of these is NOT a residence lock.
_GLOBAL_REMOTE_TOKENS = {"worldwide", "anywhere", "global", "globally", "any country", "any location"}

# Employment-type tokens, split by noise the same way _US_TOKENS / _US_LOC_ONLY are.
# These phrases can only mean an employment type, so they're matched across
# title + location + description. Stored already normalized (no punctuation): _normalize
# turns "6-month contract" -> " 6 month contract " and "fixed-term" -> " fixed term ".
# Deliberately excludes part-time / hourly — a schedule is not a contract type.
_FREELANCE_TOKENS = {
    "freelance role", "freelance position", "freelance basis", "freelance contract",
    "freelance engagement", "freelance opportunity",
    "contract role", "contract position", "contract basis", "contract engagement",
    "contract opportunity", "contract assignment", "contract to hire",
    "month contract", "months contract",
    "fixed term", "fixed term contract",
    "project based",
    "interim role", "interim position", "interim basis",
    "temporary contract", "temporary role", "temporary position",
    "independent contractor", "self employed",
    "consulting engagement",
}
# Bare words that mean an employment type in a TITLE but almost always mean something
# else in a JD body: "contract negotiation" is a marketing skill, "fractional shares" is
# a fintech product, "our consulting clients" is a company descriptor, "manage freelance
# copywriters" is a duty, "in the interim" is prose. Title-only keeps those out.
_FREELANCE_TITLE_ONLY = {
    "freelance", "contract", "contractor", "consultant", "consulting", "consultancy",
    "interim", "fractional", "temporary", "temp",
}

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
    # Trust a structured remote flag from the source API (Ashby isRemote, Lever
    # workplaceType, Recruitee remote, …) when present. ATS location text is often
    # just a city, so the keyword fallback alone wrongly drops real remote roles.
    flag = job.get("is_remote")
    if isinstance(flag, bool):
        return flag
    hay = _haystack(job)
    return any(token in hay for token in _REMOTE_TOKENS)


def detect_freelance(job: dict) -> bool:
    """True when the posting reads as freelance / contract / interim work.

    Unambiguous multi-word phrases match anywhere; the short ambiguous words in
    _FREELANCE_TITLE_ONLY are trusted only in the title (see the token comments above).
    Text-only by design — no source exposes a structured employment-type field today.

    Known limit: a title like "Contract Manager" (a role *about* contracts) reads as
    freelance. Unreachable in practice — ROLE_MATCH_KEYWORDS drops it upstream.

    TODO: Lever exposes categories.commitment ("Full-time"/"Contract") and Ashby exposes
    employmentType — ground truth rather than inference. Threading an employment_type
    through _normalize_job would let this trust a source flag first, exactly as
    detect_remote already does with is_remote.
    """
    if _has(_normalize(job.get("title")), _FREELANCE_TITLE_ONLY):
        return True
    full = _normalize(
        f"{job.get('title') or ''} {job.get('location') or ''} {job.get('description') or ''}"
    )
    return _has(full, _FREELANCE_TOKENS)


def detect_country_locked_remote(job: dict) -> str | None:
    """A remote role whose *location* is restricted to the US and/or Canada, with
    no EU/EMEA or global scope. Such roles require residence / work authorization
    there, so they won't accept an overseas (EMEA-based) candidate — even at a
    sponsor-friendly company.

    Returns the location text when locked, else None. Checks the structured
    location field plus explicit US-lock markers in the title ("US-Based"); other
    description prose ("our US team") is too noisy.
    """
    # Explicit US-lock in the title is a strong signal regardless of location
    # (e.g. "VP of Product - US-Based" with location "Remote").
    ntitle = _normalize(job.get("title"))
    if _has(ntitle, _TITLE_US_LOCK_TOKENS):
        return (job.get("title") or "").strip()
    nloc = _normalize(job.get("location"))
    # A structured is_remote flag (Ashby/Lever/Greenhouse) counts as a remote signal even
    # when the location text is bare US cities with no literal "remote" word (e.g.
    # "San Francisco, CA • New York, NY • United States") — those US-remote roles still
    # won't take an overseas hire.
    if not (detect_remote(job) or _has(nloc, _REMOTE_TOKENS)):
        return None
    locked_to_us_ca = (
        _has(nloc, _US_TOKENS)
        or _has(nloc, _US_LOC_ONLY)
        or _has(nloc, _US_STATE_HINTS)
        or _has(nloc, _CANADA_TOKENS)
    )
    if not locked_to_us_ca:
        return None
    # Also opens to the candidate's regions or is explicitly global → not a lock.
    if _has(nloc, _GLOBAL_REMOTE_TOKENS) or _has(nloc, _EU_TOKENS) or _has(nloc, _EMEA_TOKENS):
        return None
    return (job.get("location") or "").strip()


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
    # Computed once and carried onto every verdict, including the hard drops, so the
    # stored column stays answerable ("which freelance roles did we drop, and why?").
    is_freelance = detect_freelance(job)

    blocker = detect_us_only_blocker(job)
    if blocker:
        return JobVerdict(
            region="us",
            is_remote=detect_remote(job),
            is_freelance=is_freelance,
            sponsor_status="blocklist",
            verdict="drop",
            reasons=[f"us-only language: {blocker!r}"],
        )

    sponsor = check_sponsor_allowlist(job.get("company", ""))
    if sponsor == "blocklist":
        return JobVerdict(
            region=classify_region(job),
            is_remote=detect_remote(job),
            is_freelance=is_freelance,
            sponsor_status="blocklist",
            verdict="drop",
            reasons=[f"company in blocklist: {job.get('company')!r}"],
        )

    is_remote = detect_remote(job)
    if REMOTE_REQUIRED and not is_remote:
        if not (ALLOW_ONSITE_FREELANCE and is_freelance):
            return JobVerdict(
                region=classify_region(job),
                is_remote=False,
                is_freelance=is_freelance,
                sponsor_status=sponsor,
                verdict="drop",
                reasons=[
                    "not remote and no freelance/contract signal"
                    if ALLOW_ONSITE_FREELANCE
                    else "not marked remote"
                ],
            )
        reasons.append("on-site but freelance/contract — allowed")

    # Remote role locked to US/Canada (where the candidate can't work) -> drop.
    # Takes precedence over the sponsor allowlist: a US-locked posting won't take
    # an overseas candidate regardless of how visa-friendly the company is.
    if is_remote and CANDIDATE_WORK_REGIONS and "us" not in CANDIDATE_WORK_REGIONS:
        locked = detect_country_locked_remote(job)
        if locked:
            return JobVerdict(
                region=classify_region(job),
                is_remote=True,
                is_freelance=is_freelance,
                sponsor_status=sponsor,
                verdict="drop",
                reasons=[f"remote locked to US/Canada (candidate is overseas): {locked!r}"],
            )

    region = classify_region(job)
    if region == "other":
        return JobVerdict(
            region=region,
            is_remote=is_remote,
            is_freelance=is_freelance,
            sponsor_status=sponsor,
            verdict="drop",
            reasons=["region outside allowlist"],
        )
    if region != "unknown" and region not in REGION_ALLOWLIST:
        return JobVerdict(
            region=region,
            is_remote=is_remote,
            is_freelance=is_freelance,
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
            is_freelance=is_freelance,
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
        is_freelance=is_freelance,
        sponsor_status=sponsor,
        verdict="flag",
        reasons=reasons,
    )
