"""Unit tests for #903 — Slack: scope chat session to thread, not channel.

``SlackAdapter.get_session_identifier`` is the entire conversation boundary for
Slack channel chats: it feeds ``get_or_create_public_chat_session`` +
``build_public_chat_context``'s last-10-turn replay (there is no persistent
Claude session on this path). Before #903 the key was
``team_id:sender_id:channel_id``, so every thread and @mention from one user in
one channel shared a single channel-wide transcript and cross-contaminated.

After #903:
- DMs (no threads): keep ``team_id:sender_id:channel_id`` — one continuous convo.
- Channel messages: ``team_id:channel_id:thread_id`` — ``sender_id`` dropped so
  multi-participant threads share context; per-speaker attribution moves to the
  stored ``sender_label`` on each message row (see
  ``test_903_public_chat_sender.py``), not the key.

These tests exercise the real parsers (``_parse_dm``, ``_parse_mention``,
``_parse_thread_reply``) so the ``is_dm`` / ``thread_id`` metadata the key relies
on is populated exactly as production would populate it (F-TESTS — the pure
direct cases alone would bypass the parser contract the fix depends on).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from adapters.base import NormalizedMessage  # noqa: E402
from adapters.slack_adapter import SlackAdapter  # noqa: E402


TEAM = "T123"
CHANNEL = "C999"


@pytest.fixture
def adapter() -> SlackAdapter:
    return SlackAdapter()


def _mention_event(ts: str, *, thread_ts: str | None = None, user: str = "U1") -> dict:
    return {
        "user": user,
        "text": "<@U0BOT> hello",
        "channel": CHANNEL,
        "ts": ts,
        **({"thread_ts": thread_ts} if thread_ts else {}),
    }


def _dm_event(ts: str, *, thread_ts: str | None = None, user: str = "U1") -> dict:
    return {
        "user": user,
        "text": "hello",
        "channel": "D555",
        "ts": ts,
        **({"thread_ts": thread_ts} if thread_ts else {}),
    }


# ---------------------------------------------------------------------------
# get_session_identifier — direct (branch coverage)
# ---------------------------------------------------------------------------

def _msg(*, is_dm: bool, thread_id: str | None, sender: str = "U1") -> NormalizedMessage:
    return NormalizedMessage(
        sender_id=sender,
        text="hi",
        channel_id=CHANNEL,
        thread_id=thread_id,
        timestamp="",
        metadata={"team_id": TEAM, "is_dm": is_dm},
    )


def test_channel_key_is_team_channel_thread(adapter):
    key = adapter.get_session_identifier(_msg(is_dm=False, thread_id="1700.0001"))
    assert key == f"{TEAM}:{CHANNEL}:1700.0001"


def test_channel_key_drops_sender_id(adapter):
    """Two different senders in the same thread share one session (multi-participant)."""
    k1 = adapter.get_session_identifier(_msg(is_dm=False, thread_id="1700.0001", sender="U1"))
    k2 = adapter.get_session_identifier(_msg(is_dm=False, thread_id="1700.0001", sender="U2"))
    assert k1 == k2
    assert "U1" not in k1 and "U2" not in k2


def test_dm_key_keeps_sender_and_channel(adapter):
    key = adapter.get_session_identifier(_msg(is_dm=True, thread_id=None, sender="U7"))
    assert key == f"{TEAM}:U7:{CHANNEL}"


def test_dm_key_ignores_thread_ts(adapter):
    """DMs can technically carry a thread_ts; the key must stay sender+channel."""
    key = adapter.get_session_identifier(_msg(is_dm=True, thread_id="1700.9999", sender="U7"))
    assert key == f"{TEAM}:U7:{CHANNEL}"


def test_missing_team_id_falls_back_to_unknown(adapter):
    """F-TEAMID: `or "unknown"` handles a malformed event with no team_id."""
    msg = NormalizedMessage(
        sender_id="U1", text="hi", channel_id=CHANNEL, thread_id="1700.0001",
        timestamp="", metadata={"is_dm": False},
    )
    assert adapter.get_session_identifier(msg) == f"unknown:{CHANNEL}:1700.0001"


# ---------------------------------------------------------------------------
# End-to-end via parsers — the AC scenarios
# ---------------------------------------------------------------------------

def test_parsers_always_set_is_dm_and_thread_id(adapter):
    """The key relies on `is_dm` and (on the channel path) a non-None
    `thread_id`; pin the contract each parser upholds."""
    mention = adapter._parse_mention(_mention_event("1700.1000"), TEAM)
    assert mention.metadata["is_dm"] is False
    assert mention.thread_id == "1700.1000"  # thread_ts or ts → never None

    dm = adapter._parse_dm(_dm_event("1700.2000"), TEAM)
    assert dm.metadata["is_dm"] is True


def test_two_concurrent_threads_get_distinct_sessions(adapter):
    """Two top-level @mentions in one channel → two distinct sessions."""
    m_a = adapter._parse_mention(_mention_event("1700.1000"), TEAM)
    m_b = adapter._parse_mention(_mention_event("1700.2000"), TEAM)
    key_a = adapter.get_session_identifier(m_a)
    key_b = adapter.get_session_identifier(m_b)
    assert key_a != key_b
    assert key_a == f"{TEAM}:{CHANNEL}:1700.1000"
    assert key_b == f"{TEAM}:{CHANNEL}:1700.2000"


def test_reply_and_re_mention_in_thread_share_session(adapter):
    """A thread reply + a re-@mention inside the same thread → same session as the
    top-level mention that started it."""
    root = adapter._parse_mention(_mention_event("1700.1000"), TEAM)
    root_key = adapter.get_session_identifier(root)

    # Un-mentioned thread reply — gated on channel binding + active thread.
    with patch("adapters.slack_adapter.db") as mock_db:
        mock_db.get_slack_agent_name_for_channel.return_value = "agent-x"
        mock_db.is_slack_active_thread.return_value = "agent-x"
        reply = adapter._parse_thread_reply(
            {"user": "U2", "text": "more", "channel": CHANNEL,
             "ts": "1700.1500", "thread_ts": "1700.1000"},
            TEAM,
        )
    reply_key = adapter.get_session_identifier(reply)

    # Re-@mention inside the same thread carries thread_ts.
    re_mention = adapter._parse_mention(
        _mention_event("1700.1600", thread_ts="1700.1000", user="U3"), TEAM
    )
    re_mention_key = adapter.get_session_identifier(re_mention)

    assert reply_key == root_key
    assert re_mention_key == root_key


def test_fresh_mention_does_not_inherit_thread_history(adapter):
    """A brand-new top-level @mention gets a fresh session distinct from an
    earlier thread's session in the same channel."""
    thread_root = adapter._parse_mention(_mention_event("1700.1000"), TEAM)
    fresh = adapter._parse_mention(_mention_event("1700.5000"), TEAM)
    assert adapter.get_session_identifier(thread_root) != adapter.get_session_identifier(fresh)


def test_dm_continuity_same_session_across_messages(adapter):
    """DM messages from one user stay on one session regardless of ts/thread."""
    a = adapter._parse_dm(_dm_event("1700.1000"), TEAM)
    b = adapter._parse_dm(_dm_event("1700.2000"), TEAM)
    c = adapter._parse_dm(_dm_event("1700.3000", thread_ts="1700.1000"), TEAM)
    keys = {adapter.get_session_identifier(m) for m in (a, b, c)}
    assert len(keys) == 1
    assert keys.pop() == f"{TEAM}:U1:D555"
