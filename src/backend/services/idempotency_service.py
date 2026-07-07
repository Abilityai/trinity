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
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Union

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


class EffectInProgressError(Exception):
    """An effect-scoped guard hit a still-`in_flight` claim (#1084, codex #6).

    Raised by `effect_guard` when a duplicate attempt for the same resolved
    effect identity is mid-flight (claimed but not yet completed). It is a
    RETRYABLE signal — the sink must surface a 409-style "already in progress"
    rather than silently returning a None-as-success (which would skip the send
    AND report success). Distinct from a `completed` replay, which returns the
    stored snapshot.
    """


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


def derive_payment_key(
    access_token: Optional[str], body: Optional[bytes]
) -> Optional[str]:
    """Client-stable idempotency key for the paid x402 boundary (#1018).

    Keys on ``sha256(access_token \\x00 message_body)`` so a client that re-POSTs
    the same ``payment-signature`` + message dedups BOTH the LLM execution AND the
    settle. Deliberately NOT keyed on Nevermined's ``agent_request_id``: that is a
    per-verify observability id that is likely fresh on each re-POST, so keying on
    it would silently defeat the client-retry dedup (double LLM cost). The token +
    body IS the native client-retry unit, mirroring ``derive_webhook_key``.

    One-way: only the SHA-256 of the token lands in the key — no bearer credential
    at rest.

    Returns ``None`` on ANY falsy input (no token OR no body) so ``begin()``
    disables dedup (fail-open). It MUST NEVER hash empty input to a constant — a
    constant key would collide unrelated requests into one row (409 / cross-request
    snapshot replay = data leak + wrong billing). Two requests with no derivable
    key each run independently with no dedup, which is the safe default.
    """
    if not access_token or not body:
        return None
    h = hashlib.sha256()
    h.update(access_token.encode("utf-8"))
    h.update(b"\x00")
    h.update(body)
    return f"paid:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Effect-scoped idempotency (#1084)
#
# Trigger-boundary dedup (above) stops a re-POSTed /chat or webhook from
# creating a SECOND execution. It does NOT reach an agent's individual tool
# calls — so a re-delivered turn (the at-least-once semantics pull-mode /
# work-stealing will introduce, Epic #1045/#1081) re-emits the same outbound
# side effect (re-sends an email, re-charges a payment). Exactly-once external
# effects must therefore be enforced at the SINK, per resolved action identity.
#
# The key is content-derived on RESOLVED, IMMUTABLE identity only (recipient +
# channel + provider account) — NEVER the LLM-generated message body, which is
# non-deterministic across a re-run and would defeat dedup. Effects scope by
# `effect:{execution_id}`; Nevermined settles scope by `payment:{agent_request_id}`
# (a Nevermined observability id — the local guard, not the provider, enforces
# at-most-once per id; fresh-id retry residual is #1408). Long TTL is inherited from the shared 24h
# default, which already exceeds the lease window (agent_timeout + buffer ≤ ~2h),
# so a completed row outlives a late re-delivery. See the contract doc at
# docs/memory/feature-flows/effect-idempotency.md.
# ---------------------------------------------------------------------------

def make_effect_scope(execution_id: str) -> str:
    """Scope an outbound side effect to the execution that produced it.

    A re-delivery of the same turn preserves the execution_id, so the same
    resolved effect within it resolves to the same (scope, key) and dedupes.
    """
    return f"effect:{execution_id}"


def make_payment_scope(agent_request_id: str) -> str:
    """Scope a settlement to its Nevermined `agent_request_id`.

    Per the Nevermined x402 docs the agent_request_id is an OBSERVABILITY/tracking
    id (returned fresh by each `verify_permissions`), NOT a provider-enforced
    exactly-once token — the facilitator burns credits on *every* successful
    `settle_permissions` call until the token's limit. So this local effect-guard
    row is the actual dedup, and it only stops a double-burn when the SAME
    agent_request_id is re-presented (a concurrent duplicate settle). A retry that
    re-verifies gets a fresh id and is not deduped here — the residual at-least-once
    double-settle is tracked by #1408.
    """
    return f"payment:{agent_request_id}"


def _canonical_identifying_args(identifying_args: Union[dict, list, str, None]) -> str:
    """Canonicalize the resolved identity into a stable string.

    Dicts are key-sorted so arg order can't change the key; everything else is
    JSON-serialized deterministically. This must contain ONLY resolved, immutable
    identity (recipient, channel, account) — never the generated body.
    """
    if identifying_args is None:
        return ""
    if isinstance(identifying_args, str):
        return identifying_args
    return json.dumps(identifying_args, sort_keys=True, separators=(",", ":"), default=str)


def derive_effect_key(
    execution_id: str,
    effect_type: str,
    identifying_args: Union[dict, list, str, None],
    dedup_label: str = "",
) -> str:
    """Content-derived key on STABLE identity, scoped by execution_id (#1084).

    `{effect_type}:sha256(execution_id \\x00 effect_type \\x00
    resolved_identifying_args \\x00 dedup_label)`. The body is structurally
    absent — only resolved identity is hashed. `dedup_label` (agent-supplied,
    default "") lets an agent intentionally send two distinct messages to the
    same recipient in one turn; default empty → at-most-one send per
    (recipient, channel, type) per turn.
    """
    h = hashlib.sha256()
    h.update((execution_id or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(effect_type.encode("utf-8"))
    h.update(b"\x00")
    h.update(_canonical_identifying_args(identifying_args).encode("utf-8"))
    h.update(b"\x00")
    h.update((dedup_label or "").encode("utf-8"))
    return f"{effect_type}:{h.hexdigest()}"


def resolve_and_validate_execution(execution_id: Optional[str], agent_name: str) -> Optional[Any]:
    """Return the execution iff it exists AND belongs to `agent_name`; else None.

    Generalizes the MEM-001 server-side resolution (routers/public_memory.py):
    the agent supplies an execution_id, never its own identity, and the backend
    confirms ownership. FAIL-OPEN by design — a missing execution_id (old image /
    absent tool arg), a lookup error, or an agent mismatch returns None so the
    caller proceeds WITHOUT dedup rather than 5xx-ing a legitimate send. Safe
    today because pull-mode re-delivery is off (fail-open cannot produce a
    duplicate yet); enabling pull-mode default-ON for side-effect agents requires
    trusted execution_id injection + fail-closed-when-absent first (a BLOCKING
    prerequisite on Epic #1045/#1081 — see the contract doc).
    """
    if not execution_id:
        return None
    try:
        execution = db.get_execution(execution_id)
    except Exception as e:  # fail-open: a lookup hiccup must not block a real send
        logger.warning(
            "resolve_and_validate_execution: get_execution(%s) failed — fail-open: %s",
            execution_id, e,
        )
        return None
    if not execution:
        return None
    if getattr(execution, "agent_name", None) != agent_name:
        logger.warning(
            "resolve_and_validate_execution: execution %s does not belong to agent %s "
            "(owner=%s) — fail-open",
            execution_id, agent_name, getattr(execution, "agent_name", None),
        )
        return None
    return execution


class _EffectGuardState:
    """Yielded by `effect_guard`. The sink reads `replay`/`snapshot` and, on a
    fresh claim, writes the sanitized result into `snapshot` before exit."""

    __slots__ = ("replay", "snapshot", "dedup_enabled")

    def __init__(self) -> None:
        self.replay: bool = False
        self.snapshot: Optional[dict] = None
        self.dedup_enabled: bool = False


@asynccontextmanager
async def effect_guard(
    effect_type: str,
    identifying_args: Union[dict, list, str, None],
    *,
    execution_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    dedup_label: str = "",
    payment_request_id: Optional[str] = None,
) -> AsyncIterator[_EffectGuardState]:
    """Per-sink exactly-once-style guard for an outbound side effect (#1084).

    Usage::

        async with effect_guard("message", {"recipient": r, "channel": c},
                                execution_id=eid, agent_name=name,
                                dedup_label=label) as g:
            if g.replay:
                return reconstruct_from(g.snapshot)
            result = await actually_send(...)
            g.snapshot = sanitized_json_stable(result)   # stored for replay
            return result

    Behavior:
    - **completed replay** → yields `g.replay=True` + `g.snapshot`; the sink
      MUST NOT re-send (return the snapshot). No second claim/complete.
    - **in_flight replay** → raises `EffectInProgressError` BEFORE yielding
      (a concurrent attempt holds the claim) — the sink surfaces a retryable
      409, never a silent success (codex #6).
    - **fresh claim** → yields `g.dedup_enabled=True`; on clean exit `complete()`
      stores `g.snapshot`, on exception `fail()` releases the claim so a failed
      attempt retries.
    - **fail-open** → an absent/invalid execution_id (and no payment_request_id),
      or a claim hiccup, yields a no-op state so the send proceeds without dedup.

    Two scopes:
    - Effect path (messages/voip/share): `effect:{execution_id}`, after
      `resolve_and_validate_execution` confirms ownership.
    - Payment path (Nevermined): `payment:{agent_request_id}` — the native
      token IS the unit; `execution_id` (if given) is only recorded for audit.

    Documented tradeoff: a crash AFTER the claim but BEFORE the provider call
    leaves the row `in_flight`, blocking a re-send for the TTL window
    (at-most-once-with-possible-loss). See the contract doc.
    """
    state = _EffectGuardState()

    if payment_request_id:
        scope = make_payment_scope(payment_request_id)
        key = derive_effect_key(payment_request_id, effect_type, identifying_args, dedup_label)
    else:
        execution = (
            resolve_and_validate_execution(execution_id, agent_name)
            if agent_name is not None
            else None
        )
        if execution is None:
            # Dedup disabled (absent/invalid execution_id) — run the body, no guard.
            yield state
            return
        scope = make_effect_scope(execution_id)  # type: ignore[arg-type]
        key = derive_effect_key(execution_id, effect_type, identifying_args, dedup_label)  # type: ignore[arg-type]

    decision = begin(scope, key)
    if not decision.enabled:
        # Claim hiccup (Redis/DB down) → fail-open, run the body.
        yield state
        return

    if decision.replay:
        if decision.in_flight:
            raise EffectInProgressError(
                f"A duplicate '{effect_type}' for this execution is already in progress."
            )
        # completed → replay the stored snapshot; the sink must not re-send.
        state.replay = True
        state.snapshot = decision.snapshot
        yield state
        return

    # Fresh claim — run the body, then finalize on the way out.
    state.dedup_enabled = True
    try:
        yield state
    except BaseException:
        fail(decision)
        raise
    else:
        complete(decision, execution_id, state.snapshot)


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


def upgrade_snapshot(scope: Optional[str], key: Optional[str], snapshot: Optional[dict]) -> None:
    """Overwrite an already-COMPLETED trigger snapshot in place, by (scope, key) (#1018).

    Used on the paid replay-resettle convergence path: a completed-but-*unsettled*
    paid snapshot is re-driven through settle, and on success the stored snapshot
    must converge unsettled→settled so a THIRD request replays the settled receipt
    and does NOT re-drive settle wastefully. Also serves the fresh settled path
    (completes the in-flight claim with the settled snapshot in one call).

    This deliberately bypasses ``complete()``'s ``decision.replay`` no-op guard — we
    ARE mutating an existing completed row, addressed by ``(scope, key)`` rather than
    finalizing a fresh claim. No-op (and no error) when dedup is disabled, i.e. when
    ``key`` is falsy. Best-effort; never raises (fail-open, same as ``complete``).
    """
    if not scope or not key:
        return
    try:
        db.idempotency_complete(scope, key, None, snapshot)
    except Exception as e:
        logger.warning("Idempotency upgrade_snapshot failed (scope=%s): %s", scope, e)


def fail(decision: IdempotencyDecision) -> None:
    """Release a fresh in-flight claim so a failed first attempt can be retried."""
    if not decision.enabled or decision.replay:
        return
    try:
        db.idempotency_release(decision.scope, decision.key)
    except Exception as e:
        logger.warning("Idempotency release failed: %s", e)
