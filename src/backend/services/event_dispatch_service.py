"""Event dispatch service (EVT-001 delivery + #1578 system-emitted terminals).

This module owns the **delivery** half of the agent event pub/sub system:

* ``trigger_subscription`` / ``_interpolate_template`` / ``_get_internal_token`` —
  extracted verbatim from ``routers/event_subscriptions.py`` (Invariant #1: a
  service must not import a router, yet ``task_execution_service`` — a service —
  needs the dispatch primitive). The router now imports these back.
* ``emit_task_terminal_event`` / ``spawn_task_terminal_event`` (#1578) — the
  single shared helper the backend fires at **every CAS-won execution terminal**
  to synthesize ``agent.task.completed`` / ``agent.task.failed`` and deliver them
  over the SAME EVT-001 subscription-dispatch path. This is the first
  **system-emitted** event producer (no LLM in the loop); every event before it
  was **agent-emitted** (an agent's LLM calling ``emit_event``).

**Delivery is pull-transitional and best-effort.** ``trigger_subscription`` is an
HTTP loopback (``POST /api/agents/{subscriber}/task``) minted with a short-lived
admin JWT — a service self-calling its own HTTP API. It wakes a subscriber whose
container is **running** (including a #1402 parked-but-running orchestrator); a
stopped subscriber's 503 is swallowed (the ``agent_events`` row persists, the
wake does not). The durable "reply lands in the caller's queue" successor is the
pull migration's queue (Epic #1045/#1081) — this loopback is NOT a stable
contract.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from database import db
from utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reserved-namespace contract (#1578)
# ---------------------------------------------------------------------------

# System-emitted task-terminal events live under this reserved prefix. Agents
# may NOT emit into it (spoofed completions) and may NOT self-subscribe to it
# (trivial self-wake loop) — enforced in routers/event_subscriptions.py.
RESERVED_EVENT_PREFIX = "agent.task."
TASK_COMPLETED_EVENT = "agent.task.completed"
TASK_FAILED_EVENT = "agent.task.failed"

# Recursion-break (the decisive loop guard): a task spawned by an ``agent.task.*``
# subscription dispatch is tagged with this reserved ``triggered_by`` value at the
# internal ``/task`` boundary, and ``emit_task_terminal_event`` suppresses its own
# emission when the terminating execution carries it. This breaks self / A↔B /
# A→B→C→A auto-emit cycles at the root — the autonomous-runaway class deterministic
# backend auto-emit would otherwise introduce (each hop is a full LLM turn + spend).
# ``"event"`` is already a reserved value in ``_AUTONOMOUS_TRIGGERS`` (never set by
# any other producer), so it is a safe, convention-aligned sentinel.
RESERVED_EVENT_TRIGGER = "event"
# The loopback header ``trigger_subscription`` stamps on a reserved-namespace
# dispatch; ``routers/chat.py`` reads it and persists ``RESERVED_EVENT_TRIGGER``.
RESERVED_EVENT_TRIGGER_HEADER = "X-Event-Trigger"
RESERVED_EVENT_TRIGGER_HEADER_VALUE = "agent_task"

# Truncate the (best-effort, content-trusted) summary/error injected into a
# subscriber's task prompt. Credential-sanitized at the emit chokepoint below
# (`emit_task_terminal_event`) so the payload is uniformly safe whatever the
# producer — the success/pull terminals also sanitize upstream, but the failure
# error strings (`envelope.error` / `str(exc)`) reach the helper raw. It is still
# worker output — the same interpolation surface EVT-001 already has, now
# produced deterministically.
TASK_EVENT_SUMMARY_MAX = 2000


def _internal_dispatch_secret() -> str:
    """The shared secret proving a ``/task`` dispatch originated from backend
    internals — the C-003 ``X-Internal-Secret`` contract (``INTERNAL_API_SECRET``
    env, ``SECRET_KEY`` fallback), read the same way ``routers/internal.py`` reads
    it. Read at call time so a rotated secret is picked up without a restart."""
    import os
    from config import SECRET_KEY

    return os.getenv("INTERNAL_API_SECRET") or SECRET_KEY


def verify_internal_dispatch_secret(provided: Optional[str]) -> bool:
    """Constant-time check that ``provided`` is the backend-internal dispatch
    secret. The ``/task`` router calls this to authenticate the #1578
    recursion-break header: only ``trigger_subscription`` (backend, which stamps
    ``X-Internal-Secret``) can make a spawned execution persist
    ``triggered_by="event"`` — an external ``/task`` caller spoofing
    ``X-Event-Trigger`` alone is ignored, so it cannot suppress a real agent's
    completion event."""
    import hmac

    if not provided:
        return False
    return hmac.compare_digest(provided, _internal_dispatch_secret())


# ---------------------------------------------------------------------------
# Extracted EVT-001 delivery primitives (moved verbatim from the router)
# ---------------------------------------------------------------------------

def _interpolate_template(template: str, payload: dict) -> str:
    """
    Replace {{payload.field}} placeholders with actual values.

    Supports nested access: {{payload.nested.field}}
    Missing fields are left as-is.
    """
    def replacer(match):
        path = match.group(1)  # e.g., "payload.pred_id"
        parts = path.split(".")
        # Skip the leading "payload" prefix
        if parts and parts[0] == "payload":
            parts = parts[1:]
        value = payload
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return match.group(0)  # Leave placeholder as-is
        return str(value)

    return re.sub(r"\{\{(payload(?:\.[a-zA-Z0-9_]+)+)\}\}", replacer, template)


def _get_internal_token() -> str:
    """Get a JWT token for internal API calls."""
    from jose import jwt
    from config import SECRET_KEY, ALGORITHM
    from datetime import datetime, timedelta

    payload = {
        "sub": "admin",
        "exp": datetime.utcnow() + timedelta(minutes=5),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def trigger_subscription(subscription, event):
    """
    Send an async task to the subscribing agent with the interpolated message.

    Uses the backend's internal task endpoint to avoid circular MCP calls.

    #1578 recursion-break: when the dispatched event is in the reserved
    ``agent.task.*`` namespace, stamp the loopback ``/task`` with the
    ``RESERVED_EVENT_TRIGGER_HEADER`` so the spawned execution persists
    ``triggered_by="event"`` and does NOT itself re-emit a completion event.
    """
    import httpx

    # Interpolate payload into target message
    message = subscription.target_message
    if event.payload:
        message = _interpolate_template(message, event.payload)

    # Add event context to the message
    message = (
        f"[Event from {event.source_agent}: {event.event_type}]\n\n"
        f"{message}"
    )

    headers = {
        "Authorization": f"Bearer {_get_internal_token()}",
        "X-Source-Agent": event.source_agent,
        "X-Via-MCP": "true",
    }
    # #1578: tag reserved-namespace dispatches so the spawned task's terminal is
    # suppressed by the recursion-break (no A→B→A auto-emit loop). The tag is
    # authenticated as backend-internal via the C-003 `X-Internal-Secret` so an
    # external `/task` caller can't spoof `X-Event-Trigger` to suppress a real
    # agent's completion event (the router verifies it before honoring the tag).
    if str(event.event_type).startswith(RESERVED_EVENT_PREFIX):
        headers[RESERVED_EVENT_TRIGGER_HEADER] = RESERVED_EVENT_TRIGGER_HEADER_VALUE
        headers["X-Internal-Secret"] = _internal_dispatch_secret()

    try:
        # Call the agent's task endpoint directly (async, fire-and-forget)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"http://localhost:8000/api/agents/{subscription.subscriber_agent}/task",
                json={
                    "message": message,
                    "async_mode": True,  # Fire-and-forget
                    "system_prompt": (
                        f"This task was triggered by an event subscription. "
                        f"Source agent: {event.source_agent}, "
                        f"Event type: {event.event_type}, "
                        f"Event ID: {event.id}"
                    ),
                },
                headers=headers,
            )
            if response.status_code >= 400:
                logger.warning(
                    f"[EVT-001] Failed to trigger subscription {subscription.id} "
                    f"on {subscription.subscriber_agent}: {response.status_code} {response.text[:200]}"
                )
            else:
                logger.info(
                    f"[EVT-001] Triggered subscription {subscription.id}: "
                    f"{event.source_agent}.{event.event_type} -> {subscription.subscriber_agent}"
                )
    except Exception as e:
        logger.error(
            f"[EVT-001] Error triggering subscription {subscription.id} "
            f"on {subscription.subscriber_agent}: {e}"
        )


# ---------------------------------------------------------------------------
# System-emitted task-terminal events (#1578)
# ---------------------------------------------------------------------------

# Strong references to in-flight emit tasks — prevents the asyncio weak-ref GC
# footgun (a fire-and-forget task GC'd mid-flight before it awaits). Mirrors the
# `_spawn_bg` pattern in task_execution_service.
_inflight_emit_tasks: "set[asyncio.Task[Any]]" = set()


def _event_type_for_status(terminal_status: str) -> str:
    """Map a persisted terminal status to its reserved event type.

    Branch on the STATUS string (``success``/``failed``/``cancelled``), NEVER on
    ``TaskExecutionErrorCode`` identity — the fieldless ``@dataclass`` str-Enum
    gives every member a zero-field ``__eq__`` that is True for ANY two members
    (#1085 footgun). SUCCESS → completed; every other terminal → failed (the
    precise terminal — failed/cancelled — is carried in the payload ``status``).
    """
    from models import TaskExecutionStatus

    return (
        TASK_COMPLETED_EVENT
        if terminal_status == TaskExecutionStatus.SUCCESS
        else TASK_FAILED_EVENT
    )


def _is_reserved_event_triggered(execution) -> bool:
    """True when this execution was itself spawned by an ``agent.task.*`` dispatch.

    The recursion-break: such an execution must NOT re-emit a terminal event, or
    two mutually-subscribed agents would wake each other forever. Keyed on the
    persisted ``triggered_by == "event"`` sentinel (``RESERVED_EVENT_TRIGGER``).
    """
    return (
        execution is not None
        and getattr(execution, "triggered_by", None) == RESERVED_EVENT_TRIGGER
    )


async def emit_task_terminal_event(
    agent_name: str,
    execution_id: Optional[str],
    *,
    terminal_status: str,
    summary_or_error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost: Optional[float] = None,
) -> None:
    """Emit ``agent.task.completed`` / ``agent.task.failed`` for a CAS-won terminal.

    The single shared helper invoked (fire-and-forget) from EVERY CAS-won terminal
    writer (see the coverage table in the feature-flow). Contract:

    * **Matching-subscription gated (AC #1/#5)** — ``find_matching_event_subscriptions``
      runs before any persistence: zero matches ⇒ NO ``agent_events`` row, NO
      dispatch. Inert by default.
    * **Recursion-break** — a terminating execution whose ``triggered_by`` is the
      reserved ``"event"`` sentinel emits nothing (breaks auto-emit cycles).
    * **Fail-open** — the entire body is wrapped in try/except that logs and
      swallows. A broken/slow emit NEVER affects the (already billed) terminal;
      the caller invokes this fire-and-forget.

    Callers pass ``terminal_status`` + ``summary_or_error`` (+ optional
    ``duration_ms``/``cost``); ``triggered_by``/``fan_out_id``/``loop_id`` — and
    ``duration_ms``/``cost`` fallbacks — are read once from the execution row.
    """
    try:
        if not execution_id:
            return

        event_type = _event_type_for_status(terminal_status)

        # Single row read: recursion-break origin + payload correlation fields.
        execution = None
        try:
            execution = db.get_execution(execution_id)
        except Exception as e:  # fail-open: a read failure must not break the terminal
            logger.debug("[#1578] get_execution(%s) failed: %s", execution_id, e)

        if _is_reserved_event_triggered(execution):
            # Suppress — this task was spawned BY an agent.task.* dispatch.
            return

        # AC #1/#5: emit nothing (no row, no dispatch) when nobody is listening.
        matching_subs = db.find_matching_event_subscriptions(agent_name, event_type)
        if not matching_subs:
            return

        # Resolve the precise persisted status string + correlation fields.
        row_status = getattr(execution, "status", None) or terminal_status
        status_value = row_status.value if hasattr(row_status, "value") else str(row_status)

        summary = summary_or_error
        if summary is not None:
            # Redact credentials at this single chokepoint so the payload is
            # uniformly safe whatever the producer (the failure error strings
            # reach here un-sanitized). Sanitize a bounded 2×cap window BEFORE the
            # final truncation so a secret straddling the cap boundary is still
            # fully matched+redacted (a bare `[:cap]` slice could leave an
            # unmatchable secret head), then truncate for delivery.
            summary = sanitize_text(str(summary)[: TASK_EVENT_SUMMARY_MAX * 2])[
                :TASK_EVENT_SUMMARY_MAX
            ]

        payload = {
            "execution_id": execution_id,
            "status": status_value,
            "triggered_by": getattr(execution, "triggered_by", None),
            "summary_or_error": summary,
            "duration_ms": duration_ms
            if duration_ms is not None
            else getattr(execution, "duration_ms", None),
            "cost": cost if cost is not None else getattr(execution, "cost", None),
            "fan_out_id": getattr(execution, "fan_out_id", None),
            "loop_id": getattr(execution, "loop_id", None),
        }

        event = db.create_agent_event(
            source_agent=agent_name,
            event_type=event_type,
            payload=payload,
            subscriptions_triggered=len(matching_subs),
        )

        logger.info(
            "[#1578] System event %s.%s emitted for execution %s (%d subscription(s) matched)",
            agent_name,
            event_type,
            execution_id,
            len(matching_subs),
        )

        for sub in matching_subs:
            _spawn_emit_dispatch(trigger_subscription(sub, event))
    except Exception as e:  # noqa: BLE001 — fail-open: never affect the billed terminal
        logger.warning(
            "[#1578] emit_task_terminal_event failed for %s/%s: %s",
            agent_name,
            execution_id,
            e,
        )


def _spawn_emit_dispatch(coro: "Any") -> None:
    """Schedule a dispatch coroutine with a strong reference held until done."""
    task = asyncio.create_task(coro)
    _inflight_emit_tasks.add(task)
    task.add_done_callback(_inflight_emit_tasks.discard)


def spawn_task_terminal_event(
    agent_name: str,
    execution_id: Optional[str],
    *,
    terminal_status: str,
    summary_or_error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost: Optional[float] = None,
) -> None:
    """Fan an execution terminal out to everything that reacts to one.

    Every CAS-won terminal writer calls this one wrapper — it needs no ``await``
    and no per-module spawner. Requires a running event loop (every caller runs
    inside one: the async terminal writers and the async router handlers that
    drive the pull sink). Fail-open: if no loop is running the work is skipped
    (logged), never raised.

    Two consumers today, spawned independently so neither can delay or break the
    other:

    * ``emit_task_terminal_event`` (#1578) — the agent.task.* pub/sub emit.
    * ``_terminal_side_effects`` — the orchestration primitives that used to hold
      their state in a coroutine and now react to terminals instead: the loop
      advance (#2523) and the fan-out join (#2524).

    Both hang HERE rather than inside the emit, because the emit returns early
    when no event subscription matches — the common case — so anything nested
    inside it would almost never run.
    """
    _spawn_named(
        "#1578",
        emit_task_terminal_event(
            agent_name,
            execution_id,
            terminal_status=terminal_status,
            summary_or_error=summary_or_error,
            duration_ms=duration_ms,
            cost=cost,
        ),
    )
    _spawn_named("#2523/#2524", _terminal_side_effects(execution_id))


async def _terminal_side_effects(execution_id: Optional[str]) -> None:
    """React to one execution terminal on behalf of the orchestrators.

    Lazy-import shim: ``loop_service`` and ``fan_out_service`` both import
    ``task_execution_service``, which imports THIS module — a top-level import
    either way would close the cycle. Each consumer is guarded separately so a
    fault in one cannot skip the other, and neither can raise: this runs on the
    path of an already-billed terminal.
    """
    if not execution_id:
        return
    try:
        from services.loop_service import advance_loop_on_terminal

        await advance_loop_on_terminal(execution_id)
    except Exception as e:  # noqa: BLE001 — never affect the billed terminal
        logger.warning("[#2523] loop advance failed for %s: %s", execution_id, e)
    try:
        from services.fan_out_service import join_fan_out_on_terminal

        await join_fan_out_on_terminal(execution_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[#2524] fan-out join failed for %s: %s", execution_id, e)


def _spawn_named(tag: str, coro: "Any") -> None:
    try:
        _spawn_emit_dispatch(coro)
    except RuntimeError as e:
        # No running loop — close the un-awaited coroutine to avoid a warning.
        coro.close()
        logger.debug("%s terminal fan-out skipped (no loop): %s", tag, e)
