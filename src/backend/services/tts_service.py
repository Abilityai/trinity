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


def _resolve_api_key() -> str:
    """Resolve the ElevenLabs key at call time via the runtime settings resolver
    (stored setting → env → ''). ent#117 made the key runtime-configurable, so we
    never read the frozen ``config.ELEVENLABS_API_KEY`` directly."""
    from services.settings_service import settings_service
    return settings_service.get_elevenlabs_api_key()


def is_available() -> bool:
    """True when TTS can run at all (provider key configured)."""
    return bool(_resolve_api_key())


def resolve_voice_id(agent_name: str, *, default_voice_id: Optional[str] = None) -> Optional[str]:
    """The voice an agent speaks with, or ``None`` when it may not speak at all (#2157).

    ONE gate for every surface that turns an agent's words into audio, so the
    channel path (``send_voice_reply``) and the Workspace narration path can no
    longer disagree about whether voice is on for an agent:

      platform TTS key  AND  agent-level ``tts_voice_replies_enabled``
                        AND  (the agent's own ``tts_voice_id`` else the platform default)

    Per-channel flags stay with the channel path — the Workspace is not a
    messaging channel and has no per-channel row. ``default_voice_id`` lets a
    caller that already read the platform default (a roster loop) pass it in
    rather than re-resolving it per agent.

    Never raises: any lookup failure resolves to ``None`` (no voice), because the
    fail-safe answer for "may this agent be spoken aloud" is no.
    """
    try:
        if not is_available():
            return None
        from database import db

        cfg = db.get_tts_config(agent_name)
        if default_voice_id is None:
            from services.settings_service import settings_service
            default_voice_id = settings_service.get_default_voice_id()
        return resolve_voice_from_config(
            enabled=bool(cfg.get("enabled")),
            voice_id=cfg.get("voice_id"),
            default_voice_id=default_voice_id,
        )
    except Exception as e:  # noqa: BLE001 — a voice lookup never breaks a turn
        logger.warning("voice resolution failed for %s: %s", agent_name, e)
        return None


def resolve_voice_from_config(
    *, enabled: bool, voice_id: Optional[str], default_voice_id: Optional[str]
) -> Optional[str]:
    """The pure predicate behind :func:`resolve_voice_id` (#2157), for a caller
    that already holds the agent's voice columns — a roster query reads them for
    every agent at once and must not turn that into an N+1 of per-agent lookups.
    Both spellings therefore decide by the same rule, and cannot drift."""
    if not enabled:
        return None
    return (voice_id or "").strip() or (default_voice_id or "").strip() or None


def _within_cost_cap(text: str) -> bool:
    return 0 < len(text) <= config.TTS_MAX_CHARS


async def synthesize_mp3(text: str, voice_id: str) -> Optional[bytes]:
    """Synthesize ``text`` to MP3 bytes via ElevenLabs. ``None`` on any failure
    or when the shared cost cap / guards reject it (caller falls back to text)."""
    api_key = _resolve_api_key()
    if not api_key:
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
        "xi-api-key": api_key,
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
