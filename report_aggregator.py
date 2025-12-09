from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import json
import logging

from db import Report, get_reports_in_range, create_report
from news_analyzer import build_html_digest_from_json
from holidays_manager import get_holiday_emoji
from ai_client import client
from config import settings

logger = logging.getLogger(__name__)


# AI prompts for aggregating reports
AGGREGATION_PROMPTS = {
    "weekly": (
        "You're a crypto market analyst. Your input is a set of DAILY reports "
        "from the past week, each already analyzed and structured.\n\n"
        "Your task:\n"
        "Analyze all the daily reports and create a WEEKLY SUMMARY that:\n"
        "1) Identifies the main trends and themes of the week\n"
        "2) Highlights the most significant positive developments across all days\n"
        "3) Highlights the most significant negative developments and risks\n"
        "4) Summarizes key macro events that affected the market\n"
        "5) Lists the most promising assets based on the week's news (combine signals for the same asset)\n"
        "6) Filters out noise and repetitive information - focus on what truly matters\n\n"
        "Priority guidelines:\n"
        "- high: Major events with lasting impact, strong trading opportunities or risks\n"
        "- medium: Important but not critical developments\n"
        "- low: Background information worth noting\n\n"
    ),
    "monthly": (
        "You're a crypto market analyst. Your input is a set of WEEKLY reports "
        "from the past month, each already analyzed and structured.\n\n"
        "Your task:\n"
        "Analyze all the weekly reports and create a MONTHLY SUMMARY that:\n"
        "1) Identifies the main trends and themes of the month\n"
        "2) Highlights the most significant positive developments across all weeks\n"
        "3) Highlights the most significant negative developments and risks\n"
        "4) Summarizes key macro events that shaped the market\n"
        "5) Lists the most promising assets based on the month's performance (combine signals)\n"
        "6) Focus on major developments with lasting impact, ignore short-term noise\n\n"
        "Priority guidelines:\n"
        "- high: Major market-moving events, significant trend changes\n"
        "- medium: Notable developments with clear impact\n"
        "- low: Interesting context but not immediately actionable\n\n"
    ),
    "annual": (
        "You're a crypto market analyst. Your input is a set of MONTHLY reports "
        "from the past year, each already analyzed and structured.\n\n"
        "Your task:\n"
        "Analyze all the monthly reports and create an ANNUAL SUMMARY that:\n"
        "1) Identifies the major trends and themes that defined the year\n"
        "2) Highlights the most transformative positive developments\n"
        "3) Highlights the most significant negative events and their lasting impact\n"
        "4) Summarizes the macro environment and key regulatory/institutional changes\n"
        "5) Lists the year's top-performing and most promising assets\n"
        "6) Focus on developments with structural, long-term significance\n\n"
        "Priority guidelines:\n"
        "- high: Paradigm-shifting events, major market cycles, critical regulatory changes\n"
        "- medium: Significant events that shaped market sentiment\n"
        "- low: Notable but not year-defining developments\n\n"
    ),
}

AGGREGATION_FORMAT_INSTRUCTIONS = (
    "Format your answer:\n"
    "- Answer STRICTLY in JSON format without any comments before or after.\n"
    "- JSON structure:\n"
    "{\n"
    '  "summary": ["string1", "string2"],\n'
    '  "positive": [\n'
    '    {"text": "string", "tickers": ["BTC", "ETH"], "priority": "high"}\n'
    "  ],\n"
    '  "negative": [\n'
    '    {"text": "string", "tickers": ["SOL"], "priority": "medium"}\n'
    "  ],\n"
    '  "macro": [\n'
    '    {"text": "string", "tickers": [], "priority": "low"}\n'
    "  ],\n"
    '  "assets": [\n'
    '    {"ticker": "BTC", "direction": "bullish", "reason": "string", "priority": "high"}\n'
    "  ]\n"
    "}\n"
    "- All strings must be in English.\n"
    "- summary: 3-7 short strings summarizing the period's key developments\n"
    "- positive/negative/macro: lists of objects with text, tickers array, and priority\n"
    "- assets: list of objects with ticker, direction (bullish/bearish/neutral), reason, and priority\n"
    "- If any list is empty, return [].\n"
    "- Be concise and focus on the most important information.\n"
)


async def _aggregate_reports_with_ai(
    reports: List[Report],
    target_kind: str,
) -> dict:
    """
    Uses AI to create an intelligent summary of multiple reports.
    Returns a dict with the same structure as daily reports.
    """
    if not reports:
        return {
            "summary": [],
            "positive": [],
            "negative": [],
            "macro": [],
            "assets": [],
        }

    # Format each report nicely for the AI
    report_texts = []
    for idx, r in enumerate(reports, 1):
        try:
            data = json.loads(r.json_content)
            # Format each report nicely
            report_text = f"=== Report {idx} ({r.period_start_utc.date()} to {r.period_end_utc.date()}) ===\n"
            report_text += json.dumps(data, ensure_ascii=False, indent=2)
            report_texts.append(report_text)
        except Exception as e:
            logger.exception("Failed to parse report id=%s: %s", r.id, e)
            continue

    if not report_texts:
        logger.warning("No valid reports to aggregate")
        return {
            "summary": [],
            "positive": [],
            "negative": [],
            "macro": [],
            "assets": [],
        }

    joined_reports = "\n\n".join(report_texts)
    
    # Pick the right prompt and call AI
    prompt = AGGREGATION_PROMPTS.get(target_kind, AGGREGATION_PROMPTS["weekly"])
    full_instructions = prompt + AGGREGATION_FORMAT_INSTRUCTIONS

    # Make the API call
    try:
        if settings.llm_provider == "deepseek":
             resp = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=[
                    {"role": "system", "content": full_instructions},
                    {"role": "user", "content": joined_reports},
                ],
                response_format={"type": "json_object"},
            )
             raw_json = resp.choices[0].message.content
        else:
            resp = await client.responses.create(
                model=settings.openai_model,
                instructions=full_instructions,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": joined_reports,
                            }
                        ],
                    }
                ],
            )
            raw_json = resp.output[0].content[0].text
    except Exception as e:
        logger.exception("Error calling AI for %s aggregation: %s", target_kind, e)
        raise

    # Parse the response
    try:
        data = json.loads(raw_json)
    except Exception as e:
        logger.exception("Failed to parse JSON from AI: %s. Raw: %r", e, raw_json)
        raise

    return data


async def aggregate_reports(
    *,
    target_kind: str,      # 'weekly' or 'monthly' or 'annual'
    source_kind: str,      # 'daily' or 'weekly' or 'monthly'
    period_start_utc: datetime,
    period_end_utc: datetime,
) -> Optional[Report]:
    """
    A general aggregation function: takes reports of source_kind over the period
    [period_start_utc, period_end_utc] and creates/returns a report of target_kind.
    If target_kind already exists for this period, simply returns it.
    Uses AI to create an intelligent summary instead of simple concatenation.
    """
    # 1) Already exists? Return it
    existing = get_reports_in_range(target_kind, period_start_utc, period_end_utc)
    if existing:
        return existing[0]

    # 2) Get source reports for aggregation
    source_reports = get_reports_in_range(source_kind, period_start_utc, period_end_utc)
    if not source_reports:
        logger.info(
            "No %s reports found in range %s..%s to aggregate into %s",
            source_kind,
            period_start_utc.isoformat(),
            period_end_utc.isoformat(),
            target_kind,
        )
        return None

    # 3) Use AI to summarize
    try:
        combined_dict = await _aggregate_reports_with_ai(source_reports, target_kind)
        combined_json = json.dumps(combined_dict, ensure_ascii=False)
    except Exception as e:
        logger.exception("Failed to aggregate reports with AI for %s: %s", target_kind, e)
        return None

    # 4) Build HTML from summary
    try:
        emoji = get_holiday_emoji(period_end_utc) or ""
        html_content = build_html_digest_from_json(combined_dict, emoji=emoji)
    except Exception as e:
        logger.exception("Failed to build HTML for %s report: %s", target_kind, e)
        html_content = None

    # 5) Save and return
    return create_report(
        kind=target_kind,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        json_content=combined_json,
        html_content=html_content,
        pdf_path=None,
    )



def _last_full_week_period(now_utc: datetime) -> Tuple[datetime, datetime]:
    """
    The boundaries of a fully completed week:
    [start, end) — from Monday 00:00 (UTC) to the next Monday 00:00 (UTC).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    today_midnight = datetime(
        year=now_utc.year,
        month=now_utc.month,
        day=now_utc.day,
        tzinfo=timezone.utc,
    )
    weekday = today_midnight.weekday()  # Monday = 0, Sunday = 6
    current_week_start = today_midnight - timedelta(days=weekday)
    last_week_end = current_week_start
    last_week_start = last_week_end - timedelta(days=7)
    return last_week_start, last_week_end


def _last_full_month_period(now_utc: datetime) -> Tuple[datetime, datetime]:
    """
    The boundaries of a fully completed month:
    [start, end) — from the first day of the previous month 00:00 (UTC)
    to the first day of the current month 00:00 (UTC).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    this_month_start = datetime(
        year=now_utc.year,
        month=now_utc.month,
        day=1,
        tzinfo=timezone.utc,
    )

    if this_month_start.month == 1:
        prev_month_year = this_month_start.year - 1
        prev_month = 12
    else:
        prev_month_year = this_month_start.year
        prev_month = this_month_start.month - 1

    prev_month_start = datetime(
        year=prev_month_year,
        month=prev_month,
        day=1,
        tzinfo=timezone.utc,
    )
    prev_month_end = this_month_start

    return prev_month_start, prev_month_end


async def ensure_last_weekly_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is a weekly report for the last fully completed week.
    If not, creates it from daily reports (kind='daily').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    week_start, week_end = _last_full_week_period(now_utc)
    return await aggregate_reports(
        target_kind="weekly",
        source_kind="daily",
        period_start_utc=week_start,
        period_end_utc=week_end,
    )


async def ensure_last_monthly_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is a monthly report for the last fully completed month.
    If not, creates it from weekly reports (kind='weekly').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    month_start, month_end = _last_full_month_period(now_utc)
    return await aggregate_reports(
        target_kind="monthly",
        source_kind="weekly",
        period_start_utc=month_start,
        period_end_utc=month_end,
    )


def _last_full_year_period(now_utc: datetime) -> Tuple[datetime, datetime]:
    """
    The boundaries of a fully completed year:
    [start, end) — from Jan 1st of previous year to Jan 1st of current year.
    Strictly speaking, we only want to generate this if 'now' is in the new year.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    # Current year start
    this_year_start = datetime(
        year=now_utc.year,
        month=1,
        day=1,
        tzinfo=timezone.utc,
    )
    
    # Previous year start
    prev_year_start = datetime(
        year=now_utc.year - 1,
        month=1,
        day=1,
        tzinfo=timezone.utc,
    )
    
    return prev_year_start, this_year_start


async def ensure_last_annual_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is an annual report for the last fully completed year.
    If not, creates it from monthly reports (kind='monthly').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    year_start, year_end = _last_full_year_period(now_utc)
    return await aggregate_reports(
        target_kind="annual",
        source_kind="monthly",
        period_start_utc=year_start,
        period_end_utc=year_end,
    )
