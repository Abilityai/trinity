"""Which provider a Direct call uses. Resolved per call and never cached, so a key or
setting saved in Settings applies to the next call without a restart."""
from __future__ import annotations

from typing import Optional

from .base import SpeechProvider

AUTO = "auto"
GEMINI = "gemini"
ELEVENLABS = "elevenlabs"
PROVIDER_CHOICES = (AUTO, GEMINI, ELEVENLABS)
_DISPLAY = {GEMINI: "Gemini", ELEVENLABS: "ElevenLabs"}

REASON_NO_KEY = "Direct calls need a speech key. Add a Gemini or ElevenLabs key in Settings."
REASON_NO_GEMINI = "Direct calls are set to Gemini, and no Gemini key is configured."
REASON_NO_ELEVENLABS = "Direct calls are set to ElevenLabs, and it has no key or default voice."


def _build_gemini() -> Optional[SpeechProvider]:
    from services.settings_service import get_gemini_api_key
    key = get_gemini_api_key()
    if not key:
        return None
    from .gemini import GeminiSpeech
    return GeminiSpeech(key)


def _build_elevenlabs() -> Optional[SpeechProvider]:
    from services.settings_service import settings_service
    key = settings_service.get_elevenlabs_api_key()
    voice = settings_service.get_default_voice_id()
    if not key or not voice:
        return None
    from .elevenlabs import ElevenLabsSpeech
    return ElevenLabsSpeech(key, default_voice=voice)


def resolve_direct_provider(choice: Optional[str] = None) -> Optional[SpeechProvider]:
    """The provider for a call, or None when the chosen one is not ready. `auto`
    takes the first ready, Gemini first, since Live calls already need that key."""
    if choice is None:
        from services.settings_service import settings_service
        choice = settings_service.get_direct_voice_provider()
    if choice == GEMINI:
        return _build_gemini()
    if choice == ELEVENLABS:
        return _build_elevenlabs()
    return _build_gemini() or _build_elevenlabs()


def direct_voice_readiness(choice: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """`(provider display name, None)` when ready, `(None, reason)` when not."""
    if choice is None:
        from services.settings_service import settings_service
        choice = settings_service.get_direct_voice_provider()
    provider = resolve_direct_provider(choice)
    if provider is not None:
        return _DISPLAY.get(provider.name, provider.name), None
    if choice == GEMINI:
        return None, REASON_NO_GEMINI
    if choice == ELEVENLABS:
        return None, REASON_NO_ELEVENLABS
    return None, REASON_NO_KEY
