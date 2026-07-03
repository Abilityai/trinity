"""
Paid agent chat router (NVM-001: Nevermined x402 Payment Integration).

Provides the public paid endpoint for external callers using Nevermined x402 protocol.
Internal fleet traffic (chat_with_agent MCP tool) bypasses this entirely.
"""

import base64
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import PaidChatRequest

from database import db
from services.nevermined_payment_service import (
    get_nevermined_payment_service,
    NEVERMINED_AVAILABLE,
)
from services.task_execution_service import get_task_execution_service
from services.platform_prompt_service import build_public_channel_caller_prompt

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
async def paid_chat(agent_name: str, request_body: PaidChatRequest, request: Request):
    """Main paid chat endpoint using x402 payment protocol.

    Flow:
    1. No payment-signature header → 402 Payment Required
    2. Invalid/insufficient token → 403 Forbidden
    3. Valid token → verify → execute → settle → return response + receipt
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
        is_cancelled = exec_result.status == "cancelled"
        return JSONResponse(
            status_code=200,
            content={
                "response": exec_result.response,
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
            },
        )

    # Step 4: Settle payment (on success only). Effect-scoped guard (#1084) so a
    # retried paid chat reusing the same Nevermined agent_request_id settles
    # exactly once. The terminal-turn guard above (failed execution → no settle)
    # is the outer layer and is preserved; the agent_request_id native token
    # remains the real on-chain exactly-once guarantee.
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
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="settle",
            success=True,
            execution_id=exec_result.execution_id,
            subscriber_address=verify_result.payer,
            credits_amount=config.credits_per_request,
            tx_hash=settle_result.tx_hash,
            remaining_balance=int(settle_result.remaining_balance) if settle_result.remaining_balance else None,
        )

        return {
            "response": exec_result.response,
            "execution_id": exec_result.execution_id,
            "status": "success",
            "payment": {
                "settled": True,
                "credits_burned": config.credits_per_request,
                "remaining_balance": settle_result.remaining_balance,
                "tx_hash": settle_result.tx_hash,
            },
        }
    else:
        # Settlement failed after retries — work was done but unpaid
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="settle_failed",
            success=False,
            execution_id=exec_result.execution_id,
            subscriber_address=verify_result.payer,
            credits_amount=config.credits_per_request,
            error=settle_result.error,
        )

        return {
            "response": exec_result.response,
            "execution_id": exec_result.execution_id,
            "status": "success",
            "payment": {
                "settled": False,
                "settle_retry_needed": True,
                "error": settle_result.error,
            },
        }
