"""#3603 — a failed channel turn tells the chat so in plain words, never the
operator's error.

A channel (Telegram, Slack, WhatsApp) is read by its members — every member in
a group. The router used to post the execution's own `error` into the chat:
"Authentication failure: Not logged in · Please run /login. Check subscription
token or API key configuration." Now a failed turn sends a fixed member-facing
line, and the error stays on the execution row and in the router's log line for
the operator. A cancelled turn still says it was cancelled (#679). The
public-chat twin is #3478.

Driven through `ChannelMessageRouter._run_agent_task` with the dispatch stubbed
where its lazy import finds it (the `test_3274_channel_quote` harness).
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

OPERATOR_ERRORS = [
    "Authentication failure: Not logged in · Please run /login. Check subscription "
    "token or API key configuration.",
    "Task execution failed (exit code 1): No authentication configured. Set "
    "ANTHROPIC_API_KEY or assign a subscription token.",
    "Task execution timed out after 900 seconds",
    "Agent at capacity (3/3 parallel tasks running)",
    "HTTP error: rate_limit_exceeded",
]


def _harness(monkeypatch, result):
    import adapters.message_router as mr
    import services.task_execution_service as tes

    monkeypatch.setattr(tes, "dispatch_and_await_terminal", AsyncMock(return_value=result))
    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock())
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)
    channel = MagicMock()
    channel.get_source_identifier.return_value = "telegram:9001:424242"
    channel.send_response = AsyncMock()
    channel.indicate_done = AsyncMock()
    router = mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)
    return mr, channel, router


def _message():
    return SimpleNamespace(text="@bot what did we just discuss?", channel_id="-100", thread_id=None,
                           files=[], sender_id="5",
                           metadata={"raw_message": {"from": {"id": 5, "first_name": "Ada"}},
                                     "bot_id": "77", "chat_title": "Ops", "username": "ada"})


async def _run(router, channel, *, is_group=True):
    return await router._run_agent_task(
        channel, _message(), "finance", "bot-token", "telegram", is_group,
        container=None, upload_dir=None, context_prompt="[prompt]",
        verified_email=None, image_data=[])


def _sent(channel):
    channel.send_response.assert_awaited_once()
    return channel.send_response.await_args.args[1].text


@pytest.mark.asyncio
@pytest.mark.parametrize("is_group", [True, False], ids=["group", "one-to-one"])
@pytest.mark.parametrize("error", OPERATOR_ERRORS)
async def test_a_failed_turn_sends_the_plain_line_not_the_error(monkeypatch, error, is_group):
    mr, channel, router = _harness(monkeypatch, SimpleNamespace(status="failed", error=error, response=""))
    assert await _run(router, channel, is_group=is_group) is None
    text = _sent(channel)
    assert text == mr.CHANNEL_FAILURE_MESSAGE
    assert error not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "", "Unknown error"])
async def test_a_failed_turn_with_no_error_sends_the_same_line(monkeypatch, error):
    mr, channel, router = _harness(monkeypatch, SimpleNamespace(status="failed", error=error, response=""))
    await _run(router, channel)
    assert _sent(channel) == mr.CHANNEL_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_the_error_stays_with_the_operator(monkeypatch, caplog):
    error = OPERATOR_ERRORS[0]
    _, channel, router = _harness(monkeypatch, SimpleNamespace(status="failed", error=error, response=""))
    with caplog.at_level(logging.INFO, logger="adapters.message_router"):
        await _run(router, channel)
    assert any(error in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Execution cancelled by user", "Cancelled by user while queued"])
async def test_a_cancelled_turn_still_says_it_was_cancelled(monkeypatch, reason):
    """#679: the cancel reason is written for the person who asked."""
    _, channel, router = _harness(monkeypatch, SimpleNamespace(status="cancelled", error=reason, response=""))
    await _run(router, channel)
    assert _sent(channel) == reason


@pytest.mark.asyncio
async def test_a_cancelled_turn_with_no_reason_still_says_cancelled(monkeypatch):
    mr, channel, router = _harness(monkeypatch, SimpleNamespace(status="cancelled", error=None, response=""))
    await _run(router, channel)
    assert _sent(channel) == mr.CHANNEL_CANCELLED_MESSAGE
    assert "cancel" in mr.CHANNEL_CANCELLED_MESSAGE.lower()


@pytest.mark.asyncio
async def test_a_dispatch_that_raises_sends_the_same_line(monkeypatch):
    """The exception branch and the failed-row branch say the same thing."""
    import adapters.message_router as mr
    import services.task_execution_service as tes

    _, channel, router = _harness(monkeypatch, None)
    monkeypatch.setattr(tes, "dispatch_and_await_terminal",
                        AsyncMock(side_effect=RuntimeError("upstream exploded: token=abc")))
    await _run(router, channel)
    assert _sent(channel) == mr.CHANNEL_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_a_successful_turn_is_untouched(monkeypatch):
    result = SimpleNamespace(status="success", error=None, response="Here is the summary.", cost=0.0)
    _, channel, router = _harness(monkeypatch, result)
    assert await _run(router, channel) is result
    channel.send_response.assert_not_awaited()
