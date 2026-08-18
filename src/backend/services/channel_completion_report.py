"""Report a finished task back to the channel chat/thread it came from
(ent#224 Slack, ent#265 Telegram).

The scenario this exists for: a user asks an agent in a channel, the agent kicks
off (or delegates) a long-running job, the job finishes — and today nobody tells
the user. The trigger side worked; the completion side died silently.

Everything here is a JOIN of parts that already exist:
  * task-completion terminals (#1578) give the chokepoint,
  * ``schedule_executions.source_channel`` / ``_chat_id`` / ``_thread`` (ent#117)
    give the destination, persisted at /task row creation for delegated rows
    (ent#265 D0),
  * ``schedule_executions.source_channel_agent`` (ent#265 D1) gives the
    **binding agent** — the agent whose channel binding owns the inherited
    context (NULL = the executing agent, i.e. legacy/direct rows),
  * per-channel send primitives give delivery: ``slack_service.
    send_message_detailed`` (#1649) and ``TelegramAdapter._send_message``
    (#321 proactive-service precedent — service-layer reuse, no adapter edits),
  * per-channel consent units gate the post: Slack channel-binding
    ``allow_proactive`` (ent#223); Telegram groups
    ``telegram_group_configs.allow_proactive`` (ent#265, default allow);
    Telegram DMs are consent-by-construction (a chat link exists only because
    the user personally cold-started the bot — Telegram's own cold-DM
    prohibition is the transport-level guarantee),
  * ``idempotency_service.effect_guard`` (#1084) gives at-most-once.

**The no-double-post rule.** A direct channel turn is synchronous — the adapter
already replies inline — and is recorded with ``triggered_by`` == the channel
name. Reporting on those would duplicate every normal channel reply. So we
report ONLY when the execution *inherited* its channel context (a delegated or
background terminal), i.e. ``triggered_by`` is NOT a channel trigger. That
single condition is what keeps a customer workspace/chat from being spammed.

**Binding-agent resolution (ent#265 D1).** Consent and the bot token are
evaluated against ``binding_agent = row.source_channel_agent or
row.agent_name`` for BOTH channels: the report must be delivered by the bot the
user actually addressed (on Telegram no other bot even *can* deliver the DM).
Attribution stays with the EXECUTING agent: Slack posts with
``username=agent_name``; Telegram (which has no per-message sender name)
carries the executing agent in the head line when it differs (D1c).

Per-channel delivery is a resolver dispatch map (D10): ``_CHANNEL_RESOLVERS``
maps channel → resolver; a resolver does consent + destination + token
resolution and returns an async deliver closure (or None = suppress, already
logged). ``SUPPORTED_CHANNELS`` derives from the map keys, so a third channel
(WhatsApp) is additive.
"""
from __future__ import annotations

import html
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Triggers whose executions ALREADY reply inline through the channel adapter.
# An execution carrying one of these must never be reported again.
INLINE_CHANNEL_TRIGGERS = frozenset({"slack", "telegram", "whatsapp"})

_MAX_REPORT_CHARS = 2800

# Telegram sendMessage hard cap. Applied AFTER markdown→HTML conversion +
# entity escaping (D5): expansion can push 2800 source chars past 4096, and
# "message too long" is a plain 400 the adapter's parse-failure fallback does
# NOT catch — the report would be silently lost.
_TELEGRAM_MAX_MESSAGE_CHARS = 4096


def _summarize(
    status: str,
    summary_or_error: Optional[str],
    *,
    executing_agent: Optional[str] = None,
) -> str:
    """A short, honest completion line. Failures report too — a silent failure is
    exactly the bug this closes.

    ``executing_agent`` (ent#265 D1c): channels without a per-message sender
    name (Telegram) pass the executing agent when it differs from the binding
    agent, so delegated output arriving via A's bot is honestly attributed to
    the worker that produced it. Slack passes None — ``username=`` carries it.
    """
    from utils.credential_sanitizer import sanitize_text

    # Credential-sanitise BEFORE truncating, over a 2x window — the #1578 emit
    # chokepoint does exactly this (event_dispatch_service.py:311) and for the
    # same reason: a failure terminal's error text can carry secrets, and a bare
    # slice can cut a secret so the redaction pattern no longer matches and it
    # survives. A channel is a persistent, externally hosted, human-visible
    # surface, so this is the last place to skip it.
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
    else:
        head = f"⚠️ Task ended with status `{status}`"
    if executing_agent:
        head += f" — {executing_agent}"
    return f"{head}\n\n{body}" if body else head


def _channel_allows_proactive(agent_name: str, channel_id: str) -> tuple[bool, Optional[str]]:
    """ent#223 consent gate (Slack). Returns (allowed, team_id)."""
    from database import db

    binding = next(
        (c for c in db.get_slack_channels_for_agent(agent_name)
         if c.get("slack_channel_id") == channel_id),
        None,
    )
    if not binding:
        return False, None
    return bool(binding.get("allow_proactive")), binding.get("team_id")


def _resolve_slack(
    *,
    binding_agent: str,
    executing_agent: str,
    chat_id: str,
    thread: Optional[str],
    status: str,
    summary_or_error: Optional[str],
    execution_id: str,
) -> Optional[Callable[[], Awaitable[bool]]]:
    """Slack leg (ent#224). Consent + token resolve against the BINDING agent
    (ent#265 D1 — fixes the delegated case, previously suppressed whenever
    worker B wasn't itself bound to the originating channel); the displayed
    identity stays ``username=executing_agent`` (attribution unchanged, D1b)."""
    from database import db
    from services.slack_service import slack_service

    allowed, team_id = _channel_allows_proactive(binding_agent, chat_id)
    if not allowed:
        logger.info(
            "[ent#224] completion report suppressed for %s: channel %s has no "
            "proactive consent (or no binding)", binding_agent, chat_id)
        return None

    bot_token = db.get_slack_workspace_bot_token(team_id) if team_id else None
    if not bot_token:
        logger.warning("[ent#224] no Slack bot token for team %s; cannot report", team_id)
        return None

    text = _summarize(status, summary_or_error)

    async def _deliver() -> bool:
        ok, error, _ts = await slack_service.send_message_detailed(
            bot_token=bot_token,
            channel=chat_id,
            text=text,
            username=executing_agent,
            thread_ts=thread,             # answer IN the originating thread
        )
        if not ok:
            logger.warning("[ent#224] Slack completion report failed for %s: %s",
                           execution_id, error)
            return False
        return True

    return _deliver


def _resolve_telegram(
    *,
    binding_agent: str,
    executing_agent: str,
    chat_id: str,
    thread: Optional[str],
    status: str,
    summary_or_error: Optional[str],
    execution_id: str,
) -> Optional[Callable[[], Awaitable[bool]]]:
    """Telegram leg (ent#265). Consent units (D2): a known GROUP requires
    ``is_active`` AND ``allow_proactive`` (default allow, opt-out mute); a known
    DM chat link is consent-by-construction (the user cold-started this bot —
    Telegram forbids cold DMs at the transport, and a block surfaces as a 403
    the send handles gracefully); an unknown destination is suppressed (mirrors
    Slack's unbound-channel suppression — never fire a send destined to fail)."""
    from adapters.telegram_adapter import TelegramAdapter
    from database import db

    binding = db.get_telegram_binding(binding_agent)
    if not binding:
        logger.warning(
            "[ent#265] no Telegram binding for binding agent '%s'; cannot "
            "report %s", binding_agent, execution_id)
        return None

    # Destination discrimination — deterministic: group configs exist only for
    # group chats (negative ids) while DM chat links key on positive user ids;
    # the ordered check is belt+braces on top of that disjointness.
    group_cfg = db.get_telegram_group_config(binding["id"], chat_id)
    if group_cfg is not None:
        if not group_cfg.get("is_active") or not group_cfg.get("allow_proactive"):
            logger.info(
                "[ent#265] completion report suppressed for %s: group %s is "
                "inactive or has completion reports muted", binding_agent, chat_id)
            return None
    elif db.get_telegram_chat_link(binding["id"], chat_id) is None:
        logger.info(
            "[ent#265] completion report suppressed for %s: %s is neither a "
            "known group nor a known DM chat link", binding_agent, chat_id)
        return None

    bot_token = db.get_telegram_bot_token(binding_agent)
    if not bot_token:
        logger.warning(
            "[ent#265] no Telegram bot token for binding agent '%s'; cannot "
            "report %s", binding_agent, execution_id)
        return None

    adapter = TelegramAdapter()
    # D1c: Telegram has no per-message sender name — when the report arrives via
    # the binding agent's bot but a DIFFERENT agent did the work, the head line
    # names the worker.
    attribution = executing_agent if binding_agent != executing_agent else None
    # D5 rendering: _markdown_to_html is escape-first (#2277) — `<class
    # 'ValueError'>` / traceback literals survive as text on their own; a
    # pre-escape here would double-escape into visible &amp;lt; artifacts.
    text = adapter.format_response(
        _summarize(status, summary_or_error, executing_agent=attribution)
    )
    # D5 cap: entity expansion can exceed Telegram's 4096 hard cap; "message too
    # long" is a 400 the parse-fallback does not catch → silent loss. A cap cut
    # can leave a dangling tag — the adapter's parse-failure fallback (strip +
    # plain resend) remains the last-resort safety net for that.
    if len(text) > _TELEGRAM_MAX_MESSAGE_CHARS:
        text = text[: _TELEGRAM_MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    # D6 threading: the thread is the triggering message_id — anchor DMs too (a
    # report hours later needs "which request?"); allow_sending_without_reply in
    # the adapter makes a deleted original safe. isdigit() guards the int() cast
    # inside _send_message (outside its try) against a non-numeric carry-over.
    reply_to = thread if (thread and thread.isdigit()) else None

    async def _deliver() -> bool:
        result = await adapter._send_message(
            bot_token,
            chat_id,
            text,
            reply_to_message_id=reply_to,
            parse_mode="HTML",
        )
        if result is None:
            # D4 / AC#5: graceful no-op — bot kicked, blocked, or API failure.
            logger.warning(
                "[ent#265] Telegram completion report failed for %s "
                "(chat %s — bot cannot post?)", execution_id, chat_id)
            return False
        return True

    return _deliver


# D10: per-channel resolver dispatch — a third channel (WhatsApp) is one added
# entry, not another hand-rolled if/else. SUPPORTED_CHANNELS derives from it.
_CHANNEL_RESOLVERS: dict = {
    "slack": _resolve_slack,
    "telegram": _resolve_telegram,
}

SUPPORTED_CHANNELS = frozenset(_CHANNEL_RESOLVERS)


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

    The destination + trigger + binding agent are read from the execution row
    itself, so every terminal chokepoint stays a one-liner and can't drift out
    of sync with what was actually persisted.
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
        resolver = _CHANNEL_RESOLVERS.get(source_channel)
        if resolver is None:
            return False                      # channel without a delivery leg
        if (triggered_by or "") in INLINE_CHANNEL_TRIGGERS:
            return False                      # the adapter already replied — no double-post

        # ent#265 D1: deliver through the bot the user actually addressed.
        # NULL column (direct/legacy rows) falls back to the executing agent —
        # byte-identical pre-#265 behavior.
        executing_agent = getattr(row, "agent_name", None) or agent_name
        binding_agent = getattr(row, "source_channel_agent", None) or executing_agent

        deliver = resolver(
            binding_agent=binding_agent,
            executing_agent=executing_agent,
            chat_id=source_channel_chat_id,
            thread=source_channel_thread,
            status=status,
            summary_or_error=summary_or_error,
            execution_id=execution_id,
        )
        if deliver is None:
            return False        # consent / destination / token suppressed (logged)

        from services.idempotency_service import EffectInProgressError, effect_guard

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
                # D9 pin: ALWAYS the EXECUTING agent (row.agent_name) — NEVER
                # binding_agent. resolve_and_validate_execution fail-opens
                # (returns None → dedup silently DISABLED) on an agent/row
                # mismatch, which would disarm AC#4 for exactly the delegated
                # rows this feature exists for.
                agent_name=executing_agent,
            ) as guard:
                if guard.replay:
                    return False              # already reported — do NOT re-post
                if not await deliver():
                    # D4: failed send claims completed (empty snapshot) — the
                    # at-most-once bias; never blind-retry an ambiguous send.
                    return False
                guard.snapshot = {"reported": True, "channel": source_channel_chat_id}
        except EffectInProgressError:
            # A concurrent attempt holds the claim — never post a second copy.
            return False

        logger.info("[ent#224] reported %s terminal of %s back to %s:%s",
                    status, execution_id, source_channel, source_channel_chat_id)
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
