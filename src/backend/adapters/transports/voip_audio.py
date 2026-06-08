"""
Pure audio codec helpers for the VoIP Media Streams bridge (VOIP-001, #1056).

Kept dependency-free (only stdlib `audioop`) so it is unit-testable in isolation
without importing the heavy bridge (fastapi / google-genai / database). The
caller carries the per-direction `ratecv` state across chunks — that is what
prevents a filter-reset click at every chunk boundary.

`audioop` is stdlib on Python 3.11 (the backend image) and removed in 3.13; the
backend Dockerfile pins `audioop-lts` for `python_version >= "3.13"`.
"""

import audioop

# Twilio Media Streams carry G.711 μ-law, 8kHz, mono — 160 bytes = 20ms/frame.
TWILIO_SAMPLE_RATE = 8000
GEMINI_IN_RATE = 16000     # PCM16 the Gemini bridge expects on input
GEMINI_OUT_RATE = 24000    # PCM16 the Gemini bridge emits on output
SAMPLE_WIDTH = 2           # 16-bit linear PCM
FRAME_BYTES = 160          # 20ms of μ-law @ 8kHz


def ulaw8k_to_pcm16k(mulaw: bytes, state):
    """Inbound: Twilio μ-law 8kHz → PCM16 16kHz. Returns (pcm16, new_state)."""
    pcm8 = audioop.ulaw2lin(mulaw, SAMPLE_WIDTH)
    return audioop.ratecv(pcm8, SAMPLE_WIDTH, 1, TWILIO_SAMPLE_RATE, GEMINI_IN_RATE, state)


def pcm24k_to_ulaw8k(pcm24: bytes, state):
    """Outbound: Gemini PCM16 24kHz → μ-law 8kHz. Returns (mulaw, new_state).

    Direct 24k→8k decimation (exact 3:1) — no intermediate 16k hop (which would
    compound filter error and need a third state tuple for nothing).
    """
    pcm8, state = audioop.ratecv(pcm24, SAMPLE_WIDTH, 1, GEMINI_OUT_RATE, TWILIO_SAMPLE_RATE, state)
    return audioop.lin2ulaw(pcm8, SAMPLE_WIDTH), state


def pop_frames(buffer: bytearray):
    """Pop all complete 160-byte μ-law frames from `buffer` (mutates it)."""
    frames = []
    while len(buffer) >= FRAME_BYTES:
        frames.append(bytes(buffer[:FRAME_BYTES]))
        del buffer[:FRAME_BYTES]
    return frames
