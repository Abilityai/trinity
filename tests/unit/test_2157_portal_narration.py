"""Unit tests for #2157 — the Workspace narrates, and agents must know it.

The bug: a portal client asks to be spoken to, the agent tries `send_voice_reply`,
gets `not_a_channel_turn`, and tells the client the Workspace is "text-only" and
that they should go to Slack to be heard. Its own limit was real; both claims
about the *surface* were false — the browser reads the reply aloud whenever the
client switches the speaker on.

Four seams, and the pair of opposite failures each one has to avoid:

  * ``build_narrated_surface_prompt`` — says the surface narrates WITHOUT
    implying the agent can send audio here (the mirror-image wrong answer), and
    stays silent when narration would not actually work for this agent
  * ``_build_portal_system_prompt`` — carries the fragment on a portal turn; the
    channel path is untouched
  * ``POST /voice-reply`` — a portal turn gets `portal_client_narrated` + human
    guidance; a channel turn behaves exactly as before
  * the gate — ONE rule for both surfaces (platform key AND the agent-level
    enable AND own-voice-else-platform-default), tested in both directions

True unit tests: no Docker, no backend, no ElevenLabs. DB/TTS are mocked.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import services.tts_service as tts_service
from services.platform_prompt_service import (
    build_narrated_surface_prompt,
    build_voice_capability_prompt,
)


# ---------------------------------------------------------------------------
# The shared gate: one rule, both surfaces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "enabled,voice_id,default_voice,expected",
    [
        (True, "own", "plat", "own"),      # own voice wins
        (True, None, "plat", "plat"),      # #2157: the fallback the portal was missing
        (True, "", "plat", "plat"),        # blank id is not a voice
        (True, None, None, None),          # nothing to speak with
        (False, "own", "plat", None),      # #2157: operator intent, previously ignored
        (False, None, "plat", None),
    ],
)
def test_resolve_voice_from_config_truth_table(enabled, voice_id, default_voice, expected):
    assert tts_service.resolve_voice_from_config(
        enabled=enabled, voice_id=voice_id, default_voice_id=default_voice
    ) == expected


def _tts_env(*, key: bool = True, enabled: bool = True, voice_id: str | None = "own",
             default_voice: str | None = "plat"):
    """Patch the platform key + the agent's voice row + the platform default."""
    cfg = {"enabled": enabled, "voice_id": voice_id,
           "channels": {"telegram": True, "slack": True, "whatsapp": True}}
    db_mock = MagicMock()
    db_mock.get_tts_config.return_value = cfg
    settings_mock = MagicMock()
    settings_mock.get_default_voice_id.return_value = default_voice
    return (
        patch.object(tts_service, "is_available", return_value=key),
        patch("database.db", db_mock),
        patch("services.settings_service.settings_service", settings_mock),
    )


def test_resolve_voice_id_reads_agent_row_and_platform_default():
    a, b, c = _tts_env(voice_id=None)
    with a, b, c:
        assert tts_service.resolve_voice_id("agent-x") == "plat"


def test_resolve_voice_id_none_without_platform_key():
    a, b, c = _tts_env(key=False)
    with a, b, c:
        assert tts_service.resolve_voice_id("agent-x") is None


def test_resolve_voice_id_fail_safe_on_lookup_error():
    """A broken lookup answers "no voice" — never raises into a turn, and never
    guesses yes."""
    db_mock = MagicMock()
    db_mock.get_tts_config.side_effect = RuntimeError("db down")
    with patch.object(tts_service, "is_available", return_value=True), \
         patch("database.db", db_mock):
        assert tts_service.resolve_voice_id("agent-x") is None


# ---------------------------------------------------------------------------
# The prompt fragment
# ---------------------------------------------------------------------------

def test_narrated_prompt_present_when_narration_works():
    with patch.object(tts_service, "resolve_voice_id", return_value="v1"):
        frag = build_narrated_surface_prompt("agent-x")
    assert frag is not None
    low = frag.lower()
    assert "speaker" in low                      # points at the actual control
    assert "not text-only" in low or "not\ntext-only" in low


def test_narrated_prompt_forbids_the_two_wrong_answers():
    """It must kill "text-only / go to Slack" WITHOUT promising agent-sent audio —
    the over-correction AC (#2157)."""
    with patch.object(tts_service, "resolve_voice_id", return_value="v1"):
        frag = build_narrated_surface_prompt("agent-x").lower()
    # Never tell the client the surface is mute, never reroute them.
    assert "never tell them this surface is text-only" in frag
    assert "never send them to another channel" in frag
    # ...and never claim the agent itself can put audio here.
    assert "no audio file" in frag
    assert "never claim to have sent" in frag
    assert "will refuse here" in frag


def test_narrated_prompt_silent_when_agent_cannot_be_narrated():
    """No voice resolvable ⇒ no fragment: the toggle would not render, so
    promising it would be a fresh false claim."""
    with patch.object(tts_service, "resolve_voice_id", return_value=None):
        assert build_narrated_surface_prompt("agent-x") is None


def test_narrated_prompt_never_raises():
    with patch.object(tts_service, "resolve_voice_id", side_effect=RuntimeError("boom")):
        assert build_narrated_surface_prompt("agent-x") is None


def test_channel_voice_prompt_is_unchanged_and_distinct():
    """The channel fragment still advertises the TOOL and says nothing about
    client-side narration — the two paths must not converge."""
    db_mock = MagicMock()
    db_mock.get_tts_config.return_value = {
        "enabled": True, "voice_id": "v1",
        "channels": {"telegram": True, "slack": True, "whatsapp": True},
    }
    with patch.object(tts_service, "is_available", return_value=True), \
         patch("services.platform_prompt_service.db", db_mock):
        frag = build_voice_capability_prompt("agent-x", "telegram")
    assert frag is not None
    assert "send_voice_reply" in frag
    assert "speaker" not in frag.lower()
    assert "workspace" not in frag.lower()


# ---------------------------------------------------------------------------
# Portal prompt composition
# ---------------------------------------------------------------------------

def _portal_service():
    import client_portal.service as svc
    return svc


def test_portal_prompt_carries_the_fragment():
    svc = _portal_service()
    with patch("services.platform_prompt_service.build_narrated_surface_prompt",
               return_value="## NARRATION"), \
         patch("services.platform_prompt_service.build_public_channel_caller_prompt",
               return_value="## PUBLIC"), \
         patch("services.platform_prompt_service.format_user_memory_block", return_value=None), \
         patch("database.db", MagicMock()):
        prompt = svc._build_portal_system_prompt("agent-x", "client@example.com")
    assert prompt is not None
    assert "## PUBLIC" in prompt and "## NARRATION" in prompt
    # Narration comes last — the surface note, after persona + memory.
    assert prompt.index("## PUBLIC") < prompt.index("## NARRATION")


def test_portal_prompt_omits_fragment_when_narration_off():
    svc = _portal_service()
    with patch("services.platform_prompt_service.build_narrated_surface_prompt",
               return_value=None), \
         patch("services.platform_prompt_service.build_public_channel_caller_prompt",
               return_value="## PUBLIC"), \
         patch("services.platform_prompt_service.format_user_memory_block", return_value=None), \
         patch("database.db", MagicMock()):
        assert svc._build_portal_system_prompt("agent-x", "c@e.com") == "## PUBLIC"


def test_portal_prompt_survives_a_composer_failure():
    """Fail-soft: the narration note still lands even if the #1205 composer blows
    up — a personalization miss must not silently reintroduce the bug."""
    svc = _portal_service()
    with patch("services.platform_prompt_service.build_narrated_surface_prompt",
               return_value="## NARRATION"), \
         patch("services.platform_prompt_service.build_public_channel_caller_prompt",
               side_effect=RuntimeError("boom")), \
         patch("services.platform_prompt_service.format_user_memory_block", return_value="MEM"), \
         patch("database.db", MagicMock()):
        prompt = svc._build_portal_system_prompt("agent-x", "c@e.com")
    assert "MEM" in prompt and "## NARRATION" in prompt


# ---------------------------------------------------------------------------
# The tool result: portal-specific reason + guidance
# ---------------------------------------------------------------------------

def _execution(**kw):
    row = types.SimpleNamespace(
        agent_name="agent-x",
        source_channel=None,
        triggered_by="public",
        source_channel_chat_id=None,
        source_channel_thread=None,
    )
    for k, v in kw.items():
        setattr(row, k, v)
    return row


async def _call_voice_reply(execution, *, agent="agent-x"):
    import routers.agents as ar
    from models import VoiceReplyRequest

    db_mock = MagicMock()
    db_mock.get_execution.return_value = execution
    user = types.SimpleNamespace(agent_name=agent, id=1, username="u", role="admin")
    with patch("routers.agents.db", db_mock):
        return await ar.send_voice_reply_endpoint(
            agent, VoiceReplyRequest(text="hello", execution_id="e1"), user
        )


@pytest.mark.asyncio
async def test_portal_turn_gets_its_own_reason_and_guidance():
    with patch("services.tts_service.resolve_voice_id", return_value="v1"):
        res = await _call_voice_reply(_execution(source_channel=config.PORTAL_SOURCE_CHANNEL))
    assert res["delivered"] is False
    assert res["reason"] == "portal_client_narrated"      # not `not_a_channel_turn`
    guidance = res["guidance"].lower()
    assert "speaker" in guidance                          # actionable remedy
    assert "not" in guidance and "text-only" in guidance  # kills the false claim
    assert "another channel" in guidance                  # kills the false remedy
    assert "do not repeat this status" in guidance        # stop echoing internals


@pytest.mark.asyncio
async def test_portal_turn_without_a_voice_does_not_promise_a_speaker_control():
    """The mirror-image trap in the fix itself: "switch the speaker on" is a FALSE
    remedy for an agent with no voice configured — that control does not render
    for it. So the two portal answers split on the same gate the control does."""
    with patch("services.tts_service.resolve_voice_id", return_value=None):
        res = await _call_voice_reply(_execution(source_channel=config.PORTAL_SOURCE_CHANNEL))
    assert res["reason"] == "portal_voice_not_configured"
    guidance = res["guidance"].lower()
    assert "speaker" not in guidance                      # no control to point at
    assert "another channel" in guidance                  # still never reroute
    assert "answer in text" in guidance


@pytest.mark.asyncio
async def test_non_channel_non_portal_turn_keeps_the_old_reason():
    """A schedule/API turn is genuinely not user-facing — unchanged behaviour."""
    res = await _call_voice_reply(_execution(triggered_by="schedule"))
    assert res["reason"] == "not_a_channel_turn"
    assert "guidance" not in res


@pytest.mark.asyncio
async def test_channel_turn_still_delivers():
    """The channel path is untouched: same gate, same service call (#2157 AC)."""
    import routers.agents as ar
    from models import VoiceReplyRequest

    execution = _execution(source_channel="telegram", triggered_by="public",
                           source_channel_chat_id="chat-1")
    db_mock = MagicMock()
    db_mock.get_execution.return_value = execution
    sender = AsyncMock(return_value=types.SimpleNamespace(
        delivered=True, channel="telegram", reason=None))
    user = types.SimpleNamespace(agent_name="agent-x", id=1, username="u", role="admin")
    with patch("routers.agents.db", db_mock), \
         patch("services.voice_reply_service.send_voice_reply", sender):
        res = await ar.send_voice_reply_endpoint(
            "agent-x", VoiceReplyRequest(text="hi", execution_id="e1"), user
        )
    assert res == {"delivered": True, "channel": "telegram", "reason": None}
    assert sender.await_count == 1


# ---------------------------------------------------------------------------
# The surface stamp
# ---------------------------------------------------------------------------

def test_portal_source_channel_is_not_a_messaging_channel():
    """It must never resolve as a delivery destination — the completion-report
    resolver map and the voice service's supported set both have to miss it."""
    import services.voice_reply_service as vrs
    from services.channel_completion_report import _CHANNEL_RESOLVERS

    assert config.PORTAL_SOURCE_CHANNEL not in vrs._SUPPORTED_CHANNELS
    assert config.PORTAL_SOURCE_CHANNEL not in _CHANNEL_RESOLVERS


def test_portal_stamps_the_surface_on_its_executions():
    """EVERY creation site carries the stamp; one without it leaves that slice of
    turns unanswerable.

    Three since the ent#365 review: the pre-created streaming row (ent#286), the
    turn itself, and the pre-created SYNCHRONOUS row — that third one exists so
    `mark_turn_inflight` has an id on the `/chat` path, which is what lets an
    addressed report find its chat there. Asserted as "every site", not as a
    count, so adding a fourth is a decision rather than a broken test.
    """
    import ast
    import inspect
    svc = _portal_service()
    source = inspect.getsource(svc)

    # Per SITE, not by count (ent#365 review). This was
    # `count("source_channel=...") == creates + 1`, where the `+ 1` stood for the
    # turn's own dispatch stamp — so a SECOND dispatch-site stamp would have
    # broken it for a reason that has nothing to do with what it tests. Walking
    # the calls asserts the actual rule: every row this module creates carries
    # the portal stamp, however many other stamps exist elsewhere.
    creation_sites = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task_execution"
    ]
    assert len(creation_sites) >= 2, (
        "expected the streaming, synchronous and turn creation sites"
    )
    unstamped = [
        site.lineno for site in creation_sites
        if not any(kw.arg == "source_channel" for kw in site.keywords)
    ]
    assert not unstamped, (
        f"create_task_execution sites without a source_channel stamp: {unstamped}. "
        f"An unstamped portal row cannot be joined back to its chat, which is "
        f"what makes an addressed report unanswerable."
    )
    assert svc.PORTAL_SOURCE_CHANNEL == config.PORTAL_SOURCE_CHANNEL


# ---------------------------------------------------------------------------
# The speaker control renders on the same gate it speaks on
# ---------------------------------------------------------------------------

def _card(*, tts_ready=True, enabled=True, voice_id="own", default_voice="plat"):
    svc = _portal_service()
    row = {"agent_name": "agent-x", "tts_voice_id": voice_id,
           "tts_voice_replies_enabled": 1 if enabled else 0}
    return svc._row_to_card(row, tts_ready, default_voice)


def test_card_voice_available_falls_back_to_platform_default():
    """The discoverability half of the bug: an agent on the platform default voice
    used to get NO speaker control at all, so the client had nothing to reach for
    when the agent claimed the surface was mute."""
    assert _card(voice_id=None).voice_available is True


def test_card_voice_available_honours_the_operator_switch():
    """The inverse leak: voice off for this agent ⇒ no narration, either surface."""
    assert _card(enabled=False).voice_available is False
    assert _card(enabled=False, voice_id=None).voice_available is False


def test_card_voice_available_needs_the_platform_key():
    assert _card(tts_ready=False).voice_available is False


def test_card_voice_available_happy_path():
    assert _card().voice_available is True


@pytest.mark.asyncio
async def test_portal_tts_endpoint_refuses_when_voice_is_off():
    """The synth endpoint gates on the same resolver, so a client cannot narrate
    an agent whose operator disabled voice by calling /tts directly."""
    svc = _portal_service()
    with patch.object(svc, "agent_on_roster", return_value=True), \
         patch("services.tts_service.is_available", return_value=True), \
         patch("services.tts_service.resolve_voice_id", return_value=None):
        with pytest.raises(svc.ClientPortalError) as e:
            await svc.synthesize_portal_tts("agent-x", "c@e.com", "hello")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_portal_tts_endpoint_speaks_with_the_resolved_voice():
    svc = _portal_service()
    synth = AsyncMock(return_value=b"MP3")
    with patch.object(svc, "agent_on_roster", return_value=True), \
         patch("services.tts_service.is_available", return_value=True), \
         patch("services.tts_service.resolve_voice_id", return_value="plat"), \
         patch("services.tts_service.synthesize_mp3", synth):
        audio = await svc.synthesize_portal_tts("agent-x", "c@e.com", "hello")
    assert audio == b"MP3"
    assert synth.await_args.args[1] == "plat"   # the resolved voice, not a raw column
