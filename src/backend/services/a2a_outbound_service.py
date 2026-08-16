"""Outbound A2A orchestration (#736) — everything between auth and the wire.

Split out of `routers/a2a.py` on purpose (the `repo_binding.py` /
`chat_execution_service.py` shape, Invariant #1): the router keeps auth, the
HTTP error map and the audit row; this module owns resolution, validation,
dedup, rate bounds and the activity trail. `routers/a2a.py` already drags in
`task_execution_service`, and adding the orchestration inline would thicken the
one file where the inbound server's own dispatch lives.

Order of operations is load-bearing (M1):

    kill switch → rate bounds → resolve → validate → effect_guard → call → activity

* the **kill switch first**, so a disabled feature costs nothing and reveals
  nothing;
* **bounds before resolution**, so a flood cannot be turned into a DNS
  amplifier;
* **resolve + validate before `effect_guard`**, because the guard's identity is
  built from the resolved endpoint — it does not exist earlier — and because a
  refused URL must not consume an effect claim;
* **the call last**, inside the guard, so a throw releases the claim and a retry
  is possible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from services import a2a_client, a2a_outbound, rate_limiter
from services.a2a_client import A2ACallError
from services.idempotency_service import EffectInProgressError, effect_guard

logger = logging.getLogger(__name__)

# --- Bounds (§32.5 FR-9) ----------------------------------------------------
# Two keys, both through the shared Redis limiter. A per-agent limit bounds ONE
# agent; it does not bound the fleet, and the fleet is the actual exhaustion
# path. A per-process `asyncio.Semaphore` was rejected for two reasons: prod
# runs `--workers 2`, so the real ceiling would be `N x workers` and would drift
# with the worker count; and `async with sem` *blocks*, recreating the coroutine
# hold it was meant to prevent. `rate_limiter` is the codebase's actual
# fleet-bound primitive (cf. `redelivery:fleet`), it is non-blocking, and it
# refuses rather than queues.
A2A_AGENT_RATE_LIMIT = 30
A2A_AGENT_RATE_WINDOW = 60
A2A_FLEET_RATE_LIMIT = 120
A2A_FLEET_RATE_WINDOW = 60


class A2AOutboundDisabled(Exception):
    """The `A2A_OUTBOUND_ENABLED` kill switch is off."""


class A2AEndpointNotFound(Exception):
    """No endpoint matched the caller's reference (or no provider resolved one)."""


@dataclass
class OutboundOutcome:
    """What the router turns into a response + an audit row."""

    result: a2a_client.A2AResult
    endpoint_id: str
    endpoint_name: str
    replayed: bool = False


def is_outbound_enabled() -> bool:
    """`system_settings` row → `A2A_OUTBOUND_ENABLED` env → **OFF** (§32.5 FR-11).

    The brain-orb resolution shape, chosen so the primary leg is the DB row: a
    compose file that forgets the variable can then never make the feature
    unreachable, which is the failure mode this codebase has shipped six times.
    Runtime-resolved, so an admin flip needs no restart.
    """
    from services.settings_service import settings_service

    return settings_service._resolve_bool_flag(
        "a2a_outbound_enabled", "A2A_OUTBOUND_ENABLED", default=False
    )


def _enforce_bounds(agent_name: str) -> None:
    rate_limiter.enforce(
        f"a2a_out:agent:{agent_name}",
        A2A_AGENT_RATE_LIMIT,
        A2A_AGENT_RATE_WINDOW,
        detail="Too many outbound A2A calls from this agent.",
    )
    rate_limiter.enforce(
        "a2a_out:fleet",
        A2A_FLEET_RATE_LIMIT,
        A2A_FLEET_RATE_WINDOW,
        detail="Too many outbound A2A calls across the fleet.",
    )


async def _record_activity(agent_name: str, endpoint_name: str, host: str,
                           state: str, error: Optional[str] = None) -> None:
    """One `agent_activities` row per outbound call (F12).

    The audit log is admin-gated and unwatched; `agent_activities` is the stream
    that already answers "what is this agent doing", and it is what the Dashboard
    timeline renders. #1804's terminal-close contract does NOT apply — this is
    not an execution terminal and mints no `schedule_executions` row — so the row
    is written already-closed rather than opened and closed.

    Fail-open: an observability write must never turn a completed outbound call
    into an error. `details` carries the endpoint NAME and HOST only, never the
    URL, the message or the credential.
    """
    try:
        from models import ActivityState, ActivityType
        from services.activity_service import activity_service

        activity_id = await activity_service.track_activity(
            agent_name=agent_name,
            activity_type=ActivityType.AGENT_COLLABORATION,
            triggered_by="agent",
            details={
                "direction": "a2a_outbound",
                "endpoint": endpoint_name,
                "host": host,
                "state": state,
            },
        )
        await activity_service.complete_activity(
            activity_id,
            status=ActivityState.COMPLETED if error is None else ActivityState.FAILED,
            error=error,
        )
    except Exception:  # noqa: BLE001 — observability never breaks the call
        logger.warning("[a2a_outbound] activity write failed for %s", agent_name, exc_info=True)


async def call_agent(
    *,
    agent_name: str,
    endpoint_ref: str,
    message: str,
    dedup_label: str,
    context_id: Optional[str] = None,
    task_id: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> OutboundOutcome:
    """Resolve, validate, dedup and place one outbound A2A call."""
    if not is_outbound_enabled():
        raise A2AOutboundDisabled()

    _enforce_bounds(agent_name)

    endpoint = a2a_outbound.resolve_endpoint(agent_name, endpoint_ref)
    if endpoint is None:
        raise A2AEndpointNotFound(endpoint_ref)

    # Validate BEFORE claiming an effect key: a refused URL must not burn one.
    validated = await a2a_client.validate_endpoint(endpoint.url)

    # #1084 effect identity. `{endpoint_id, resolved_url, context_id, task_id}`
    # plus a REQUIRED `dedup_label` — deliberately NOT the `send_message` shape
    # of "one effect per recipient per turn". That shape is right for a
    # notification sink and WRONG for a request/response conversation: a second
    # question to the same endpoint inside one execution would read as a
    # completed replay and return the answer to the FIRST question, with no
    # error and nothing logged. The message body is deliberately absent from the
    # key (#1084's rule — an LLM body is non-deterministic across a re-run and
    # would defeat re-delivery dedup entirely), so the caller's explicit label is
    # what distinguishes two calls, and requiring it keeps the agent's intent
    # legible instead of inferred.
    identity = {
        "endpoint_id": endpoint.id,
        "resolved_url": endpoint.url,
        "context_id": context_id or "",
        "task_id": task_id or "",
    }

    try:
        async with effect_guard(
            "a2a_call",
            identity,
            execution_id=execution_id,
            agent_name=agent_name,
            dedup_label=dedup_label,
        ) as guard:
            if guard.replay:
                snapshot = guard.snapshot if isinstance(guard.snapshot, dict) else {}
                return OutboundOutcome(
                    result=a2a_client.A2AResult(
                        state=str(snapshot.get("state") or "unknown"),
                        text=snapshot.get("text"),
                        task_id=snapshot.get("task_id"),
                        context_id=snapshot.get("context_id"),
                        truncated=bool(snapshot.get("truncated")),
                        protocol_version=str(snapshot.get("protocol_version") or "0.3"),
                        host=str(snapshot.get("host") or validated.hostname),
                    ),
                    endpoint_id=endpoint.id,
                    endpoint_name=endpoint.name,
                    replayed=True,
                )

            result = await a2a_client.call_endpoint(
                endpoint_url=endpoint.url,
                credential=endpoint.credential,
                message=message,
                context_id=context_id,
                task_id=task_id,
                validated=validated,
            )
            guard.snapshot = {
                "state": result.state,
                "text": result.text,
                "task_id": result.task_id,
                "context_id": result.context_id,
                "truncated": result.truncated,
                "protocol_version": result.protocol_version,
                "host": result.host,
            }
    except EffectInProgressError:
        raise
    except A2ACallError as exc:
        await _record_activity(agent_name, endpoint.name, validated.hostname,
                               "failed", error=exc.reason)
        raise

    await _record_activity(agent_name, endpoint.name, result.host, result.state)
    return OutboundOutcome(
        result=result, endpoint_id=endpoint.id, endpoint_name=endpoint.name
    )


async def poll_task(
    *,
    agent_name: str,
    endpoint_ref: str,
    task_id: str,
) -> OutboundOutcome:
    """Poll a remote task on a registered endpoint.

    Deliberately **not** `effect_guard`-wrapped: a poll is a read, so deduping
    it would return a stale snapshot for the very question the caller is asking
    ("has it finished yet?") — the C2 bug in miniature. It still passes the same
    bounds, because a poll is still an egress.
    """
    if not is_outbound_enabled():
        raise A2AOutboundDisabled()

    _enforce_bounds(agent_name)

    endpoint = a2a_outbound.resolve_endpoint(agent_name, endpoint_ref)
    if endpoint is None:
        raise A2AEndpointNotFound(endpoint_ref)

    validated = await a2a_client.validate_endpoint(endpoint.url)
    result = await a2a_client.get_task(
        endpoint_url=endpoint.url,
        credential=endpoint.credential,
        task_id=task_id,
        validated=validated,
    )
    return OutboundOutcome(
        result=result, endpoint_id=endpoint.id, endpoint_name=endpoint.name
    )


def audit_details(outcome: OutboundOutcome, *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Audit `details` — endpoint id/name, HOST, state, remote task id. Nothing else.

    Never the full URL (it may carry a path or query the operator considers
    sensitive, and audit rows are durable), never the message, never the
    credential, never the response text.
    """
    details: Dict[str, Any] = {
        "endpoint_id": outcome.endpoint_id,
        "endpoint": outcome.endpoint_name,
        "host": outcome.result.host,
        "state": outcome.result.state,
        "protocol_version": outcome.result.protocol_version,
    }
    if outcome.result.task_id:
        details["remote_task_id"] = outcome.result.task_id
    if outcome.replayed:
        details["replayed"] = True
    if extra:
        details.update(extra)
    return details
