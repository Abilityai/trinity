"""The provider contract. Input is PCM16 16 kHz mono; output PCM16 24 kHz mono, the
two rates the browser's mic path and player already use (`utils/audio.js`)."""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

INPUT_SAMPLE_RATE = 16000
# STT models drop trailing words from audio that ends without silence.
STT_PAD_SECONDS = 0.7


class SpeechError(Exception):
    """A provider failed. The message is for logs; callers speak fixed lines."""


@dataclass(frozen=True)
class Transcript:
    text: str


@runtime_checkable
class SpeechProvider(Protocol):
    name: str

    async def transcribe(self, pcm16_16k: bytes) -> Transcript: ...

    def synthesize_stream(self, text: str, voice: Optional[str] = None) -> AsyncIterator[bytes]: ...


def pcm_to_padded_wav(pcm16_16k: bytes) -> bytes:
    pad = b"\0" * (int(INPUT_SAMPLE_RATE * STT_PAD_SECONDS) * 2)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(INPUT_SAMPLE_RATE)
        w.writeframes(pad + pcm16_16k + pad)
    return out.getvalue()
