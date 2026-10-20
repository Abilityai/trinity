"""A delegated child does not speak into its parent's conversation (#3232, T21).

A child execution inherits its parent's channel context (`source_channel*`) so
that its completion report finds its way back (ent#224/ent#265). The
completion report is consent-gated; `send_voice_reply` is not. Before #3232
the voice-reply route checked only that the execution belonged to the calling
agent, then delivered to the row's `source_channel*` — so a delegated child
with voice enabled could post a voice note into the user's Slack or Telegram
thread under its own execution id, with no proactive-consent check on Slack.

#3232 makes async MCP delegations carry the parent by DEFAULT, which turns that
from a rare path (an agent had to type its execution id) into the common one.
The route now refuses a delegated child before any channel check:
`source_channel_agent` is set by inheritance and by nothing else
(`turn_audience._is_delegated`, same rule), so non-NULL means "inherited".

Each test pairs the refusal with the direct channel turn that must still
deliver, so the file is red on a tree without the guard.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

AGENT = "helper-bot"
PARENT_AGENT = "front-desk"


def _execution(**over):
    row = types.SimpleNamespace(
        id="exec-1",
        agent_name=AGENT,
        status="running",
        triggered_by="slack",
        source_channel="slack",
        source_channel_chat_id="C0123",
        source_channel_thread="1700000000.000100",
        source_channel_agent=None,
        source_channel_client=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


async def _call(execution):
    """Drive the real route with the real self-gate; only the db read and the
    channel send are stubbed. Returns (response, send-calls)."""
    import routers.agents as ar
    from models import VoiceReplyRequest
    from services import voice_reply_service

    sends = []

    async def _send(**kwargs):
        sends.append(kwargs)
        return types.SimpleNamespace(
            delivered=True, channel=kwargs["channel"], reason=None
        )

    db_mock = MagicMock()
    db_mock.get_execution.return_value = execution
    user = types.SimpleNamespace(agent_name=AGENT, id=1, username="u", role="user")
    with patch("routers.agents.db", db_mock), patch.object(
        voice_reply_service, "send_voice_reply", _send
    ):
        res = await ar.send_voice_reply_endpoint(
            AGENT,
            VoiceReplyRequest(text="hello", execution_id="exec-1"),
            user,
        )
    return res, sends


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel, chat_id, thread",
    [
        ("slack", "C0123", "1700000000.000100"),
        ("telegram", "123456789", "42"),
    ],
)
async def test_a_delegated_child_is_refused_and_a_direct_turn_still_delivers(
    channel, chat_id, thread
):
    direct, direct_sends = await _call(
        _execution(
            triggered_by=channel,
            source_channel=channel,
            source_channel_chat_id=chat_id,
            source_channel_thread=thread,
        )
    )
    assert direct["delivered"] is True, direct
    assert len(direct_sends) == 1
    assert direct_sends[0]["chat_id"] == chat_id

    child, child_sends = await _call(
        _execution(
            triggered_by="agent",
            source_channel=channel,
            source_channel_chat_id=chat_id,
            source_channel_thread=thread,
            source_channel_agent=PARENT_AGENT,
        )
    )
    assert child == {"delivered": False, "channel": channel, "reason": "delegated_turn"}
    assert child_sends == [], "a delegated child must never reach the channel send"


@pytest.mark.asyncio
async def test_an_agent_tasking_itself_is_still_a_delegated_child():
    """`source_channel_agent` equal to the executing agent is the self-task and
    A→B→A case — inherited all the same (`turn_audience._is_delegated`)."""
    direct, _ = await _call(_execution())
    assert direct["delivered"] is True

    child, sends = await _call(
        _execution(triggered_by="agent", source_channel_agent=AGENT)
    )
    assert child["reason"] == "delegated_turn"
    assert sends == []
