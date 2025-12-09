from __future__ import annotations
import logging
import asyncio
import json
from typing import Any, Dict, List

from db import get_cached_translation, save_translation
from ai_client import client
from config import settings

logger = logging.getLogger(__name__)

async def get_translation(text: str, target_lang: str) -> str:
    """
    Get translation of text to target_lang.
    """
    if not text:
        return ""
    
    lang = target_lang.split("-")[0].lower()
    
    # Skip unsupported languages
    if lang not in settings.allowed_languages:
        return text

    cached = get_cached_translation(text, lang)
    if cached:
        return cached
    
    try:
        translated = await _fetch_translation(text, lang)
        save_translation(text, lang, translated)
        return translated
    except Exception as e:
        logger.error(f"Translation error to {lang}: {e}")
        return text

async def _fetch_translation(text: str, lang: str) -> str:
    system_instruction = (
        f"Translate the following text into language code '{lang}'. "
        "Preserve all HTML tags (like <b>, <a>, etc.) and emojis. "
        "Do not interpret the text, just translate it. "
        "Return ONLY the translated text, no markdown block."
    )
    
    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()
    return content

async def translate_report_data(data: Dict[str, Any], target_lang: str) -> Dict[str, Any]:
    """
    Deeply translate specific fields in the report JSON structure.
    """
    new_data = json.loads(json.dumps(data))
    
    tasks = []
    
    # Summary
    if "summary" in new_data and isinstance(new_data["summary"], list):
        for i, s in enumerate(new_data["summary"]):
            if isinstance(s, str):
                tasks.append((new_data["summary"], i, s))
                
    # Pos/Neg/Macro
    for key in ["positive", "negative", "macro"]:
        if key in new_data and isinstance(new_data[key], list):
            for item in new_data[key]:
                if isinstance(item, dict) and "text" in item:
                    tasks.append((item, "text", item["text"]))

    # Assets
    if "assets" in new_data and isinstance(new_data["assets"], list):
        for item in new_data["assets"]:
            if isinstance(item, dict) and "reason" in item:
                tasks.append((item, "reason", item["reason"]))
                
    if not tasks:
        return new_data

    # Execute
    results = await asyncio.gather(*[get_translation(t[2], target_lang) for t in tasks])
    
    # Apply
    for task, res in zip(tasks, results):
        container, key, _ = task
        container[key] = res
        
    return new_data
