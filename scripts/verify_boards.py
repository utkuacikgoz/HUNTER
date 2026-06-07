#!/usr/bin/env python3
"""Verify the configured ATS board tokens are still live and carry PM roles.

ATS board tokens drift (companies churn, rename, or move ATS). Run this
periodically to catch dead seeds before they silently shrink the funnel:

    python -m scripts.verify_boards            # check all configured boards
    python -m scripts.verify_boards --quiet    # only print problems

Exits non-zero if any configured board is unreachable/empty, so it can gate a
scheduled maintenance check. Stdlib only — no project imports beyond settings.
"""
import argparse
import concurrent.futures
import json
import sys
import urllib.request

from config.settings import (
    ASHBY_BOARDS,
    GREENHOUSE_BOARDS,
    LEVER_BOARDS,
    RECRUITEE_BOARDS,
    ROLE_MATCH_KEYWORDS,
    SMARTRECRUITERS_COMPANIES,
)


def _get(url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _is_pm(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in ROLE_MATCH_KEYWORDS)


# Each checker returns (total_jobs, pm_role_count). Raises on a dead board.
def _check_greenhouse(tok):
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=false")
    jobs = d.get("jobs", [])
    return len(jobs), sum(_is_pm(j.get("title", "")) for j in jobs)


def _check_ashby(tok):
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
    jobs = d.get("jobs", [])
    return len(jobs), sum(_is_pm(j.get("title", "")) for j in jobs)


def _check_lever(tok):
    d = _get(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    return len(d), sum(_is_pm(j.get("text", "")) for j in d)


def _check_recruitee(tok):
    d = _get(f"https://{tok}.recruitee.com/api/offers/")
    offers = d.get("offers", [])
    return len(offers), sum(_is_pm(o.get("title", "")) for o in offers)


def _check_smartrecruiters(tok):
    d = _get(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100")
    items = d.get("content", [])
    return len(items), sum(_is_pm(i.get("name", "")) for i in items)


_ATS = {
    "greenhouse": (_check_greenhouse, GREENHOUSE_BOARDS),
    "ashby": (_check_ashby, ASHBY_BOARDS),
    "lever": (_check_lever, LEVER_BOARDS),
    "recruitee": (_check_recruitee, RECRUITEE_BOARDS),
    "smartrecruiters": (_check_smartrecruiters, SMARTRECRUITERS_COMPANIES),
}


def _verify_one(ats, fn, tok):
    try:
        total, pm = fn(tok)
        status = "DEAD" if total == 0 else ("ok" if pm else "no-PM")
        return (status, ats, tok, total, pm, "")
    except Exception as e:
        # Any failure (network, 404, bad JSON) means the board is unusable.
        return ("DEAD", ats, tok, 0, 0, str(e)[:60])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only print DEAD/no-PM boards")
    args = ap.parse_args()

    jobs = [(ats, fn, tok) for ats, (fn, toks) in _ATS.items() for tok in toks]
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(lambda t: _verify_one(*t), jobs))

    dead = [r for r in results if r[0] == "DEAD"]
    no_pm = [r for r in results if r[0] == "no-PM"]
    for status, ats, tok, total, pm, err in sorted(results, key=lambda r: (r[1], r[2])):
        if args.quiet and status == "ok":
            continue
        line = f"[{status:5}] {ats:15} {tok:24} jobs={total:<4} pm={pm}"
        print(line + (f"  ({err})" if err else ""))

    print(
        f"\n{len(results)} boards: {len(results) - len(dead) - len(no_pm)} ok, "
        f"{len(no_pm)} no-PM, {len(dead)} DEAD"
    )
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
