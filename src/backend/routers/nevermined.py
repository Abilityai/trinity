"""
Nevermined admin configuration router (NVM-001).

Authenticated endpoints for managing per-agent Nevermined payment configuration.
Read endpoints (GET) allow owner, shared users, and admins.
Write endpoints (POST/PUT/DELETE) require owner or admin access.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path
from typing import List

from models import User
from database import db
from dependencies import get_current_user
from db_models import NeverminedConfigCreate, NeverminedConfig, NeverminedPaymentLog
from services.nevermined_payment_service import (
    get_nevermined_payment_service,
    NEVERMINED_AVAILABLE,
)

router = APIRouter(prefix="/api/nevermined", tags=["nevermined"])
logger = logging.getLogger(__name__)


def _agent_exists(agent_name: str) -> bool:
    """True if the agent exists in the system (container or ownership row)."""
    from services.docker_service import get_agent_by_name
    return bool(get_agent_by_name(agent_name)) or bool(db.get_agent_owner(agent_name))


def _require_read_access(agent_name: str, current_user: User):
    """Verify the user can view agent payment config (owner, shared, or admin).

    Uniform 404 for both non-existent and inaccessible agents so config
    presence can't be used as an existence oracle (#186).
    """
    # Evaluate existence AND access unconditionally (no short-circuit) so the
    # query count — hence timing — is identical for the non-existent and the
    # existing-but-inaccessible case, mirroring _require_write_access and the
    # dependencies.py helpers (#186 equal-query-count discipline).
    exists = _agent_exists(agent_name)
    allowed = db.can_user_access_agent(current_user.username, agent_name)
    if not (exists and allowed):
        raise HTTPException(status_code=404, detail="Agent not found")


def _require_write_access(agent_name: str, current_user: User):
    """Verify the user can modify agent payment config (owner or admin only).

    Uniform 404 for both non-existent and unowned agents (#186).
    """
    allowed = current_user.role == "admin" or db.can_user_share_agent(current_user.username, agent_name)
    if not (_agent_exists(agent_name) and allowed):
        raise HTTPException(status_code=404, detail="Agent not found")


def _check_sdk():
    """Return 501 if payments-py is not installed."""
    if not NEVERMINED_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="Nevermined payment integration is not available (payments-py not installed)",
        )


@router.post("/agents/{name}/config", response_model=NeverminedConfig)
async def configure_nevermined(
    name: str,
    request: NeverminedConfigCreate,
    current_user: User = Depends(get_current_user),
):
    """Configure Nevermined payment settings for an agent."""
    _check_sdk()
    _require_write_access(name, current_user)

    try:
        config = db.create_or_update_nevermined_config(
            agent_name=name,
            nvm_api_key=request.nvm_api_key,
            nvm_environment=request.nvm_environment,
            nvm_agent_id=request.nvm_agent_id,
            nvm_plan_id=request.nvm_plan_id,
            credits_per_request=request.credits_per_request,
        )
        logger.info(f"Nevermined config updated for agent '{name}' by {current_user.username}")
        return config
    except Exception as e:
        logger.error(f"Failed to configure Nevermined for agent '{name}': {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/agents/{name}/config", response_model=NeverminedConfig)
async def get_nevermined_config(
    name: str,
    current_user: User = Depends(get_current_user),
):
    """Get Nevermined config for an agent (no decrypted key)."""
    _require_read_access(name, current_user)

    config = db.get_nevermined_config(name)
    if not config:
        raise HTTPException(status_code=404, detail="Nevermined not configured for this agent")
    return config


@router.delete("/agents/{name}/config")
async def delete_nevermined_config(
    name: str,
    current_user: User = Depends(get_current_user),
):
    """Remove Nevermined config from an agent."""
    _require_write_access(name, current_user)

    deleted = db.delete_nevermined_config(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Nevermined not configured for this agent")

    logger.info(f"Nevermined config deleted for agent '{name}' by {current_user.username}")
    return {"detail": "Nevermined configuration removed"}


@router.put("/agents/{name}/config/toggle")
async def toggle_nevermined(
    name: str,
    enabled: bool,
    current_user: User = Depends(get_current_user),
):
    """Enable or disable Nevermined payments for an agent."""
    _check_sdk()
    _require_write_access(name, current_user)

    updated = db.set_nevermined_enabled(name, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Nevermined not configured for this agent")

    state = "enabled" if enabled else "disabled"
    logger.info(f"Nevermined {state} for agent '{name}' by {current_user.username}")
    return {"detail": f"Nevermined payments {state}", "enabled": enabled}


@router.get("/agents/{name}/payments", response_model=List[NeverminedPaymentLog])
async def get_payment_history(
    name: str,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """Get payment history for an agent."""
    _require_read_access(name, current_user)
    return db.get_nevermined_payment_log(name, limit)


@router.get("/settlement-failures", response_model=List[NeverminedPaymentLog])
async def get_settlement_failures(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """List unsettled payments across all agents (admin only)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return db.get_nevermined_settlement_failures(limit)


@router.post("/retry-settlement/{log_id}")
async def retry_settlement(
    log_id: str,
    current_user: User = Depends(get_current_user),
):
    """Retry a failed settlement (admin only).

    Honest not-implemented (#1018): server-side retry is not supported because the
    payer's x402 access token is never stored (it is a whole-plan-budget bearer
    credential — storing it durably is the Tier 2 follow-up to #1018, pending a
    security review). This used to return HTTP 200 "Settlement retry queued" while
    doing nothing — a misleading stub. The workable path today is client-driven:
    the caller re-presents the same ``payment-signature`` to
    ``POST /api/paid/{agent}/chat``, which replays the completed work
    (Idempotency-Key, #1018) and re-attempts settlement deterministically via the
    ``payment:{agent_request_id}`` effect guard (#1084) — no re-charge, no re-run.
    """
    _check_sdk()
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    log_entry = db.get_nevermined_payment_log_entry(log_id)
    if not log_entry:
        raise HTTPException(status_code=404, detail="Payment log entry not found")

    if log_entry.action != "settle_failed":
        raise HTTPException(status_code=400, detail="Only failed settlements can be retried")

    logger.info(
        f"retry-settlement requested for {log_id} (agent '{log_entry.agent_name}') "
        f"by {current_user.username} — returning 501 (server-side retry not supported)"
    )

    raise HTTPException(
        status_code=501,
        detail=(
            "Server-side settlement retry is not supported: the payer access token "
            "is not stored. Ask the caller to re-present the same payment-signature to "
            "POST /api/paid/{agent}/chat — it replays the completed work and re-attempts "
            "settlement without re-charging. Durable server-side retry is tracked as a "
            "follow-up to #1018."
        ),
    )
