# news_fetcher.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import snscrape.modules.telegram as telegram

from config import settings


def _extract_message_id_from_url(url: str) -> Optional[int]:
    """
    Extract numeric message id from TelegramPost.url, if possible.

    Examples of URLs:
      - https://t.me/channel/12345
      - https://t.me/s/channel/12345
      - https://t.me/channel/12345?single
      - https://t.me/s/channel/12345?something=1

    Returns:
      int message id or None if it cannot be parsed.
    """
    # Drop query params
    base = url.split("?", 1)[0].rstrip("/")
    last_part = base.rsplit("/", 1)[-1]
    try:
        return int(last_part)
    except ValueError:
        return None


def _fetch_recent_from_channel(
    channel: str,
    max_posts: int,
    max_hours: int,
) -> List[Dict]:
    """
    Scrape last messages from a public Telegram channel using snscrape.

    channel: channel username without https://t.me/ (e.g. "channel1").
    """
    scraper = telegram.TelegramChannelScraper(channel)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    items: List[Dict] = []

    for msg in scraper.get_items():
        # msg.date: datetime with tz
        if msg.date < cutoff:
            break

        content = (msg.content or "").strip()
        if not content:
            continue

        url = msg.url  # use URL provided by snscrape
        msg_id = _extract_message_id_from_url(url)

        items.append(
            {
                # Keep id as parsed from URL (may be None, but we don't strictly need it)
                "id": msg_id,
                "date": msg.date,
                "channel": channel,
                "text": content,
                "url": url,
            }
        )

        if len(items) >= max_posts:
            break

    return items


def fetch_all_channels() -> List[Dict]:
    """
    Fetch messages from all configured channels
    for the last settings.max_hours hours.
    """
    all_msgs: List[Dict] = []
    for ch in settings.channels:
        all_msgs.extend(
            _fetch_recent_from_channel(
                channel=ch,
                max_posts=settings.max_posts_per_channel,
                max_hours=settings.max_hours,
            )
        )

    # Sort by date (newest first)
    all_msgs.sort(key=lambda m: m["date"], reverse=True)
    return all_msgs
