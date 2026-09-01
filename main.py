"""HUNTER — job hunting automation.

Scrapes job sources, filters to remote roles the candidate can actually take,
stores them in SQLite, and prints the result. Runs entirely locally: no
credentials, no network beyond the job sources themselves.
"""
import asyncio
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from config.settings import (
    BASE_DIR,
    DB_BACKUP_DIR,
    DB_PATH,
    ENABLE_BROWSER_SCRAPERS,
    LOCATIONS,
    LOG_LEVEL,
    MAX_JOBS_PER_DAY,
    MAX_QUERIES_PER_RUN,
    SCRAPER_RETRY_AFTER_DAYS,
    SCRAPER_SKIP_AFTER_ZEROS,
    SEARCH_QUERIES,
    SOURCE_FETCH_CAP,
    VELOCITY_BOOST_RANK,
    VELOCITY_HOT_THRESHOLD,
    VELOCITY_WINDOW_DAYS,
)
from scraper.ats import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    RecruiteeSource,
)
from scraper.filters import evaluate_job
from scraper.remoteok import RemoteOKScraper
from scraper.wellfound import WellfoundScraper
from scraper.weworkremotely import WeWorkRemotelySource
from tracker.database import (
    get_company_velocity,
    get_pending_jobs,
    get_stats,
    init_db,
    insert_job,
    record_scraper_run,
    should_skip_scraper,
)

LOG_FILE = BASE_DIR / "hunter.log"

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_log_formatter)
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[_stream_handler, _file_handler],
)
logger = logging.getLogger("hunter")


def _build_scrapers() -> list:
    """Construct the enabled scraper instances in survival-rate order.

    Order matters: both the collection ceiling in _scrape_all and the reviewable-job
    budget in _classify_and_store are filled in this order. Highest-survival first:
    remote-only boards, then ATS sources that expose a structured remote flag (Ashby
    isRemote / Lever workplaceType / Recruitee). The text-only Greenhouse catalog
    (no remote field → most roles read as not-remote) and the flaky browser scraper
    go last so they don't starve the better sources.

    Wellfound is the only browser scraper (it launches Chromium);
    ENABLE_BROWSER_SCRAPERS=false runs API-only, with no browser install needed.
    """
    scrapers: list = [
        RemoteOKScraper(),                # remote-only, API (no browser)
        WeWorkRemotelySource(),           # remote-only
        AshbySource(),                    # structured isRemote / workplaceType
        LeverSource(),                    # structured workplaceType
        RecruiteeSource(),                # structured remote flag
        GreenhouseSource(),               # text-only remote detection (no API flag)
    ]
    if ENABLE_BROWSER_SCRAPERS:
        scrapers.append(WellfoundScraper(headless=True))  # browser, fragile
    return scrapers


def _skip_recovery_hint() -> str:
    """How an auto-skipped source comes back — same wording everywhere."""
    if SCRAPER_RETRY_AFTER_DAYS > 0:
        return f"Retrying automatically in {SCRAPER_RETRY_AFTER_DAYS}d."
    return "Clear scraper_health to re-enable (SCRAPER_RETRY_AFTER_DAYS=0 → no auto-retry)."


async def _scrape_all() -> list[dict]:
    """Run every enabled scraper. Skips scrapers stuck on zero-yield streaks."""
    scrapers = _build_scrapers()
    fetch_cap = max(1, SOURCE_FETCH_CAP)
    # Bound total raw collection so a quiet filter run can't exhaust memory; the
    # classifier stops at MAX_JOBS_PER_DAY fresh regardless.
    collect_ceiling = MAX_JOBS_PER_DAY * 4
    location = LOCATIONS[0] if LOCATIONS else ""
    queries = SEARCH_QUERIES if MAX_QUERIES_PER_RUN <= 0 else SEARCH_QUERIES[:MAX_QUERIES_PER_RUN]

    all_jobs: list[dict] = []
    for scraper in scrapers:
        if len(all_jobs) >= collect_ceiling:
            logger.info(f"Collection ceiling {collect_ceiling} reached; stopping early.")
            break
        platform = scraper.platform_name
        if should_skip_scraper(platform, SCRAPER_SKIP_AFTER_ZEROS, SCRAPER_RETRY_AFTER_DAYS):
            logger.warning(
                f"Scraper {platform} skipped: last {SCRAPER_SKIP_AFTER_ZEROS} runs returned 0 jobs "
                f"(check selectors / site changes). {_skip_recovery_hint()}"
            )
            continue

        scraper_jobs: list[dict] = []
        try:
            async with scraper:
                if getattr(scraper, "accepts_query", True):
                    for query in queries:
                        # Scrapers currently ignore `location` but we still pass it.
                        jobs = await scraper.scrape(
                            query=query, location=location, max_results=fetch_cap,
                        )
                        scraper_jobs.extend(jobs)
                        if len(scraper_jobs) >= fetch_cap:
                            break
                else:
                    # Catalog source (e.g. an ATS board): fetch once, it filters
                    # its own catalog against the configured queries internally.
                    jobs = await scraper.scrape(
                        query="", location=location, max_results=fetch_cap,
                    )
                    scraper_jobs.extend(jobs)
        except Exception as e:
            logger.error(f"Scraper {platform} failed: {e}")
        finally:
            record_scraper_run(platform, len(scraper_jobs))
            all_jobs.extend(scraper_jobs)

        # This run just completed the zero-yield streak, so the next hunt will skip
        # this source. Say so once, here, at the transition — a silently dropped
        # source is otherwise invisible until the feed quietly thins out.
        if not scraper_jobs and should_skip_scraper(
            platform, SCRAPER_SKIP_AFTER_ZEROS, SCRAPER_RETRY_AFTER_DAYS
        ):
            logger.warning(
                f"🚨 Source '{platform}' returned 0 jobs {SCRAPER_SKIP_AFTER_ZEROS} runs in a row "
                f"— pausing it. {_skip_recovery_hint()}"
            )

    return all_jobs


def _classify_and_store(jobs: list[dict]) -> tuple[int, dict[str, int]]:
    """Evaluate each scraped job, persist it, and return (new_count, verdict_counts)."""
    counts = {"include": 0, "flag": 0, "drop": 0}
    new_count = 0
    reviewable = 0
    for job in jobs:
        verdict = evaluate_job(job)
        counts[verdict.verdict] += 1

        job_id = insert_job(
            title=job["title"],
            company=job["company"],
            location=job["location"],
            salary=job["salary"],
            url=job["url"],
            platform=job["platform"],
            description=job.get("description", ""),
            region=verdict.region,
            is_remote=verdict.is_remote,
            is_freelance=verdict.is_freelance,
            sponsor_status=verdict.sponsor_status,
            filter_verdict=verdict.verdict,
            filter_reasons="; ".join(verdict.reasons)[:500] if verdict.reasons else None,
        )
        if job_id:
            new_count += 1
            if verdict.verdict != "drop":
                reviewable += 1
        # Cap on REVIEWABLE (include/flag) jobs, not total inserts — drops are still
        # persisted for dedup but must not consume the day's budget, or a high-drop
        # source could exhaust it before the good jobs are reached.
        if reviewable >= MAX_JOBS_PER_DAY:
            break
    return new_count, counts


# Mirrors the verdict ordering get_pending_jobs applies in SQL. Kept here so the
# velocity boost can re-rank inside a verdict group without crossing groups.
_VERDICT_RANK = {"include": 0, "flag": 1}


def _rank_pending_by_velocity(velocity: dict[str, int]) -> list[dict]:
    """Fetch pending jobs and re-rank by hiring velocity *within* each verdict group.

    Verdict stays the primary key (include → flag → other), matching the order
    get_pending_jobs already applied; velocity only reorders jobs that share a
    verdict, and equal-velocity ties keep the DB's scraped_at DESC order (stable
    sort). Sorting on velocity alone would float a flagged job from a hot company
    above every confident include.
    """
    pending = get_pending_jobs(limit=MAX_JOBS_PER_DAY)
    if VELOCITY_BOOST_RANK and velocity:
        pending.sort(
            key=lambda j: (
                _VERDICT_RANK.get(j.get("filter_verdict") or "", 2),
                -velocity.get((j.get("company") or "").strip().lower(), 0),
            )
        )
    return pending


def print_job_list(jobs: list[dict]) -> None:
    """Print open roles grouped by company. ⚠️ marks a flag verdict — the remote
    scope wasn't explicit, so check the posting before applying."""
    by_company: dict[str, list[dict]] = {}
    for j in jobs:
        company = (j.get("company") or "").strip() or "(unknown company)"
        by_company.setdefault(company, []).append(j)
    print(f"\n📋 {len(jobs)} open role(s) across {len(by_company)} companies:\n")
    for company in sorted(by_company, key=str.lower):
        print(company)
        for j in by_company[company]:
            loc = (j.get("location") or "").strip()
            flag = "  ⚠️ check remote scope" if j.get("filter_verdict") == "flag" else ""
            print(f"  • {j.get('title', '')}" + (f" — {loc}" if loc else "") + flag)
            if j.get("url"):
                print(f"    {j['url']}")
        print()


async def hunt():
    """Scrape every source, filter, store, and print the resulting roles."""
    logger.info("🎯 Starting job hunt...")

    all_jobs = await _scrape_all()
    new_count, counts = _classify_and_store(all_jobs)

    velocity = get_company_velocity(days=VELOCITY_WINDOW_DAYS)
    hot = sorted(
        ((c, n) for c, n in velocity.items() if n >= VELOCITY_HOT_THRESHOLD),
        key=lambda kv: kv[1],
        reverse=True,
    )[:5]
    logger.info(
        f"📊 Scraped {len(all_jobs)} total, {new_count} new jobs added "
        f"(filter: {counts['include']} include / {counts['flag']} flag / {counts['drop']} drop) "
        f"· hot companies (≥{VELOCITY_HOT_THRESHOLD} roles in {VELOCITY_WINDOW_DAYS}d): "
        f"{hot or 'none'}"
    )

    pending = _rank_pending_by_velocity(velocity)
    if pending:
        print_job_list(pending)
    else:
        logger.info("No new roles found.")

    return {"scraped": len(all_jobs), "new": new_count, "listed": len(pending)}


async def list_jobs():
    """List stored open roles (include/flag verdicts), grouped by company."""
    pending = get_pending_jobs(limit=200)
    if not pending:
        print("No open roles in the queue. Run `python main.py hunt` first.")
        return
    print_job_list(pending)


async def stats():
    """Print DB stats."""
    s = get_stats()
    print("\n" + "=" * 40)
    print("🎯 HUNTER STATS")
    print("=" * 40)
    print(f"  Total scraped:    {s['total']}")
    print(f"  Open roles:       {s['pending']}")
    print(f"  Saved:            {s['approved']}")
    print(f"  Applied:          {s['applied']}")
    print(f"  Interviewing:     {s['interviewing']}")
    print(f"  Offered:          {s['offered']}")
    print(f"  Rejected/Skipped: {s['rejected']}")
    print(f"  Closed:           {s['closed']}")
    print("=" * 40 + "\n")
    return s


def backup_database():
    """Create a timestamped copy of the SQLite database."""
    if not DB_PATH.exists():
        logger.warning("No database file to back up")
        return
    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    # Key the backup name on the DB stem so co-located profiles (hunter.db vs
    # hunter_<profile>.db) don't collide on the same-second timestamp.
    dest = DB_BACKUP_DIR / f"{DB_PATH.stem}_{timestamp}.db"
    import sqlite3
    src_conn = sqlite3.connect(str(DB_PATH))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    logger.info(f"Database backed up to {dest}")

    # Prune backups older than 30 days
    cutoff = datetime.now(UTC).timestamp() - (30 * 86400)
    for f in DB_BACKUP_DIR.glob("hunter_*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            logger.info(f"Pruned old backup: {f.name}")


def main():
    """CLI entry point."""
    init_db()

    if len(sys.argv) < 2:
        print("""
HUNTER — Job Hunting Automation

Usage:
  python main.py hunt       - Scrape all sources, filter, store, and print the roles
  python main.py list       - Print stored open roles, grouped by company
  python main.py stats      - Show statistics
  python main.py backup     - Backup the database
        """)
        return

    command = sys.argv[1].lower()
    commands = {"hunt": hunt, "list": list_jobs, "stats": stats}

    if command == "backup":
        backup_database()
    elif command in commands:
        asyncio.run(commands[command]())
    else:
        print(f"Unknown command: {command}")
        print("Available: hunt, list, stats, backup")


if __name__ == "__main__":
    main()
