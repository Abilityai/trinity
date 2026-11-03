"""trinity-enterprise#1087: interrupting a Direct call mid-reply.

The browser side lives in `src/frontend/tests/unit/directCallInterrupt.spec.js`. Pinned
here: once the person has stopped the agent, no audio from the speech that was cut may
reach the browser. The interrupt is handled on the same loop as the speaker, so it can land
while `_speak_sentence` is awaiting its `spoken` send, after the generation check that
preceded it. A chunk sent after that would arrive after `audio_flush` and play.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import direct_voice_service as dvs  # noqa: E402


class InterruptingSocket:
    """Delivers the person's interrupt while the bridge is mid-send, as the socket loop can."""

    def __init__(self, on):
        self.frames = []
        self.bridge = None
        self.on = on
        self.pending = []

    async def send_json(self, frame):
        self.frames.append(frame)
        if frame["type"] == self.on and self.bridge is not None and not self.pending:
            # The receive loop is its own task: the interrupt runs while this send is
            # still in flight, and its own `audio_flush` waits for the send lock.
            self.pending.append(asyncio.ensure_future(self.bridge.handle({"type": "interrupt"})))
            await asyncio.sleep(0)

    def of(self, kind):
        return [f for f in self.frames if f["type"] == kind]


class ChunkyProvider:
    name = "fake"

    async def synthesize_stream(self, text, voice=None):
        for _ in range(3):
            yield b"\x00\x01"


def _session():
    return dvs.DirectVoiceSession(session_id="vd_x", agent_name="scribe", user_id=1,
                                  client_email="alice@example.com", portal_session_id="thread",
                                  max_duration=60)


@pytest.fixture(autouse=True)
def _no_scrub(monkeypatch):
    monkeypatch.setattr("services.runtime_secret_scrub.get_staged_values", lambda: [])


@pytest.mark.parametrize("cut_on", ["spoken", "audio"])
def test_no_audio_reaches_the_browser_after_it_was_told_to_flush(cut_on):
    socket = InterruptingSocket(on=cut_on)
    bridge = dvs.DirectBridge(socket, _session(), ChunkyProvider(), lambda *_: None)
    socket.bridge = bridge

    async def go():
        await bridge._speak_sentence(bridge._generation, "One sentence.")
        await asyncio.gather(*socket.pending)

    asyncio.run(go())

    flush_at = next(i for i, f in enumerate(socket.frames) if f["type"] == "audio_flush")
    after = [f for f in socket.frames[flush_at + 1:] if f["type"] == "audio"]
    assert after == [], f"audio sent after audio_flush: {len(after)} chunk(s)"


def test_an_interrupt_during_the_spoken_send_records_no_first_audio():
    """First-audio time is the moment audio reaches the browser. An interrupt landing while
    the `spoken` send is in flight means no audio was sent, so none may be recorded."""
    socket = InterruptingSocket(on="spoken")
    bridge = dvs.DirectBridge(socket, _session(), ChunkyProvider(), lambda *_: None)
    socket.bridge = bridge

    async def go():
        bridge._turn_first_audio = asyncio.get_running_loop().create_future()
        await bridge._speak_sentence(bridge._generation, "One sentence.")
        await asyncio.gather(*socket.pending)
        return bridge._turn_first_audio

    first = asyncio.run(go())
    assert socket.of("audio") == []
    assert not first.done(), "first-audio time recorded for a sentence that sent no audio"
