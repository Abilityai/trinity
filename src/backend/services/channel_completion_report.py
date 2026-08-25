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
# ent#457 adds "public": a Workspace turn is synchronous too — `portal_chat`
# persists the assistant reply itself — so the turn's OWN execution must never
# be reported or every chat message would be followed by a duplicate "done".
# Public links and x402 share this trigger and are unaffected: they carry no
# `source_channel_chat_id`, so they never reach this check.
INLINE_CHANNEL_TRIGGERS = frozenset({"slack", "telegram", "whatsapp", "public"})

_MAX_REPORT_CHARS = 2800

# Telegram sendMessage hard cap. Applied AFTER markdown→HTML conversion +
# entity escaping (D5): expansion can push 2800 source chars past 4096, and
# "message too long" is a plain 400 the adapter's parse-failure fallback does
# NOT catch — the report would be silently lost.
_TELEGRAM_MAX_MESSAGE_CHARS = 4096


def _sanitized_detail(summary_or_error: Optional[str]) -> str:
    """Credential-sanitise, THEN truncate. The one rule every channel uses.

    Review finding: this was inline in `_summarize` (Slack/Telegram) and the
    portal leg did a bare `strip()` and slice — no sanitizer anywhere on its
    path. The failure call sites pass raw text (`_write_terminal_and_gate`
    passes `error`; `apply_result`'s failure branch passes `envelope.error` —
    only the SUCCESS branch passes an already-sanitized string), so a traceback
    carrying `ANTHROPIC_API_KEY=sk-ant-…` or a `https://x:ghp_…@github.com/…`
    clone URL was written verbatim into an EXTERNAL client's Workspace thread,
    permanently, and replayed into the agent's own history context on the next
    cold turn. It also contradicted #2320's rule that raw failure text is
    operator-only.

    Extracted rather than copied: a second implementation of a redaction rule is
    a second place for it to be forgotten, which is how this happened once.

    Order is load-bearing. Sanitise over a 2x window BEFORE truncating (the
    #1578 emit chokepoint does the same, `event_dispatch_service.py:311`): a
    bare slice can cut a secret so the redaction pattern no longer matches and
    the tail survives. Truncation is then decided on what was actually cut, not
    against `raw` — redaction changes length and would append a phantom ellipsis
    to text that was never truncated.
    """
    from utils.credential_sanitizer import sanitize_text

    raw = (summary_or_error or "").strip()
    window = raw[: _MAX_REPORT_CHARS * 2]
    cleaned = sanitize_text(window)
    truncated = len(cleaned) > _MAX_REPORT_CHARS or len(raw) > len(window)
    body = cleaned[:_MAX_REPORT_CHARS].rstrip()
    if truncated:
        body += "…"
    return body


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
    # Sanitise-then-truncate lives in `_sanitized_detail`, shared with the
    # portal leg — a channel is a persistent, externally hosted, human-visible
    # surface, so this is the last place to skip it.
    body = _sanitized_detail(summary_or_error)
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


def _resolve_portal(
    *,
    binding_agent: str,
    executing_agent: str,
    chat_id: str,
    thread: Optional[str],
    status: str,
    summary_or_error: Optional[str],
    execution_id: str,
) -> Optional[Callable[[], Awaitable[bool]]]:
    """Deliver a terminal into the Workspace chat that started it (ent#457).

    The Workspace was the one surface with no report-back: #2157 stamped portal
    rows with the SURFACE but never a destination, so every one of them died at
    `report_completion`'s `if not source_channel_chat_id` gate. `chat_id` is the
    portal session, stamped at both turn-creation sites.

    **Consent is by construction, like a Telegram DM** (ent#265): the session
    belongs to one client, and delivering into a person's own conversation with
    the agent they are talking to is what the conversation is for. There is no
    `allow_proactive` flag to consult because there is no third party to protect
    — which is exactly why the destination is read from the SESSION ROW rather
    than from the execution's stamp alone. A stamp is a string that rode through
    an inheritance chain; the session row is the platform's own record of whose
    chat this is, and it is what decides the recipient.

    Delivery is a persisted assistant message, so it renders through the same
    history the client already reads.

    **Durable, but not immediately visible** (review finding). An earlier
    version of this docstring claimed the Workspace polls its threads. It does
    not: `PortalConversation.vue` loads history only on mount and on an
    agent/session prop change, `stores/clientPortal.js` states outright that
    `refreshThreads()` is event-driven rather than periodic, and the only
    interval in the Workspace is the 20s asks poll on a different surface. So a
    client sitting on the thread sees the report at their next reload or thread
    switch — the row is never lost, but "it appears while they watch" was not
    true, and AC #7's "degrades to poll" does not hold by construction here. An
    idle history poll is the obvious follow-up (the asks poll is the precedent);
    this docstring stops asserting it in the meantime.

    **Known limitation — a report landing mid-turn can be read as that turn's
    answer.** `PortalConversation` decides a reply arrived with
    `assistants.length > baseline` (a count taken before dispatch) and returns
    the LAST assistant row. That heuristic assumed `portal_chat` was the only
    writer of assistant rows in a session, and this resolver is now a second
    one — so a delegated child finishing while the parent still works can have
    its report returned as the reply. Fixing it properly needs a per-row
    discriminator the table does not have (`enterprise_portal_messages` carries
    no `execution_id`), i.e. a dual-track migration; overloading `role` was
    rejected because two server-side readers and the client's rendering all
    branch on it.
    """
    from client_portal import db as portal_db

    session = portal_db.get_portal_session_by_id(chat_id)
    if not session:
        logger.info(
            "[ent#457] portal completion suppressed: session %s no longer exists "
            "(execution %s)", chat_id, execution_id,
        )
        return None
    # The report belongs to the agent whose chat this is. A delegated child may
    # execute as a DIFFERENT agent (A asks B); the client is talking to A, so
    # the message is filed under A and names B in the body.
    session_agent = session.get("agent_name") or binding_agent
    client_email = session.get("client_email")
    if not client_email:
        logger.info(
            "[ent#457] portal completion suppressed: session %s has no client "
            "(execution %s)", chat_id, execution_id,
        )
        return None

    body = _portal_body(
        executing_agent=executing_agent,
        session_agent=session_agent,
        status=status,
        summary_or_error=summary_or_error,
    )

    async def deliver() -> bool:
        import asyncio
        import uuid as _uuid
        from utils.helpers import utc_now_iso

        def _write() -> None:
            now = utc_now_iso()
            portal_db.add_portal_message(
                _uuid.uuid4().hex, session_agent, client_email, "assistant", body,
                None, now, session_id=chat_id,
            )
            # Every other writer of a portal message touches its session, and
            # this one has to for the same reason: `last_message_at` is what
            # orders the sidebar. The unread badge is safe either way — ent#359
            # counts message ROWS against a read cursor — but a badge on a
            # thread that has not moved is a notification pointing at the middle
            # of a list. A report nobody notices is the same silence this
            # contract exists to end.
            portal_db.touch_portal_session(chat_id, now, added=1)

        try:
            # Review finding: these are synchronous SQLAlchemy writes, and they
            # ran directly on the event loop. Both existing resolvers do their
            # I/O with `await` (httpx). A batch of delegated terminals firing at
            # 03:30, while `db_backup_service` holds SQLite's read lock, blocks
            # the single backend loop for up to the 30s busy timeout PER TASK —
            # stalling every in-flight chat, heartbeat and WS fan-out on that
            # worker. architecture.md records the same rule for the ent#433
            # headroom write: try/except handles errors, not blocking.
            await asyncio.to_thread(_write)
        except Exception:  # noqa: BLE001
            # Review finding: without this, a raise escapes into `effect_guard`,
            # which calls `fail()` and RELEASES the claim — so a re-delivered
            # terminal (a #1083 callback, the lease-reaper) finds no claim and
            # appends a SECOND identical report. `add_portal_message` commits
            # before `touch_portal_session` runs, so a transient
            # "database is locked" on the second write is exactly that shape.
            #
            # Both existing resolvers catch and return False for this reason —
            # the file documents it as "D4: failed send claims completed — the
            # at-most-once bias; never blind-retry an ambiguous send." The
            # portal leg was the one that opted out.
            logger.exception(
                "[ent#457] portal completion delivery failed for session %s (execution %s)",
                chat_id, execution_id,
            )
            return False
        return True

    return deliver


def _portal_body(*, executing_agent: str, session_agent: str, status: str,
                 summary_or_error: Optional[str]) -> str:
    """The message a person reads when background work finishes.

    Honest about failure (AC #3: "never a silent vanish") and about WHO did the
    work when it was delegated — a client talking to A should not see B's result
    appear with no explanation of where it came from.
    """
    done = status == "success"
    who = "" if executing_agent == session_agent else f" ({executing_agent})"
    head = f"**Finished{who}**" if done else f"**Didn't finish{who}** — {status}"
    # The same sanitise-then-truncate rule the other channels use. A Workspace
    # thread is a persistent, client-visible surface — if anything, MORE exposed
    # than a Slack channel, since the reader is an external client.
    detail = _sanitized_detail(summary_or_error)
    return f"{head}\n\n{detail}" if detail else head


# D10: per-channel resolver dispatch — a fourth channel is one added entry,
# not another hand-rolled if/else. SUPPORTED_CHANNELS derives from it.
_CHANNEL_RESOLVERS: dict = {
    "slack": _resolve_slack,
    "telegram": _resolve_telegram,
    "portal": _resolve_portal,
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
