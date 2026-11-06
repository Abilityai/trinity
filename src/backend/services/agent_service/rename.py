"""Agent rename, as a primitive (abilityai/trinity-enterprise#1111, T3).

The whole rename used to be the body of `routers/agent_rename.py`'s handler —
human-only, dereferencing `request` for its audit, reachable only over HTTP. A
platform caller acting on its own behalf (`actor=None`) therefore had no rename
at all, and the alternative was a second implementation that would drift from
the five fixes this one has accumulated: the #1560 Redis sweep of BOTH names,
the #1664 `volume_base_name` pin, the #1671 base-claim recheck inside
`db.rename_agent`, the #3813 / #1159 env re-bake on the next start, and the
ent#751 gate cancel.

Same shape as `purge_agent_now` (its sibling in this package): an
edition-agnostic enforcement primitive with no route of its own, audited as
`source="system"` when `actor is None`.

What stays with the ROUTE, deliberately:

* the gates — `require_person`, `reject_agent_principal`, `can_user_rename_agent`;
* the name sanitiser and its 400s. The route's sanitiser differs from
  `utils.helpers.sanitize_agent_name` (it keeps `.` out), a pre-existing
  divergence this extraction does not inherit: the primitive takes a FINAL
  name and never rewrites it, so a caller that derives a name elsewhere gets
  the name it asked for or a refusal;
* the "that name already has a container" 409 and the #1671 volume-base 409,
  both of which must fire before anything is touched.

Three refusals come out of here, each mapped by the route to the code it
answered before: `SystemAgentRefused` → 403, `AgentContainerMissing` → 404, and
`RenameRefused` → 500. None of them is a FastAPI error: a service holds no HTTP
concerns (Invariant #1), and a platform caller with no route needs the refusal,
not a status code. The rename leaves the agent STOPPED; the next
`start_agent_internal` recreate is what re-bakes `AGENT_NAME`, the
`trinity.agent-name` label and `TRINITY_AGENT_AUTH_TOKEN`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from database import db
from models import User
from services.docker_service import get_agent_container
from services.docker_utils import container_rename, container_stop
from services.image_generation_prompts import AVATAR_EMOTIONS
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

# AVATAR-001 / AVATAR-002: the cached avatar, reference and emotion images are
# keyed by agent name on disk, so they follow the rename.
AVATAR_DIR = Path("/data/avatars")


@dataclass
class RenameResult:
    """`was_running` is what the route reports so the caller knows a restart is
    needed — the rename deliberately leaves the agent stopped."""

    old_name: str
    new_name: str
    was_running: bool


class RenameRefused(Exception):
    """`db.rename_agent` would not take the new name (or failed inside its
    transaction — it re-checks the #1671 volume-base claim there).

    The Docker container has been renamed BACK before this is raised, so the
    agent is left exactly as it was found, under its old name and stopped —
    unless that rename-back itself failed, in which case the container is
    stranded under the new name and `_rename_container_back` has logged it
    (the refusal is still what is raised, so no caller loses its branch).
    """


class SystemAgentRefused(Exception):
    """The agent is a system agent, which is never renamed.

    Raised before any side effect. The route answers it 403 with the text it
    always had; the user-facing route refuses a system agent itself, earlier,
    so this is the floor for a platform caller that has no gate above it.
    """


class AgentContainerMissing(Exception):
    """There is no container for the agent, so there is nothing to rename.

    Raised before any write — distinct from `RenameRefused` because the route
    answers it with 404 rather than 500.
    """


async def rename_agent_now(
    old_name: str,
    new_name: str,
    *,
    actor: Optional[User],
    reason: Optional[str] = None,
    audit_ctx: Optional[Mapping[str, Any]] = None,
    ws_manager: Any = None,
    filtered_ws_manager: Any = None,
) -> RenameResult:
    """Rename `old_name` to `new_name`: container + every DB reference.

    `new_name` is taken as final — it is not sanitised or de-duplicated here.
    `actor=None` is the platform acting on its own behalf (audited as
    `system`); a person caller passes their `User` plus `audit_ctx`
    (`actor_ip` / `endpoint` / `request_id`) so the audit row keeps the HTTP
    context it had before. The WebSocket managers are optional and injected
    (`create_agent_internal`'s shape): a caller with none broadcasts nothing
    rather than reaching back into a router for a global.

    Raises `SystemAgentRefused` for a system agent, `AgentContainerMissing`
    when there is no container, `RenameRefused` when the DB will not take the
    name, and propagates a Docker fault to the caller.
    """
    if db.is_system_agent(old_name):
        raise SystemAgentRefused(old_name)

    container = get_agent_container(old_name)
    if not container:
        raise AgentContainerMissing(old_name)

    # The agent has to be stopped to be renamed, and it stays stopped.
    was_running = container.status == "running"
    if was_running:
        await container_stop(container)

    old_container_name = f"agent-{old_name}"
    await container_rename(container, f"agent-{new_name}")

    # Container env and labels change only on recreate. The next start's
    # recreate re-bakes AGENT_NAME and the trinity.agent-name label from the
    # new name (#3813, check_agent_name_env_matches).
    #
    # #1159: the running container still carries TRINITY_AGENT_AUTH_TOKEN=
    # derive(old_name); under the new name that token is stale and would 401
    # once enforcement is on. No extra work here — the rename leaves the agent
    # stopped, and the next start_agent_internal recreate is forced by
    # check_agent_auth_token_env_matches (token mismatch) and re-injects
    # derive(new_name).
    #
    # Docker volumes are NOT renamed: Docker supports neither renaming a volume
    # nor editing its (immutable) labels, so copying gigabytes of
    # `/home/developer` on every rename is the only alternative. The agent keeps
    # its existing `agent-{old_name}-*` volumes and the container carries the
    # same mounts forward (recreate_container_with_updated_config rebuilds the
    # mount set from the old container's Mounts).
    #
    # #1664: that makes the volume's own identity (name + `trinity.agent-name`
    # label) permanently stale, which the #1581 orphan sweep once read as "this
    # volume's agent no longer exists" — and force-removed the LIVE agent's home
    # volume during a recreate gap. `db.rename_agent` therefore pins
    # `agent_ownership.volume_base_name = old_name` atomically with the rename;
    # the sweep resolves ownership from that, never from the volume. Anything
    # that needs this agent's volume names must ask
    # `db.get_volume_base_name(agent)` — NOT f"agent-{agent_name}-workspace".
    try:
        renamed = db.rename_agent(old_name, new_name)
    except Exception as e:
        # The DB re-checks the #1671 base claim inside its transaction, so a
        # raise here is a refusal too — and leaves the rows untouched. Undo the
        # container rename either way, or the agent is stranded under a name
        # nothing in the database knows (eng H3).
        await _rename_container_back(container, old_container_name, new_name)
        raise RenameRefused(f"{old_name} -> {new_name}: {e}") from e
    if not renamed:
        await _rename_container_back(container, old_container_name, new_name)
        raise RenameRefused(f"{old_name} -> {new_name}: the name is taken")

    # trinity-enterprise#751: a gated request approved for the old name must not
    # run under the new one. The rename re-keyed the records; cancel the pending
    # ones and tell their requesters. Best effort.
    try:
        from services import skill_gate_service

        await skill_gate_service.cancel_pending_for_agent(
            new_name,
            actor_email=actor.email if actor is not None else None,
            actor_user=actor,
            reason="agent_renamed")
    except Exception as e:
        logger.warning(f"Failed to cancel gated requests after renaming {old_name}: {e}")

    # #1560 / RELIABILITY-004 (#307): every per-agent Redis keyspace is keyed by
    # name, so a rename orphans all of them under the old name — the heartbeat
    # `seen` marker (no TTL), both circuit breakers, and the slot ZSET. The new
    # name is swept too: it may have been used by an agent that the retention
    # purge has since removed, whose breaker verdict would otherwise be
    # inherited here. The container is stopped for the whole rename, so neither
    # sweep can race an in-flight execution's slot. The renamed container
    # re-establishes its own state on next beat/dispatch. Best-effort.
    try:
        from services.agent_runtime_state import clear_agent_runtime_state

        await clear_agent_runtime_state(old_name)
        await clear_agent_runtime_state(new_name)
    except Exception as e:
        logger.warning(
            f"Failed to clear Redis runtime state on rename {old_name} -> {new_name}: {e}")

    _move_avatars(old_name, new_name)
    await _broadcast_rename(old_name, new_name, ws_manager, filtered_ws_manager)

    # SEC-001: audit rename.
    details = {"old_name": old_name, "new_name": new_name}
    if reason:
        details["reason"] = reason
    ctx = audit_ctx or {}
    await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="rename",
        source="api" if actor is not None else "system",
        actor_user=actor,
        actor_ip=ctx.get("actor_ip"),
        target_type="agent",
        target_id=new_name,
        endpoint=ctx.get("endpoint"),
        request_id=ctx.get("request_id"),
        details=details,
    )

    return RenameResult(old_name=old_name, new_name=new_name, was_running=was_running)


async def _rename_container_back(container, old_container_name: str, new_name: str) -> None:
    """Undo the container rename after the DB refused (I5).

    A Docker fault HERE is the one state this flow cannot repair: the rows kept
    the old name, the container is under the new one, and nothing in the
    database points at it — the #1581 orphan sweep reads it as unowned and the
    next start recreates a second container. So it is logged by name and
    swallowed, and the ORIGINAL refusal is the error the caller sees: masking a
    `RenameRefused` with a Docker error loses the reason the rename failed and
    sends every caller down the wrong branch (the route would 500 with
    "Failed to rename agent: <docker>" instead of the DB text, and adoption's
    `except RenameRefused` would miss its own release path entirely).
    """
    try:
        await container_rename(container, old_container_name)
    except Exception as e:
        logger.error(
            "Failed to rename the container back after the database refused: "
            f"{old_container_name} is STRANDED under agent-{new_name} while the "
            f"database kept its old name. Rename the container by hand or stop it "
            f"and let the next start recreate it: {e}")


def _move_avatars(old_name: str, new_name: str) -> None:
    """Rename the cached avatar, reference and emotion images (best effort)."""
    try:
        for ext in (".webp", ".png"):
            old_path = AVATAR_DIR / f"{old_name}{ext}"
            new_path = AVATAR_DIR / f"{new_name}{ext}"
            if old_path.exists():
                old_path.rename(new_path)
        # Reference stays .png
        old_ref = AVATAR_DIR / f"{old_name}_ref.png"
        new_ref = AVATAR_DIR / f"{new_name}_ref.png"
        if old_ref.exists():
            old_ref.rename(new_ref)
        for emotion in AVATAR_EMOTIONS:
            for ext in (".webp", ".png"):
                old_path = AVATAR_DIR / f"{old_name}_emotion_{emotion}{ext}"
                new_path = AVATAR_DIR / f"{new_name}_emotion_{emotion}{ext}"
                if old_path.exists():
                    old_path.rename(new_path)
    except Exception as e:
        logger.warning(f"Failed to rename avatar for agent {old_name}: {e}")


async def _broadcast_rename(old_name: str, new_name: str, ws_manager, filtered_ws_manager
                            ) -> None:
    """Invariant #10: through the managers' broadcast API, never `event_bus`
    directly. A thin trigger — identifiers only (#918)."""
    event = {
        "event": "agent_renamed",
        "type": "agent_renamed",
        "name": new_name,
        "data": {
            "old_name": old_name,
            "new_name": new_name
        }
    }
    if ws_manager:
        await ws_manager.broadcast(json.dumps(event))
    if filtered_ws_manager:
        await filtered_ws_manager.broadcast_filtered(event)
