from __future__ import annotations

import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import json
from config import settings
from news_fetcher import fetch_all_channels
from news_analyzer import build_digest, build_html_digest_from_json
from translator import get_translation, translate_report_data
from db import (
    init_db,
    get_or_create_user,
    get_user,
    update_user,
    get_all_users,
    get_latest_report,
    get_user_stats,
    get_all_admin_users,
    create_support_ticket,
    get_support_ticket_by_admin_message,
    update_support_ticket_response,
    User,
    Report,
)

from pdf_renderer import generate_pdf_report, cleanup_old_reports
from report_aggregator import ensure_last_weekly_report, ensure_last_monthly_report, ensure_last_annual_report
from holidays_manager import get_holiday_emoji

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DIGEST_JOB_NAME = "global_digest_job"
REMINDER_JOB_NAME = "subscription_reminder_job"
AGGREGATE_JOB_NAME = "aggregate_reports_job"

MAX_TG_MESSAGE_LEN = 4096

TOPUP_PAYLOAD_PREFIX = "TOPUP:"  # to differentiate the payment type by payload

# Global digest interval (minutes) - can be changed via /auto_digest by admin
global_digest_interval_min: int = settings.digest_interval_min


# --- Helpers --------------------------------------------------------------

def _split_digest_into_messages(html_text: str, max_len: int = 3900) -> list[str]:
    """
    Split big HTML digest into multiple Telegram messages.

    Logic:
    - Split text into sections, each section starts with <b>Section name</b>...
    - DO NOT split sections, each section is sent as a whole.
    - Collect several sections into one message, until the length <= max_len.
    - As soon as the next section doesn't fit, send the current message
      and start a new one with this section.
    """
    if not html_text:
        return []

    # Split by section boundaries ("\n\n<b>")
    raw_chunks = html_text.split("\n\n<b>")
    sections: list[str] = []

    for idx, chunk in enumerate(raw_chunks):
        if not chunk:
            continue
        if idx == 0 and chunk.startswith("<b>"):
            # the first section already starts with <b>
            sections.append(chunk)
        else:
            # the rest lost "<b>" during split, let's return it
            sections.append("<b>" + chunk)

    if not sections:
        return []

    messages: list[str] = []
    current: str = ""

    for sec in sections:
        # theoretically, one section shouldn't be longer than max_len,
        # since we limit text length in _render_blockquote_section.
        # But just in case, let's check and send it separately.
        if len(sec) > max_len:
            # if something went wrong, first send what we have accumulated
            if current:
                messages.append(current)
                current = ""
            # then send the section as a separate message
            messages.append(sec)
            continue

        if not current:
            # first content of the current message
            current = sec
        else:
            candidate = current + "\n\n" + sec
            if len(candidate) <= max_len:
                # still fits - add section to the current message
                current = candidate
            else:
                # doesn't fit - send the old message,
                # and start a new one with this section
                messages.append(current)
                current = sec

    if current:
        messages.append(current)

    # No hard-truncations [:MAX_TG_MESSAGE_LEN] here,
    # to avoid breaking HTML tags.
    return messages


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _next_digest_run_time_utc(interval_min: int) -> datetime:
    """
    Compute the next run time in UTC for a repeating job with a given interval (in minutes),
    aligned to startTime UTC.

    Principle:
    - we take today's startTime UTC.
    - we build a grid: 06:00, 06:00 + interval, 06:00 + 2*interval, ...
    - we take the nearest time from this grid, which is strictly > now.

    Examples:
    - interval=60  -> runs at 06:00, 07:00, 08:00, ...
    - interval=720 -> 06:00 and 18:00
    """
    now = _now_utc()
    interval = timedelta(minutes=interval_min)

    startTime = now.replace(hour=6, minute=0, second=0, microsecond=0)
    elapsed = now - startTime

    # how many full intervals have passed since midnight
    intervals_passed = elapsed // interval

    # next slot after "now"
    next_run = startTime + (intervals_passed + 1) * interval
    return next_run


def _format_dt_utc(dt: datetime) -> str:
    """
    Format datetime in UTC as 'YYYY-MM-DD HH:MM UTC'.
    Accepts both naive and tz-aware datetimes.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _user_has_active_subscription(user: User, now: datetime) -> bool:
    if user.is_admin:
        return True
    return user.subscription_until is not None and user.subscription_until > now


def _user_can_receive_digest(user: User, now: datetime) -> bool:
    """
    Decide if user should receive digest in the global broadcast:
    - admins: always
    - non-admins: if free trial not used OR subscription is active
    """
    if user.is_admin:
        return True
    if not user.free_digest_used:
        return True
    return _user_has_active_subscription(user, now)


async def _format_subscription_status(user: User, now: datetime, lang: str) -> str:
    if user.is_admin:
        return await get_translation("🛡️ You are an <b>administrator</b>, you do not need a subscription.", lang)
    if user.subscription_until and user.subscription_until > now:
        dt_str = _format_dt_utc(user.subscription_until)
        msg = await get_translation("✅ Your subscription is active until", lang)
        return f"{msg} <b>{dt_str}</b>."
    return await get_translation("❌ Your subscription is <b>not active</b>.", lang)


def _normalize_delivery_mode(value: str) -> Optional[str]:
    v = (value or "").strip().lower()
    if v in ("pdf", "file", "doc"):
        return "pdf"
    if v in ("messages", "message", "msg", "text", "txt"):
        return "messages"
    return None



async def _get_pdf_labels(lang: str) -> dict[str, str]:
    labels_to_translate = {
        "digest_title": "Crypto Digest",
        "summary": "Summary",
        "assets": "Promising Assets",
        "positive": "Positive News",
        "negative": "Negative Factors",
        "macro": "Macro Events",
        "footer": "Generated by Crypto AI Digest Bot"
    }
    
    tasks = []
    keys = []
    for k, v in labels_to_translate.items():
        keys.append(k)
        tasks.append(get_translation(v, lang))
        
    results = await asyncio.gather(*tasks)
    return dict(zip(keys, results))


async def _generate_digest_report(

    start_utc: datetime, end_utc: datetime
) -> tuple[str, Optional[Report]]:
    """Fetch posts and build digest using OpenAI once."""
    posts = await asyncio.to_thread(fetch_all_channels)
    digest_text, report = await build_digest(
        posts,
        save_report=True,
        report_kind="daily",
        period_start_utc=start_utc,
        period_end_utc=end_utc,
    )
    return digest_text, report


def _get_next_digest_run_time(context: ContextTypes.DEFAULT_TYPE) -> Optional[datetime]:
    jobs = context.job_queue.get_jobs_by_name(DIGEST_JOB_NAME)
    if not jobs:
        return None
    job = jobs[0]
    return job.next_t.replace(tzinfo=timezone.utc)



# --- Jobs -----------------------------------------------------------------



async def _send_report_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    report: Optional[Report],
    translated_cache: Dict[str, Dict],
    pdf_cache: Dict[str, str],
) -> None:
    """
    Helper to translate and send the report (or 'no news' message) to a single user.
    """
    lang = user.language or "ru"
    
    # 1. Prepare Data
    data = None
    if report and report.json_content:
        default_json = json.loads(report.json_content)
        if lang == "en":
            data = default_json
        else:
            if lang not in translated_cache:
                # We assume caching happens here for the duration of the job context
                translated_cache[lang] = await translate_report_data(default_json, lang)
            data = translated_cache[lang]
            
    # If no data (no news), send translated "No news" message
    if not data:
        msg = await get_translation(
            "There were no important news items found in the monitored channels for the selected period.", 
            lang
        )
        await context.bot.send_message(
            chat_id=user.user_id,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # 2. Send Report
    sent_as_pdf = False
    
    if user.delivery_mode == "pdf":
        if lang not in pdf_cache:
            # Generate PDF for this language
            labels = await _get_pdf_labels(lang)
            
            # Create temp report with translated content
            temp_report = Report(
                id=report.id,
                kind=report.kind,
                period_start_utc=report.period_start_utc,
                period_end_utc=report.period_end_utc,
                json_content=json.dumps(data),
                html_content=None,
                pdf_path=None,
                created_at=report.created_at
            )
            
            pdf_path = await asyncio.to_thread(
                generate_pdf_report, 
                temp_report, # report
                labels, # labels
                lang, # lang_suffix
                False # save_to_db (do not overwrite main record)
            )
            if pdf_path:
                pdf_cache[lang] = pdf_path
        
        final_pdf_path = pdf_cache.get(lang)
        if final_pdf_path and os.path.exists(final_pdf_path):
             caption = await get_translation("Here is your crypto digest (PDF).", lang)
             try:
                await context.bot.send_document(
                    chat_id=user.user_id,
                    document=open(final_pdf_path, "rb"),
                    caption=caption,
                )
                sent_as_pdf = True
             except Exception as e:
                logger.error(f"Failed to send PDF to {user.user_id}: {e}")

    if not sent_as_pdf:
        # Text mode
        emoji = get_holiday_emoji(report.period_end_utc) or ""
        html = build_html_digest_from_json(data, emoji=emoji)
        parts = _split_digest_into_messages(html)
        for part in parts:
            await context.bot.send_message(
                chat_id=user.user_id,
                text=part,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )


async def global_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global job: called every global_digest_interval_min minutes.
    - Decide which users should receive digest
    - Generate digest ONCE (in RU default)
    - Send to all eligible users, translating on the fly if needed.
    """
    logger.info("Running global digest job")

    now = _now_utc()
    users = get_all_users()
    recipients: List[User] = [
        u for u in users if _user_can_receive_digest(u, now)
    ]

    if not recipients:
        logger.info("No eligible recipients for digest, skipping OpenAI call")
        return

    period_end_utc = now
    period_start_utc = period_end_utc - timedelta(minutes=global_digest_interval_min)

    try:
        # report contains the canonical (RU) data
        _, report = await _generate_digest_report(period_start_utc, period_end_utc)
    except Exception as e:
        logger.exception("Error generating digest in global job: %s", e)
        return

    # Caches for this run
    translated_data_cache: Dict[str, Dict] = {} 
    pdf_path_cache: Dict[str, str] = {}

    for user in recipients:
        try:
            await _send_report_to_user(
                context, 
                user, 
                report, 
                translated_data_cache, 
                pdf_path_cache
            )
        except Exception as e:
            logger.exception(
                "Failed to send digest to user %s: %s", user.user_id, e
            )
            continue

        # Mark free trial as used
        if (not user.is_admin) and (not user.free_digest_used):
            user.free_digest_used = True
            user.updated_at = now
            update_user(user)

    # After global digest is done (daily tick), trigger aggregation for Weekly/Monthly/Annual
    # This ensures sequential "absorption" of the latest daily data.
    logger.info("Global digest finished. Triggering aggregation job.")
    await aggregate_reports_job(context)



async def subscription_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily job: remind users about subscription expiration within N days.
    """
    now = _now_utc()
    today = now.date()
    horizon = today + timedelta(days=settings.renewal_reminder_days)

    users = get_all_users()
    for user in users:
        if user.is_admin:
            continue
        if not user.subscription_until:
            continue

        subs_date = user.subscription_until.date()
        if subs_date <= today:
            # already expired - reminders are not needed, can write something in the future
            continue
        if subs_date > horizon:
            # too far from the end
            continue

        # Avoid spamming multiple reminders per day
        if user.last_renewal_reminder_date == today:
            continue

        try:
            lang = user.language or "ru"
            expiration_str = _format_dt_utc(user.subscription_until)

            msg_exp = await get_translation("⚠️ Your subscription is about to expire.", lang)
            msg_date = await get_translation("📅 Expiration date:", lang)
            msg_renew = await get_translation("To continue receiving digests, renew your subscription with the <b>/subscribe</b> command.", lang)

            await context.bot.send_message(
                chat_id=user.user_id,
                text=(
                    f"{msg_exp}\n"
                    f"{msg_date} <b>{expiration_str}</b>.\n\n"
                    f"👉 {msg_renew}"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.exception(
                "Failed to send subscription reminder to %s: %s",
                user.user_id,
                e,
            )
            continue

        user.last_renewal_reminder_date = today
        user.updated_at = now
        update_user(user)


def _schedule_digest_job(job_queue) -> None:
    """
    Schedule (or reschedule) global digest job using the current
    global_digest_interval_min (in minutes).

    Schedule:
    - every global_digest_interval_min minutes
    - first run - in the nearest slot by UTC, multiple of the interval, counting from midnight.
    """
    # remove old jobs with this name
    for job in job_queue.get_jobs_by_name(DIGEST_JOB_NAME):
        job.schedule_removal()

    interval_min = global_digest_interval_min
    interval_sec = interval_min * 60

    first_dt = _next_digest_run_time_utc(interval_min)

    job_queue.run_repeating(
        global_digest_job,
        interval=interval_sec,
        first=first_dt,
        name=DIGEST_JOB_NAME,
    )

    logger.info(
        "Scheduled global digest job every %s minutes. First run at %s (UTC).",
        interval_min,
        first_dt.isoformat(),
    )


async def aggregate_reports_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily job: ensures that:
    - there is a weekly report for the last fully completed week,
    - there is a monthly report for the last fully completed month.
    """
    now = _now_utc()
    logger.info("Running aggregate_reports_job at %s", now.isoformat())

    try:
        weekly_report = ensure_last_weekly_report(now)
        if weekly_report:
            logger.info(
                "Weekly report ready: id=%s, period=%s..%s",
                weekly_report.id,
                weekly_report.period_start_utc.isoformat(),
                weekly_report.period_end_utc.isoformat(),
            )
    except Exception as e:
        logger.exception("Failed to ensure weekly report: %s", e)

    try:
        monthly_report = ensure_last_monthly_report(now)
        if monthly_report:
            logger.info(
                "Monthly report ready: id=%s, period=%s..%s",
                monthly_report.id,
                monthly_report.period_start_utc.isoformat(),
                monthly_report.period_end_utc.isoformat(),
            )
    except Exception as e:
        logger.exception("Failed to ensure monthly report: %s", e)

    try:
        annual_report = ensure_last_annual_report(now)
        if annual_report:
            logger.info(
                "Annual report ready: id=%s, period=%s..%s",
                annual_report.id,
                annual_report.period_start_utc.isoformat(),
                annual_report.period_end_utc.isoformat(),
            )
    except Exception as e:
        logger.exception("Failed to ensure annual report: %s", e)


async def cleanup_reports_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily job: clean up old PDF files to save space.
    """
    logger.info("Running cleanup_reports_job...")
    count = await asyncio.to_thread(cleanup_old_reports, 24)
    if count > 0:
        logger.info(f"Cleanup finished. Deleted {count} old PDF files.")
    else:
        logger.info("Cleanup finished. No files were deleted.")


# --- Command Handlers -----------------------------------------------------


async def _append_user_commands(lines: List[str], lang: str) -> None:
    lines.append(await get_translation("Commands:", lang))
    # Admin commands list - mostly tech, maybe keep english or translate basic descriptions?
    # Let's translate descriptions.
    lines.append("/start - " + await get_translation("start the bot", lang))
    lines.append("/status - " + await get_translation("show your status and subscription", lang))
    lines.append("/subscribe - " + await get_translation("subscribe to the global digest", lang))
    lines.append("/topup - " + await get_translation("top up your balance", lang))
    lines.append("/digest_mode - " + await get_translation("change digest mode", lang))
    lines.append("/language - " + await get_translation("change language", lang))
    lines.append("/support - " + await get_translation("send a message to support", lang))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return

    # Check if user already exists
    # If not, we will show language selection
    is_new_user = (get_user(tg_user.id) is None)

    user = get_or_create_user(tg_user.id, language=(tg_user.language_code or "ru"))
    lang = user.language or "ru"
    now = _now_utc()



    lines: List[str] = []
    
    msg_intro = await get_translation("👋 Hello! I'm a <b>crypto news aggregator</b>.", lang)
    lines.append(msg_intro)
    lines.append("")
    
    msg_channels = await get_translation("📢 I collect news from channels:", lang)
    lines.append(msg_channels)
    for ch in settings.channels:
        lines.append(f"- <b>@{ch}</b>")
    lines.append("")
    
    msg_auto = await get_translation("⏱️ Auto-digest is currently set to run every", lang)
    lines.append(f"{msg_auto} <code>{global_digest_interval_min}</code> min.")

    # Next digest info
    next_run = _get_next_digest_run_time(context)
    if next_run:
        diff_hours = (next_run - now).total_seconds() / 3600
        time_str = _format_dt_utc(next_run)
        
        # Relative time string, e.g. (in ~4.2h)
        # Translate template with placeholder, then substitute actual value
        rel_template = await get_translation("(in 999 h)", lang)
        rel_str = rel_template.replace("999", f"~{diff_hours:.1f}")
        
        msg_next = await get_translation("⏰ Next digest scheduled for:", lang)
        lines.append(f"{msg_next} <code>{time_str}</code> {rel_str}.")
    else:
        msg_disabled = await get_translation("Auto-digest is currently disabled.", lang)
        lines.append(f"⏰ {msg_disabled}")
        
    lines.append("")

    if user.is_admin:
        msg_admin = await get_translation("🛡️ You are marked as an <b>administrator</b>.", lang)
        msg_subs = await get_translation("⚡ Subscription is not needed, access to digests is always open.", lang)
        
        lines.append(msg_admin)
        lines.append(msg_subs)
        lines.append("")

        await _append_user_commands(lines, lang)

        lines.append("")
        lines.append(await get_translation("🛠️ Admin commands:", lang))
        lines.append("/digest - " + await get_translation("run digest now", lang))
        lines.append("/auto_digest [minutes] - " + await get_translation("change global digest interval", lang))
        lines.append("/start_digest - " + await get_translation("start the auto-digest job", lang))
        lines.append("/stop_digest - " + await get_translation("stop the auto-digest job", lang))
        lines.append("/add_stars [user_id] [amount] - " + await get_translation("add stars to user balance", lang))
        lines.append("/rebuild_last_digest - " + await get_translation("rebuild the last digest", lang))
        lines.append("/stats - " + await get_translation("show statistics", lang))
    else:
        price = settings.subscription_price_stars
        period_days = settings.subscription_period_days

        msg_cost = await get_translation(f"💳 Monthly subscription costs <b>{price} ⭐</b> and lasts <b>{period_days} days</b>.", lang)
        lines.append(msg_cost)
        
        msg_reminder = await get_translation("Three days before the end of the subscription, I will send a reminder to renew.", lang)
        lines.append(msg_reminder)
        lines.append("")
        
        if not user.free_digest_used:
            msg_free = await get_translation("🎁 All new users get ONE <b>free digest message</b>.", lang)
            lines.append(msg_free)
            msg_next_free = await get_translation("🔜 Your free digest will be sent in the <b>next digest</b>.", lang)
            lines.append(msg_next_free)
        else:
            msg_used = await get_translation("🏁 Your free digest has already been <b>used</b>. To continue receiving digests,", lang)
            lines.append(msg_used)
            msg_sub_cmd = await get_translation("👉 subscribe with the <b>/subscribe</b> command (stars are credited to your balance manually or later through payment).", lang)
            lines.append(msg_sub_cmd)
            
        lines.append("")
        await _append_user_commands(lines, lang)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # Show language selection only for new users
    if is_new_user:
        await language_command(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if tg_user is None:
        return
    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"
    now = _now_utc()

    sub_status = await _format_subscription_status(user, now, lang)

    lines: List[str] = []
    
    msg_id = await get_translation("🆔 Your Telegram ID:", lang)
    lines.append(f"{msg_id} <code>{user.user_id}</code>")
    
    msg_role = await get_translation("👤 Role:", lang)
    role_val = await get_translation("admin" if user.is_admin else "user", lang)
    lines.append(f"{msg_role} <code>{role_val}</code>")
    
    msg_balance = await get_translation("💰 Balance:", lang)
    lines.append(f"{msg_balance} <b>{user.balance_stars}</b> ⭐")
    
    lines.append(sub_status)
    
    msg_free_lbl = await get_translation("🎁 Free digest:", lang)
    msg_avail = await get_translation("available", lang)
    msg_used = await get_translation("used", lang)
    val_free = msg_avail if not user.free_digest_used else msg_used
    
    lines.append(
        f"{msg_free_lbl} <code>{val_free}</code>"
    )
    
    delivery_mode = getattr(user, "delivery_mode", "pdf")
    msg_mode = await get_translation("📨 Digest mode:", lang)
    lines.append(f"{msg_mode} <code>{await get_translation(delivery_mode, lang)}</code>")
    lines.append("")
    
    msg_interval = await get_translation("⏱️ Current auto-digest interval: every", lang)
    msg_min = await get_translation("minutes", lang)
    lines.append(
        f"{msg_interval} <code>{global_digest_interval_min}</code> {msg_min}."
    )
    
    # Next digest info
    next_run = _get_next_digest_run_time(context)
    if next_run:
        diff_hours = (next_run - now).total_seconds() / 3600
        time_str = _format_dt_utc(next_run)
        rel_template = await get_translation("(in 999 h)", lang)
        rel_str = rel_template.replace("999", f"~{diff_hours:.1f}")
        
        msg_next = await get_translation("⏰ Next digest scheduled for:", lang)
        lines.append(f"{msg_next} <code>{time_str}</code> {rel_str}.")
    else:
        msg_disabled = await get_translation("Auto-digest is currently disabled.", lang)
        lines.append(f"⏰ {msg_disabled}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def digest_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /digest_mode - show buttons to select digest mode.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"
    
    current = getattr(user, "delivery_mode", "pdf")
    
    msg_select = await get_translation("📝 Select digest mode:", lang)
    msg_current = await get_translation("📌 Current:", lang)

    keyboard = [
        [
            InlineKeyboardButton("PDF", callback_data="set_mode_pdf"),
            InlineKeyboardButton("Messages", callback_data="set_mode_messages"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"{msg_select}\n{msg_current} <code>{await get_translation(current, lang)}</code>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def digest_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle mode selection callback.
    """
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    data = query.data
    if data == "set_mode_pdf":
        new_mode = "pdf"
    elif data == "set_mode_messages":
        new_mode = "messages"
    else:
        return

    user.delivery_mode = new_mode
    user.updated_at = _now_utc()
    update_user(user)

    msg_updated = await get_translation("Digest mode updated to:", lang)
    
    # Update message text removing buttons
    await query.edit_message_text(
        text=f"{msg_updated} <code>{new_mode}</code>",
        parse_mode="HTML"
    )


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /digest - manual trigger.
    If regular user: one-time personal digest.
    If admin: runs GLOBAL digest job (broadcast to all).
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    chat_id = update.effective_chat.id
    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"
    now = _now_utc()
    
    # --- ADMIN BROADCAST LOGIC ---
    if user.is_admin:
        # Ask for confirmation or just run? User said "I could manually trigger report, and it would go to people".
        # Let's just run it and notify admin.
        msg_start = await get_translation("Starting global digest broadcast...", lang)
        await update.message.reply_text(
            msg_start,
            parse_mode="HTML"
        )
        
        # We need to manually invoke global_digest_job logic.
        # But global_digest_job takes `context`. 
        # We can call it directly.
        await global_digest_job(context)
        return

    # --- REGULAR USER PERSONAL DIGEST ---
    # Access control
    if not _user_can_receive_digest(user, now):
        msg = await get_translation(
            "You don't have an active subscription and your free digest has already been used.\n"
            "Subscribe with the <b>/subscribe</b> command.",
            lang
        )
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Immediate response
    msg = await get_translation(
        "Digest generation started in the background.\n"
        "You will receive it in a separate message.",
        lang
    )
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # Start background task
    context.application.create_task(
        _background_digest(
            chat_id=chat_id,
            user_id=user.user_id,
            free_trial_was_used=user.free_digest_used,
            started_at=now,
            context=context,
        )
    )


async def check_missed_reports_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Checks if we missed a global digest due to downtime.
    If missed - triggers global_digest_job once.
    Also runs weekly/monthly aggregation (idempotent).
    """
    now = _now_utc()

    # === 1) Daily / global digest ===
    interval_td = timedelta(minutes=global_digest_interval_min)
    daily_report = get_latest_report("daily")

    should_run_digest = False

    if daily_report is None:
        # Bot just started, no reports, but users exist - can give them the first digest
        users = get_all_users()
        if users:
            should_run_digest = True
    else:
        end = daily_report.period_end_utc
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        time_since_end = now - end

        # If since the end of the period passed more than interval + buffer -> consider it missed
        if time_since_end > (interval_td + timedelta(minutes=15)):
            should_run_digest = True

    if should_run_digest:
        logger.warning("Auto-recovery: triggering global digest now.")
        await global_digest_job(context)

    # === 2) Weekly / Monthly ===
    # Here everything is simpler: our ensure_last_* are already idempotent in themselves and are not tied to "today is Monday/1st"
    try:
        weekly = ensure_last_weekly_report(now)
        if weekly:
            logger.info(
                "Weekly report ensured: id=%s (%s..%s)",
                weekly.id,
                weekly.period_start_utc,
                weekly.period_end_utc,
            )

        monthly = ensure_last_monthly_report(now)
        if monthly:
            logger.info(
                "Monthly report ensured: id=%s (%s..%s)",
                monthly.id,
                monthly.period_start_utc,
                monthly.period_end_utc,
            )
    except Exception as e:
        logger.exception("Error in weekly/monthly auto-recovery: %s", e)


async def _background_digest(
    chat_id: int,
    user_id: int,
    free_trial_was_used: bool,
    started_at,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Background job: build digest and send it to the user.
    Now sends digest in several messages if needed (Telegram length limit).
    """
    try:
        period_end_utc = started_at
        period_start_utc = period_end_utc - timedelta(minutes=global_digest_interval_min)

        _, report = await _generate_digest_report(period_start_utc, period_end_utc)

        user = get_or_create_user(user_id)
        
        # Caches
        translated_cache: Dict[str, Dict] = {}
        pdf_cache: Dict[str, str] = {}
        
        await _send_report_to_user(context, user, report, translated_cache, pdf_cache)

        # Mark free trial as used if it was not used before
        if not free_trial_was_used:
            # Re-fetch user to update
            user = get_or_create_user(user_id)
            if (not user.is_admin) and (not user.free_digest_used):
                user.free_digest_used = True
                user.updated_at = started_at
                update_user(user)

    except Exception as e:
        logger.exception("Error while building digest in background: %s", e)
        try:
            user = get_or_create_user(user_id)
            lang = user.language or "ru"
            msg = await get_translation("An error occurred while building the digest 🥲", lang)
            
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /subscribe - activates or extends subscription.
    For now, uses internal balance_stars (manual top-up via admin).
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"
    now = _now_utc()

    if user.is_admin:
        msg = await get_translation("You are an <b>administrator</b> - subscription is not needed, access is always open.", lang)
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    price = settings.subscription_price_stars
    period_days = settings.subscription_period_days

    if user.subscription_until and user.subscription_until > now:
        dt_str = _format_dt_utc(user.subscription_until)
        msg_active = await get_translation("✅ Your subscription is active until", lang)
        msg_renew = await get_translation("🔄 If you want to renew it early, make sure you have enough stars on your balance.", lang)
        
        await update.message.reply_text(
            f"{msg_active} <b>{dt_str}</b>.\n"
            f"{msg_renew}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if user.balance_stars < price:
        missing = price - user.balance_stars
        
        msg_cost = await get_translation(f"Subscription costs <b>{price} ⭐</b> for <b>{period_days}</b> days.", lang)
        msg_have = await get_translation(f"Now you have <b>{user.balance_stars} ⭐</b> on your balance.", lang)
        msg_miss = await get_translation(f"You are missing <b>{missing} ⭐</b>.", lang)
        msg_instr = await get_translation("Top up your balance (you can do this later through payment with stars) and call <b>/subscribe</b> again.", lang)

        await update.message.reply_text(
            f"{msg_cost}\n"
            f"{msg_have}\n"
            f"{msg_miss} {msg_instr}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # Enough stars - activate or extend subscription
    user.balance_stars -= price

    if user.subscription_until and user.subscription_until > now:
        base = user.subscription_until
    else:
        base = now

    new_until = base + timedelta(days=period_days)
    new_until_str = _format_dt_utc(new_until)
    user.subscription_until = new_until
    user.updated_at = now
    update_user(user)

    msg_done = await get_translation("✅ Subscription activated/extended.", lang)
    msg_exp = await get_translation("📅 New expiration date:", lang)
    msg_debited = await get_translation(f"💸 <b>{price} ⭐</b> debited.", lang)
    msg_bal = await get_translation("💰 Balance:", lang)

    await update.message.reply_text(
        f"{msg_done}\n"
        f"{msg_exp} <b>{new_until_str}</b>.\n"
        f"{msg_debited} {msg_bal} <b>{user.balance_stars} ⭐</b>.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def auto_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auto_digest <minutes> - admin-only:
    change global digest interval and reschedule global job.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return
    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    if not user.is_admin:
        msg = await get_translation("Only admins can change the auto-digest interval.", lang)
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    global global_digest_interval_min

    try:
        interval_min = int(context.args[0]) if context.args else settings.digest_interval_min
        if interval_min <= 0:
            raise ValueError
    except (ValueError, IndexError):
        msg_usage = await get_translation("Usage: <b>/auto_digest</b> <b>minutes</b>, e.g. <b>/auto_digest 60</b>", lang)
        await update.message.reply_text(
            msg_usage,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    global_digest_interval_min = interval_min
    _schedule_digest_job(context.job_queue)

    msg_done = await get_translation("Global auto-digest interval changed to", lang)
    msg_min = await get_translation("minutes", lang)

    await update.message.reply_text(
        f"{msg_done} <b>{interval_min}</b> {msg_min}.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_auto_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start_digest - admin-only: (re)start global digest job
    with current global_digest_interval_min.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return
    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    if not user.is_admin:
        msg = await get_translation("Only admins can start the auto-digest.", lang)
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    _schedule_digest_job(context.job_queue)
    
    msg_started = await get_translation("Global auto-digest started every", lang)
    msg_min = await get_translation("minutes", lang)
    
    await update.message.reply_text(
        f"{msg_started} <b>{global_digest_interval_min}</b> {msg_min}.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def stop_auto_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stop_digest - admin-only: stop global digest job.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return
    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    if not user.is_admin:
        msg = await get_translation("Only admins can stop the auto-digest.", lang)
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    jobs = context.job_queue.get_jobs_by_name(DIGEST_JOB_NAME)
    if not jobs:
        msg_not_run = await get_translation("Global auto-digest is not running.", lang)
        await update.message.reply_text(msg_not_run)
        return

    for job in jobs:
        job.schedule_removal()

    msg_stopped = await get_translation("Global auto-digest stopped.", lang)
    await update.message.reply_text(msg_stopped)


async def add_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /add_stars <user_id> <amount> - admin-only:
    manually adjust user balance (for tests / temporarily instead of real payment).
    """
    tg_user = update.effective_user
    if tg_user is None:
        return
    admin = get_or_create_user(tg_user.id)
    lang = admin.language or "ru"

    if not admin.is_admin:
        msg = await get_translation("Only admins can use this command.", lang)
        await update.message.reply_text(msg)
        return

    if len(context.args) != 2:
        msg_usage = await get_translation("Usage: <b>/add_stars</b> <b>user_id</b> <b>amount</b>, e.g. <b>/add_stars 123456789 10</b>", lang)
        await update.message.reply_text(
            msg_usage,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        msg_err = await get_translation("<b>user_id</b> and <b>amount</b> must be integers.", lang)
        await update.message.reply_text(
            msg_err,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if amount == 0:
        msg_zero = await get_translation("Adding <b>0</b> stars doesn't make sense.", lang)
        await update.message.reply_text(msg_zero, parse_mode="HTML", disable_web_page_preview=True)
        return

    user = get_or_create_user(target_id)
    user.balance_stars += amount
    user.updated_at = _now_utc()
    update_user(user)

    msg_cred = await get_translation(f"User <b>{target_id}</b> has been credited with <b>{amount}</b> ⭐.", lang)
    msg_bal = await get_translation(f"New balance: <b>{user.balance_stars}</b> ⭐.", lang)

    await update.message.reply_text(
        f"{msg_cred}\n"
        f"{msg_bal}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _parse_topup_amount(args: list[str]) -> int | None:
    """
    Parse /topup arguments into integer amount of Stars.
    If no args passed, return default = subscription price from settings.
    """
    if not args:
        # default: same as monthly subscription price
        return settings.subscription_price_stars

    try:
        amount = int(args[0])
    except ValueError:
        return None

    if amount <= 0:
        return None

    return amount


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /topup [amount] - create an invoice in Telegram Stars to top up internal balance.
    Example: /topup 50
    If amount is omitted, uses settings.subscription_price_stars.
    """
    tg_user = update.effective_user
    if tg_user is None or update.effective_chat is None:
        return

    chat_id = update.effective_chat.id
    user_db = get_or_create_user(tg_user.id) # Re-fetch to be sure, though we need lang
    lang = user_db.language or "ru"

    amount = _parse_topup_amount(context.args)
    if amount is None:
        msg_usage = await get_translation("Usage: /topup <amount_in_stars>", lang)
        msg_ex = await get_translation("Example: /topup 50", lang)
        msg_def = await get_translation("If you omit the amount, I will charge the default subscription price:", lang)
        
        await update.message.reply_text(
            f"{msg_usage}\n"
            f"{msg_ex}\n"
            f"{msg_def} "
            f"{settings.subscription_price_stars} ⭐",
        )
        return

    # Just to be sure user exists in DB
    user = get_or_create_user(tg_user.id)

    title = await get_translation("Balance top-up", lang) 
    description = await get_translation(f"Top up your digest bot balance by {amount} Telegram Stars.", lang)
    currency = "XTR"  # Telegram Stars
    payload = f"{TOPUP_PAYLOAD_PREFIX}{user.user_id}"

    prices = [LabeledPrice(label=f"{amount} Stars", amount=amount)]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency=currency,
        prices=prices,
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Answer pre-checkout query from Telegram.
    Here you can additionally validate invoice_payload/amount if needed.
    """
    query = update.pre_checkout_query
    if query is None:
        return

    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle successful payments (Telegram Stars).
    Increase user's balance_stars accordingly.
    """
    message = update.effective_message
    if message is None or message.successful_payment is None:
        return

    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    stars_amount = sp.total_amount

    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    if payload.startswith(TOPUP_PAYLOAD_PREFIX):
        # This is our balance top-up
        old_balance = user.balance_stars
        new_balance = old_balance + stars_amount

        user.balance_stars = new_balance
        user.updated_at = _now_utc()
        update_user(user)

        msg_rec = await get_translation("✅ Payment received!", lang)
        msg_add = await get_translation(f"+{stars_amount} ⭐ to your balance.", lang)
        msg_cur = await get_translation("Current balance:", lang)

        await message.reply_text(
            f"{msg_rec}\n"
            f"{msg_add}\n"
            f"{msg_cur} {new_balance} ⭐",
        )
    else:
        msg_ok = await get_translation("✅ Payment received. Thank you!", lang)
        await message.reply_text(msg_ok)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /language - show buttons to select language.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    # Build keyboard from settings.allowed_languages
    # We might want a mapping for flags/names.
    # Simple mapping for common languages, fallback to code.
    known_langs = {
        "ar": "🇸🇦 العربية",
        "cs": "🇨🇿 Čeština",
        "de": "🇩🇪 Deutsch",
        "en": "🇬🇧 English",
        "es": "🇪🇸 Español",
        "fa": "🇮🇷 فارسی",
        "fr": "🇫🇷 Français",
        "he": "🇮🇱 עברית",
        "hu": "🇭🇺 Magyar",
        "it": "🇮🇹 Italiano",
        "ja": "🇯🇵 日本語",
        "kk": "🇰🇿 Қазақша",
        "ko": "🇰🇷 한국어",
        "pt": "🇵🇹 Português",
        "ru": "🇷🇺 Русский",
        "sk": "🇸🇰 Slovenčina",
        "sl": "🇸🇮 Slovenščina",
        "sv": "🇸🇪 Svenska",
        "ta": "🇮🇳 தமிழ்",
        "th": "🇹🇭 ไทย",
        "tl": "🇵🇭 Tagalog",
        "tr": "🇹🇷 Türkçe",
        "uk": "🇺🇦 Українська",
        "ur": "🇵🇰 اردو",
        "uz": "🇺🇿 Oʻzbekcha",
        "vi": "🇻🇳 Tiếng Việt",
        "zh": "🇨🇳 中文"
    }
    
    keyboard = []
    row = []
    for lang_code in settings.allowed_languages:
        label = known_langs.get(lang_code, lang_code.upper())
        row.append(InlineKeyboardButton(label, callback_data=f"set_lang_{lang_code}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🌐💬🗣️?",
        reply_markup=reply_markup
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle language selection callback.
    """
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    if tg_user is None:
        return

    data = query.data
    # Pattern: set_lang_<code>
    if not data.startswith("set_lang_"):
        return
        
    lang_code = data.replace("set_lang_", "")
    
    if lang_code not in settings.allowed_languages:
        # Should not happen if buttons are generated from allowed list
        return

    user = get_or_create_user(tg_user.id)
    user.language = lang_code
    user.updated_at = _now_utc()
    update_user(user)

    msg_ok = await get_translation("Language set to:", lang_code)
    
    await query.edit_message_text(
        text=f"{msg_ok} {lang_code.upper()}",
    )


async def rebuild_last_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /rebuild_last_digest - (Admin only) Resend the last daily digest to the admin,
    rebuilding the view (PDF/Messages) from stored JSON.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    if not user.is_admin:
        return

    # Get latest daily report
    report = get_latest_report("daily")
    if not report:
        await update.message.reply_text("No daily reports found in DB.")
        return

    # Notify starting
    msg = await get_translation("Rebuilding and sending last digest...", user.language or "ru")
    await update.message.reply_text(msg)

    # Send
    # We pass empty caches so it re-translates/re-renders if needed
    try:
        await _send_report_to_user(context, user, report, {}, {})
    except Exception as e:
        logger.exception("Failed to rebuild/send digest: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /stats - Admin only: show statistics about users and stars.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"
    
    if not user.is_admin:
        msg = await get_translation("Only admins can use this command.", lang)
        await update.message.reply_text(msg)
        return

    stats = get_user_stats()
    
    total_users = stats.get("total_users", 0)
    total_stars = stats.get("total_stars", 0)
    lang_counts = stats.get("languages", {})
    
    lines = []
    lines.append(await get_translation(f"📊 <b>Statistics</b>", lang))
    lines.append(await get_translation(f"👥 Users: <b>{total_users}</b>", lang))
    lines.append(await get_translation(f"⭐ Total Balance: <b>{total_stars}</b>", lang))
    lines.append("")
    lines.append(await get_translation("<b>Languages:</b>", lang))
    
    # Sort by count desc
    sorted_langs = sorted(lang_counts.items(), key=lambda item: item[1], reverse=True)
    
    for code, count in sorted_langs:
        # Calculate percentage
        pct = (count / total_users * 100) if total_users > 0 else 0
        lines.append(f"- {code}: {count} ({pct:.1f}%)")
        
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /support <message> - Send a support request to all administrators.
    """
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = get_or_create_user(tg_user.id)
    lang = user.language or "ru"

    # Get message text after /support
    message_text = " ".join(context.args) if context.args else ""
    
    if not message_text.strip():
        msg = await get_translation(
            "📝 Usage: <b>/support your message</b>\n\n"
            "Example: <b>/support I have a question about subscription</b>",
            lang
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    # Collect user info
    username = tg_user.username
    first_name = tg_user.first_name
    last_name = tg_user.last_name
    language_code = tg_user.language_code

    # Build admin message
    lines = []
    lines.append(await get_translation("📩 <b>Support Request</b>", lang))
    lines.append("")
    lines.append(await get_translation("👤 <b>User Info:</b>", lang))
    lines.append(f"🆔 <b>{await get_translation('ID', lang)}:</b> <code>{tg_user.id}</code>")
    
    if username:
        lines.append(f"📧 <b>{await get_translation('Username', lang)}:</b> @{username}")
    if first_name or last_name:
        name_parts = [p for p in [first_name, last_name] if p]
        lines.append(f"👤 <b>{await get_translation('Name', lang)}:</b> {' '.join(name_parts)}")
    if language_code:
        lines.append(f"🌐 <b>{await get_translation('TG Language', lang)}:</b> {language_code}")
    
    lines.append(f"💬 <b>{await get_translation('DB Language', lang)}:</b> {user.language}")
    lines.append(f"🎭 <b>{await get_translation('Role', lang)}:</b> {'Admin' if user.is_admin else 'User'}")
    lines.append(f"⭐ <b>{await get_translation('Balance', lang)}:</b> {user.balance_stars}")
    
    if user.subscription_until:
        sub_str = _format_dt_utc(user.subscription_until)
        lines.append(f"📅 <b>{await get_translation('Subscription', lang)}:</b> until {sub_str}")
    else:
        lines.append(f"📅 <b>{await get_translation('Subscription', lang)}:</b> None")
    
    lines.append("")
    lines.append(await get_translation("📝 <b>Message:</b>", lang))
    lines.append(message_text)

    
    admin_message = "\n".join(lines)
    
    # Send to all admins
    admins = get_all_admin_users()
    sent_count = 0
    
    for admin in admins:
        try:
            sent_msg = await context.bot.send_message(
                chat_id=admin.user_id,
                text=admin_message,
                parse_mode="HTML",
            )
            # Create ticket with reference to this message
            create_support_ticket(
                user_id=tg_user.id,
                message=message_text,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                admin_message_id=sent_msg.message_id,
                admin_id=admin.user_id,
            )
            sent_count += 1
        except Exception as e:
            logger.error("Failed to send support message to admin %s: %s", admin.user_id, e)
    
    if sent_count > 0:
        msg = await get_translation(
            "✅ Your message has been sent to the support team.\n"
            "We will reply as soon as possible.",
            lang
        )
    else:
        msg = await get_translation(
            "❌ Failed to send your message. Please try again later.",
            lang
        )
    
    await update.message.reply_text(msg, parse_mode="HTML")


async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle admin replies to support tickets.
    When admin replies to a support message, forward the reply to the user.
    """
    message = update.effective_message
    if message is None or message.reply_to_message is None:
        return
    
    tg_user = update.effective_user
    if tg_user is None:
        return
    
    # Check if user is admin
    admin = get_or_create_user(tg_user.id)
    admin_lang = admin.language or "ru"
    if not admin.is_admin:
        return
    
    # Check if this is a reply to a support ticket message
    replied_msg_id = message.reply_to_message.message_id
    ticket = get_support_ticket_by_admin_message(tg_user.id, replied_msg_id)
    
    if ticket is None:
        return
    
    reply_text = message.text or ""
    if not reply_text.strip():
        return
    
    # Send reply to user
    try:
        # Get user's language
        ticket_user = get_user(ticket.user_id)
        user_lang = ticket_user.language if ticket_user else "ru"
        
        msg_header = await get_translation("📬 <b>Administrator Response</b>", user_lang)
        msg_body = await get_translation("Your support request has been answered:", user_lang)
        
        response_message = (
            f"{msg_header}\n\n"
            f"{msg_body}\n\n"
            f"«{reply_text}»"
        )
        
        await context.bot.send_message(
            chat_id=ticket.user_id,
            text=response_message,
            parse_mode="HTML",
        )
        
        # Update ticket status
        update_support_ticket_response(ticket.id, reply_text)
        
        # Confirm to admin
        await message.reply_text(await get_translation("✅ Reply sent to user.", admin_lang))
        
    except Exception as e:
        logger.error("Failed to send reply to user %s: %s", ticket.user_id, e)
        await message.reply_text(await get_translation("❌ Failed to send reply: {e}", admin_lang))


def main() -> None:

    # Initialize DB
    init_db()

    application: Application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )

    # Handlers
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("digest_mode", digest_mode_command))
    application.add_handler(CallbackQueryHandler(digest_mode_callback, pattern="^set_mode_"))
    
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CallbackQueryHandler(language_callback, pattern="^set_lang_"))
    
    application.add_handler(CommandHandler("digest", digest_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))

    application.add_handler(CommandHandler("auto_digest", auto_digest))
    application.add_handler(CommandHandler("start_digest", start_auto_digest))
    application.add_handler(CommandHandler("stop_digest", stop_auto_digest))
    
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    application.add_handler(CommandHandler("add_stars", add_stars))
    application.add_handler(CommandHandler("rebuild_last_digest", rebuild_last_digest_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("support", support_command))
    
    # Reply handler for admin responses to support tickets (must be after other handlers)
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, admin_reply_handler))

    # Schedule jobs:
    # 1) Global digest job (starts immediately with configured interval)
    _schedule_digest_job(application.job_queue)

    # 2) Daily reminder job (once per 24 hours, first run in 1 hour)
    application.job_queue.run_repeating(
        subscription_reminder_job,
        interval=24 * 60 * 60,
        first=60 * 60,
        name=REMINDER_JOB_NAME,
    )

    # 3) Weekly and monthly report aggregation job (once per day, first run in 5 minutes)
    application.job_queue.run_repeating(
        aggregate_reports_job,
        interval=24 * 60 * 60,
        first=5 * 60,
        name=AGGREGATE_JOB_NAME,
    )

    # 4) Auto-recovery job (check for missed digests every 30 minutes)
    application.job_queue.run_repeating(
        check_missed_reports_job,
        interval=30 * 60,
        first=60, # First check relatively soon
        name="check_missed_reports_job",
    )

    # 5) PDF Cleanup Job (once per day)
    application.job_queue.run_repeating(
        cleanup_reports_job,
        interval=24 * 60 * 60,
        first=60 * 60, # Start in 1 hour
        name="cleanup_reports_job",
    )

    application.run_polling()


if __name__ == "__main__":
    main()
