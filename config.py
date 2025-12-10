from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple, Optional

from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()


def _parse_channels(raw: str) -> Tuple[str, ...]:
    return tuple(
        ch.strip()
        for ch in raw.split(",")
        if ch.strip()
    )


def _parse_launch_times(raw: str) -> Tuple[str, ...]:
    return tuple(
        t.strip()
        for t in raw.split(",")
        if t.strip()
    )


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # Optional HTTP proxy for OpenAI requests
    openai_proxy: Optional[str] = (
        os.getenv("OPENAI_PROXY", "").strip() or None
    )

    # Channels to scrape (without https://t.me/)
    channels: Tuple[str, ...] = _parse_channels(
        os.getenv("CHANNELS", "channel1,channel2")
    )

    # SnScrape lookback
    max_hours: int = int(os.getenv("MAX_HOURS", "6"))
    max_posts_per_channel: int = int(os.getenv("MAX_POSTS_PER_CHANNEL", "40"))

    # OpenAI model
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    # LLM Provider
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai").lower().strip()

    # DeepSeek
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # Global digest interval (minutes) — how often we send digest to users
    digest_interval_min: int = int(os.getenv("DIGEST_INTERVAL_MINUTES", "60"))

    # Start hour for digest schedule (0-23 UTC) — digest grid is aligned to this hour
    digest_start_hour: int = int(os.getenv("DIGEST_START_HOUR", "4"))

    # Specific launch times (UTC HH:MM) like "08:00,16:00,23:59"
    # If set, overrides DIGEST_INTERVAL_MINUTES and DIGEST_START_HOUR
    digest_launch_times: Tuple[str, ...] = _parse_launch_times(
        os.getenv("DIGEST_LAUNCH_TIMES", "")
    )

    # Subscription settings
    subscription_price_stars: int = int(os.getenv("SUBSCRIPTION_PRICE_STARS", "10"))
    subscription_period_days: int = int(os.getenv("SUBSCRIPTION_PERIOD_DAYS", "30"))
    renewal_reminder_days: int = int(os.getenv("RENEWAL_REMINDER_DAYS", "3"))

    # SQLite DB path
    db_path: str = os.getenv("DB_PATH", "bot.db")

    # Optional: initial admin user id (Telegram ID). If set, this user will be admin on first /start.
    initial_admin_id: Optional[int] = (
        int(os.getenv("INITIAL_ADMIN_ID")) if os.getenv("INITIAL_ADMIN_ID") else None
    )

    # Allowed languages for translation
    allowed_languages: Tuple[str, ...] = _parse_channels(
        os.getenv("ALLOWED_LANGUAGES", "ru,en,ua")
    )


settings = Settings()

if not settings.telegram_bot_token:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

if not settings.openai_api_key and settings.llm_provider == "openai":
    raise RuntimeError("OPENAI_API_KEY is not set in .env")

if settings.llm_provider == "deepseek" and not settings.deepseek_api_key:
    raise RuntimeError("DEEPSEEK_API_KEY is not set in .env (required for deepseek provider)")

if not settings.channels:
    raise RuntimeError("CHANNELS is empty or not set in .env")
