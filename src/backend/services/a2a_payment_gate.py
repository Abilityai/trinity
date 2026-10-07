"""The x402 payment gate on the A2A inbound door (abilityai/trinity-enterprise#679).

`POST /a2a/{name}` authenticated a Trinity MCP key and nothing else, so a
stranger — including a remote Trinity holding a perfectly good x402 payment
token — got 401 and could never reach the 402 that would let it pay. This
module is the branch that serves that caller: token extraction, the paid door's
own 402/403 bytes, the shared money orchestrator, and the Task the payer gets
back.

**Mechanism in OSS, reachable only in an entitled build (T1).** Nothing here
is edition-aware. The path is reachable only when BOTH `db.get_a2a_exposed`
(set exclusively by the entitled enterprise setter) and the OSS
`nevermined_agent_config.enabled` are true, which is the same shape the paid
door (`routers/paid.py`, NVM-001) has always had. In an OSS-only build every
agent is non-exposed, so `is_priced` is False everywhere and the anonymous
branch answers today's 401 — and the card's price block, if one is configured,
points at a door that 404s until exposure is on.

**The money logic is NOT here.** It is `services/paid_turn_service.py`, shared
with the paid door, so the three #1018 settle branches exist once. This module
is the A2A-shaped adapter around it: what a token looks like on this wire, what
a refusal looks like, and how an outcome becomes a Task.

**The in-band token never reaches the agent.** `extract_token` reads
`message.metadata` and the request header; `run_a2a_paid_turn` passes the
message's TEXT parts to the execution stack and the token only to the
facilitator. Nothing logs the token — `derive_payment_key` stores a SHA-256 of
it, and the log rows carry the payer wallet, which is an identity, not a
credential.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from services import a2a_gate, a2a_protocol, paid_turn_service

logger = logging.getLogger(__name__)

#: Per-IP budget on the anonymous branch. Each hit can cost a 15-second
#: facilitator verify, which is exactly what an unauthenticated flood would
#: amplify — so the limiter runs before any DB or SDK work. Mirrors the
#: well-known card's limiter (`A2A_CARD_RATE_LIMIT`), which exists for the same
#: reason on the same public surface.
A2A_PAY_RATE_LIMIT = 30
A2A_PAY_RATE_WINDOW = 60

#: Per-agent budget, in addition to the per-IP one. A distributed flood across
#: many source addresses passes every per-IP bucket while still pinning one
#: agent's facilitator quota, and the per-agent limit is the only thing that
#: sees it. Higher than the per-IP limit: it must bound abuse without
#: throttling an agent's legitimate payers to one caller's share.
A2A_PAY_AGENT_RATE_LIMIT = 120
A2A_PAY_AGENT_RATE_WINDOW = 60


@dataclass
class PricedAgent:
    """An exposed, priced agent's payment configuration."""

    config: Any
    nvm_api_key: str


def is_priced(agent_name: str, *, db) -> Optional[PricedAgent]:
    """The agent's payment config when a stranger may pay to task it, else None.

    Two conditions, both required:

    * A2A exposure is ON (the entitled enterprise setter's flag). A non-exposed
      agent answers a uniform 404 to a principal, and today's 401 to a
      stranger — the gate must not make exposure observable.
    * The OSS Nevermined config exists, is enabled, and carries an API key.

    Deliberately NOT the SDK check. "This agent takes payment" and "this
    install can process one right now" are different facts with different
    honest answers: the first missing means 401 (the stranger has no business
    here), the second missing means 501 (the door exists and is broken). Fusing
    them would answer 401 to a caller holding a valid token for an agent whose
    card advertises a price — telling it to authenticate when no credential it
    could obtain would work.

    Returning the config rather than a bool is deliberate: the caller needs it
    for the 402 body anyway, and a second read would let the two answers
    disagree between them.
    """
    if not db.get_a2a_exposed(agent_name):
        return None
    config_data = db.get_nevermined_config_with_key(agent_name)
    if not config_data:
        return None
    config = config_data.get("config")
    nvm_api_key = config_data.get("nvm_api_key")
    if not config or not getattr(config, "enabled", False) or not nvm_api_key:
        return None
    return PricedAgent(config=config, nvm_api_key=nvm_api_key)


def extract_token(message: Any, headers: Any) -> Optional[str]:
    """The x402 access token for this request — in-band first, header fallback.

    Ruling 3 (2026-10-03): payments-py 1.18.0 carries payment IN-BAND in the
    A2A message metadata (`x402.payment.payload`), and the `payment-signature`
    header is a deprecated fallback kept for one release. This is the
    provider-side mirror of `payments_py.a2a.inband.extract_inband_token`,
    including its precedence (`inband_token or header_token`): when both are
    present the metadata wins, so a client migrating between rails cannot have
    a stale header silently decide what it pays with.

    Re-encoding the in-band payload into the base64 token the facilitator's
    verify/settle APIs consume is byte-safe: the EIP-712 signature lives INSIDE
    `payload.authorization` / `payload.signature`, not over the base64
    envelope, which is transport-only (the SDK's own round-trip note).

    Tolerant by construction. Every input is caller-controlled on a route
    reachable without a Trinity credential, so a missing, malformed or
    unencodable payload means "no in-band payment" → header → 402. It never
    raises, and it never 500s a request into a shape the caller cannot act on.
    """
    payload = a2a_protocol.payment_payload_from_message(message)
    if payload is not None:
        token = _encode_payload(payload)
        if token:
            return token
    header = None
    if headers is not None:
        try:
            header = headers.get(a2a_protocol.X402_PAYMENT_SIGNATURE_HEADER)
        except Exception:  # noqa: BLE001 — a header mapping that misbehaves is "no header"
            header = None
    return header or None


def _encode_payload(payload: Dict[str, Any]) -> Optional[str]:
    """`PaymentPayload` dict → the facilitator's base64url access token.

    Delegates to the SDK so there is ONE definition of the encoding on both
    sides of the wire. The import is lazy and its failure is not an error
    condition here: `is_priced` already required the SDK, so an ImportError on
    this line means the gate was called in a configuration that cannot verify
    anything — the honest answer is "no in-band token", which falls through to
    the header and then to the 402.
    """
    try:
        from payments_py.x402.token import encode_access_token

        return encode_access_token(payload)
    except Exception:  # noqa: BLE001 — unencodable payload ⇒ no in-band payment
        logger.debug("a2a: in-band x402 payload could not be encoded", exc_info=True)
        return None


def payment_required_response(
    agent_name: str, config, *, payment_service, base_url: str,
    plan_scheme=None,
) -> tuple[int, dict, dict]:
    """The 402, byte-identical to the paid door's (T2) → (status, body, headers).

    One builder, two doors: `build_402_response` produces the requirements
    document the facilitator will later check the token against, so a 402 built
    differently from the verify is a rejection the caller cannot act on.

    `endpoint` is THIS door, not the paid one (#679 E2). An x402 v3 token signs
    `resourceUrl` and the facilitator compares origin+path, so a token minted
    against `/api/paid/{name}/chat` cannot authorize a call to `/a2a/{name}` —
    and a non-Trinity client follows `resource.url` out of the 402 verbatim.

    `plan_scheme` (#3215) is the `(scheme, network)` pair the plan is actually
    payable with, resolved by the router before it calls: a fiat plan advertised
    under the SDK's default `nvm:erc4337` mints a token the facilitator rejects.
    `None` keeps the pre-#3215 document.

    A builder failure is the paid door's 500 branch: a broken plan config is
    ours, not the caller's, and inventing requirements would mint a token
    nothing can verify.
    """
    try:
        payment_required = payment_service.build_402_response(
            config, base_url, f"{base_url}/a2a/{agent_name}",
            plan_scheme=plan_scheme,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to build 402 response for a2a/%s: %s", agent_name, e)
        return 500, {"detail": "Failed to build payment requirements"}, {}

    payment_required_b64 = base64.b64encode(
        json.dumps(payment_required).encode()
    ).decode()
    return (
        402,
        {
            "detail": "Payment required",
            "payment_required": payment_required,
            "credits_per_request": config.credits_per_request,
        },
        {a2a_protocol.X402_PAYMENT_REQUIRED_HEADER: payment_required_b64},
    )


def payment_caller_allowed(agent_name: str, payer: Optional[str]) -> bool:
    """The enterprise inbound allow-list, consulted for a PAYING caller (T7).

    Deliberately NOT `a2a_gate.check_inbound_allowed`, and the difference is the
    failure direction. That function fails OPEN because the caller it guards is
    already authenticated as an owner/shared identity — the allow-list is an
    extra layer over a decision already made. Here there is no such decision:
    the payment IS the authorization, so a provider error must refuse rather
    than admit an unlisted wallet. Same provider, same empty-list-means-no-
    restriction contract, opposite bias, because the thing underneath it is
    different.

    The identity is `x402:{payer}` — prefixed so an operator reading a
    configured list can tell a wallet from an email, and so a wallet can never
    collide with a Trinity identity in the same list.
    """
    provider = a2a_gate.get_provider()
    if provider is None:
        return True
    if not payer:
        return False
    try:
        return bool(provider.is_inbound_allowed(agent_name, f"x402:{payer}"))
    except Exception:  # noqa: BLE001 — fail CLOSED: here the gate IS the authorization
        logger.warning(
            "[a2a_payment_gate] allow-list provider error for %s; refusing the "
            "paying caller (fail-closed)", agent_name, exc_info=True,
        )
        return False


class AllowlistRefused(Exception):
    """The allow-list refused this payer after a successful verify (T7)."""


async def run_a2a_paid_turn(
    *,
    agent_name: str,
    priced: PricedAgent,
    access_token: str,
    text: str,
    base_url: str,
    execute: Callable[[], Awaitable[Any]],
    payment_service,
    idem,
    db,
) -> paid_turn_service.PaidTurnOutcome:
    """One paid A2A turn, through the orchestrator the paid door runs.

    Two A2A-specific decisions, both load-bearing:

    **The dedup scope is `a2a:{agent}:pay:{payer}`** — resolved from the verify
    result, so every payer gets a private replay namespace (FR-4). A shared
    scope would let one payer's key resolve to another's stored snapshot, which
    carries the agent's full response text.

    **The dedup key is `derive_payment_key(token, text)`, not `messageId`.**
    #3209's client mints a fresh `uuid4().hex` messageId per call, so a retry
    after its 30-second RPC timeout would carry a new id, re-execute the turn
    and re-settle it — the payer pays twice for one answer. The (token, text)
    pair IS what a retry repeats. Residual, stated not solved: a v3 single-use
    token changes per call for SDK clients and defeats any key derived from it.

    The allow-list is consulted AFTER verify (T7) — the payer wallet only exists
    once the facilitator has answered — and before execution, as `pre_execute`,
    so a refusal lands at exactly the point the paid door's own refusal does and
    never consumes a dedup key for work it won't do.
    """
    def _check_allowlist(verify_result) -> None:
        if payment_caller_allowed(agent_name, verify_result.payer):
            return
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="reject",
            success=False,
            subscriber_address=verify_result.payer,
            error="Payer not on the agent's A2A inbound allow-list",
        )
        raise paid_turn_service.PaidTurnAbort(
            {"detail": "Caller not on the agent's A2A inbound allow-list"},
            status_code=403,
        )

    return await paid_turn_service.run_paid_turn(
        agent_name=agent_name,
        config=priced.config,
        nvm_api_key=priced.nvm_api_key,
        access_token=access_token,
        idem_scope=lambda verify: f"a2a:{agent_name}:pay:{verify.payer}",
        idem_key=idem.derive_payment_key(
            access_token, text.encode("utf-8") if text else None
        ),
        execute=execute,
        pre_execute=_check_allowlist,
        payment_service=payment_service,
        idem=idem,
        db=db,
        base_url=base_url,
        endpoint=f"{base_url}/a2a/{agent_name}",
    )


#: Outcome kind → (A2A task state, x402 payment status, error code). The table
#: IS the contract #3209's client reads, so it lives in one place rather than as
#: branches scattered through the router.
#:
#: `payment-verified` on a delivered-but-unsettled turn is the SDK's own
#: "verified, not settled" state, and it is chosen over `payment-failed`
#: because #3209's client parses the task NORMALLY on anything it does not
#: recognise as a refusal — so the artifact the payer paid for survives
#: (#1018's deliver-then-reconcile, carried onto this wire).
_OUTCOME_RENDER = {
    paid_turn_service.SETTLED: ("completed", a2a_protocol.X402_STATUS_COMPLETED, None),
    paid_turn_service.REPLAY_SETTLED: ("completed", a2a_protocol.X402_STATUS_COMPLETED, None),
    paid_turn_service.UNSETTLED: ("completed", a2a_protocol.X402_STATUS_VERIFIED, None),
    paid_turn_service.REPLAY_UNSETTLED: ("completed", a2a_protocol.X402_STATUS_VERIFIED, None),
    paid_turn_service.EXECUTION_FAILED: (
        "failed", a2a_protocol.X402_STATUS_VERIFIED, "execution_failed"),
    paid_turn_service.EXECUTION_CANCELLED: (
        "canceled", a2a_protocol.X402_STATUS_VERIFIED, "execution_cancelled"),
    # trinity-enterprise#751: nothing ran and nothing was charged. A held request
    # is `input-required`, keyed on its approval request (the unpaid door's
    # shape); a refusal is `rejected`, carrying the gate's own code.
    paid_turn_service.GATE_HELD: (
        "input-required", a2a_protocol.X402_STATUS_VERIFIED, "approval_pending"),
    paid_turn_service.GATE_REFUSED: (
        "rejected", a2a_protocol.X402_STATUS_VERIFIED, "gated"),
}


def task_from_paid_payload(
    outcome: paid_turn_service.PaidTurnOutcome,
    *,
    task_builder: Callable[..., Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """A paid outcome → the A2A Task its payer gets back, or None for a JSON-RPC error.

    Rebuilt FROM `outcome.payload` — the paid door's snapshot dict — on every
    answer INCLUDING a replay, which is why the snapshot shape is shared with
    the paid door: the replay logic in `paid_turn_service` is then one code path
    rather than one per door, and a replayed Task cannot drift from the Task
    that was originally served.

    `task_builder` is passed in rather than imported: the router owns
    `_task_object` and the artifact shape, and this service must not acquire a
    second opinion about what an A2A Task looks like.

    Returns None for the outcomes that are not a Task at all (a verification
    failure, an allow-list refusal, an in-flight duplicate, a raised execution)
    — the router answers those in their own shapes, which are an HTTP status or
    a JSON-RPC error, not a task state.
    """
    render = _OUTCOME_RENDER.get(outcome.kind)
    if render is None:
        return None
    state, payment_status, error_code = render
    payload = outcome.payload or {}
    payment = payload.get("payment") or {}
    gate_detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    if outcome.kind == paid_turn_service.GATE_REFUSED:
        error_code = gate_detail.get("code") or error_code
    execution_id = (payload.get("execution_id") or outcome.execution_id
                    or payload.get("request_id") or uuid.uuid4().hex)

    metadata: Dict[str, Any] = {a2a_protocol.X402_STATUS_KEY: payment_status}
    if payment_status == a2a_protocol.X402_STATUS_COMPLETED:
        # The SDK's `SettleResponse` alias names, so a non-Trinity reader sees
        # the spec shape rather than Trinity's internal snapshot keys.
        receipt = {
            # The payer is on the verify result, not in the snapshot — and the
            # replay path re-verifies, so it is present on a replayed receipt
            # too (which is why the receipt is not stored in the snapshot).
            "payer": getattr(outcome.verify, "payer", None),
            "transaction": payment.get("tx_hash"),
            "creditsRedeemed": payment.get("credits_burned"),
            "remainingBalance": payment.get("remaining_balance"),
        }
        metadata[a2a_protocol.X402_RECEIPTS_KEY] = [
            {k: v for k, v in receipt.items() if v is not None}
        ]
    else:
        # Verified but not settled, or verified and nothing owed. Name WHY in a
        # code the caller can branch on, and say plainly that nothing was
        # charged — a payer holding an artifact with no receipt otherwise has
        # to guess whether it was billed.
        code = error_code or (
            "settle_in_progress" if payment.get("settle_in_progress")
            else "settle_retry_needed"
        )
        reason = payment.get("reason") or payment.get("error")
        if error_code in ("execution_failed", "execution_cancelled") and not reason:
            reason = "no charge"
        metadata[a2a_protocol.X402_ERROR_KEY] = {"code": code, "reason": reason}

    # A failed turn gets NO artifact (#1018): the output may be partial or
    # garbled and the caller was not charged for it. A cancelled turn keeps its
    # text (#679) — the caller cancelled its own work and may still want it.
    text = payload.get("response") if state in ("completed", "canceled") else None
    if state == "input-required":
        text = payload.get("message")          # the gate's notice (trinity-enterprise#751)
    return task_builder(
        execution_id,
        state,
        # A failed turn's honest text is the orchestrator's own reason
        # ("Execution failed — no charge"): the snapshot deliberately carries no
        # response on that branch, so this is the only thing to tell the caller.
        # A gate refusal's is the refusal itself.
        error=(payment.get("reason") if state == "failed"
               else gate_detail.get("message") if state == "rejected" else None),
        text=text,
        metadata=metadata,
    )
