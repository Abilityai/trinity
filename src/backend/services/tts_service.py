"""Shared text-to-speech service — outbound voice messages across channels (epic #24).

Single provider-agnostic TTS layer the channel adapters call to speak an agent's
reply. #25 (Telegram) is the first consumer; #26 (Slack) and trinity-enterprise#56
(WhatsApp) reuse it unchanged — no per-channel TTS duplication.

Provider: ElevenLabs (`/v1/text-to-speech/{voice_id}`), returning MP3. Chat
voice-notes on Telegram/WhatsApp render from OGG/Opus, so `synthesize_voice_note`
transcodes MP3 → OGG/Opus via ffmpeg and returns the OGG bytes ready for
`sendVoice` / Twilio `MediaUrl`.

Design:
- **Fail-soft everywhere.** Every entry point returns ``None`` on any problem
  (no key, over the cost cap, provider error, ffmpeg missing/error). The caller
  treats ``None`` as "deliver text instead" — voice is strictly additive and must
  never break a reply.
- **Cost guardrail** is a shared char cap (``TTS_MAX_CHARS``): a reply longer than
  the cap is refused up front (``None``) so we never pay to synthesize an essay.
"""
import asyncio
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

_ELEVENLABS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
# ElevenLabs MP3 output; transcoded to OGG/Opus for the voice-note bubble.
_OUTPUT_FORMAT = "mp3_44100_128"
_HTTP_TIMEOUT = 30.0


def is_available() -> bool:
    """True when TTS can run at all (provider key configured)."""
    return bool(config.ELEVENLABS_API_KEY)


def _within_cost_cap(text: str) -> bool:
    return 0 < len(text) <= config.TTS_MAX_CHARS


async def synthesize_mp3(text: str, voice_id: str) -> Optional[bytes]:
    """Synthesize ``text`` to MP3 bytes via ElevenLabs. ``None`` on any failure
    or when the shared cost cap / guards reject it (caller falls back to text)."""
    if not is_available():
        return None
    if not voice_id:
        return None
    if not _within_cost_cap(text):
        logger.info(
            "TTS skipped: reply length %d outside cost cap (0, %d] — falling back to text",
            len(text), config.TTS_MAX_CHARS,
        )
        return None

    url = f"{_ELEVENLABS_BASE}/{voice_id}"
    headers = {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": config.ELEVENLABS_MODEL_ID,
        "output_format": _OUTPUT_FORMAT,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            # Body may carry the ElevenLabs error; never log the key (it's a header).
            logger.warning("TTS provider error %s: %s", resp.status_code, resp.text[:300])
            return None
        return resp.content
    except Exception as e:
        logger.warning("TTS request failed: %s", e)
        return None


async def to_ogg_opus(mp3_bytes: bytes) -> Optional[bytes]:
    """Transcode MP3 → OGG/Opus (the chat voice-note codec) via ffmpeg over pipes.
    ``None`` if ffmpeg is missing or errors (caller falls back to text)."""
    if not mp3_bytes:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-f", "ogg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found — cannot transcode TTS audio to OGG/Opus")
        return None
    except Exception as e:
        logger.warning("ffmpeg spawn failed: %s", e)
        return None

    try:
        stdout, stderr = await proc.communicate(input=mp3_bytes)
    except Exception as e:
        logger.warning("ffmpeg transcode failed: %s", e)
        return None
    if proc.returncode != 0 or not stdout:
        logger.warning("ffmpeg transcode error (rc=%s): %s", proc.returncode, stderr[:300])
        return None
    return stdout


async def synthesize_voice_note(text: str, voice_id: str) -> Optional[bytes]:
    """End-to-end: text → ElevenLabs MP3 → OGG/Opus voice-note bytes.

    Returns OGG/Opus bytes ready for Telegram ``sendVoice`` / Twilio ``MediaUrl``,
    or ``None`` at any failure point (no key, over cap, provider error, transcode
    failure) so the caller delivers text instead.
    """
    mp3 = await synthesize_mp3(text, voice_id)
    if mp3 is None:
        return None
    return await to_ogg_opus(mp3)
