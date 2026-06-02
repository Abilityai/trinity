"""
Idempotency-key enforcement service (RELIABILITY-006, #525).

Thin orchestration over `db.idempotency_*`. Routers call `begin()` at a trigger
boundary; a replay short-circuits the duplicate, a first-seen key proceeds and
is finalized with `complete()` (or released with `fail()` on dispatch failure).

The "single funnel" (`TaskExecutionService`) is not actually single — sync
`/chat` runs an inline path and `/api/webhooks/{token}` creates no execution at
all — so enforcement lives at each router boundary, backed by this service.

Header is OPTIONAL on chat/task/MCP (absent → no dedup, full back-compat). The
webhook boundary auto-derives a key from `(token, body_hash)` so naive senders
that retry without idempotency awareness are still covered. The scheduler sends
a deterministic key derived from the per-fire execution_id.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from database import db
from db.idempotency import STATE_COMPLETED, STATE_IN_FLIGHT, STATE_NEW

logger = logging.getLogger(__name__)


@dataclass
class IdempotencyDecision:
    """Outcome of begin() at a trigger boundary."""
    enabled: bool                      # False when no key supplied (dedup off)
    replay: bool                       # True → caller must NOT dispatch again
    in_flight: bool                    # replay of a still-running claim → 409
    scope: Optional[str] = None
    key: Optional[str] = None
    execution_id: Optional[str] = None
    snapshot: Optional[dict] = None


# ---------------------------------------------------------------------------
# Scope + key derivation
# ---------------------------------------------------------------------------

def make_agent_scope(agent_name: str) -> str:
    """Scope execution-creating boundaries per agent (cross-tenant isolation)."""
    return f"agent:{agent_name}"


def make_webhook_scope(token: str) -> str:
    """Scope the webhook trigger boundary per webhook token."""
    return f"webhook:{token}"


def derive_webhook_key(token: str, body: Optional[bytes]) -> str:
    """Stable key from (token, body) for naive webhook senders.

    SHA-256 over token + raw body bytes. Header-independent, so a sender that
    retries the same POST resolves to the same key. Different bodies (distinct
    intentional triggers) get distinct keys.
    """
    h = hashlib.sha256()
    h.update(token.encode("utf-8"))
    h.update(b"\x00")
    h.update(body or b"")
    return f"auto:{h.hexdigest()}"


def derive_schedule_key(execution_id: str) -> str:
    """Deterministic key for scheduler dispatch.

    The scheduler creates one execution_id per fire and reuses it across an
    HTTP-level resend of the same dispatch (the network-blip case #525 targets),
    so the execution_id is the natural per-fire idempotency token. Intentional
    #271 retries create a fresh execution_id → fresh key → not suppressed.
    """
    return f"sched:{execution_id}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def begin(scope: str, key: Optional[str]) -> IdempotencyDecision:
    """Claim (scope, key). No-op decision when key is falsy (dedup disabled)."""
    if not key:
        return IdempotencyDecision(enabled=False, replay=False, in_flight=False)
    try:
        res = db.idempotency_claim(scope, key)
    except Exception as e:  # fail-open: never block a real execution on the dedup layer
        logger.warning("Idempotency claim failed (scope=%s) — proceeding without dedup: %s", scope, e)
        return IdempotencyDecision(enabled=False, replay=False, in_flight=False)

    state = res.get("state")
    if state == STATE_NEW:
        return IdempotencyDecision(enabled=True, replay=False, in_flight=False, scope=scope, key=key)
    if state == STATE_IN_FLIGHT:
        return IdempotencyDecision(
            enabled=True, replay=True, in_flight=True, scope=scope, key=key,
            execution_id=res.get("execution_id"),
        )
    if state == STATE_COMPLETED:
        return IdempotencyDecision(
            enabled=True, replay=True, in_flight=False, scope=scope, key=key,
            execution_id=res.get("execution_id"), snapshot=res.get("snapshot"),
        )
    # Unknown state — treat as no-dedup rather than wedge the caller.
    logger.warning("Idempotency claim returned unknown state %r — proceeding", state)
    return IdempotencyDecision(enabled=False, replay=False, in_flight=False)


def attach_execution(decision: IdempotencyDecision, execution_id: Optional[str]) -> None:
    """Record the execution_id on a fresh claim once it's known (best-effort)."""
    if not decision.enabled or decision.replay or not execution_id:
        return
    try:
        db.idempotency_attach_execution(decision.scope, decision.key, execution_id)
    except Exception as e:
        logger.warning("Idempotency attach_execution failed: %s", e)


def complete(decision: IdempotencyDecision, execution_id: Optional[str], snapshot: Optional[dict]) -> None:
    """Finalize a fresh claim with its result snapshot for future replays."""
    if not decision.enabled or decision.replay:
        return
    try:
        db.idempotency_complete(decision.scope, decision.key, execution_id, snapshot)
    except Exception as e:
        logger.warning("Idempotency complete failed: %s", e)


def fail(decision: IdempotencyDecision) -> None:
    """Release a fresh in-flight claim so a failed first attempt can be retried."""
    if not decision.enabled or decision.replay:
        return
    try:
        db.idempotency_release(decision.scope, decision.key)
    except Exception as e:
        logger.warning("Idempotency release failed: %s", e)
