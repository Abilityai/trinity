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


# Value the agent's MCP config sends in `X-Trinity-Execution-Id` when the
# spawning process carries no `TRINITY_EXECUTION_ID` — a person running
# `claude` in the web terminal or over SSH (#2392). There is no execution to
# re-deliver, so a send can go without dedup; it is logged, never rejected.
MANUAL_EXECUTION_ID = "manual"

# Id prefix of the unguarded-effect alarm — reserved in operator_queue_service
# so an agent cannot pre-create (and so suppress) its own alarm.
EFFECT_UNGUARDED_ALERT_PREFIX = "effect-unguarded-"


class EffectUnguardedError(Exception):
    """A side effect on a pull-mode agent arrived without a usable execution id (#2392).

    Pull re-delivers the SAME execution after a lease expiry, so the effect
    guard is the only thing between a re-run and a duplicate message, call or
    share. Without an id that resolves to the calling agent's own execution the
    guard cannot de-duplicate, so the effect is refused rather than sent
    (fail-closed, `TARGET_ARCHITECTURE.md` §Re-Delivery and Side-Effect
    Recovery). Not retryable: a retry carries the same missing id.
    """

    def __init__(self, effect_type: str, reason: str) -> None:
        self.effect_type = effect_type
        self.reason = reason
        super().__init__(
            f"'{effect_type}' refused: this agent runs on the durable queue, where a "
            f"turn can be re-delivered, and the request carried no usable execution id "
            f"({reason}), so the send could not be de-duplicated. Do not retry — an "
            f"operator has been alerted."
        )


# ---------------------------------------------------------------------------
# Scope + key derivation
# ---------------------------------------------------------------------------

def make_agent_scope(agent_name: str) -> str:
    """Scope execution-creating boundaries per agent (cross-tenant isolation)."""
    return f"agent:{agent_name}"


def make_inline_auth_scope(agent_name: str, email: str) -> str:
    """Scope an MCP inline-auth (#848) chat per (agent, verified email).

    NOT ``make_agent_scope``. Every other ``make_agent_scope`` caller reaches
    the boundary through an already-authenticated per-user dependency, so the
    caller's identity is implicit and agent scope is sufficient. Inline auth is
    the exception: the identity arrives in the request BODY, and the key is
    caller-supplied — so an agent-only scope makes the (scope, key) tuple
    identical for two different verified users of the same shared agent. The
    second caller then replays the first caller's stored response snapshot and
    execution_id: a cross-user disclosure, reachable by collision as well as by
    malice (MCP clients derive deterministic keys from call args, so two users
    asking the same agent the same question collide by design).

    Folding the verified email in makes the tuple per-identity, which is what
    every other boundary gets for free from its auth dependency. Compare
    ``derive_payment_key`` (#1018), which solves the same problem for x402 by
    binding the payer's signature into the key.
    """
    return f"agent:{agent_name}:mcp-inline:{(email or '').strip().lower()}"


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


def derive_reminder_key(agent_name: str, message: str, raw_fire_spec: str) -> str:
    """Stable create-idempotency key over the RAW reminder input (#1296).

    Hashes ``(agent_name, message, raw_fire_spec)`` where ``raw_fire_spec`` is the
    literal ``delay_seconds=N`` or ``fire_at=<string>`` as supplied — NOT the
    resolved instant. A ``delay_seconds`` client-retry resolves ``now+delay``
    differently on each call, so keying on the resolved ``fire_at`` would defeat
    dedup; the raw spec is the native client-retry unit (mirrors
    ``derive_webhook_key``). A caller-supplied ``Idempotency-Key`` header still
    wins (the router prefers it).
    """
    h = hashlib.sha256()
    h.update(agent_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(message.encode("utf-8"))
    h.update(b"\x00")
    h.update(raw_fire_spec.encode("utf-8"))
    return f"reminder:{h.hexdigest()}"


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


def _resolve_execution_with_reason(
    execution_id: Optional[str], agent_name: str, *, log: bool = True
) -> "tuple[Optional[Any], str]":
    """Resolve an agent's execution; return `(execution, reason)`.

    `reason` is `ok` when the execution exists AND belongs to `agent_name`,
    else one of `absent` (no id), `manual` (a person's terminal session, see
    `MANUAL_EXECUTION_ID`), `unknown` (no such execution), `foreign` (another
    agent's execution) or `lookup_error` (the lookup itself failed).
    """
    if not execution_id:
        return None, "absent"
    if execution_id == MANUAL_EXECUTION_ID:
        return None, "manual"
    try:
        execution = db.get_execution(execution_id)
    except Exception as e:
        if log:
            logger.warning(
                "resolve_and_validate_execution: get_execution(%s) failed: %s",
                execution_id, e,
            )
        return None, "lookup_error"
    if not execution:
        return None, "unknown"
    if getattr(execution, "agent_name", None) != agent_name:
        if log:
            logger.warning(
                "resolve_and_validate_execution: execution %s does not belong to agent %s "
                "(owner=%s)",
                execution_id, agent_name, getattr(execution, "agent_name", None),
            )
        return None, "foreign"
    return execution, "ok"


def resolve_and_validate_execution(execution_id: Optional[str], agent_name: str) -> Optional[Any]:
    """Return the execution iff it exists AND belongs to `agent_name`; else None.

    Generalizes the MEM-001 server-side resolution (routers/public_memory.py):
    the backend confirms ownership of the id it is handed. The id reaches the
    effect sinks from the platform — the agent's MCP config carries it per
    process (#2392) — with the agent-supplied tool argument as the fallback for
    older images. What happens when this returns None is the caller's policy;
    `effect_guard` refuses on a pull-mode agent (`_on_unguarded_effect`).
    """
    return _resolve_execution_with_reason(execution_id, agent_name)[0]


def effect_dedup_required(agent_name: str) -> bool:
    """True when this agent's turns can be re-delivered, so an effect must dedup.

    Today that is the pull pilots. Phase 5 (#429) puts every agent on the
    durable queue; this is the one place that flips.
    """
    from services.pull_pilot import is_pull_pilot_agent
    return is_pull_pilot_agent(agent_name)


# Reasons that refuse the effect on a pull-mode agent. `manual` has no
# execution to re-deliver; `lookup_error` is a DB blip that must not stop every
# send (and `begin()` would fail open on the same outage anyway).
_REFUSED_REASONS = frozenset({"absent", "unknown", "foreign"})


async def _on_unguarded_effect(
    effect_type: str, agent_name: Optional[str], reason: str
) -> None:
    """Refuse (pull-mode) or log (otherwise) an effect sent without dedup (#2392).

    Either way the degraded case is visible: an operator alarm for a refusal,
    an `effect_guard.degraded` warning for a send that went out unguarded.
    """
    if agent_name and reason in _REFUSED_REASONS and effect_dedup_required(agent_name):
        logger.error(
            "effect_guard.refused effect_type=%s agent=%s reason=%s — "
            "pull-mode agent sent an effect without a usable execution id",
            effect_type, agent_name, reason,
        )
        from services.operator_queue_service import create_bounded_alert
        from utils.helpers import utc_now_iso
        now = utc_now_iso()
        await create_bounded_alert(agent_name, {
            "id": f"{EFFECT_UNGUARDED_ALERT_PREFIX}{agent_name}-{now}",
            "agent_name": agent_name,
            "type": "effect_unguarded",
            "status": "pending",
            "priority": "high",
            "title": "Side effect refused: no execution id",
            "question": (
                f"{agent_name} tried to send a '{effect_type}' without a usable execution "
                f"id ({reason}). It runs on the durable queue, where a turn can be "
                f"re-delivered, so the send was refused rather than risk a duplicate. "
                f"Usual causes: the agent runs an image older than #2392 (rebuild the "
                f"base image and restart it); the call came from outside a turn (a "
                f"person running Codex in the agent's terminal, or a user-scoped key "
                f"acting for this agent), which carries no id; or the API was called "
                f"directly instead of through the agent's Trinity MCP tools."
            ),
            "context": {"effect_type": effect_type, "reason": reason},
            "created_at": now,
        })
        raise EffectUnguardedError(effect_type, reason)
    logger.warning(
        "effect_guard.degraded effect_type=%s agent=%s reason=%s — "
        "sent without de-duplication",
        effect_type, agent_name, reason,
    )


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
    - **no usable execution_id** (and no payment_request_id) → on a pull-mode
      agent, raises `EffectUnguardedError` BEFORE yielding and raises an operator
      alarm (#2392); otherwise yields a no-op state and logs
      `effect_guard.degraded`. A `manual` id or a lookup error always proceeds.
    - **fail-open** → a claim hiccup yields a no-op state so the send proceeds
      without dedup.

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
            # Why it did not resolve — re-derived here, not returned above, so
            # `resolve_and_validate_execution` stays the one patchable seam.
            reason = (
                _resolve_execution_with_reason(execution_id, agent_name, log=False)[1]
                if agent_name is not None
                else "absent"
            )
            # Raises on a pull-mode agent; otherwise the send runs unguarded, logged.
            await _on_unguarded_effect(effect_type, agent_name, reason)
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
# Caller-declared intent keys (ent#665)
#
# `effect_guard` dedupes WITHIN one execution. A recurring agent's runs are
# separate executions, so it has no way to say "I already told this person X;
# do not say it again for N seconds". The caller names the intent with a key;
# the store decides. The key is ALWAYS the caller's string — never derived from
# the message, which is the #1422 failure (silent suppression of a legitimate
# send whose bytes happened to match). Scope is the agent; the key folds in the
# effect type and the resolved target (recipient email, E.164 number, group).
# The channel is deliberately NOT part of it: the same news on Telegram then
# Slack is one interruption.
# ---------------------------------------------------------------------------

# Ceiling = the cleanup sweep's purge window (`idempotency_purge_expired(ttl_hours=24)`):
# a row is hard-deleted after 24h, so a longer TTL would promise suppression the
# store cannot keep.
# ponytail: per-row expires_at column (+ both migrations) if a weekly window is ever needed.
INTENT_TTL_MAX_SECONDS = 24 * 3600
INTENT_TTL_MIN_SECONDS = 60
# A claim whose sender died mid-send is reclaimable after this long.
INTENT_IN_FLIGHT_LEASE_SECONDS = 300
SUPPRESSED_BY_IDEMPOTENCY_KEY = "idempotency_key"


class IntentInProgressError(EffectInProgressError):
    """Another execution holds this intent key and has not finished sending.

    Retryable (409). Never reported as "suppressed": if that send then fails,
    nobody delivers the message while both callers believe it was sent.
    """


def make_intent_scope(agent_name: str) -> str:
    return f"intent:{agent_name}"


def derive_intent_key(effect_type: str, target: str, idempotency_key: str) -> str:
    """`{effect_type}:sha256(effect_type \\x00 target \\x00 idempotency_key)`.

    There is no text argument: message content cannot reach the key.
    """
    h = hashlib.sha256()
    h.update(effect_type.encode("utf-8"))
    h.update(b"\x00")
    h.update((target or "").strip().lower().encode("utf-8"))
    h.update(b"\x00")
    h.update(idempotency_key.encode("utf-8"))
    return f"{effect_type}:{h.hexdigest()}"


class _IntentGuardState:
    """Yielded by `intent_guard`. On a fresh claim the sink puts what a later
    suppressed call needs (channel, session_identifier, ...) into `record`."""

    __slots__ = ("suppressed", "first_sent_at", "first_execution_id", "first", "record", "keyed")

    def __init__(self, keyed: bool) -> None:
        self.keyed = keyed
        self.suppressed = False
        self.first_sent_at: Optional[str] = None
        self.first_execution_id: Optional[str] = None
        self.first: dict = {}
        self.record: dict = {}

    def result_fields(self) -> dict:
        """The fields every sink adds to its result when a key was supplied."""
        if not self.keyed:
            return {}
        if not self.suppressed:
            return {"sent": True}
        return {
            "sent": False,
            "suppressed_by": SUPPRESSED_BY_IDEMPOTENCY_KEY,
            "first_sent_at": self.first_sent_at,
            "first_execution_id": self.first_execution_id,
        }


@asynccontextmanager
async def intent_guard(
    effect_type: str,
    *,
    agent_name: str,
    target: str,
    idempotency_key: Optional[str],
    ttl_seconds: Optional[int],
    execution_id: Optional[str],
) -> AsyncIterator[_IntentGuardState]:
    """Cross-execution "at most once per TTL" for a human-facing send (ent#665).

    Usage::

        async with intent_guard("message", agent_name=a, target=email,
                                idempotency_key=k, ttl_seconds=t,
                                execution_id=eid) as g:
            if g.suppressed:
                return suppressed_result(g)      # nothing is sent
            result = await actually_send(...)
            g.record = {"channel": ..., "session_identifier": ...}
            return result

    - no key → a no-op state; nothing is claimed (keyless sends are unchanged).
    - completed claim inside the TTL → `g.suppressed`, with `first_sent_at`,
      `first_execution_id` and the first send's `record` in `g.first`.
    - in-flight claim → `IntentInProgressError` (retryable 409).
    - fresh claim → the body runs; a clean exit records the send, an exception
      releases the claim so a refused or failed send does not spend the key.
    - store error on claim → fail-open, the send proceeds unguarded (logged).

    The TTL is the checking call's: "suppress if this was sent within MY window".
    """
    state = _IntentGuardState(keyed=bool(idempotency_key))
    if not idempotency_key:
        yield state
        return

    ttl = ttl_seconds or INTENT_TTL_MAX_SECONDS
    scope = make_intent_scope(agent_name)
    key = derive_intent_key(effect_type, target, idempotency_key)
    try:
        res = db.idempotency_claim(
            scope, key, ttl_seconds=ttl,
            in_flight_lease_seconds=INTENT_IN_FLIGHT_LEASE_SECONDS,
        )
    except Exception as e:  # fail-open, like begin()
        logger.warning(
            "intent_guard.degraded effect_type=%s agent=%s — claim failed, sending "
            "without the idempotency key: %s", effect_type, agent_name, e,
        )
        state.keyed = False
        yield state
        return

    claim_state = res.get("state")
    if claim_state == STATE_IN_FLIGHT:
        raise IntentInProgressError(
            f"Another run of this agent is sending '{effect_type}' under idempotency key "
            f"'{idempotency_key}' right now. Retry shortly; do not change the key."
        )
    if claim_state == STATE_COMPLETED:
        snap = res.get("snapshot") or {}
        state.suppressed = True
        state.first_sent_at = snap.get("sent_at")
        state.first_execution_id = res.get("execution_id")
        state.first = snap
        yield state
        return

    # Only an execution that is this agent's own is recorded as the first sender.
    owned_execution_id = (
        execution_id if resolve_and_validate_execution(execution_id, agent_name) else None
    )
    try:
        yield state
    except BaseException:
        try:
            db.idempotency_release(scope, key)
        except Exception as e:
            logger.warning("intent_guard release failed: %s", e)
        raise
    else:
        from utils.helpers import utc_now_iso
        try:
            db.idempotency_complete(
                scope, key, owned_execution_id, {**state.record, "sent_at": utc_now_iso()},
            )
        except Exception as e:
            logger.warning("intent_guard complete failed: %s", e)


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


def discard_stale_replay(scope: Optional[str], key: Optional[str]) -> None:
    """Drop a completed replay row whose recorded resource no longer exists
    (#2040 review F3) so the caller can run a genuinely fresh attempt.
    Fail-open — a failed discard just means the stale replay survives until
    the 24h purge."""
    if not scope or not key:
        return
    try:
        db.idempotency_discard_completed(scope, key)
    except Exception as e:
        logger.warning("Idempotency discard_stale_replay failed (scope=%s): %s", scope, e)


def fail(decision: IdempotencyDecision) -> None:
    """Release a fresh in-flight claim so a failed first attempt can be retried."""
    if not decision.enabled or decision.replay:
        return
    try:
        db.idempotency_release(decision.scope, decision.key)
    except Exception as e:
        logger.warning("Idempotency release failed: %s", e)
