"""ElevenLabs speech: Scribe for transcription and the streaming speech endpoint
with raw PCM output."""
from __future__ import annotations

from typing import AsyncIterator, Optional

import httpx

from .base import SpeechError, Transcript, pcm_to_padded_wav

_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_STT_MODEL = "scribe_v1"
_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_TTS_TIMEOUT = 30.0
_STT_TIMEOUT = 30.0
_CHUNK_BYTES = 4800


class ElevenLabsSpeech:
    name = "elevenlabs"

    def __init__(self, api_key: str, default_voice: Optional[str] = None):
        self._key = api_key
        self._default_voice = default_voice

    async def transcribe(self, pcm16_16k: bytes) -> Transcript:
        wav = pcm_to_padded_wav(pcm16_16k)
        try:
            async with httpx.AsyncClient(timeout=_STT_TIMEOUT) as client:
                resp = await client.post(
                    _STT_URL, headers={"xi-api-key": self._key}, data={"model_id": _STT_MODEL},
                    files={"file": ("speech.wav", wav, "audio/wav")},
                )
        except httpx.HTTPError as e:
            raise SpeechError(f"elevenlabs stt request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SpeechError(f"elevenlabs stt returned {resp.status_code}")
        return Transcript(text=((resp.json() or {}).get("text") or "").strip())

    async def synthesize_stream(self, text: str, voice: Optional[str] = None) -> AsyncIterator[bytes]:
        import config
        voice_id = voice or self._default_voice
        if not voice_id:
            raise SpeechError("elevenlabs has no voice to speak with")
        spill = b""
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as client:
                async with client.stream(
                    "POST", f"{_TTS_URL}/{voice_id}/stream?output_format=pcm_24000",
                    headers={"xi-api-key": self._key, "content-type": "application/json"},
                    json={"text": text, "model_id": config.ELEVENLABS_MODEL_ID},
                ) as resp:
                    if resp.status_code != 200:
                        raise SpeechError(f"elevenlabs tts returned {resp.status_code}")
                    async for chunk in resp.aiter_bytes(_CHUNK_BYTES):
                        pcm = spill + chunk
                        even = len(pcm) & ~1
                        spill = pcm[even:]
                        if even:
                            yield pcm[:even]
        except httpx.HTTPError as e:
            raise SpeechError(f"elevenlabs tts failed: {type(e).__name__}") from e
