"""Immediate agent purge (abilityai/trinity-enterprise#1055).

Agent delete is a SOFT delete (#834): the container goes, everything else —
volumes, sharing, permissions, the MCP key, every child row — is kept for the
retention window until `cleanup_service._sweep_soft_deleted_agents` hard-purges
it. `purge_agent_now` runs that hard purge at once, for the case where an agent
must not outlive the decision to remove it (an invited member is removed, or a
half-created agent is rolled back).

It is an edition-agnostic enforcement primitive (the `users.suspended_at` /
ent#995 shape): no route calls it here; an entitled module or a later admin
surface does. Three properties are load-bearing:

* **Audited first, fail-closed.** The `agent_lifecycle:purge` row is written
  while the agent still exists. If it cannot be written, nothing is destroyed —
  an unrecoverable action never runs unrecorded. The audit table is not in the
  #816 cascade, so the row outlives the agent.
* **Admin-only for a person.** A person caller must pass `assert_admin`, which
  also refuses an agent key carrying its admin owner's role (ent#293/#1890).
  `actor=None` is the platform acting on its own behalf (audited as system).
* **Docker is best-effort, rows are not.** Container removal and volume removal
  tolerate a daemon fault (#2196: unreadable is not "nothing there"); a residual
  volume is reclaimed later by the orphan-volume sweep (#1581). The row purge
  reuses `purge_agent_ownership` → `cascade_delete`, the same path the sweep runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from database import db
from models import User
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)


@dataclass
class PurgeResult:
    """`purged` is True when this call removed the agent's ownership row.
    `audit_id` is the `audit_log.event_id` of the purge row (None when there
    was nothing to purge)."""
    purged: bool
    audit_id: Optional[str] = None
    volumes_removed: int = 0


async def purge_agent_now(
    agent_name: str,
    *,
    actor: Optional[User],
    reason: Optional[str] = None,
) -> PurgeResult:
    """Hard-remove `agent_name` now: container, queued work, Redis state, every
    DB row the #816 cascade covers, the agent's MCP key, and its data volumes.

    Raises 403 for a person caller who is not a human admin and for a system
    agent, 409 for an ephemeral agent (it has its own discard path), and 503
    when the audit row cannot be written. An agent with no row is a no-op."""
    if actor is not None:
        from dependencies import assert_admin

        assert_admin(actor)

    if not db.is_agent_name_reserved(agent_name):
        return PurgeResult(purged=False)
    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="System agents cannot be purged")
    eph = db.get_agent_ephemeral_info(agent_name)
    if isinstance(eph, dict) and eph.get("is_ephemeral"):
        raise HTTPException(
            status_code=409,
            detail="Ephemeral agents are removed by discard, not purge",
        )

    # #1664: read the volume base BEFORE the rows go — a renamed agent's
    # workspace volume is named after its former self, and after the purge
    # nothing remembers that.
    try:
        volume_base = db.get_volume_base_name(agent_name) or agent_name
    except Exception:
        volume_base = agent_name

    details = {"immediate": True, "volume_base": volume_base}
    if reason:
        details["reason"] = reason
    audit_id = await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="purge",
        source="api" if actor is not None else "system",
        actor_user=actor,
        target_type="agent",
        target_id=agent_name,
        details=details,
    )
    if not audit_id:
        raise HTTPException(
            status_code=503,
            detail="Purge refused: the audit record could not be written",
        )

    # Container (best-effort; a Docker fault must not stop the row purge).
    try:
        from services.docker_service import get_agent_container
        from services.docker_utils import container_remove, container_stop

        container = get_agent_container(agent_name)
        if container:
            try:
                await container_stop(container)
            except Exception as e:
                logger.warning(f"[purge] stop failed for {agent_name}: {e}")
            await container_remove(container, force=True)
    except Exception as e:
        logger.warning(f"[purge] container removal failed for {agent_name}: {e}")

    # Queued and in-flight work cannot run against a removed agent.
    try:
        from services.capacity_manager import get_capacity_manager

        await get_capacity_manager().cancel_all_overflow(agent_name, reason="agent_purged")
    except Exception as e:
        logger.warning(f"[purge] backlog cancel failed for {agent_name}: {e}")
    try:
        db.fail_all_nonterminal_for_agent(agent_name, "agent_purged")
    except Exception as e:
        logger.warning(f"[purge] non-terminal fail failed for {agent_name}: {e}")
    try:
        from services import skill_gate_service

        await skill_gate_service.cancel_pending_for_agent(
            agent_name, actor_email=getattr(actor, "email", None),
            actor_user=actor, reason="agent_deleted")
    except Exception as e:
        logger.warning(f"[purge] gated-request cancel failed for {agent_name}: {e}")

    # #1560: clear per-agent Redis state BEFORE the purge frees the name.
    try:
        from services.agent_runtime_state import clear_agent_runtime_state

        await clear_agent_runtime_state(agent_name)
    except Exception as e:
        logger.warning(f"[purge] Redis state clear failed for {agent_name}: {e}")

    # Rows: `purge_agent_ownership` refuses a live row, so soft-delete first
    # (idempotent on an already soft-deleted one). The agent's MCP key is in the
    # cascade too; the explicit delete keeps "no key survives" independent of it.
    db.delete_agent_ownership(agent_name)
    db.delete_agent_mcp_api_key(agent_name)
    purged = bool(db.purge_agent_ownership(agent_name))

    volumes_removed = 0
    if purged:
        from services.docker_utils import remove_agent_volumes

        # Both identities (#1664), never a base another row still claims.
        for base in dict.fromkeys((volume_base, agent_name)):
            try:
                if db.is_volume_base_reserved(base):
                    logger.warning(
                        "[purge] volume base %r of %s is still claimed by another "
                        "agent — skipping removal", base, agent_name)
                    continue
                volumes_removed += await remove_agent_volumes(base)
            except Exception as e:
                logger.warning(
                    f"[purge] volume removal for {agent_name} ({base}) failed "
                    f"(orphan sweep will retry): {e}")
        logger.warning(
            f"[purge] Hard-purged agent {agent_name} immediately, removed "
            f"{volumes_removed} data volume(s) — unrecoverable (audit {audit_id})")

    return PurgeResult(purged=purged, audit_id=audit_id, volumes_removed=volumes_removed)
