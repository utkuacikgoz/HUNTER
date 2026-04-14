"""Telegram bot for job review, approval, and notifications."""
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from applicant.engine import apply_to_single_job
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from tracker.database import (
    get_pending_jobs,
    get_approved_jobs,
    approve_job,
    reject_job,
    get_stats,
    get_jobs_needing_followup,
    record_followup,
    get_all_applied_jobs,
    get_job_by_id,
    update_job_status,
)

logger = logging.getLogger(__name__)


def truncate(text: str, max_len: int = 100) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def format_job_message(job: dict, index: int = 0) -> str:
    salary_line = f"💰 {_escape_md(job['salary'])}\n" if job.get("salary") else ""
    escaped_url = _escape_md(job['url'])
    sep = '─' * 30
    idx = _escape_md(str(index))
    return (
        f"{sep}\n"
        f"🔹 *{idx}\\. {_escape_md(job['title'])}*\n"
        f"🏢 {_escape_md(job['company'])}\n"
        f"📍 {_escape_md(job['location'] or 'Not specified')}\n"
        f"{salary_line}"
        f"🌐 {_escape_md(job['platform'].capitalize())}\n"
        f"🔗 [Apply Link]({escaped_url})\n"
    )


def _escape_md(text: str) -> str:
    """Escape Markdown V2 special characters."""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special:
        text = text.replace(char, f'\\{char}')
    return text


async def send_jobs_batch(jobs: list[dict]) -> None:
    """Send a batch of jobs to the Telegram channel for review."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    header = (
        f"🎯 *HUNTER \\- New Jobs Found*\n"
        f"📊 {len(jobs)} jobs ready for review\n\n"
        f"Use buttons to Approve ✅ or Skip ❌ each job\\.\n"
        f"Then send /apply to auto\\-apply to all approved jobs\\."
    )

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=header,
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Failed to send header: {e}")

    for i, job in enumerate(jobs, 1):
        try:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{job['id']}"),
                    InlineKeyboardButton("❌ Skip", callback_data=f"reject_{job['id']}"),
                ],
                [
                    InlineKeyboardButton("🔗 View Job", url=job["url"]),
                ],
            ])

            text = format_job_message(job, i)
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.5)  # Rate limit
        except Exception as e:
            logger.error(f"Failed to send job {job.get('id')}: {e}")


async def send_followup_reminders() -> None:
    """Send follow-up reminders for jobs applied 7+ days ago."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    jobs = get_jobs_needing_followup()

    if not jobs:
        return

    header = f"📬 *FOLLOW\\-UP REMINDERS*\n{len(jobs)} jobs need follow\\-up emails\\!"

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=header,
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Failed to send followup header: {e}")

    for job in jobs:
        try:
            followup_count = job.get("followup_count", 0)
            text = (
                f"📧 *Follow\\-up \\#{followup_count + 1}*\n"
                f"🔹 {_escape_md(job['title'])} at {_escape_md(job['company'])}\n"
                f"📅 Applied: {_escape_md(job.get('applied_at', 'N/A')[:10])}\n"
                f"🔗 [Job Link]({job['url']})"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Followed Up", callback_data=f"followedup_{job['id']}"),
                    InlineKeyboardButton("🔄 Interviewing", callback_data=f"interviewing_{job['id']}"),
                    InlineKeyboardButton("❌ Close", callback_data=f"close_{job['id']}"),
                ],
            ])

            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Failed to send followup for job {job.get('id')}: {e}")


async def send_stats_message() -> None:
    """Send current stats to Telegram."""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    stats = get_stats()

    text = (
        f"📊 *HUNTER STATS*\n\n"
        f"🔹 Total jobs scraped: {stats['total']}\n"
        f"⏳ Pending review: {stats['pending']}\n"
        f"✅ Approved: {stats['approved']}\n"
        f"📨 Applied: {stats['applied']}\n"
        f"🎤 Interviewing: {stats['interviewing']}\n"
        f"🎉 Offered: {stats['offered']}\n"
        f"❌ Rejected/Skipped: {stats['rejected']}\n"
        f"🚪 Closed: {stats['closed']}\n\n"
        f"📅 Applied this week: {stats['applied_this_week']}\n"
        f"📅 Applied this month: {stats['applied_this_month']}"
    )

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
    )


# === Bot command handlers (when running as persistent bot) ===

def _is_authorized(update: Update) -> bool:
    """Check if the message is from the authorized chat."""
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "🎯 *HUNTER Bot Active*\n\n"
        "Commands:\n"
        "/hunt - Scrape new jobs\n"
        "/review - Show pending jobs\n"
        "/apply - Apply to approved jobs\n"
        "/stats - Show statistics\n"
        "/followups - Check follow-up reminders\n"
        "/applied - List all applied jobs",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    stats = get_stats()
    text = (
        f"📊 *HUNTER STATS*\n\n"
        f"Total: {stats['total']} | Pending: {stats['pending']}\n"
        f"Approved: {stats['approved']} | Applied: {stats['applied']}\n"
        f"Interviewing: {stats['interviewing']} | Offered: {stats['offered']}\n"
        f"Rejected: {stats['rejected']} | Closed: {stats['closed']}\n\n"
        f"📅 This week: {stats['applied_this_week']} | This month: {stats['applied_this_month']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_applied(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    jobs = get_all_applied_jobs()
    if not jobs:
        await update.message.reply_text("No applications yet. Start hunting! 🎯")
        return

    text = f"📨 *Applied Jobs ({len(jobs)})*\n\n"
    for i, job in enumerate(jobs[:30], 1):
        status_emoji = {"applied": "📨", "interviewing": "🎤", "offered": "🎉"}.get(job["status"], "📨")
        text += (
            f"{i}. {status_emoji} {job['title']} @ {job['company']}\n"
            f"   📅 {job.get('applied_at', 'N/A')[:10]} | "
            f"Follow-ups: {job.get('followup_count', 0)}\n\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_followups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    jobs = get_jobs_needing_followup()
    if not jobs:
        await update.message.reply_text("✅ No follow-ups needed right now!")
        return
    await send_followup_reminders()


_active_apply_tasks: set = set()


async def _auto_apply(query, job_id: int, job: dict):
    """Background task: apply to a single job after approval."""
    task = asyncio.current_task()
    _active_apply_tasks.add(task)
    try:
        success = await apply_to_single_job(job, headless=True)
        if success:
            text = f"\u2705 *APPLIED*\n{job['title']} @ {job['company']}"
        else:
            text = f"\u26a0\ufe0f *APPLY INCOMPLETE*\n{job['title']} @ {job['company']}\nUse link to apply manually"
    except Exception as e:
        logger.error(f"Auto-apply failed for job {job_id}: {e}")
        text = f"\u274c *APPLY FAILED*\n{job['title']} @ {job['company']}\n{str(e)[:100]}"
    finally:
        _active_apply_tasks.discard(task)
    try:
        await query.edit_message_text(text=text, parse_mode="Markdown")
    except Exception:
        logger.debug(f"Could not update message for job {job_id}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query

    # Auth check FIRST: only allow the configured chat to interact
    if str(query.message.chat_id) != str(TELEGRAM_CHAT_ID):
        logger.warning(f"Unauthorized callback from chat_id={query.message.chat_id}")
        await query.answer(text="Unauthorized", show_alert=True)
        return

    await query.answer()

    data = query.data
    parts = data.split("_", 1)
    if len(parts) != 2:
        return

    action, job_id_str = parts
    try:
        job_id = int(job_id_str)
        if job_id <= 0 or job_id > 2**31 - 1:
            return
    except (ValueError, OverflowError):
        return

    job = get_job_by_id(job_id)
    if not job:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "approve":
        approve_job(job_id)
        title_esc = _escape_md(job['title'])
        company_esc = _escape_md(job['company'])
        dots = r"\.\.\."
        await query.edit_message_text(
            text=f"\u2705 *APPROVED* \u2014 applying{dots}\n{title_esc} @ {company_esc}",
            parse_mode="MarkdownV2",
        )
        # Auto-apply in background
        asyncio.create_task(_auto_apply(query, job_id, job))
    elif action == "reject":
        reject_job(job_id)
        await query.edit_message_text(
            text=f"❌ *SKIPPED*\n{job['title']} @ {job['company']}",
            parse_mode="Markdown",
        )
    elif action == "followedup":
        record_followup(job_id)
        await query.edit_message_text(
            text=f"📧 *FOLLOWED UP*\n{job['title']} @ {job['company']}",
            parse_mode="Markdown",
        )
    elif action == "interviewing":
        update_job_status(job_id, "interviewing")
        await query.edit_message_text(
            text=f"🎤 *INTERVIEWING*\n{job['title']} @ {job['company']}",
            parse_mode="Markdown",
        )
    elif action == "close":
        update_job_status(job_id, "closed")
        await query.edit_message_text(
            text=f"🚪 *CLOSED*\n{job['title']} @ {job['company']}",
            parse_mode="Markdown",
        )


def build_bot_app() -> Application:
    """Build and return the Telegram bot Application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("applied", cmd_applied))
    app.add_handler(CommandHandler("followups", cmd_followups))
    app.add_handler(CallbackQueryHandler(callback_handler))
    return app
