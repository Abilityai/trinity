"""
Unit tests for the VoIP audio codec helpers (VOIP-001, #1056).

Covers the load-bearing correctness of the Twilio↔Gemini audio bridge:
  - inbound  μ-law 8kHz → PCM16 16kHz
  - outbound PCM16 24kHz → μ-law 8kHz (direct 3:1 decimation)
  - stateful `ratecv` continuity across chunk boundaries (no per-chunk reset →
    no audible click), the single most common failure mode of naïve bridges
  - 160-byte/20ms frame slicing for Twilio Media Streams

Module: src/backend/adapters/transports/voip_audio.py

`audioop` is stdlib on Python 3.11 (the backend image) but removed in 3.13;
this module is skipped where neither stdlib audioop nor `audioop-lts` is present.
"""

import math
import struct
import sys
from pathlib import Path

import pytest

# Skip cleanly on a host that has neither stdlib audioop (≤3.12) nor audioop-lts.
pytest.importorskip("audioop")

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from adapters.transports.voip_audio import (  # noqa: E402
    FRAME_BYTES,
    pcm24k_to_ulaw8k,
    pop_frames,
    ulaw8k_to_pcm16k,
)


def _sine_pcm16(freq_hz: float, rate: int, n_samples: int) -> bytes:
    """n_samples of a 16-bit PCM sine at freq_hz sampled at `rate`."""
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq_hz * i / rate)))
        for i in range(n_samples)
    )


def _ulaw_from_pcm8k(freq_hz: float, n_samples: int) -> bytes:
    import audioop
    pcm = _sine_pcm16(freq_hz, 8000, n_samples)
    return audioop.lin2ulaw(pcm, 2)


class TestInbound:
    def test_ulaw8k_to_pcm16k_roughly_doubles_samples(self):
        """160 μ-law bytes (20ms @ 8kHz) → ~640 bytes PCM16 (320 samples @ 16kHz).
        ratecv warm-up may drop ~1 sample, so allow a few bytes of slack."""
        mulaw = _ulaw_from_pcm8k(300, 160)  # 160 samples = 20ms
        pcm16, state = ulaw8k_to_pcm16k(mulaw, None)
        # 160 μ-law samples → 320 PCM8k bytes → ~320 samples @16k → ~640 bytes
        assert abs(len(pcm16) - 640) <= 6, len(pcm16)
        assert state is not None

    def test_inbound_preserves_signal_energy(self):
        """A non-silent input must yield non-silent output (rms > 0)."""
        import audioop
        mulaw = _ulaw_from_pcm8k(440, 320)
        pcm16, _ = ulaw8k_to_pcm16k(mulaw, None)
        assert audioop.rms(pcm16, 2) > 0


class TestOutbound:
    def test_pcm24k_to_ulaw8k_thirds_the_samples(self):
        """480 samples PCM24k (20ms @ 24kHz, 960 bytes) → ~160 μ-law bytes."""
        pcm24 = _sine_pcm16(300, 24000, 480)
        mulaw, state = pcm24k_to_ulaw8k(pcm24, None)
        assert abs(len(mulaw) - 160) <= 4, len(mulaw)
        assert state is not None


class TestStatefulContinuity:
    # Misaligned chunk size (962 bytes = 481 samples, NOT a multiple of the 3:1
    # ratio) is the realistic case — Gemini emits arbitrary-size PCM chunks, so
    # boundaries rarely land on a clean sample multiple. That's exactly where
    # carrying ratecv state matters.
    _MISALIGNED = 962

    def test_carried_state_is_byte_for_byte_identical_to_single_pass(self):
        """The anti-click guarantee: converting a stream in misaligned chunks
        while carrying ratecv state produces output **byte-for-byte identical**
        to a single-pass conversion — no boundary discontinuity at all."""
        pcm24 = _sine_pcm16(330, 24000, 5000)
        one, _ = pcm24k_to_ulaw8k(pcm24, None)
        out = bytearray()
        state = None
        for i in range(0, len(pcm24), self._MISALIGNED):
            mulaw, state = pcm24k_to_ulaw8k(pcm24[i:i + self._MISALIGNED], state)
            out.extend(mulaw)
        assert bytes(out) == one  # identical samples → no click

    def test_stateless_chunking_diverges_from_single_pass(self):
        """Control: WITHOUT carried state, each misaligned chunk re-runs filter
        warm-up and drops the fractional-sample carry — the output diverges from
        the single-pass, proving the state is load-bearing (a click per boundary)."""
        pcm24 = _sine_pcm16(330, 24000, 5000)
        one, _ = pcm24k_to_ulaw8k(pcm24, None)
        out = bytearray()
        for i in range(0, len(pcm24), self._MISALIGNED):
            mulaw, _ = pcm24k_to_ulaw8k(pcm24[i:i + self._MISALIGNED], None)  # reset each chunk
            out.extend(mulaw)
        assert bytes(out) != one


class TestFraming:
    def test_pop_frames_slices_160_byte_frames(self):
        buf = bytearray(b"\x7f" * 400)  # 2 full frames + 80 remainder
        frames = pop_frames(buf)
        assert len(frames) == 2
        assert all(len(f) == FRAME_BYTES for f in frames)
        assert len(buf) == 80  # remainder carried for the next round

    def test_pop_frames_empty_when_under_one_frame(self):
        buf = bytearray(b"\x00" * 159)
        assert pop_frames(buf) == []
        assert len(buf) == 159
