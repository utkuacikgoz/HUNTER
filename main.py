"""HUNTER - Job Hunting Automation Tool.

Main orchestrator that ties scraping, Telegram, auto-apply, and tracking together.
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, UTC
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import (
    BASE_DIR,
    DB_BACKUP_DIR,
    DB_PATH,
    HUNT_SCHEDULE_HOUR,
    HUNT_SCHEDULE_MINUTE,
    FOLLOWUP_SCHEDULE_HOUR,
    LOG_LEVEL,
    SEARCH_QUERIES,
    LOCATIONS,
    MAX_JOBS_PER_DAY,
    TELEGRAM_BOT_TOKEN,
    validate_config,
)
from tracker.database import (
    init_db,
    insert_job,
    get_pending_jobs,
    get_approved_jobs,
    get_stats,
    log_action,
)
from scraper.linkedin import LinkedInScraper
from scraper.indeed import IndeedScraper
from scraper.glassdoor import GlassdoorScraper
from scraper.wellfound import WellfoundScraper
from scraper.remoteok import RemoteOKScraper
from telegram_bot.bot import (
    send_jobs_batch,
    send_followup_reminders,
    send_stats_message,
    build_bot_app,
)
from applicant.engine import apply_to_approved_jobs

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


async def hunt():
    """Scrape jobs from all platforms and send to Telegram."""
    logger.info("🎯 Starting job hunt...")
    all_jobs = []

    scrapers = [
        # LinkedInScraper(headless=True),  # Skipped - cookie issues
        # IndeedScraper(headless=True),  # Skipped
        # GlassdoorScraper(headless=True),  # Skipped
        WellfoundScraper(headless=True),
        RemoteOKScraper(headless=True),
    ]

    per_platform = MAX_JOBS_PER_DAY // len(scrapers)

    for scraper in scrapers:
        try:
            async with scraper:
                for query in SEARCH_QUERIES[:3]:  # Top 3 queries per platform
                    for location in LOCATIONS[:2]:  # Top 2 locations
                        jobs = await scraper.scrape(
                            query=query,
                            location=location,
                            max_results=per_platform,
                        )
                        all_jobs.extend(jobs)

                        if len(all_jobs) >= MAX_JOBS_PER_DAY:
                            break
                    if len(all_jobs) >= MAX_JOBS_PER_DAY:
                        break
        except Exception as e:
            logger.error(f"Scraper {scraper.platform_name} failed: {e}")
            continue

    # Deduplicate and insert into DB (INSERT OR IGNORE handles dupes)
    new_count = 0
    for job in all_jobs:
        job_id = insert_job(
            title=job["title"],
            company=job["company"],
            location=job["location"],
            salary=job["salary"],
            url=job["url"],
            platform=job["platform"],
            description=job.get("description", ""),
        )
        if job_id:
            new_count += 1

        if new_count >= MAX_JOBS_PER_DAY:
            break

    logger.info(f"📊 Scraped {len(all_jobs)} total, {new_count} new jobs added")

    # Send new pending jobs to Telegram
    pending = get_pending_jobs(limit=MAX_JOBS_PER_DAY)
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

    logger.info(f"🚀 Applying to {len(approved)} approved jobs...")
    results = await apply_to_approved_jobs(approved, headless=True)
    logger.info(
        f"✅ Applied: {results['success']}/{results['total']} "
        f"(Failed: {results['failed']}, Needs manual: {results['needs_manual']})"
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

    scheduler = AsyncIOScheduler()

    async def scheduled_hunt():
        logger.info("Scheduled hunt triggered")
        try:
            await hunt()
        except Exception as e:
            logger.error(f"Scheduled hunt failed: {e}")

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
        await update.message.reply_text(
            f"Done!\n"
            f"Applied: {results['success']}/{results['total']}\n"
            f"Failed: {results['failed']}"
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

    logger.info("Bot is running. Scheduler active. Send SIGINT/SIGTERM to stop.")
    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        scheduler.shutdown(wait=True)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
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
