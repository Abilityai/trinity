"""Report a finished task back to the Slack channel/thread it came from (ent#224).

The scenario this exists for: a user asks an agent in Slack, the agent kicks off
(or delegates) a long-running job, the job finishes — and today nobody tells the
user. The trigger side worked; the completion side died silently.

Everything here is a JOIN of parts that already exist:
  * task-completion terminals (#1578) give the chokepoint,
  * ``schedule_executions.source_channel`` / ``_chat_id`` / ``_thread`` (ent#117)
    give the destination,
  * ``slack_service.send_message_detailed`` (#1649) gives the send + thread_ts,
  * ``slack_channel_agents.allow_proactive`` (ent#223) gives consent,
  * ``idempotency_service.effect_guard`` (#1084) gives at-most-once.

**The no-double-post rule.** A direct channel turn is synchronous — the adapter
already replies inline — and is recorded with ``triggered_by`` == the channel
name. Reporting on those would duplicate every normal Slack reply. So we report
ONLY when the execution *inherited* its channel context (a delegated or
background terminal), i.e. ``triggered_by`` is NOT a channel trigger. That single
condition is what keeps a customer workspace from being spammed.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Triggers whose executions ALREADY reply inline through the channel adapter.
# An execution carrying one of these must never be reported again.
INLINE_CHANNEL_TRIGGERS = frozenset({"slack", "telegram", "whatsapp"})

# v1 covers Slack only — it is the channel the partner scenario needs and the one
# with a per-channel consent unit (ent#223). Telegram/WhatsApp are group-shaped
# differently and are a deliberate follow-up.
SUPPORTED_CHANNELS = frozenset({"slack"})

_MAX_REPORT_CHARS = 2800


def _summarize(status: str, summary_or_error: Optional[str]) -> str:
    """A short, honest completion line. Failures report too — a silent failure is
    exactly the bug this closes."""
    from utils.credential_sanitizer import sanitize_text

    # Credential-sanitise BEFORE truncating, over a 2x window — the #1578 emit
    # chokepoint does exactly this (event_dispatch_service.py:311) and for the
    # same reason: a failure terminal's error text can carry secrets, and a bare
    # slice can cut a secret so the redaction pattern no longer matches and it
    # survives. Slack is a persistent, externally hosted, human-visible surface,
    # so this is the last place to skip it.
    raw = (summary_or_error or "").strip()
    window = raw[: _MAX_REPORT_CHARS * 2]
    cleaned = sanitize_text(window)
    # Truncation is decided on what we actually cut — NOT by comparing against
    # `raw`, because redaction changes length and would append a phantom ellipsis
    # to text that was never truncated.
    truncated = len(cleaned) > _MAX_REPORT_CHARS or len(raw) > len(window)
    body = cleaned[:_MAX_REPORT_CHARS].rstrip()
    if truncated:
        body += "…"
    if status == "success":
        head = "✅ Task finished"
        return f"{head}\n\n{body}" if body else head
    head = f"⚠️ Task ended with status `{status}`"
    return f"{head}\n\n{body}" if body else head


def _channel_allows_proactive(agent_name: str, channel_id: str) -> tuple[bool, Optional[str]]:
    """ent#223 consent gate. Returns (allowed, team_id)."""
    from database import db

    binding = next(
        (c for c in db.get_slack_channels_for_agent(agent_name)
         if c.get("slack_channel_id") == channel_id),
        None,
    )
    if not binding:
        return False, None
    return bool(binding.get("allow_proactive")), binding.get("team_id")


async def report_completion(
    *,
    execution_id: str,
    agent_name: str,
    status: str,
    summary_or_error: Optional[str],
) -> bool:
    """Post the terminal back to its originating channel/thread. Returns True if a
    message was delivered. Never raises — a reporting failure must not disturb the
    execution that already completed.

    The destination + trigger are read from the execution row itself, so every
    terminal chokepoint stays a one-liner and can't drift out of sync with what
    was actually persisted.
    """
    try:
        from database import db

        row = db.get_execution(execution_id) if execution_id else None
        if row is None:
            return False
        source_channel = getattr(row, "source_channel", None)
        source_channel_chat_id = getattr(row, "source_channel_chat_id", None)
        source_channel_thread = getattr(row, "source_channel_thread", None)
        triggered_by = getattr(row, "triggered_by", None)

        if not source_channel or not source_channel_chat_id:
            return False                      # nothing to report back to
        if source_channel not in SUPPORTED_CHANNELS:
            return False
        if (triggered_by or "") in INLINE_CHANNEL_TRIGGERS:
            return False                      # the adapter already replied — no double-post

        allowed, team_id = _channel_allows_proactive(agent_name, source_channel_chat_id)
        if not allowed:
            logger.info(
                "[ent#224] completion report suppressed for %s: channel %s has no "
                "proactive consent (or no binding)", agent_name, source_channel_chat_id)
            return False

        from database import db
        from services.idempotency_service import EffectInProgressError, effect_guard
        from services.slack_service import slack_service

        bot_token = db.get_slack_workspace_bot_token(team_id) if team_id else None
        if not bot_token:
            logger.warning("[ent#224] no Slack bot token for team %s; cannot report", team_id)
            return False

        # At-most-once per (execution, destination) — a re-delivered or retried
        # terminal must not post the same completion twice (#1084). Identity is
        # the resolved destination only, never the generated body.
        try:
            async with effect_guard(
                "channel_completion_report",
                {
                    "channel": source_channel,
                    "chat_id": source_channel_chat_id,
                    "thread": source_channel_thread or "",
                },
                execution_id=execution_id,
                agent_name=agent_name,
            ) as guard:
                if guard.replay:
                    return False              # already reported — do NOT re-post

                ok, error, _ts = await slack_service.send_message_detailed(
                    bot_token=bot_token,
                    channel=source_channel_chat_id,
                    text=_summarize(status, summary_or_error),
                    username=agent_name,
                    thread_ts=source_channel_thread,   # answer IN the originating thread
                )
                if not ok:
                    logger.warning("[ent#224] Slack completion report failed for %s: %s",
                                   execution_id, error)
                    return False
                guard.snapshot = {"reported": True, "channel": source_channel_chat_id}
        except EffectInProgressError:
            # A concurrent attempt holds the claim — never post a second copy.
            return False

        logger.info("[ent#224] reported %s terminal of %s back to %s",
                    status, execution_id, source_channel_chat_id)
        return True
    except Exception as e:  # noqa: BLE001 — never disturb a completed execution
        logger.warning("[ent#224] completion report raised for %s: %s", execution_id, e)
        return False


def spawn_completion_report(**kwargs) -> None:
    """Fire-and-forget wrapper mirroring ``spawn_task_terminal_event`` (#1578).

    Keeps a strong reference to the task (the #1083 GC footgun) and is a no-op
    when there is no running loop.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(report_completion(**kwargs))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


_inflight: set = set()
