"""Who may hold a capability, and the act of granting it (trinity-enterprise#596).

The grant is the GRANT half of grant-vs-use: the router lets only an admin,
signed in interactively, reach this. What this module decides is which AGENTS
may receive a capability at all:

* a live agent only — a nonexistent and a soft-deleted agent are the same
  uniform "not found" (#186), so the grant list can't be used to probe;
* never an ephemeral ("ghost") agent — it runs an arbitrary, possibly untrusted
  workspace, and `_enforce_ephemeral_key_fence` already treats its key as a
  fleet skeleton key; granting it `skills.manage` would hand that key the one
  write the fence exists to deny;
* never the system agent — `trinity-system` already passes every capability by
  its scope, so a grant row for it would be a lie about where its authority
  comes from.

HTTP-free (Invariant #1): raises `CapabilityGrantRefused`, the router maps it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from database import db
from db.capability_grants import CAPABILITIES, CAPABILITY_INSTRUCTIONS_MANAGE, CAPABILITY_SKILLS_MANAGE

logger = logging.getLogger(__name__)


def _is_calibrating(agent_name: str) -> bool:
    """The owner's readiness stamp says `calibrating` (ent#527/#663). The stamp
    is the only authority — the agent can't write it; an unstamped agent is not
    treated as calibrating (the ent#689 honest limit)."""
    stamp = db.get_agent_role_readiness(agent_name)
    return bool(stamp) and stamp.get("status") == "calibrating"


class CapabilityGrantRefused(Exception):
    """A named refusal the router maps to an HTTP status."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def grant_audit_action(capability: str, granted: bool) -> str:
    """The AUTHORIZATION verb for one grant change, whichever door made it.

    `skills.manage` keeps the skill-manager route's verb (#3236 review), so an
    audit query for it finds both doors; every other capability is
    `capability_grant` / `capability_revoke`.
    """
    if capability == CAPABILITY_SKILLS_MANAGE:
        return "skill_manager_grant" if granted else "skill_manager_revoke"
    return "capability_grant" if granted else "capability_revoke"


async def audit_grant_change(
    agent_name: str,
    capability: str,
    granted: bool,
    actor_user: Any,
    *,
    request: Any = None,
    source: str = "api",
    log: Optional[Callable[..., Any]] = None,
) -> None:
    """The per-change audit row every grant door writes (trinity-enterprise#1030
    CP2): the two routes and the manifest apply engine call this, so they
    cannot drift on the verb or the row shape. Best-effort — the grant has
    already committed, so a failed audit is logged, never raised.

    `log` is the caller's audit sink (`platform_audit_service.log` as the
    caller has it bound); it defaults to the service's own instance.
    """
    if log is None:
        from services.platform_audit_service import platform_audit_service
        log = platform_audit_service.log
    from services.platform_audit_service import AuditEventType
    client = getattr(request, "client", None)
    try:
        await log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action=grant_audit_action(capability, granted),
            source=source,
            actor_user=actor_user,
            actor_ip=getattr(client, "host", None) if client else None,
            endpoint=(getattr(request, "scope", None) or {}).get("path") if request else None,
            request_id=getattr(getattr(request, "state", None), "request_id", None),
            target_type="agent",
            target_id=agent_name,
            details={"capability": capability, "granted": granted},
        )
    except Exception:  # noqa: BLE001
        logger.warning("capability grant audit failed for %s", agent_name)


def list_holders(capability: str) -> List[dict]:
    """Every live agent holding `capability`, oldest grant first."""
    if capability not in CAPABILITIES:
        raise CapabilityGrantRefused(
            "unknown_capability", f"'{capability}' is not a capability.", 422
        )
    return db.list_capability_holders(capability)


def set_grant(agent_name: str, capability: str, granted: bool, actor: str) -> dict:
    """Grant or revoke. Idempotent both ways; `changed` says whether it moved.

    Revoking needs no target validation — taking a capability away from any
    name is always safe, and refusing a revoke for a since-deleted agent would
    leave an orphan row nobody could clear.
    """
    if capability not in CAPABILITIES:
        raise CapabilityGrantRefused(
            "unknown_capability", f"'{capability}' is not a capability.", 422
        )
    if not granted:
        changed = db.revoke_agent_capability(agent_name, capability)
        return {"agent_name": agent_name, "capability": capability,
                "granted": False, "changed": changed}

    owner = db.get_agent_owner(agent_name)  # None for nonexistent AND soft-deleted
    if not owner:
        raise CapabilityGrantRefused("agent_not_found", "Agent not found", 404)
    if owner.get("is_system"):
        raise CapabilityGrantRefused(
            "system_agent_not_grantable",
            "The system agent already holds every capability by its scope; "
            "it needs no grant.",
            422,
        )
    if capability == CAPABILITY_INSTRUCTIONS_MANAGE and _is_calibrating(agent_name):
        # ent#164 / ent#663: a calibrating companion's instructions are still
        # being tuned through the queue; it may not rewrite them itself.
        raise CapabilityGrantRefused(
            "calibrating_agent",
            "This companion is still calibrating — its instructions change "
            "through the operator queue until its owner marks it ready.",
            422,
        )
    info = db.get_agent_ephemeral_info(agent_name)
    if isinstance(info, dict) and info.get("is_ephemeral"):
        raise CapabilityGrantRefused(
            "ephemeral_agent_not_grantable",
            "An ephemeral agent can't hold a capability — it runs a workspace "
            "the platform can't vouch for.",
            422,
        )
    changed = db.grant_agent_capability(agent_name, capability, actor)
    return {"agent_name": agent_name, "capability": capability,
            "granted": True, "changed": changed}
