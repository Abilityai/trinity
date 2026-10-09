"""
Telegram media download and processing service.

Handles:
- File download via getFile API
- Voice message transcription via Gemini API
- SSRF prevention: only downloads from api.telegram.org
- File size limits: 20MB max (10MB for voice)

Photos and documents arrive as FileAttachment via
upload_service.process_file_uploads, not through this module.
"""

import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import GEMINI_TRANSCRIPTION_MODEL

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB — Telegram bot API limit
MAX_VOICE_SIZE = 10 * 1024 * 1024  # 10MB — soft limit for voice transcription
MAX_VOICE_DURATION = 300  # 5 minutes max for voice transcription
ALLOWED_DOWNLOAD_HOST = "api.telegram.org"


async def download_telegram_file(bot_token: str, file_id: str) -> Optional[bytes]:
    """
    Download a file from Telegram via getFile API.

    SSRF prevention: only downloads from api.telegram.org.
    Size limit: 20MB (Telegram bot API limit).
    """
    # Step 1: Get file path from Telegram
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json={"file_id": file_id})
            if resp.status_code != 200:
                logger.error(f"Telegram getFile failed: {resp.text}")
                return None

            result = resp.json()
            if not result.get("ok"):
                logger.error(f"Telegram getFile error: {result.get('description')}")
                return None

            file_path = result["result"].get("file_path")
            file_size = result["result"].get("file_size", 0)

            if not file_path:
                logger.error("Telegram getFile returned no file_path")
                return None

            # Enforce size limit
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"Telegram file too large: {file_size} bytes (max {MAX_FILE_SIZE})")
                return None

    except Exception as e:
        logger.error(f"Telegram getFile request error: {e}", exc_info=True)
        return None

    # Step 2: Download the file
    download_url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"

    # SSRF check: verify the download URL points to api.telegram.org
    parsed = urlparse(download_url)
    if parsed.hostname != ALLOWED_DOWNLOAD_HOST:
        logger.error(f"SSRF blocked: download URL host {parsed.hostname} is not {ALLOWED_DOWNLOAD_HOST}")
        return None

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(download_url)
            if resp.status_code != 200:
                logger.error(f"Telegram file download failed: {resp.status_code}")
                return None

            content = resp.content
            if len(content) > MAX_FILE_SIZE:
                logger.warning(f"Downloaded file exceeds size limit: {len(content)} bytes")
                return None

            return content
    except Exception as e:
        logger.error(f"Telegram file download error: {e}", exc_info=True)
        return None


async def process_voice(bot_token: str, voice: dict) -> str:
    """
    Download a voice message and transcribe it via Gemini API.

    Limits:
    - Duration: 5 minutes max (300 seconds)
    - File size: 10MB max

    Returns transcribed text with 🎙️ prefix, or error placeholder.
    """
    file_id = voice.get("file_id")
    duration = voice.get("duration", 0)
    file_size = voice.get("file_size", 0)

    if not file_id:
        return "[Voice message received but file_id missing]"

    # Check duration limit
    if duration > MAX_VOICE_DURATION:
        minutes = duration // 60
        return f"[Voice message too long ({minutes}+ minutes) — transcription limit is 5 minutes]"

    # Check file size before download
    if file_size > MAX_VOICE_SIZE:
        size_mb = file_size / (1024 * 1024)
        return f"[Voice message too large ({size_mb:.1f}MB) — transcription limit is 10MB]"

    # Check if Gemini API is configured (resolved per call — Settings → env, ent#582)
    from services.settings_service import get_gemini_api_key
    if not get_gemini_api_key():
        return "[Voice message received — transcription not available (API not configured)]"

    # Download the voice file
    data = await download_telegram_file(bot_token, file_id)
    if not data:
        return "[Voice message received but could not be downloaded]"

    # Double-check size after download
    if len(data) > MAX_VOICE_SIZE:
        return "[Voice message too large for transcription]"

    # Transcribe via Gemini
    transcript = await _transcribe_audio_gemini(data, voice.get("mime_type", "audio/ogg"))
    if transcript:
        return f"🎙️ \"{transcript}\""
    else:
        return "[Voice message received — transcription failed]"


async def _transcribe_audio_gemini(audio_data: bytes, mime_type: str = "audio/ogg") -> Optional[str]:
    """
    Transcribe audio using Gemini API.

    Uses Gemini's multimodal capability to process inline audio data.
    """
    try:
        from google import genai
        from services.settings_service import get_gemini_api_key

        client = genai.Client(api_key=get_gemini_api_key())

        response = await client.aio.models.generate_content(
            model=GEMINI_TRANSCRIPTION_MODEL,
            contents=[
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": audio_data,
                            }
                        },
                        {
                            "text": "Transcribe this audio message. Output only the transcribed text, nothing else. If no speech is detected, respond with [no speech detected]."
                        },
                    ]
                }
            ],
        )

        if response and response.text:
            transcript = response.text.strip()
            if transcript.lower() == "[no speech detected]":
                return None
            return transcript

        return None

    except Exception as e:
        # 404 NOT_FOUND here usually means Google retired the model (#1130) —
        # recoverable by config, no code change needed.
        if "404" in str(e) or "NOT_FOUND" in str(e):
            logger.error(
                f"Gemini transcription error: model '{GEMINI_TRANSCRIPTION_MODEL}' "
                f"not found — it may have been retired by Google. Set the "
                f"GEMINI_TRANSCRIPTION_MODEL env var to a current model. {e}"
            )
        else:
            logger.error(f"Gemini transcription error: {e}", exc_info=True)
        return None
