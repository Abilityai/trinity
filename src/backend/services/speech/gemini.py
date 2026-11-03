"""Gemini speech over the REST API: one transcription request per utterance, and
server-sent audio per sentence."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import AsyncIterator, Optional

import httpx

from .base import SpeechError, Transcript, pcm_to_padded_wav

logger = logging.getLogger(__name__)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
_STT_PROMPT = "Transcribe this audio verbatim, in the language spoken. Output only the transcript."
_STT_TIMEOUT = 20.0
_TTS_TIMEOUT = 30.0
_OUTPUT_RATE = 24000


def _part_text(part: dict) -> str:
    """A transcribe model answers in `audioTranscription.text`; a general model in
    `text`. Both measured live, 2026-11-02. A thought part is never the transcript."""
    if not isinstance(part, dict) or part.get("thought"):
        return ""
    heard = part.get("audioTranscription")
    if isinstance(heard, dict):
        return heard.get("text") or ""
    return part.get("text") or ""


class GeminiSpeech:
    name = "gemini"

    def __init__(self, api_key: str, *, stt_model: Optional[str] = None,
                 tts_models: Optional[tuple] = None, default_voice: Optional[str] = None):
        import config
        self._key = api_key
        self._stt_model = stt_model or config.GEMINI_STT_MODEL
        self._tts_models = tts_models or config.GEMINI_TTS_MODELS
        self._default_voice = default_voice or config.DEFAULT_VOICE_NAME

    def _headers(self) -> dict:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}

    async def transcribe(self, pcm16_16k: bytes) -> Transcript:
        wav = base64.b64encode(pcm_to_padded_wav(pcm16_16k)).decode("ascii")
        body = {"contents": [{"parts": [
            {"text": _STT_PROMPT},
            {"inlineData": {"mimeType": "audio/wav", "data": wav}},
        ]}]}
        try:
            async with httpx.AsyncClient(timeout=_STT_TIMEOUT) as client:
                resp = await client.post(f"{_BASE}{self._stt_model}:generateContent",
                                         headers=self._headers(), json=body)
        except httpx.HTTPError as e:
            raise SpeechError(f"gemini stt request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise SpeechError(f"gemini stt returned {resp.status_code}")
        parts = ((resp.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return Transcript(text=" ".join(_part_text(p) for p in parts).strip())

    async def synthesize_stream(self, text: str, voice: Optional[str] = None) -> AsyncIterator[bytes]:
        last: Optional[SpeechError] = None
        for model in self._tts_models:
            got_audio = False
            try:
                async for chunk in self._stream_model(model, text, voice or self._default_voice):
                    got_audio = True
                    yield chunk
                if got_audio:
                    return
                last = SpeechError(f"gemini tts {model} returned no audio")
            except SpeechError as e:
                if got_audio:
                    raise
                last = e
        raise last or SpeechError("no gemini tts model configured")

    async def _stream_model(self, model: str, text: str, voice: str) -> AsyncIterator[bytes]:
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }
        spill = b""
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as client:
                async with client.stream("POST", f"{_BASE}{model}:streamGenerateContent?alt=sse",
                                         headers=self._headers(), json=body) as resp:
                    if resp.status_code != 200:
                        raise SpeechError(f"gemini tts {model} returned {resp.status_code}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        event = json.loads(line[5:])
                        parts = ((event.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                        for part in parts:
                            inline = part.get("inlineData")
                            if not inline:
                                continue
                            rate = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
                            if rate and int(rate.group(1)) != _OUTPUT_RATE:
                                raise SpeechError(f"gemini tts {model} returned rate {rate.group(1)}")
                            pcm = spill + base64.b64decode(inline["data"])
                            even = len(pcm) & ~1
                            spill = pcm[even:]
                            if even:
                                yield pcm[:even]
        except (httpx.HTTPError, ValueError) as e:
            raise SpeechError(f"gemini tts {model} failed: {type(e).__name__}") from e
