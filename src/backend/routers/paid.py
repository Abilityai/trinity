"""
Paid agent chat router (NVM-001: Nevermined x402 Payment Integration).

Provides the public paid endpoint for external callers using Nevermined x402 protocol.
Internal fleet traffic (chat_with_agent MCP tool) bypasses this entirely.
"""

import base64
import json
import logging
from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from models import PaidChatRequest

from database import db
from services import idempotency_service
from services.nevermined_payment_service import (
    get_nevermined_payment_service,
    NEVERMINED_AVAILABLE,
)
from services.task_execution_service import get_task_execution_service
from services.platform_prompt_service import build_public_channel_caller_prompt
from services import paid_turn_service

router = APIRouter(prefix="/api/paid", tags=["paid"])
logger = logging.getLogger(__name__)


@router.get("/{agent_name}/info")
async def get_paid_agent_info(agent_name: str):
    """Get agent payment info and requirements.

    Returns 404 if agent doesn't exist or Nevermined is not enabled
    (prevents agent name enumeration).
    """
    if not NEVERMINED_AVAILABLE:
        return JSONResponse(
            status_code=501,
            content={"detail": "Nevermined payment integration is not available"},
        )

    config = db.get_nevermined_config(agent_name)
    if not config or not config.enabled:
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent not found or payments not enabled"},
        )

    payment_service = get_nevermined_payment_service()

    try:
        payment_required = payment_service.build_402_response(config)
    except Exception as e:
        logger.error(f"Failed to build payment info for {agent_name}: {e}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to build payment requirements"},
        )

    return {
        "agent_name": agent_name,
        "credits_per_request": config.credits_per_request,
        "nvm_plan_id": config.nvm_plan_id,
        "payment_required": payment_required,
    }


@router.post("/{agent_name}/chat")
async def paid_chat(
    agent_name: str,
    request_body: PaidChatRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(None),
):
    """Main paid chat endpoint using x402 payment protocol.

    Flow:
    1. No payment-signature header → 402 Payment Required
    2. Invalid/insufficient token → 403 Forbidden
    3. Valid token → verify → (idempotency gate) → execute → settle → respond

    Idempotency (Invariant #18, #1018): an accepted ``Idempotency-Key`` header is
    honored for contract compliance, but the effective key is ALWAYS derived from
    ``(payment-signature + message)`` — the native client-retry unit — so a client
    re-POST replays the completed work + re-attempts settle instead of re-running
    the LLM. A divergent client header must not fork execution.
    """
    if not NEVERMINED_AVAILABLE:
        return JSONResponse(
            status_code=501,
            content={"detail": "Nevermined payment integration is not available"},
        )

    # Load config
    config_data = db.get_nevermined_config_with_key(agent_name)
    if not config_data:
        return JSONResponse(
            status_code=404,
            content={"detail": "Agent not found or payments not configured"},
        )

    config = config_data["config"]
    nvm_api_key = config_data["nvm_api_key"]

    if not config.enabled:
        return JSONResponse(
            status_code=404,
            content={"detail": "Payments not enabled for this agent"},
        )

    payment_service = get_nevermined_payment_service()

    # Determine base URL for payment_required construction
    base_url = str(request.base_url).rstrip("/")

    # Step 1: Check for payment-signature header
    access_token = request.headers.get("payment-signature")

    if not access_token:
        # Return 402 Payment Required
        try:
            payment_required = payment_service.build_402_response(config, base_url)
        except Exception as e:
            logger.error(f"Failed to build 402 response for {agent_name}: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to build payment requirements"},
            )

        # x402 spec: payment-required header as base64-encoded JSON
        payment_required_b64 = base64.b64encode(
            json.dumps(payment_required).encode()
        ).decode()

        return JSONResponse(
            status_code=402,
            content={
                "detail": "Payment required",
                "payment_required": payment_required,
                "credits_per_request": config.credits_per_request,
            },
            headers={
                "payment-required": payment_required_b64,
            },
        )

    # Steps 2-4 (verify → dedup → execute → settle) are the SHARED orchestrator
    # (ent#679): the same code the A2A payment gate runs, so the #1018 settle
    # branches exist once. Every collaborator is passed from THIS module's
    # globals — `db`, `idempotency_service`, the payment service and the execute
    # closure — so a test that patches `paid.db` or `paid.idempotency_service`
    # still decides what the money path talks to (decision 20).
    from services.task_execution_service import dispatch_and_await_terminal

    async def _execute():
        # #3114: on a pull pilot the turn is queued and awaited here; a
        # resumed session is its conversation key.
        return await dispatch_and_await_terminal(
            service=get_task_execution_service(),
            agent_name=agent_name,
            message=request_body.message,
            triggered_by="paid",
            conversation_key=(
                f"paid:{request_body.session_id}" if request_body.session_id else None
            ),
            system_prompt=build_public_channel_caller_prompt(agent_name),  # #1205
            resume_session_id=request_body.session_id,
            # #894: per-agent public-channel model override (None → platform default).
            model=db.get_public_channel_model(agent_name),
        )

    def _reject_dispatch_sentinel(_verify):
        """EXEC-023 (#1672): reject the #1083 dispatch sentinels as a resume target.

        'dispatched'/'dispatched_async' are never a resumable session, so
        `--resume dispatched_async` would just fail. Scoped slice only: unlike the
        authenticated /task gate, there is no Trinity `current_user` here (an
        anonymous x402 payer, session_id self-asserted), so full payer→session
        ownership can't be enforced without a payer-identity binding that doesn't
        exist yet — tracked as a follow-up, not silently ignored. Runs where it
        has always run: after the dedup gate, before execution.
        """
        if request_body.session_id in ("dispatched", "dispatched_async"):
            raise paid_turn_service.PaidTurnAbort(
                {"detail": "This execution was never assigned a resumable session."},
                status_code=400,
            )

    # Idempotency gate (Invariant #18, #1018) — applied by the orchestrator AFTER a
    # successful verify so a rejected 403 never consumes a key. The key is derived
    # from (payment-signature + message); the client `Idempotency-Key` header is
    # accepted for contract compliance but intentionally does NOT participate in
    # derivation — a divergent header must not fork execution. None (missing
    # token/body) → dedup disabled (fail-open), never a constant key.
    turn = await paid_turn_service.run_paid_turn(
        agent_name=agent_name,
        config=config,
        nvm_api_key=nvm_api_key,
        access_token=access_token,
        idem_scope=idempotency_service.make_agent_scope(agent_name),
        idem_key=idempotency_service.derive_payment_key(
            access_token,
            request_body.message.encode("utf-8") if request_body.message else None,
        ),
        execute=_execute,
        pre_execute=_reject_dispatch_sentinel,
        payment_service=payment_service,
        idem=idempotency_service,
        db=db,
        base_url=base_url,
    )

    # Render the outcome into this door's historical response bodies. The paid
    # door's bytes are unchanged on every branch; only the code that produced
    # them moved.
    if turn.kind in (paid_turn_service.REPLAY_SETTLED, paid_turn_service.REPLAY_UNSETTLED):
        return JSONResponse(
            status_code=200,
            content=turn.payload,
            headers={"X-Idempotent-Replay": "true"},
        )
    if turn.kind in (
        paid_turn_service.VERIFY_FAILED,
        paid_turn_service.ABORTED,
        paid_turn_service.IN_FLIGHT,
        paid_turn_service.EXECUTION_ERROR,
        # trinity-enterprise#751: 202 pending_approval / the named refusal, unsettled.
        paid_turn_service.GATE_HELD,
        paid_turn_service.GATE_REFUSED,
    ):
        return JSONResponse(status_code=turn.status_code, content=turn.payload)
    if turn.kind in (
        paid_turn_service.EXECUTION_FAILED,
        paid_turn_service.EXECUTION_CANCELLED,
    ):
        return JSONResponse(status_code=200, content=turn.payload)
    # settled (fresh or replay-resettled) and unsettled success: a plain dict, as
    # this endpoint has always returned on the success paths.
    return turn.payload
