"""Outbound channel-message history (#1649).

When an agent sends a **proactive group/channel message** (`send_group_message`,
#349/#350), the delivered text has to land in the channel session history —
otherwise the agent has no record of its own broadcast. The next inbound turn
builds context from `db.build_public_chat_context(session_id, ...)`, which only
contains turns the message router persisted, so the agent can repeat itself,
contradict the broadcast, or fail to parse a reply to it.

This is the group counterpart of the DM fix (#1600). It lives here rather than in
a router because routers hold no business logic (Invariant #1), and because both
the Telegram-group and Slack-channel endpoints need identical semantics.

**The session key must be the one an inbound message resolves to.** Persisting
into *a* session is useless — it has to be the session a reply lands in, or the
write succeeds and the agent still can't recall the message. So callers derive
the key by driving the channel adapter's own `get_session_identifier()`, never by
re-implementing the format (which drifts). See `session_key_for_*` below.
"""

from __future__ import annotations

import logging
from typing import Optional

from database import db
from services.runtime_secret_scrub import get_staged_values, scrub_text

logger = logging.getLogger(__name__)


def session_key_for_telegram_group(bot_id: str, sender_id: str, chat_id: str) -> str:
    """Session key for a Telegram group message, via the adapter itself."""
    from adapters.base import NormalizedMessage
    from adapters.telegram_adapter import TelegramAdapter

    return TelegramAdapter().get_session_identifier(
        NormalizedMessage(
            sender_id=str(sender_id),
            text="",
            channel_id=str(chat_id),
            timestamp="",
            metadata={"bot_id": bot_id, "is_group": True},
        )
    )


def session_key_for_slack_channel(team_id: str, channel_id: str, thread_ts: str) -> str:
    """Session key for a Slack channel message, via the adapter itself.

    Slack channel sessions are thread-scoped (`team:channel:thread`, #903), so
    `thread_ts` is the parent message's ts — for a top-level post, the post's OWN
    ts, which is what any in-thread reply will carry.
    """
    from adapters.base import NormalizedMessage
    from adapters.slack_adapter import SlackAdapter

    return SlackAdapter().get_session_identifier(
        NormalizedMessage(
            sender_id="",  # dropped by the adapter for channel (non-DM) messages
            text="",
            channel_id=str(channel_id),
            thread_id=str(thread_ts),
            timestamp="",
            metadata={"team_id": team_id, "is_dm": False},
        )
    )


def persist_outbound_group_message(
    agent_name: str,
    channel: str,
    session_identifier: Optional[str],
    text: str,
) -> None:
    """Append a delivered proactive group message to its channel session (#1649).

    Attribution follows router step 11 (#903): role `assistant`, `sender_label` =
    agent name, and **`sender_email=None`**. This is the key difference from the
    DM case (#1600, which stamps the recipient's email): a group/channel session
    is shared and multi-participant, so `_assistant_sender_email`'s rule returns
    None — a broadcast addressed to a whole thread must never be folded into one
    participant's durable MEM-001 memory.

    Call ONLY on confirmed delivery, so a failed send never writes a phantom
    assistant turn into history.

    Fail-soft: the message is already delivered when this runs, so a persistence
    error must never surface as a send failure. It is logged loudly instead — a
    silent swallow is what let the original gap go unnoticed.
    """
    if not session_identifier:
        logger.warning(
            "[#1649] proactive %s message for agent %s delivered but NOT persisted: "
            "no session identifier resolved",
            channel, agent_name,
        )
        return
    # ent#279: scrub the broadcast body before it lands in the durable channel
    # session (public_chat_messages). Delivery already happened elsewhere; this is
    # the persisted-history copy.
    _staged = get_staged_values()
    if _staged:
        text = scrub_text(_staged, text)
    try:
        session = db.get_or_create_public_chat_session(
            agent_name, session_identifier, channel
        )
        session_id = session.id if hasattr(session, "id") else session["id"]
        db.add_public_chat_message(
            session_id,
            "assistant",
            text,
            sender_email=None,  # shared thread — never one participant's memory (#903)
            sender_label=agent_name,
        )
        logger.debug(
            "[#1649] persisted proactive %s message to session %s", channel, session_id
        )
    except Exception:
        logger.error(
            "[#1649] failed to persist delivered proactive %s message for agent %s "
            "— the message WAS delivered; history will be missing this turn",
            channel, agent_name, exc_info=True,
        )
