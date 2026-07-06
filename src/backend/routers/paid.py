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

router = APIRouter(prefix="/api/paid", tags=["paid"])
logger = logging.getLogger(__name__)


def _finalize_settled(
    *,
    agent_name: str,
    config,
    response,
    execution_id: Optional[str],
    settle_result,
    payer: Optional[str],
    idem: idempotency_service.IdempotencyDecision,
) -> dict:
    """Shared success finalizer for BOTH the fresh-settle and replay-resettle paths (#1018).

    A client retry that finally settles must still (a) log ``action="settle"`` and
    (b) converge the stored trigger snapshot unsettled→settled — otherwise the
    settle is never recorded and every replay re-drives settle forever. Kept as one
    helper so neither path can drift on the bookkeeping.
    """
    db.log_nevermined_payment(
        agent_name=agent_name,
        action="settle",
        success=True,
        execution_id=execution_id,
        subscriber_address=payer,
        credits_amount=config.credits_per_request,
        tx_hash=settle_result.tx_hash,
        remaining_balance=(
            int(settle_result.remaining_balance)
            if settle_result.remaining_balance
            else None
        ),
    )

    settled_payload = {
        "response": response,
        "execution_id": execution_id,
        "status": "success",
        "payment": {
            "settled": True,
            "credits_burned": config.credits_per_request,
            "remaining_balance": settle_result.remaining_balance,
            "tx_hash": settle_result.tx_hash,
        },
    }

    # Converge the stored trigger snapshot (#1018): on the fresh path this completes
    # the in-flight claim with the settled snapshot; on the replay-resettle path it
    # upgrades a completed-but-unsettled snapshot so a THIRD request replays
    # 'settled' and never re-drives settle. No-op when dedup is disabled.
    idempotency_service.upgrade_snapshot(idem.scope, idem.key, settled_payload)
    return settled_payload


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

    # Step 2: Verify payment
    verify_result = await payment_service.verify_payment(
        nvm_api_key=nvm_api_key,
        nvm_environment=config.nvm_environment,
        config=config,
        access_token=access_token,
        base_url=base_url,
    )

    if not verify_result.success:
        # Log rejected verification
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="reject",
            success=False,
            subscriber_address=verify_result.payer,
            error=verify_result.error,
        )
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Payment verification failed",
                "error": verify_result.error,
            },
        )

    # Log successful verification
    db.log_nevermined_payment(
        agent_name=agent_name,
        action="verify",
        success=True,
        subscriber_address=verify_result.payer,
    )

    # Idempotency gate (Invariant #18, #1018) — placed AFTER a successful verify so
    # a rejected 403 never consumes a key. The key is derived from
    # (payment-signature + message); the client `Idempotency-Key` header is accepted
    # for contract compliance but intentionally does NOT participate in derivation —
    # a divergent header must not fork execution. None (missing token/body) → dedup
    # disabled (fail-open), never a constant key.
    idem_scope = idempotency_service.make_agent_scope(agent_name)
    idem_key = idempotency_service.derive_payment_key(
        access_token, request_body.message.encode("utf-8") if request_body.message else None
    )
    idem = idempotency_service.begin(idem_scope, idem_key)

    if idem.replay:
        if idem.in_flight:
            # A concurrent duplicate of the same (token+body) is mid-flight.
            return JSONResponse(
                status_code=409,
                content={"detail": "A duplicate paid request is still being processed."},
            )

        snapshot = idem.snapshot or {}
        payment_snap = snapshot.get("payment") or {}

        if payment_snap.get("settled"):
            # Already settled — replay the receipt verbatim, no re-execute/re-settle.
            return JSONResponse(
                status_code=200,
                content=snapshot,
                headers={"X-Idempotent-Replay": "true"},
            )

        # Completed but UNSETTLED — re-drive settle WITHOUT re-running the LLM (the
        # trigger key already deduped the execution), then converge the snapshot. This
        # is why the unsettled branch stores the claim with complete() (not fail()):
        # fail() would re-execute here. NOTE: the re-settle is NOT provider-idempotent —
        # Nevermined's agent_request_id is an observability id (fresh per verify) and the
        # facilitator burns on every successful settle_permissions call. Re-driving is
        # safe here only because the prior settle genuinely did NOT complete; a settle
        # that burned on-chain but reported failure would re-burn (at-least-once residual,
        # tracked by #1408). The payment:{agent_request_id} effect guard only dedups a
        # concurrent settle that reuses the SAME id.
        resettle = await payment_service.settle_payment_once(
            config=config,
            nvm_api_key=nvm_api_key,
            nvm_environment=config.nvm_environment,
            access_token=access_token,
            agent_request_id=verify_result.agent_request_id,
            execution_id=snapshot.get("execution_id"),
            base_url=base_url,
        )
        if resettle.success:
            return _finalize_settled(
                agent_name=agent_name,
                config=config,
                response=snapshot.get("response"),
                execution_id=snapshot.get("execution_id"),
                settle_result=resettle,
                payer=verify_result.payer,
                idem=idem,
            )
        # Still unsettled — replay the stored unsettled snapshot; the claim stays
        # 'completed unsettled' so a later retry re-drives settle again.
        return JSONResponse(
            status_code=200,
            content=snapshot,
            headers={"X-Idempotent-Replay": "true"},
        )

    # Step 3: Execute task
    task_service = get_task_execution_service()
    try:
        exec_result = await task_service.execute_task(
            agent_name=agent_name,
            message=request_body.message,
            triggered_by="paid",
            system_prompt=build_public_channel_caller_prompt(agent_name),  # #1205
            resume_session_id=request_body.session_id,
            # #894: per-agent public-channel model override (None → platform default).
            model=db.get_public_channel_model(agent_name),
        )
    except Exception as e:
        logger.error(f"Task execution failed for paid request on {agent_name}: {e}")
        # Nothing dispatched — release the claim so a legitimate retry re-executes.
        idempotency_service.fail(idem)
        # Don't settle — caller keeps credits
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="verify",
            success=True,
            subscriber_address=verify_result.payer,
            error=f"Execution failed: {e}",
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Task execution failed",
                "error": str(e),
                "payment": {"settled": False, "reason": "Execution failed — no charge"},
            },
        )

    if exec_result.status in ("failed", "cancelled"):
        # Don't settle — caller keeps credits. #679: a CANCELLED turn must NOT
        # settle either — settling on cancel is the charge-on-cancel money bug.
        # Release the claim so a retry re-executes (no completed work to replay).
        idempotency_service.fail(idem)
        is_cancelled = exec_result.status == "cancelled"
        content = {
            "execution_id": exec_result.execution_id,
            "status": "cancelled" if is_cancelled else "failed",
            "payment": {
                "settled": False,
                "reason": (
                    "Execution cancelled — no charge"
                    if is_cancelled
                    else "Execution failed — no charge"
                ),
            },
        }
        # #1018 hardening: a FAILED execution may hold partial/garbled output; the
        # caller paid nothing, so don't leak the body. A CANCELLED turn keeps its
        # response (#679 — the user cancelled their own work and may want it).
        if is_cancelled:
            content["response"] = exec_result.response
        return JSONResponse(status_code=200, content=content)

    # Record the execution on the claim now that it exists (best-effort).
    idempotency_service.attach_execution(idem, exec_result.execution_id)

    # Step 4: Settle payment (on success only). Effect-scoped guard (#1084) so a
    # concurrent settle reusing the SAME agent_request_id is deduped locally. The
    # terminal-turn guard above (failed execution → no settle) is the outer layer
    # and is preserved. NOTE: agent_request_id is a Nevermined observability id, not
    # a provider exactly-once token — this local guard is the only settle dedup, and
    # a fresh-id retry's double-settle residual is tracked by #1408.
    settle_result = await payment_service.settle_payment_once(
        config=config,
        nvm_api_key=nvm_api_key,
        nvm_environment=config.nvm_environment,
        access_token=access_token,
        agent_request_id=verify_result.agent_request_id,
        execution_id=exec_result.execution_id,
        base_url=base_url,
    )

    if settle_result.success:
        # Logs settle + completes the idempotency claim with the settled snapshot.
        return _finalize_settled(
            agent_name=agent_name,
            config=config,
            response=exec_result.response,
            execution_id=exec_result.execution_id,
            settle_result=settle_result,
            payer=verify_result.payer,
            idem=idem,
        )

    # Settlement did not complete — the work WAS delivered, so deliver-then-reconcile:
    # keep HTTP 200 + the response, but tell the truth with status "success_unsettled"
    # (the filed #1018 bug: this used to lie with status "success"). Two sub-cases:
    #   * concurrent settle in-flight (effect guard) → settle_in_progress, no log
    #     (the concurrently-running settle logs its own outcome; it completes once).
    #   * genuine failure after retries → settle_retry_needed + settle_failed log.
    settle_in_progress = settle_result.error == "settlement already in progress"
    payment_block = {"settled": False, "error": settle_result.error}
    if settle_in_progress:
        payment_block["settle_in_progress"] = True
    else:
        payment_block["settle_retry_needed"] = True
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="settle_failed",
            success=False,
            execution_id=exec_result.execution_id,
            subscriber_address=verify_result.payer,
            credits_amount=config.credits_per_request,
            error=settle_result.error,
        )

    unsettled_payload = {
        "response": exec_result.response,
        "execution_id": exec_result.execution_id,
        "status": "success_unsettled",
        "payment": payment_block,
    }
    # complete() — NOT fail() — so a client re-POST replays the completed work and
    # re-drives settle (idempotent) rather than re-running the LLM (double cost).
    # The snapshot stays 'unsettled' until a settle finally succeeds and upgrades it.
    idempotency_service.complete(idem, exec_result.execution_id, unsettled_payload)
    return unsettled_payload
