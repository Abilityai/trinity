"""
Gated skills — a Telegram reply's quoted message is part of the request
(trinity#3274, item 2; the Workspace twin is `client_portal/service.py`,
`request_text=reply_prefix + client_text`).

In a group, a tagged reply puts the quoted message in front of the executor
(`[Replying to Bob: "…"]`, `telegram_group_context.reply_quote_line`). Replying
"@bot do this" to an untagged `/pay-invoice 100 EUR` used to dispatch with the
command only in the quote, unscanned. The gate now reads the quote line plus
what the sender typed — never the group's history block (decided: one past
mention would hold every later tagged turn; the in-container hook is the
backstop), and never the sender label or the group title.

Driven through `ChannelMessageRouter._run_agent_task` with the dispatch stubbed
where its lazy import finds it (`test_ent751_gate_callers` harness).
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

BOT_ID = "77"


@pytest.fixture
def dispatch(monkeypatch):
    import services.task_execution_service as tes
    from services.skill_gate_errors import SkillApprovalRequired

    mock = AsyncMock(side_effect=SkillApprovalRequired(
        request_id="gate-q", agent_name="finance", skills=["pay-invoice"],
        approver_role="primary", expires_at=None))
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", mock)
    return mock


def _router(monkeypatch):
    import adapters.message_router as mr

    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock())
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)
    channel = MagicMock()
    channel.get_source_identifier.return_value = "telegram:9001:424242"
    channel.send_response = AsyncMock()
    channel.indicate_done = AsyncMock()
    return mr, channel, mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)


def _group_reply(text, quoted, *, quoted_from=None):
    raw = {"from": {"id": 5, "first_name": "Ada", "username": "ada"},
           "reply_to_message": {"text": quoted,
                                "from": quoted_from or {"id": 6, "first_name": "Bob", "username": "bob"}}}
    return SimpleNamespace(text=text, channel_id="-100", thread_id=None, files=[], sender_id="5",
                           metadata={"raw_message": raw, "bot_id": BOT_ID, "chat_title": "Ops",
                                     "username": "ada"})


async def _run(router, channel, message, *, is_group=True):
    return await router._run_agent_task(
        channel, message, "finance", "bot-token", "telegram", is_group,
        container=None, upload_dir=None,
        context_prompt="[composed prompt: sender, history, quote, text]",
        verified_email=None, image_data=[])


@pytest.mark.asyncio
async def test_a_group_reply_is_scanned_with_the_message_it_quotes(monkeypatch, dispatch):
    _, channel, router = _router(monkeypatch)
    await _run(router, channel, _group_reply("@finance_bot do this", "/pay-invoice 100 EUR"))
    scanned = dispatch.await_args.kwargs["request_text"]
    assert scanned == '[Replying to Bob (@bob): "/pay-invoice 100 EUR"]\n@finance_bot do this'
    channel.send_response.assert_awaited_once()          # the held notice is the reply


@pytest.mark.asyncio
async def test_a_reply_to_the_bot_itself_quotes_nothing(monkeypatch, dispatch):
    _, channel, router = _router(monkeypatch)
    msg = _group_reply("thanks", "/pay-invoice done", quoted_from={"id": int(BOT_ID), "is_bot": True})
    await _run(router, channel, msg)
    assert dispatch.await_args.kwargs["request_text"] == "thanks"


@pytest.mark.asyncio
async def test_a_one_to_one_message_is_scanned_as_typed(monkeypatch, dispatch):
    """Outside a group the prompt carries no quote line, so neither does the scan."""
    _, channel, router = _router(monkeypatch)
    await _run(router, channel, _group_reply("hello", "/pay-invoice 100 EUR"), is_group=False)
    assert dispatch.await_args.kwargs["request_text"] == "hello"


def test_the_scanned_quote_is_the_line_the_prompt_carries():
    """One helper renders the quote for both the prompt and the scan."""
    import adapters.message_router as mr

    msg = _group_reply("@finance_bot do this", "/pay-invoice 100 EUR")
    assert mr._reply_quote(msg) in mr._format_group_sender(msg).splitlines()
