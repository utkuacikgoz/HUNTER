"""HUNTER - Job Hunting Automation Tool.

Main orchestrator that ties scraping, Telegram, auto-apply, and tracking together.
"""
import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler

from applicant.engine import apply_to_approved_jobs
from config.log_redaction import install_redaction
from config.settings import (
    BASE_DIR,
    CLASSIFY_CONCURRENCY,
    DB_BACKUP_DIR,
    DB_PATH,
    ENABLE_LLM_SPONSOR_SCORING,
    FOLLOWUP_SCHEDULE_HOUR,
    HUNT_SCHEDULE_HOUR,
    HUNT_SCHEDULE_MINUTE,
    LOCATIONS,
    LOG_LEVEL,
    MAX_APPLIES_PER_RUN,
    MAX_JOBS_PER_DAY,
    MAX_QUERIES_PER_RUN,
    SCRAPER_SKIP_AFTER_ZEROS,
    SEARCH_QUERIES,
    SOURCE_FETCH_CAP,
    TELEGRAM_BOT_TOKEN,
    VELOCITY_BOOST_RANK,
    VELOCITY_HOT_THRESHOLD,
    VELOCITY_WINDOW_DAYS,
    validate_config,
)
from scraper.ats import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    RecruiteeSource,
)
from scraper.filters import evaluate_job_async
from scraper.remoteok import RemoteOKScraper
from scraper.wellfound import WellfoundScraper
from scraper.weworkremotely import WeWorkRemotelySource
from telegram_bot.bot import (
    _active_apply_tasks,
    build_bot_app,
    send_followup_reminders,
    send_jobs_batch,
    send_stats_message,
    send_telegram_alert,
    shutdown_apply_worker,
)
from tracker.database import (
    get_approved_jobs,
    get_company_velocity,
    get_last_scrape_time,
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
# Defense-in-depth: scrub secret-shaped strings (Telegram token, API keys, li_at)
# from EVERY log record on our handlers, and quiet chatty libraries (httpx/telegram
# log request URLs that embed the bot token). See config/log_redaction.py.
install_redaction(logging.getLogger(), quiet_chatty=True)
logger = logging.getLogger("hunter")


async def _scrape_all() -> list[dict]:
    """Run every enabled scraper. Skips scrapers stuck on zero-yield streaks."""
    # Order is by expected survival rate, because both the collection ceiling here
    # and the reviewable-job budget in _classify_and_store are filled in this order.
    # Highest-survival first: remote-only boards, then ATS sources that expose a
    # structured remote flag (Ashby isRemote / Lever workplaceType / Recruitee /
    # SmartRecruiters). The text-only Greenhouse catalog (no remote field → most
    # roles read as not-remote) and the flaky browser scraper go last so they don't
    # starve the better sources. Each source returns up to SOURCE_FETCH_CAP.
    scrapers = [
        # LinkedInScraper(headless=True),  # Disabled - session-cookie issues; kept for Phase 3 hybrid apply
        RemoteOKScraper(headless=True),   # remote-only
        WeWorkRemotelySource(),           # remote-only
        AshbySource(),                    # structured isRemote / workplaceType
        LeverSource(),                    # structured workplaceType
        RecruiteeSource(),                # structured remote flag
        GreenhouseSource(),               # text-only remote detection (no API flag)
        WellfoundScraper(headless=True),  # browser, fragile
    ]
    fetch_cap = max(1, SOURCE_FETCH_CAP)
    # Bound total raw collection so a quiet filter run can't OOM the box; the
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
        if should_skip_scraper(platform, SCRAPER_SKIP_AFTER_ZEROS):
            logger.warning(
                f"Scraper {platform} skipped: last {SCRAPER_SKIP_AFTER_ZEROS} runs returned 0 jobs "
                "(check selectors / site changes; clear scraper_health to re-enable)."
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

    return all_jobs


async def _classify_and_store(jobs: list[dict]) -> tuple[int, dict[str, int]]:
    """Evaluate each scraped job, persist it, and return (new_count, verdict_counts)."""
    llm_score = None
    if ENABLE_LLM_SPONSOR_SCORING:
        from prompts.generator import score_sponsor_signal
        llm_score = score_sponsor_signal

    counts = {"include": 0, "flag": 0, "drop": 0}
    new_count = 0
    reviewable = 0
    # Classify in bounded-concurrency chunks: the sponsor-scoring LLM call is a network
    # round-trip, so scoring a chunk in parallel cuts the classify phase several-fold.
    # Inserts and the reviewable cap stay sequential to preserve source-survival ordering;
    # we over-score at most one chunk past the budget before breaking.
    for start in range(0, len(jobs), CLASSIFY_CONCURRENCY):
        chunk = jobs[start:start + CLASSIFY_CONCURRENCY]
        verdicts = await asyncio.gather(
            *(evaluate_job_async(job, llm_score=llm_score) for job in chunk)
        )
        for job, verdict in zip(chunk, verdicts, strict=True):
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
                sponsor_status=verdict.sponsor_status,
                filter_verdict=verdict.verdict,
                filter_reasons="; ".join(verdict.reasons)[:500] if verdict.reasons else None,
            )
            if job_id:
                new_count += 1
                if verdict.verdict != "drop":
                    reviewable += 1
        # Cap on REVIEWABLE (include/flag) jobs, not total inserts — drops are still
        # persisted for dedup but must not consume the day's review budget, or a
        # high-drop source could exhaust it before the good jobs are reached.
        if reviewable >= MAX_JOBS_PER_DAY:
            break
    return new_count, counts


def _rank_pending_by_velocity(velocity: dict[str, int]) -> list[dict]:
    """Fetch pending jobs and stable-sort by velocity desc when enabled."""
    pending = get_pending_jobs(limit=MAX_JOBS_PER_DAY)
    if VELOCITY_BOOST_RANK and velocity:
        # Primary verdict order (include→flag) was set by get_pending_jobs;
        # this stable sort only reshuffles within each group.
        pending.sort(
            key=lambda j: velocity.get((j.get("company") or "").strip().lower(), 0),
            reverse=True,
        )
    return pending


async def hunt():
    """Scrape jobs from all platforms, filter, and send to Telegram."""
    logger.info("🎯 Starting job hunt...")

    all_jobs = await _scrape_all()
    new_count, counts = await _classify_and_store(all_jobs)

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
        logger.info(f"📱 Sending {len(pending)} jobs to Telegram...")
        await send_jobs_batch(pending)
    else:
        logger.info("No new jobs to send")

    return {"scraped": len(all_jobs), "new": new_count, "sent_to_telegram": len(pending)}


async def apply():
    """Apply to all approved jobs."""
    approved = get_approved_jobs()
    if not approved:
        logger.info("No approved jobs to apply to")
        return {"total": 0, "success": 0, "failed": 0, "needs_manual": 0}

    logger.info(f"🚀 Applying to {len(approved)} approved jobs (cap {MAX_APPLIES_PER_RUN}/run)...")
    results = await apply_to_approved_jobs(approved, headless=True)
    skipped = results.get("skipped_over_cap", 0)
    logger.info(
        f"✅ Applied: {results['success']}/{results['total']} "
        f"(Failed: {results['failed']}, Needs manual: {results['needs_manual']}"
        + (f", {skipped} left for next run — over the per-run cap" if skipped else "")
        + ")"
    )

    # Send stats after applying
    await send_stats_message()
    return results


async def followup():
    """Send follow-up reminders."""
    logger.info("📬 Checking for follow-ups...")
    await send_followup_reminders()


async def stats():
    """Print and send stats."""
    s = get_stats()
    print("\n" + "=" * 40)
    print("🎯 HUNTER STATS")
    print("=" * 40)
    print(f"  Total scraped:    {s['total']}")
    print(f"  Pending review:   {s['pending']}")
    print(f"  Approved:         {s['approved']}")
    print(f"  Applied:          {s['applied']}")
    print(f"  Interviewing:     {s['interviewing']}")
    print(f"  Offered:          {s['offered']}")
    print(f"  Rejected/Skipped: {s['rejected']}")
    print(f"  Closed:           {s['closed']}")
    print(f"  Applied (week):   {s['applied_this_week']}")
    print(f"  Applied (month):  {s['applied_this_month']}")
    print("=" * 40 + "\n")
    return s


async def bot():
    """Run the Telegram bot with scheduled jobs and graceful shutdown."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set. Configure .env first.")
        return

    logger.info("Starting Telegram bot with scheduler...")
    app = build_bot_app()

    # --- APScheduler: daily hunt + followup ---
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    # coalesce: collapse multiple missed runs into one; misfire_grace_time: still
    # run a job that fired late (e.g. machine was briefly busy/restarting) within
    # the hour rather than skipping it silently.
    scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "misfire_grace_time": 3600}
    )

    async def scheduled_hunt():
        logger.info("Scheduled hunt triggered")
        try:
            results = await hunt()
            sent = results.get("sent_to_telegram", 0)
            if sent == 0:
                await send_telegram_alert(
                    "⚠️ Daily hunt found 0 new roles to review. Check scraper health "
                    "(/report) — sources may be down or everything was already seen."
                )
        except Exception as e:
            logger.error(f"Scheduled hunt failed: {e}")
            await send_telegram_alert(f"🚨 Daily hunt FAILED: {str(e)[:200]}")

    async def scheduled_followup():
        logger.info("Scheduled follow-up check triggered")
        try:
            await followup()
        except Exception as e:
            logger.error(f"Scheduled follow-up failed: {e}")

    async def scheduled_backup():
        logger.info("Scheduled DB backup triggered")
        try:
            backup_database()
        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}")

    async def scheduled_screenshot_prune():
        logger.info("Scheduled screenshot prune triggered")
        try:
            prune_screenshots(max_age_days=30)
        except Exception as e:
            logger.error(f"Scheduled screenshot prune failed: {e}")

    scheduler.add_job(
        scheduled_hunt,
        CronTrigger(hour=HUNT_SCHEDULE_HOUR, minute=HUNT_SCHEDULE_MINUTE),
        id="daily_hunt",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_followup,
        CronTrigger(hour=FOLLOWUP_SCHEDULE_HOUR, minute=0),
        id="daily_followup",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_backup,
        CronTrigger(hour=3, minute=0),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_screenshot_prune,
        CronTrigger(hour=3, minute=30),
        id="daily_screenshot_prune",
        replace_existing=True,
    )

    # --- Telegram command handlers ---
    from telegram.ext import CommandHandler

    from telegram_bot.bot import _is_authorized

    async def cmd_hunt(update, context):
        if not _is_authorized(update):
            return
        await update.message.reply_text("Starting job hunt... This may take a few minutes.")
        results = await hunt()
        await update.message.reply_text(
            f"Hunt complete!\n"
            f"Scraped: {results['scraped']}\n"
            f"New: {results['new']}\n"
            f"Sent to review: {results['sent_to_telegram']}"
        )

    async def cmd_apply(update, context):
        if not _is_authorized(update):
            return
        approved = get_approved_jobs()
        if not approved:
            await update.message.reply_text("No approved jobs. Review pending jobs first!")
            return
        await update.message.reply_text(f"Applying to {len(approved)} jobs... This will take a while.")
        results = await apply()
        skipped = results.get("skipped_over_cap", 0)
        await update.message.reply_text(
            f"Done!\n"
            f"✅ Submitted: {results['success']}/{results['total']}\n"
            f"📝 Manual: {results['needs_manual']} | ❌ Failed: {results['failed']}"
            + (f"\n⏭️ {skipped} left for next /apply (per-run cap {MAX_APPLIES_PER_RUN})" if skipped else "")
        )

    async def cmd_review(update, context):
        if not _is_authorized(update):
            return
        pending = get_pending_jobs(limit=50)
        if not pending:
            await update.message.reply_text("No pending jobs. Run /hunt first!")
            return
        await send_jobs_batch(pending)

    async def cmd_schedule(update, context):
        if not _is_authorized(update):
            return
        jobs_info = []
        for job in scheduler.get_jobs():
            next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M") if job.next_run_time else "paused"
            jobs_info.append(f"  {job.id}: next at {next_run}")
        msg = "Scheduled jobs:\n" + "\n".join(jobs_info) if jobs_info else "No scheduled jobs"
        await update.message.reply_text(msg)

    app.add_handler(CommandHandler("hunt", cmd_hunt))
    app.add_handler(CommandHandler("apply", cmd_apply))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, _frame):
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    scheduler.start()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Catch-up: if the worker was down across the scheduled hunt, the cron simply
    # doesn't fire. Detect a stale last-scrape (>20h) and run one shortly after boot.
    async def _catch_up_if_missed():
        last = get_last_scrape_time()
        stale = True
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                stale = (datetime.now(UTC) - last_dt) > timedelta(hours=20)
            except ValueError:
                stale = True
        if stale:
            logger.info(f"Last scrape was {last or 'never'} (>20h) — running catch-up hunt.")
            await send_telegram_alert("⏰ Missed the daily hunt while offline — running a catch-up now.")
            await scheduled_hunt()

    asyncio.create_task(_catch_up_if_missed())

    logger.info("Bot is running. Scheduler active. Send SIGINT/SIGTERM to stop.")
    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        # 1. Stop accepting new Telegram updates so no new apply tasks spawn.
        try:
            await app.updater.stop()
        except Exception as e:
            logger.warning(f"updater.stop failed: {e}")

        # 2. Drain the apply queue + in-flight apply, then stop the worker.
        await shutdown_apply_worker(timeout=60.0)
        if _active_apply_tasks:
            pending = list(_active_apply_tasks)
            logger.info(f"Awaiting {len(pending)} in-flight apply task(s)...")
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.warning("Apply tasks did not finish; cancelling.")
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        # 3. Stop the scheduler (waits for any running scheduled jobs).
        scheduler.shutdown(wait=True)

        # 4. Tear down the Telegram application.
        try:
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning(f"app shutdown failed: {e}")
        logger.info("Shutdown complete.")


def backup_database():
    """Create a timestamped copy of the SQLite database."""
    if not DB_PATH.exists():
        logger.warning("No database file to back up")
        return
    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = DB_BACKUP_DIR / f"hunter_{timestamp}.db"
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


def prune_screenshots(max_age_days: int = 30):
    """Delete apply screenshots older than `max_age_days` to bound disk growth."""
    from applicant.engine import SCREENSHOTS_DIR
    if not SCREENSHOTS_DIR.exists():
        return
    cutoff = datetime.now(UTC).timestamp() - (max_age_days * 86400)
    removed = 0
    for f in SCREENSHOTS_DIR.glob("*.png"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        logger.info(f"Pruned {removed} screenshot(s) older than {max_age_days}d")


def main():
    """CLI entry point."""
    init_db()

    if len(sys.argv) < 2:
        print("""
HUNTER - Job Hunting Automation

Usage:
  python main.py hunt       - Scrape jobs and send to Telegram
  python main.py apply      - Apply to all approved jobs
  python main.py followup   - Send follow-up reminders
  python main.py stats      - Show statistics
  python main.py bot        - Run interactive Telegram bot (with scheduler)
  python main.py backup     - Backup the database
        """)
        return

    command = sys.argv[1].lower()

    commands = {
        "hunt": hunt,
        "apply": apply,
        "followup": followup,
        "stats": stats,
        "bot": bot,
        "backup": None,  # handled separately below
    }

    if command not in commands:
        print(f"Unknown command: {command}")
        print("Available: hunt, apply, followup, stats, bot, backup")
        return

    # Validate config before running
    errors = validate_config(command)
    if errors:
        for err in errors:
            logger.error(f"Config error: {err}")
        sys.exit(1)

    if command == "backup":
        backup_database()
    else:
        asyncio.run(commands[command]())


if __name__ == "__main__":
    main()
