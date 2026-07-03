"""Unit tests for #350 Phase 1 — Slack channel/user identity in agent context.

Covers:
- `message_router._format_channel_identity` — the pure prefix builder (channel
  messages get `[Channel: #x]\\n[From: ...]`; DMs / un-enriched → empty).
- `SlackAdapter.enrich_message` — populates sender/channel identity into
  `message.metadata` via a mocked slack_service, best-effort.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from adapters.base import NormalizedMessage  # noqa: E402
from adapters.message_router import _agent_avatar_url, _format_channel_identity  # noqa: E402


def _msg(metadata: dict) -> NormalizedMessage:
    return NormalizedMessage(
        sender_id="U123",
        text="hi",
        channel_id="C123",
        timestamp="",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# _format_channel_identity — pure
# ---------------------------------------------------------------------------

def test_channel_message_full_identity():
    out = _format_channel_identity(_msg({
        "channel_name": "engineering",
        "sender_display_name": "John Smith",
        "sender_username": "johndoe",
    }))
    assert out == "[Channel: #engineering]\n[From: John Smith (@johndoe)]"


def test_channel_message_display_only():
    out = _format_channel_identity(_msg({
        "channel_name": "general", "sender_display_name": "Jane",
    }))
    assert out == "[Channel: #general]\n[From: Jane]"


def test_channel_message_username_only():
    out = _format_channel_identity(_msg({
        "channel_name": "general", "sender_username": "jane",
    }))
    assert out == "[Channel: #general]\n[From: @jane]"


def test_channel_message_no_identity_falls_back_to_id():
    out = _format_channel_identity(_msg({"channel_name": "general"}))
    assert out == "[Channel: #general]\n[From: User U123]"


def test_dm_or_unenriched_returns_empty():
    # No channel_name (DM or enrichment failed) → no prefix, context unchanged.
    assert _format_channel_identity(_msg({"is_dm": True})) == ""
    assert _format_channel_identity(_msg({})) == ""


# ---------------------------------------------------------------------------
# SlackAdapter.enrich_message — populates metadata (mocked API)
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_enrich_channel_message_sets_channel_and_sender():
    from adapters.slack_adapter import SlackAdapter
    adapter = SlackAdapter()
    msg = _msg({"team_id": "T1", "is_dm": False})

    with patch("adapters.slack_adapter.db") as mock_db, \
         patch("adapters.slack_adapter.slack_service") as mock_svc:
        mock_db.get_slack_workspace_bot_token.return_value = "xoxb-test"
        mock_svc.get_user_info = AsyncMock(return_value={
            "real_name": "John Smith", "display_name": "JS", "name": "johndoe",
        })
        mock_svc.get_channel_info = AsyncMock(return_value={"name": "engineering", "is_private": False})
        _run(adapter.enrich_message(msg))

    assert msg.metadata["sender_display_name"] == "John Smith"
    assert msg.metadata["sender_username"] == "johndoe"
    assert msg.metadata["channel_name"] == "engineering"


def test_enrich_dm_does_not_set_channel_name():
    from adapters.slack_adapter import SlackAdapter
    adapter = SlackAdapter()
    msg = _msg({"team_id": "T1", "is_dm": True})

    with patch("adapters.slack_adapter.db") as mock_db, \
         patch("adapters.slack_adapter.slack_service") as mock_svc:
        mock_db.get_slack_workspace_bot_token.return_value = "xoxb-test"
        mock_svc.get_user_info = AsyncMock(return_value={"real_name": "John", "name": "john"})
        mock_svc.get_channel_info = AsyncMock(return_value={"name": "should-not-fetch"})
        _run(adapter.enrich_message(msg))

    assert msg.metadata.get("sender_display_name") == "John"
    assert "channel_name" not in msg.metadata  # DMs never get a channel prefix
    mock_svc.get_channel_info.assert_not_called()


def test_enrich_best_effort_swallows_errors():
    from adapters.slack_adapter import SlackAdapter
    adapter = SlackAdapter()
    msg = _msg({"team_id": "T1", "is_dm": False})

    with patch("adapters.slack_adapter.db") as mock_db, \
         patch("adapters.slack_adapter.slack_service") as mock_svc:
        mock_db.get_slack_workspace_bot_token.return_value = "xoxb-test"
        mock_svc.get_user_info = AsyncMock(side_effect=RuntimeError("boom"))
        mock_svc.get_channel_info = AsyncMock(return_value={"name": "eng"})
        # Must not raise — enrichment is best-effort.
        _run(adapter.enrich_message(msg))
    # Nothing crashed; identity simply absent for the failed call.
    assert "sender_display_name" not in msg.metadata


# ---------------------------------------------------------------------------
# _agent_avatar_url — best-effort bot icon (#292)
# ---------------------------------------------------------------------------

def test_avatar_url_with_cache_bust():
    with patch("adapters.message_router.settings_service") as mock_settings, \
         patch("adapters.message_router.db") as mock_db:
        mock_settings.get_public_chat_url.return_value = "https://trinity.example.com/"
        mock_db.get_avatar_identity.return_value = {"updated_at": "2026-07-02T10:00:00Z"}
        url = _agent_avatar_url("research-agent")
    assert url == "https://trinity.example.com/api/agents/research-agent/avatar?v=20260702100000"


def test_avatar_url_without_updated_at_has_no_version():
    with patch("adapters.message_router.settings_service") as mock_settings, \
         patch("adapters.message_router.db") as mock_db:
        mock_settings.get_public_chat_url.return_value = "https://trinity.example.com"
        mock_db.get_avatar_identity.return_value = None
        url = _agent_avatar_url("sales-ops")
    assert url == "https://trinity.example.com/api/agents/sales-ops/avatar"


def test_avatar_url_none_when_no_public_base():
    with patch("adapters.message_router.settings_service") as mock_settings:
        mock_settings.get_public_chat_url.return_value = ""
        assert _agent_avatar_url("a") is None


def test_avatar_url_best_effort_on_error():
    with patch("adapters.message_router.settings_service") as mock_settings:
        mock_settings.get_public_chat_url.side_effect = RuntimeError("boom")
        assert _agent_avatar_url("a") is None


def test_enrich_no_token_is_noop():
    from adapters.slack_adapter import SlackAdapter
    adapter = SlackAdapter()
    msg = _msg({"team_id": "T1", "is_dm": False})
    with patch("adapters.slack_adapter.db") as mock_db, \
         patch("adapters.slack_adapter.slack_service") as mock_svc:
        mock_db.get_slack_workspace_bot_token.return_value = None
        mock_svc.get_user_info = AsyncMock()
        _run(adapter.enrich_message(msg))
    mock_svc.get_user_info.assert_not_called()
