"""Library skill assignment — the write, its gates and its delivery, as one call
(trinity-enterprise#1030, extracted from ``routers/skills.py``).

Two callers drive the same sequence: the assign route, and a system manifest's
``skills:`` field (apply and deploy). The order is the contract — the row, then
the gates (ent#753: gated before delivered), then delivery (#2703), then the
thin change broadcast — so it lives here once rather than in each caller.

Governance is the caller's: the route's ``get_skill_managed_agent_by_name``
(ent#596), apply's owner gate. Nothing here checks who may write.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from database import db
from dependencies import is_person_principal
from services.skill_packaging import validate_skill_name
from services.skill_service import skill_service, broadcast_skills_changed

logger = logging.getLogger(__name__)


async def deliver_assigned_skills(
    agent_name: str, added_names: List[str]
) -> Optional[Dict[str, Any]]:
    """Deliver just-assigned skill packages to the agent (#2703) — the write-side
    twin of the router's `_remove_unassigned_skills`, with the same contract: the
    row has committed and is authoritative, delivery is best-effort, and a
    stopped agent / busy lock / transport failure is a NAMED outcome in the body,
    never a failed request. See `skill_service.deliver_assigned` for the
    vocabulary.

    Returns the delivery report for the response body, or None when nothing was
    assigned.
    """
    names = [n for n in added_names if validate_skill_name(n)]
    if not names:
        return None
    try:
        return await skill_service.deliver_assigned(agent_name, names)
    except Exception as e:  # noqa: BLE001 — never fail a committed assign
        logger.warning(f"skill delivery failed for {agent_name}: {e}")
        return {
            "status": "not_delivered",
            "reason": "injection_error",
            "skills": {n: {"status": "not_delivered"} for n in sorted(names)},
        }


async def sync_gates(
    agent_name: str,
    current_user,
    request=None,
    *,
    removed: Optional[List[str]] = None,
    conflict: Optional[List[str]] = None,
    removal: Optional[Dict[str, Any]] = None,
    prune: Optional[Dict[str, Any]] = None,
    add: bool = True,
) -> Optional[Dict[str, Any]]:
    """Bring the agent's skill gates in line with an assignment write
    (trinity-enterprise#753). Every site that writes `agent_skills` calls it —
    `tests/unit/test_ent753_assignment_hooks.py` holds that.

    * `add` — library defaults for every assigned recommended skill. Called
      BEFORE `deliver_assigned_skills`, so a package never lands ungated.
    * A gate on a `removed` name goes only once its package has left the agent
      (`removal`, the route's own report; `prune`, the inject route's). A
      deferred or failed removal keeps it — the files may still be there.
    * An explicit gate goes only when a PERSON unassigned the skill. An agent's
      unassign — a skills.manage holder, itself or a sibling — keeps it and says
      so, so unassign-then-reassign cannot launder a gate away (plan-gate
      ruling 2026-10-07).

    Returns `{gates_removed, gates_kept, gate_defaults}` (only what happened),
    or None. Never raises: the assignment has committed.
    """
    from services import skill_gate_map_service as gate_map

    ctx = gate_map.gate_context(
        current_user, trigger="assignment",
        ip=request.client.host if request is not None and request.client else None,
        endpoint=request.scope.get("path") if request is not None else None,
        request_id=getattr(request.state, "request_id", None) if request is not None else None,
    )
    report: Dict[str, Any] = {}
    names = [n for n in (removed or []) if n]
    gone = gate_map.packages_gone(removal)
    try:
        dropped: List[str] = []
        if names and is_person_principal(current_user) and not getattr(
                current_user, "vouched_source_agent", None):
            dropped = await gate_map.drop_explicit_gates_on_unassign(
                agent_name, [n for n in names if n.lower() in gone], ctx=ctx, keep=conflict or [])
            if dropped:
                report["gates_removed"] = dropped
        if names:
            kept = [n for n in await asyncio.to_thread(gate_map.explicit_gate_names, agent_name, names)
                    if n not in dropped]
            if kept:
                report["gates_kept"] = kept
    except Exception as e:  # noqa: BLE001 — an explicit gate left in place is the safe direction
        logger.warning("[ent#753] gate removal after unassign failed for %s: %s", agent_name, e)
    defaults = await gate_map.reconcile_library_gates(
        agent_name, ctx=ctx, add=add,
        drop_defaults=gate_map.drop_after_prune(prune) if prune is not None else gone)
    if defaults:
        report["gate_defaults"] = defaults
    return report or None


async def assign_and_deliver(
    agent_name: str,
    names: List[str],
    actor,
    *,
    acting_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Assign each library skill in `names` to the agent, gate, deliver, broadcast.

    The caller has already checked each name is in the library (the route's
    404, apply's `skill_unknown` blocker). A row that exists only because a set
    names it becomes an individual assignment (ent#530) and is reported under
    `already`. Delivery runs on BOTH branches (#2703: a re-assign after a
    `not_delivered` is the only retry some surfaces have).

    Returns `{assigned: [names], rows: {name: row}, already: [names],
    delivery, gates}`.
    """
    rows: Dict[str, Any] = {}
    already: List[str] = []
    for name in names:
        row = db.assign_skill(agent_name, name, actor.username,
                              assigned_by_agent=acting_agent)  # ent#596, R29
        if row is None:
            db.set_skill_individual(agent_name, name, True)
            already.append(name)
        else:
            rows[name] = row
    gates = await sync_gates(agent_name, actor)   # ent#753: gated before delivered
    delivery = await deliver_assigned_skills(agent_name, list(names))
    await broadcast_skills_changed(agent_name)
    return {"assigned": list(rows), "rows": rows, "already": already,
            "delivery": delivery, "gates": gates}
