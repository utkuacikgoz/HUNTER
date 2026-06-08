"""Telegram bot for job review, approval, and notifications."""
import asyncio
import logging
import os

import pytz
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
)

from applicant.engine import apply_to_single_job
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, VELOCITY_HOT_THRESHOLD
from tracker.database import (
    approve_job,
    get_all_applied_jobs,
    get_filter_precision,
    get_funnel,
    get_job_by_id,
    get_jobs_needing_followup,
    get_last_scrape_time,
    get_stats,
    log_action,
    record_followup,
    reject_job,
    update_job_status,
)

logger = logging.getLogger(__name__)


def truncate(text: str, max_len: int = 100) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


_VERDICT_BADGE = {
    "include": "✅ Sponsor-friendly",
    "flag": "🟡 Unclear sponsor — review",
    "drop": "⛔ Disqualified",
}
_REGION_LABEL = {"us": "US", "eu": "EU", "emea": "EMEA", "other": "Other", "unknown": "?"}


def format_job_message(
    job: dict,
    index: int = 0,
    velocity: dict[str, int] | None = None,
) -> str:
    salary_line = f"💰 {_escape_md(job['salary'])}\n" if job.get("salary") else ""
    escaped_url = _escape_md(job['url'])
    sep = '─' * 30
    idx = _escape_md(str(index))

    verdict = job.get("filter_verdict")
    region = _REGION_LABEL.get(job.get("region") or "", "?")
    remote_tag = "Remote" if job.get("is_remote") else "On-site"
    badge = _VERDICT_BADGE.get(verdict) if verdict else None
    flag_line = f"{_escape_md(badge)}\n" if badge else ""
    region_line = f"🌍 {_escape_md(f'{region} · {remote_tag}')}\n" if verdict else ""

    velocity_line = ""
    if velocity:
        company_key = (job.get("company") or "").strip().lower()
        n = velocity.get(company_key, 0)
        if n >= VELOCITY_HOT_THRESHOLD:
            velocity_line = f"{_escape_md(f'🔥 {n} open roles · hiring fast')}\n"

    return (
        f"{sep}\n"
        f"{flag_line}"
        f"{velocity_line}"
        f"🔹 *{idx}\\. {_escape_md(job['title'])}*\n"
        f"🏢 {_escape_md(job['company'])}\n"
        f"📍 {_escape_md(job['location'] or 'Not specified')}\n"
        f"{region_line}"
        f"{salary_line}"
        f"🌐 {_escape_md(job['platform'].capitalize())}\n"
        f"🔗 [Apply Link]({escaped_url})\n"
    )


def _record_filter_outcome(job: dict, decision: str) -> None:
    """Persist the user's decision against the filter's verdict for precision tracking."""
    verdict = job.get("filter_verdict") or "unknown"
    sponsor = job.get("sponsor_status") or "unknown"
    region = job.get("region") or "unknown"
    try:
        log_action(
            job["id"],
            "filter_outcome",
            f"verdict={verdict};sponsor={sponsor};region={region};decision={decision}",
        )
    except Exception as e:
        logger.debug(f"filter_outcome log failed for job {job.get('id')}: {e}")


def _escape_md(text: str) -> str:
    """Escape Markdown V2 special characters."""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special:
        text = text.replace(char, f'\\{char}')
    return text


async def send_jobs_batch(
    jobs: list[dict],
    velocity: dict[str, int] | None = None,
) -> None:
    """Send a batch of jobs to the Telegram channel for review.

    `velocity` is an optional {company_lowercase: open_role_count} dict used to
    surface 🔥 badges on companies that are hiring aggressively.
    """
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

            text = format_job_message(job, i, velocity=velocity)
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


async def send_telegram_alert(text: str) -> None:
    """Send a plain operational alert / heartbeat to the configured chat.

    Used by the scheduler (missed-run / zero-yield) and apply worker. Never
    raises — alerting must not crash the caller.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured; alert dropped: %s", text)
        return
    try:
        await Bot(token=TELEGRAM_BOT_TOKEN).send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        logger.warning(f"Failed to send alert: {e}")


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


def _format_filter_precision(precision: dict[str, dict[str, int]]) -> str:
    if not precision:
        return "_no filter outcomes logged yet_"
    order = ["include", "flag", "drop", "unknown"]
    lines = []
    for verdict in order:
        bucket = precision.get(verdict)
        if not bucket:
            continue
        approves = bucket.get("approve", 0)
        rejects = bucket.get("reject", 0)
        total = approves + rejects
        rate = f"{approves * 100 // total}%" if total else "—"
        lines.append(f"  {verdict}: {approves}/{total} approved ({rate})")
    return "\n".join(lines) or "_no filter outcomes logged yet_"


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    stats = get_stats()
    precision = get_filter_precision()
    text = (
        f"📊 *HUNTER STATS*\n\n"
        f"Total: {stats['total']} | Pending: {stats['pending']}\n"
        f"Approved: {stats['approved']} | Applied: {stats['applied']}\n"
        f"Interviewing: {stats['interviewing']} | Offered: {stats['offered']}\n"
        f"Rejected: {stats['rejected']} | Closed: {stats['closed']}\n\n"
        f"📅 This week: {stats['applied_this_week']} | This month: {stats['applied_this_month']}\n\n"
        f"*Filter precision (approve-rate by verdict):*\n"
        f"{_format_filter_precision(precision)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def _format_report() -> str:
    """Build the end-to-end funnel report (scrape -> filter -> review -> apply)."""
    f = get_funnel()
    v = f["verdicts"]
    s = f["statuses"]
    last = get_last_scrape_time() or "never"
    scraped = sum(v.values())
    return (
        f"📈 *HUNTER FUNNEL*\n\n"
        f"Last scrape: {last}\n\n"
        f"*Sourced* ({scraped} total):\n"
        f"  ✅ include: {v.get('include', 0)}\n"
        f"  🟡 flag: {v.get('flag', 0)}\n"
        f"  ❌ drop: {v.get('drop', 0)}\n\n"
        f"*Pipeline:*\n"
        f"  ⏳ pending: {s.get('pending', 0)}\n"
        f"  👍 approved: {s.get('approved', 0)}\n"
        f"  📨 applied: {s.get('applied', 0)}\n"
        f"  🎤 interviewing: {s.get('interviewing', 0)}\n"
        f"  🎉 offered: {s.get('offered', 0)}\n\n"
        f"*Apply outcomes (logged):*\n"
        f"  confirmed: {f['apply_confirmed']} | failed: {f['apply_failed']}\n\n"
        f"*Filter approve-rate by verdict:*\n"
        f"{_format_filter_precision(get_filter_precision())}"
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(_format_report(), parse_mode="Markdown")


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

# Single-worker apply queue: every "Apply Now" tap is enqueued and processed
# one at a time, so we never run more than one browser concurrently.
_apply_queue: "asyncio.Queue" = asyncio.Queue()
_apply_worker_task: "asyncio.Task | None" = None


def _ensure_apply_worker() -> None:
    """Start the apply worker lazily (first enqueue), or restart if it died."""
    global _apply_worker_task
    if _apply_worker_task is None or _apply_worker_task.done():
        _apply_worker_task = asyncio.create_task(_apply_worker())


async def _apply_worker() -> None:
    while True:
        query, job_id, job = await _apply_queue.get()
        try:
            await _auto_apply(query, job_id, job)
        except Exception as e:
            logger.error(f"apply worker error for job {job_id}: {e}")
        finally:
            _apply_queue.task_done()


async def shutdown_apply_worker(timeout: float = 60.0) -> None:
    """Drain queued applies, then stop the worker. Called from main.bot() shutdown."""
    if _apply_queue.qsize() or _active_apply_tasks:
        logger.info(f"Draining apply queue ({_apply_queue.qsize()} queued)...")
        try:
            await asyncio.wait_for(_apply_queue.join(), timeout=timeout)
        except TimeoutError:
            logger.warning("Apply queue did not drain within timeout.")
    global _apply_worker_task
    if _apply_worker_task and not _apply_worker_task.done():
        _apply_worker_task.cancel()
        try:
            await _apply_worker_task
        except asyncio.CancelledError:
            pass


def _format_apply_result(result, job: dict) -> str:
    """Build the Telegram status message for a finished apply attempt.

    Honest about reality: only ``success`` means the bot actually submitted and
    saw a confirmation. Everything else is a MANUAL apply \u2014 the bot fills the
    form in its own server-side browser, which the user's browser can't see, so
    we never tell them a link is "pre-filled". The tailored cover letter is sent
    separately for copy/paste (see _auto_apply).
    """
    header = f"{job.get('title', '')} @ {job.get('company', '')}"
    url = job.get("url", "")

    if result.method == "already_applied":
        return f"\u21a9\ufe0f *ALREADY APPLIED*\n{header}\n_{result.message}_"

    if result.success:
        return f"\u2705 *APPLIED & CONFIRMED*\n{header}\n_{result.message}_"

    # Non-success: the bot could not submit for you (this ATS needs a manual
    # submit, the submit wasn't confirmed, or APPLY_DRY_RUN is on). The link opens
    # a BLANK form \u2014 the server-side fill doesn't transfer to your browser.
    why = {
        "form_filled": "I prepared this one but can't submit it for you here \u2014 apply manually (the link is a blank form).",
        "screenshot_only": "No apply form I can drive \u2014 apply manually.",
        "external_redirect": "Application is on an external site \u2014 apply manually.",
    }.get(result.method)
    if why is None:
        return f"\u274c *APPLY FAILED*\n{header}\n_{result.message}_"
    return f"\U0001f4dd *APPLY MANUALLY*\n{header}\n_{why}_\n{url}"


async def _auto_apply(query, job_id: int, job: dict):
    """Apply to a single job (runs inside the serial apply worker)."""
    task = asyncio.current_task()
    _active_apply_tasks.add(task)
    result = None
    try:
        result = await apply_to_single_job(job, headless=True)
        text = _format_apply_result(result, job)
    except Exception as e:
        logger.error(f"Auto-apply failed for job {job_id}: {e}")
        text = f"\u274c *APPLY FAILED*\n{job['title']} @ {job['company']}\n{str(e)[:100]}"
    finally:
        _active_apply_tasks.discard(task)

    try:
        await query.edit_message_text(text=text, parse_mode="Markdown")
    except Exception:
        logger.debug(f"Could not update message for job {job_id}")

    # For a manual apply, hand over the tailored cover letter as a separate
    # PLAIN-TEXT message (no Markdown, so it can't break formatting) for copy/paste.
    if result and not result.success and result.method != "already_applied":
        try:
            fresh = get_job_by_id(job_id)
            cover_letter = (fresh or {}).get("cover_letter") or ""
            if cover_letter:
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=f"📄 Cover letter for {job['title']} @ {job['company']} (copy/paste):\n\n{cover_letter}",
                )
        except Exception as e:
            logger.debug(f"Could not send cover letter for job {job_id}: {e}")

    # Send screenshot as proof
    if result and result.screenshot_path and os.path.exists(result.screenshot_path):
        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            with open(result.screenshot_path, "rb") as f:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=InputFile(f),
                    caption=f"\U0001f4f8 {job['title']} @ {job['company']}\nMethod: {result.method}",
                )
        except Exception as e:
            logger.debug(f"Could not send screenshot for job {job_id}: {e}")


async def _handle_approve(query, job: dict) -> None:
    """Mark approved and surface the URL. Does NOT auto-apply — the user reviews
    the destination URL first, then explicitly taps Apply Now (or sends /apply)."""
    job_id = job["id"]
    approve_job(job_id)
    _record_filter_outcome(job, "approve")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Apply Now", callback_data=f"applynow_{job_id}")],
        [InlineKeyboardButton("🔗 View Job", url=job["url"])],
    ])
    # Plain text (no parse_mode) so the raw URL is fully visible and unescaped.
    await query.edit_message_text(
        text=(
            f"✅ APPROVED\n{job['title']} @ {job['company']}\n"
            f"🔗 {job['url']}\n\n"
            f"Review the destination URL above, then tap 🚀 Apply Now "
            f"(or send /apply to apply to all approved jobs)."
        ),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _handle_applynow(query, job: dict) -> None:
    """Enqueue an approved job for the serial apply worker."""
    job_id = job["id"]
    _ensure_apply_worker()
    await _apply_queue.put((query, job_id, job))
    position = _apply_queue.qsize()
    await query.edit_message_text(
        text=(
            f"⏳ QUEUED FOR APPLY (position {position})\n"
            f"{job['title']} @ {job['company']}"
        ),
        disable_web_page_preview=True,
    )


async def _handle_reject(query, job: dict) -> None:
    reject_job(job["id"])
    _record_filter_outcome(job, "reject")
    await query.edit_message_text(
        text=f"❌ *SKIPPED*\n{job['title']} @ {job['company']}",
        parse_mode="Markdown",
    )


async def _handle_followedup(query, job: dict) -> None:
    record_followup(job["id"])
    await query.edit_message_text(
        text=f"📧 *FOLLOWED UP*\n{job['title']} @ {job['company']}",
        parse_mode="Markdown",
    )


async def _handle_interviewing(query, job: dict) -> None:
    update_job_status(job["id"], "interviewing")
    await query.edit_message_text(
        text=f"🎤 *INTERVIEWING*\n{job['title']} @ {job['company']}",
        parse_mode="Markdown",
    )


async def _handle_close(query, job: dict) -> None:
    update_job_status(job["id"], "closed")
    await query.edit_message_text(
        text=f"🚪 *CLOSED*\n{job['title']} @ {job['company']}",
        parse_mode="Markdown",
    )


_CALLBACK_HANDLERS = {
    "approve": _handle_approve,
    "applynow": _handle_applynow,
    "reject": _handle_reject,
    "followedup": _handle_followedup,
    "interviewing": _handle_interviewing,
    "close": _handle_close,
}


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if query is None or query.message is None:
        return

    # Auth check FIRST: only allow the configured chat to interact
    if str(query.message.chat_id) != str(TELEGRAM_CHAT_ID):
        logger.warning(f"Unauthorized callback from chat_id={query.message.chat_id}")
        await query.answer(text="Unauthorized", show_alert=True)
        return

    await query.answer()

    parts = (query.data or "").split("_", 1)
    if len(parts) != 2:
        return
    action, job_id_str = parts

    handler = _CALLBACK_HANDLERS.get(action)
    if handler is None:
        return

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

    await handler(query, job)


def build_bot_app() -> Application:
    """Build and return the Telegram bot Application."""
    defaults = Defaults(tzinfo=pytz.timezone("Europe/Istanbul"))
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).defaults(defaults).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("applied", cmd_applied))
    app.add_handler(CommandHandler("followups", cmd_followups))
    app.add_handler(CallbackQueryHandler(callback_handler))
    return app
