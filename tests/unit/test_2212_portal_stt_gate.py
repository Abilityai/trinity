"""Unit tests for #2212 — the portal mic must be gated on the ability to TRANSCRIBE.

The bug this half fixes: the mic button rendered on a pure browser-capability
check (`window.SpeechRecognition` exists), while the speaker rendered on
`voice_available`. So on an instance with no ElevenLabs key the mic was a dead
affordance, and on an instance WITH a key but no effective agent voice the card
carried no bit the client could use to prefer the server path at all.

`stt_available` is that bit, and its whole value is that it equals the `/stt`
endpoint's own gate. These tests pin exactly that:

  * key, no agent voice  → transcription YES, narration NO  (the combination
    that has no correct answer without a second bit)
  * no key               → both NO (fails closed, like `voice_available`)
  * the card bit and `transcribe_portal_audio`'s refusal are the SAME condition,
    so the control a client sees can never disagree with the endpoint it calls

True unit tests: no DB, no HTTP, no ElevenLabs.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import services.tts_service as tts_service
from client_portal import service as portal_service
from client_portal.service import ClientPortalError

ROW = {"agent_name": "acme-bot", "owner": "owner@example.com"}


def _card(*, tts_ready: bool, effective_voice: str | None):
    """Build a card with the two voice inputs pinned independently."""
    with patch.object(tts_service, "resolve_voice_from_config", return_value=effective_voice):
        return portal_service._row_to_card(dict(ROW), tts_ready, "platform-default")


def test_key_but_no_agent_voice_can_transcribe_yet_cannot_narrate():
    # The case the single-bit design could not express: dictation works (input
    # needs no voice), narration does not.
    card = _card(tts_ready=True, effective_voice=None)
    assert card.stt_available is True
    assert card.voice_available is False


def test_no_platform_key_fails_closed_on_both():
    card = _card(tts_ready=False, effective_voice="some-voice")
    assert card.stt_available is False
    assert card.voice_available is False


def test_key_and_voice_enables_both():
    card = _card(tts_ready=True, effective_voice="some-voice")
    assert card.stt_available is True
    assert card.voice_available is True


def test_default_is_absent_not_present():
    """A card built without the field must not claim the capability — an older
    payload should hide the mic, never render a dead one."""
    from client_portal.models import PortalAgentCard

    assert PortalAgentCard(name="x").stt_available is False


@pytest.mark.parametrize("key_available", [True, False])
def test_card_bit_and_endpoint_gate_are_the_same_condition(key_available):
    """`stt_available` promises what `/stt` will do. Drift here is exactly the
    dead-control bug, so it is asserted as one fact, not two."""
    async def _call():
        return await portal_service.transcribe_portal_audio(
            "acme-bot", "client@example.com", "voice.webm", "audio/webm", b"x" * 4000
        )

    with patch.object(portal_service, "agent_on_roster", return_value=True), \
            patch.object(tts_service, "is_available", return_value=key_available), \
            patch.object(tts_service, "resolve_voice_from_config", return_value=None):
        card = portal_service._row_to_card(dict(ROW), tts_service.is_available())
        if key_available:
            # Past the gate: it fails later, at the provider call, not at the gate.
            with pytest.raises(ClientPortalError) as exc:
                asyncio.run(_call())
            assert exc.value.status_code != 404
            assert card.stt_available is True
        else:
            with pytest.raises(ClientPortalError) as exc:
                asyncio.run(_call())
            assert exc.value.status_code == 404
            assert card.stt_available is False
