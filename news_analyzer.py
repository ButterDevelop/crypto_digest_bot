# news_analyzer.py
from __future__ import annotations

from typing import Any, List, Dict, Optional
import json
import html
from datetime import datetime


from openai import AsyncOpenAI
import logging

from config import settings
from db import create_report, Report
from holidays_manager import get_holiday_emoji


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from ai_client import client


def _render_blockquote_section(title: str, lines: List[str]) -> str:
    clean_lines = [line for line in lines if line.strip()]
    if not clean_lines:
        return ""

    text = "\n".join(clean_lines)
    escaped = html.escape(text)
    # Let's limit the length of one section so that it fits into the message
    if len(escaped) > 3000:
        escaped = escaped[:3000] + "…"

    title_html = f"<b>{html.escape(title)}</b>"
    block_html = (
        '<blockquote expandable>'
        f'<span class="tg-spoiler">{escaped}</span>'
        "</blockquote>"
    )

    return f"{title_html}\n{block_html}"


def _normalize_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    return []


def _normalize_news_items(value: Any) -> List[Dict[str, Any]]:
    """
    positive/negative/macro:
      -> [{ "text": str, "tickers": [str], "priority": "high|medium|low" }]
    """
    items: List[Dict[str, Any]] = []

    def _norm_priority(p: Any) -> str:
        p_str = str(p).lower()
        if p_str not in ("high", "medium", "low"):
            return "medium"
        return p_str

    def _norm_tickers(t: Any) -> List[str]:
        if isinstance(t, str):
            t_list = [t]
        elif isinstance(t, list):
            t_list = t
        else:
            return []
        return [str(x).upper().strip() for x in t_list if str(x).strip()]

    if isinstance(value, list):
        for it in value:
            if isinstance(it, str):
                text = it.strip()
                if not text:
                    continue
                items.append(
                    {"text": text, "tickers": [], "priority": "medium"}
                )
            elif isinstance(it, dict):
                text = str(it.get("text", "")).strip()
                if not text:
                    continue
                tickers = _norm_tickers(it.get("tickers", []))
                priority = _norm_priority(it.get("priority", "medium"))
                items.append(
                    {
                        "text": text,
                        "tickers": tickers,
                        "priority": priority,
                    }
                )
    elif isinstance(value, str):
        text = value.strip()
        if text:
            items.append(
                {"text": text, "tickers": [], "priority": "medium"}
            )

    return items


def _normalize_assets(value: Any) -> List[Dict[str, Any]]:
    """
    assets:
      -> [{ "ticker": str, "direction": "bullish|bearish|neutral",
            "reason": str, "priority": "high|medium|low" }]
    """
    assets: List[Dict[str, Any]] = []

    def _norm_priority(p: Any) -> str:
        p_str = str(p).lower()
        if p_str not in ("high", "medium", "low"):
            return "medium"
        return p_str

    def _norm_direction(d: Any) -> str:
        d_str = str(d).lower()
        if d_str not in ("bullish", "bearish", "neutral"):
            return "neutral"
        return d_str

    if isinstance(value, list):
        for it in value:
            if not isinstance(it, dict):
                continue
            ticker = str(it.get("ticker", "")).upper().strip()
            reason = str(it.get("reason", "")).strip()
            if not ticker or not reason:
                continue
            direction = _norm_direction(it.get("direction", "neutral"))
            priority = _norm_priority(it.get("priority", "medium"))
            assets.append(
                {
                    "ticker": ticker,
                    "direction": direction,
                    "reason": reason,
                    "priority": priority,
                }
            )

    return assets


def _priority_tag(priority: str) -> str:
    p = priority.lower()
    if p == "high":
        return "🔥🥇"
    if p == "low":
        return "🟨🥉"
    return "❗🥈"


def _direction_tag(direction: str) -> str:
    d = direction.lower()
    if d == "bullish":
        return "🟢"
    if d == "bearish":
        return "🔴"
    return "⚪"


def build_html_digest_from_json(data: Dict, emoji: str = "") -> str:
    """
    We assemble the final HTML from the model's JSON structure.

    Expected structure:
    {
      "summary": [...],
      "positive": [...],
      "negative": [...],
      "macro": [...],
      "assets": [...]
    }
    """
    summary_raw = data.get("summary")
    positive_raw = data.get("positive")
    negative_raw = data.get("negative")
    macro_raw = data.get("macro")
    assets_raw = data.get("assets")

    summary        = _normalize_str_list(summary_raw)
    positive_items = _normalize_news_items(positive_raw)
    negative_items = _normalize_news_items(negative_raw)
    macro_items    = _normalize_news_items(macro_raw)
    asset_items    = _normalize_assets(assets_raw)

    sections: List[str] = []

    # 1) Summary
    summary_lines: List[str] = []
    for s in summary:
        summary_lines.append(f"• {s}")
    
    summary_title = "Summary"
    if emoji:
        summary_title = f"{emoji} {summary_title}"
        
    sec = _render_blockquote_section(summary_title, summary_lines)
    if sec:
        sections.append(sec)

    # 2) Promising assets
    asset_lines: List[str] = []
    for a in asset_items:
        t = a["ticker"]
        direction = _direction_tag(a["direction"])
        priority = _priority_tag(a["priority"])
        reason = a["reason"]
        asset_lines.append(f"• [{t}] ({direction}, {priority}) {reason}")
    sec = _render_blockquote_section("Promising assets", asset_lines)
    if sec:
        sections.append(sec)

    # Helper for positive/negative/macro
    def build_news_lines(items: List[Dict[str, Any]]) -> List[str]:
        lines: List[str] = []
        for it in items:
            text = it["text"]
            priority = _priority_tag(it["priority"])
            tickers = it.get("tickers") or []
            tickers_str = ""
            if tickers:
                tickers_str = "<b>[" + ", ".join(tickers) + "]</b> "
            lines.append(f"• {priority} {tickers_str}{text}")
        return lines

    # 3) Positive news about projects
    positive_lines = build_news_lines(positive_items)
    sec = _render_blockquote_section("Positive news about projects", positive_lines)
    if sec:
        sections.append(sec)

    # 4) Negative news / pressure factors
    negative_lines = build_news_lines(negative_items)
    sec = _render_blockquote_section(
        "Negative news and price pressure factors", negative_lines
    )
    if sec:
        sections.append(sec)

    # 5) Macro events
    macro_lines = build_news_lines(macro_items)
    sec = _render_blockquote_section("Macro events", macro_lines)
    if sec:
        sections.append(sec)

    if not sections:
        return "No important news found in the selected period in the monitored channels."

    return "\n\n".join(sections)


# Prompt: ask for strict JSON in English with 5 sections
INSTRUCTIONS = (
    "You're a crypto market analyst. Your input is raw messages from"
    "telegram channels with news about crypto currencies.\n\n"
    "Your task:\n"
    "1) Extract general market context and make a conclusion: which coins/segments "
    "look better, which worse, what is currently pressuring prices, what supports "
    "the market (this will go into the summary block).\n"
    "2) Extract important positive news about projects/tokens "
    "(partnerships, network updates, listings, strong on-chain metrics, grants and more.) - this is the positive block.\n"
    "3) Extract negative news and factors that are pressuring prices of "
    "coins or segments (delistings, bans, hacks, investigations, negative "
    "regulatory decisions and more.) - this is the negative block.\n"
    "4) Extract important macro events, affecting the market as a whole (regulatory "
    "ETF, key interest rates, new laws, large hacks and bankruptcies, "
    "judicial decisions, large institutional entries/exits) - this is the macro block.\n"
    "5) List PERFORMANT ASSETS (assets): by which coins/tokens "
    "from the news have the strongest signal (bullish/ bearish/neutral). Combine "
    "signals by one coin into one short card.\n"
    "6) Ignore everything that looks like local noise, marketing noise or minor.\n\n"
    "Priority category:"
    "- high - urgently, requires trader's attention (strong impulse, large risk or opportunity).\n"
    "- medium - important, but not critical.\n"
    "- low - background information, useful to know, but not urgent.\n\n"
    "Format answer:\n"
    "- Answer STRICTLY in JSON format without any comments before or after.\n"
    "- JSON structure:\n"
    "{\n"
    '  \"summary\": [\"string1\", \"string2\"],\n'
    '  \"positive\": [\n'
    '    {\"text\": \"string\", \"tickers\": [\"BTC\", \"ETH\"], \"priority\": \"high\"}\n'
    "  ],\n"
    '  \"negative\": [\n'
    '    {\"text\": \"string\", \"tickers\": [\"SOL\"], \"priority\": \"medium\"}\n'
    "  ],\n"
    '  \"macro\": [\n'
    '    {\"text\": \"string\", \"tickers\": [], \"priority\": \"low\"}\n'
    "  ],\n"
    '  \"assets\": [\n'
    '    {\"ticker\": \"BTC\", \"direction\": \"bullish\", \"reason\": \"string\", \"priority\": \"high\"}\n'
    "  ]\n"
    "}\n"
    "- All strings (text, reason, summary) must be in English.\n"
    "- summary - 1–5 short strings of general market context and coin performance: "
    "who looks better/worse, due to which news, and general mood.\n"
    "- positive / negative / macro - lists of objects:\n"
    "    - text     - concise, but clear description of the news;\n"
    "    - tickers  - array of tickers (e.g. [\"BTC\", \"ETH\"]), if it is clear to which coins the news relates; "
    "if not - [];\n"
    "    - priority - one of the strings: \"high\", \"medium\", \"low\".\n"
    "- assets - list of objects:\n"
    "    - ticker    - main ticker of the coin/token (e.g. \"BTC\", \"SOL\");\n"
    "    - direction - \"bullish\" (bullish signal), \"bearish\" (bearish) or \"neutral\";\n"
    "    - reason    - concise summary, why this asset ended up on the list (by news over the period);\n"
    "    - priority  - \"high\" / \"medium\" / \"low\" by signal importance.\n"
    "- If any list is missing, return it as an empty array [].\n"
    "- Do not copy the entire text of messages verbatim: make a summary. Better less, but by the point.\n"
)


async def build_digest(
    posts: list[dict],
    *,
    save_report: bool = False,
    report_kind: str = "daily",
    period_start_utc: Optional[datetime] = None,
    period_end_utc: Optional[datetime] = None,
) -> tuple[str, Optional[Report]]:
    """
    Collects HTML digest of posts.
    If save_report=True and period_start_utc/period_end_utc are provided,
    then also saves the report to the reports table.
    """

    if not posts:
        return "There were no important news items found in the monitored channels for the selected period.", None

    # Collecting raw text messages
    raw_messages: list[str] = []
    for p in posts:
        ch = p.get("channel") or p.get("channel_name") or ""
        dt = p.get("date") or ""
        txt = p.get("text") or ""
        raw_messages.append(f"[{ch}] {dt}\n{txt}")

    joined = "\n\n---\n\n".join(raw_messages)

    # OpenAI call
    try:
        resp = await client.responses.create(
            model=settings.openai_model,
            instructions=INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": joined,
                        }
                    ],
                }
            ],
        )
        raw_json = resp.output[0].content[0].text
    except Exception as e:
        logger.exception("Error calling OpenAI for digest: %s", e)
        return "Error calling OpenAI, digest could not be built.", None

    # JSON parsing
    try:
        data = json.loads(raw_json)
    except Exception as e:
        logger.exception("Failed to parse JSON from model: %s. Raw: %r", e, raw_json)
        return "Model returned invalid JSON, digest could not be built.", None

    # HTML building
    try:
        emoji = ""
        if period_end_utc:
            emoji = get_holiday_emoji(period_end_utc) or ""
            
        html_digest = build_html_digest_from_json(data, emoji=emoji)
    except Exception as e:
        logger.exception("Error building HTML digest from JSON: %s", e)
        return "Error building HTML digest from JSON, digest could not be built.", None

    # Save report to DB (optional)
    report = None
    if save_report and period_start_utc and period_end_utc:
        try:
            report = create_report(
                kind=report_kind,
                period_start_utc=period_start_utc,
                period_end_utc=period_end_utc,
                json_content=raw_json,      # raw JSON from model
                html_content=html_digest,   # ready HTML for Telegram
                pdf_path=None,              # PDF will be added later
            )
        except Exception as e:
            logger.exception("Failed to save report to DB: %s", e)

    return html_digest, report
