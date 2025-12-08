from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import json
import logging

from db import Report, get_reports_in_range, create_report
from news_analyzer import build_html_digest_from_json
from holidays_manager import get_holiday_emoji

logger = logging.getLogger(__name__)


def _combine_reports_dict(reports: List[Report]) -> dict:
    """
    We merge the JSON of several reports into a single dictionary of the same format:
    {
      "summary": [...],
      "positive": [...],
      "negative": [...],
      "macro": [...],
      "assets": [...]
    }
    """
    combined = {
        "summary": [],
        "positive": [],
        "negative": [],
        "macro": [],
        "assets": [],
    }

    for r in reports:
        try:
            data = json.loads(r.json_content)
        except Exception as e:
            logger.exception(
                "Failed to parse json_content for report id=%s: %s", r.id, e
            )
            continue

        # summary
        if isinstance(data.get("summary"), list):
            combined["summary"].extend(str(x) for x in data["summary"])

        # positive / negative / macro / assets
        for key in ("positive", "negative", "macro", "assets"):
            items = data.get(key)
            if isinstance(items, list):
                combined[key].extend(items)

    return combined


def aggregate_reports(
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
    """
    # 1) If such a report already exists — simply return it
    existing = get_reports_in_range(target_kind, period_start_utc, period_end_utc)
    if existing:
        return existing[0]

    # 2) Get source reports
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

    # 3) Merge JSON and build HTML using the same builder as for daily digest
    combined_dict = _combine_reports_dict(source_reports)
    combined_json = json.dumps(combined_dict, ensure_ascii=False)

    try:
        emoji = get_holiday_emoji(period_end_utc) or ""
        html_content = build_html_digest_from_json(combined_dict, emoji=emoji)
    except Exception as e:
        logger.exception("Failed to build HTML for %s report: %s", target_kind, e)
        html_content = None

    # 4) Save to the reports table
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


def ensure_last_weekly_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is a weekly report for the last fully completed week.
    If not, creates it from daily reports (kind='daily').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    week_start, week_end = _last_full_week_period(now_utc)
    return aggregate_reports(
        target_kind="weekly",
        source_kind="daily",
        period_start_utc=week_start,
        period_end_utc=week_end,
    )


def ensure_last_monthly_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is a monthly report for the last fully completed month.
    If not, creates it from weekly reports (kind='weekly').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    month_start, month_end = _last_full_month_period(now_utc)
    return aggregate_reports(
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


def ensure_last_annual_report(now_utc: Optional[datetime] = None) -> Optional[Report]:
    """
    Ensures that there is an annual report for the last fully completed year.
    If not, creates it from monthly reports (kind='monthly').
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    year_start, year_end = _last_full_year_period(now_utc)
    return aggregate_reports(
        target_kind="annual",
        source_kind="monthly",
        period_start_utc=year_start,
        period_end_utc=year_end,
    )
