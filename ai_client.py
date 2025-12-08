from __future__ import annotations

from typing import Optional
import httpx
from openai import AsyncOpenAI

from config import settings

# Build HTTP client for OpenAI with optional proxy from .env
_http_client: Optional[httpx.AsyncClient] = None

if settings.openai_proxy:
    _http_client = httpx.AsyncClient(
        proxy=settings.openai_proxy,
        timeout=30.0,
    )
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        http_client=_http_client,
    )
else:
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
    )
