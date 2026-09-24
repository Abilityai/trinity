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

from typing import List

from database import db
from db.capability_grants import CAPABILITIES


class CapabilityGrantRefused(Exception):
    """A named refusal the router maps to an HTTP status."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


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
