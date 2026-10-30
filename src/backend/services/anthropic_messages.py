"""One call to the Anthropic Messages API on the platform key.

The MEM-001 summarizer (`platform_prompt_service.summarize_user_memory_background`)
and the trinity-enterprise#1022 retrieval memory both need "send one prompt to
Haiku on the platform key, get the text back, never raise". This is that call,
in one place, so the key path, the headers and the error handling do not fork.

Returns ``None`` on any failure (no key, non-200, malformed body, network
error) after logging a bounded line — never the key, never the prompt.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


def has_platform_key() -> bool:
    from services.settings_service import get_anthropic_api_key
    try:
        return bool(get_anthropic_api_key())
    except Exception:  # noqa: BLE001 — a settings read failure reads as "no key"
        return False


async def complete(
    prompt: str, *, max_tokens: int = 512, model: str = HAIKU_MODEL,
    timeout: float = 30.0, tag: str = "anthropic",
) -> Optional[str]:
    """The model's text for ``prompt`` as a single user message, or None."""
    from services.settings_service import get_anthropic_api_key

    api_key = get_anthropic_api_key()
    if not api_key:
        logger.warning("[%s] No ANTHROPIC_API_KEY configured, skipping", tag)
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                _API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
    except Exception as e:  # noqa: BLE001 — network failure is a logged None
        logger.error("[%s] Anthropic API request failed: %s", tag, e)
        return None
    if response.status_code != 200:
        logger.error("[%s] Anthropic API error %s: %s", tag, response.status_code, response.text[:200])
        return None
    try:
        data = response.json()
        return (data.get("content", [{}])[0].get("text", "") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.error("[%s] Anthropic API response unreadable: %s", tag, e)
        return None
