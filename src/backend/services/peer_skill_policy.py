"""What an agent-permissions edge exposes (abilityai/trinity-enterprise#662).

A peer may run what an agent declares; what an admin assigned needs a grant.

An `agent_permissions` edge from B to A decides WHETHER B may reach A (P-02,
`checkAgentEdge` at the MCP layer, `dependencies.agent_may_reach` at the
backend). Until ent#662 nothing decided WHAT B may invoke there, so assigning a
library skill to A — directly, or by a commit to a skill set A holds, which
propagates fleet-wide as `assigned_by="system"` — silently widened what every
existing caller of A could trigger through A's identity and credentials.

The rule, decided here and enforced at `skill_gate_service.enforce` (which every
admission seam and the `execute_task` backstop call):

* **library-assigned** on A = a name with an `agent_skills` row for A,
  individual or set-propagated alike, unless the row is stamped
  `delivery_status = "conflict"` (#2914 — A's own directory of that name is
  kept and is what runs, so the name is A's declared skill);
* a **peer** = a requester that is another agent (never A itself, never a
  person, a schedule, a channel, a connector — whose surface is its own
  allow-list, ent#46 — and never `trinity-system`, which holds no edge);
* a peer request that invokes a library-assigned name without a per-skill grant
  (`agent_peer_skill_grants`, the ent#590 primitive) is refused 403
  `skill_not_granted` before anything is claimed or dispatched, and audited.

Matching is the skill gate's own (`utils.skill_invocation`): the requester's
text and the context text the executor's prompt carries beside it. Every "could
not tell" refuses a peer (503 `peer_surface_unavailable`), never "unrestricted".

HTTP-free (Invariant #1): raises `SkillGateRefused` / `PeerSkillGrantRefused`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from database import db
from db.agents import SYSTEM_AGENT_NAME
from services.skill_gate_errors import SkillGateRefused
from utils.skill_invocation import find_gated_invocations

logger = logging.getLogger(__name__)

REFUSED_CODE = "skill_not_granted"
UNAVAILABLE_CODE = "peer_surface_unavailable"
_AGENT_KIND = "agent"   # skill_gate_service.KIND_AGENT (no import: that module imports this one)


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------

def library_assigned(agent_name: str) -> Dict[str, Any]:
    """The library-assigned skills on `agent_name`, keyed by name (the
    `AgentSkill` row). A #2914 conflict row is excluded: the agent's own copy
    runs. Raises on an unreadable table — the caller decides what that means."""
    return {row.skill_name: row for row in db.get_agent_skills(agent_name)
            if row.delivery_status != "conflict"}


def has_library_skills(agent_name: str) -> bool:
    """Could the peer rule apply to `agent_name` at all? A cheap pre-check for
    the `execute_task` backstop. An unreadable table answers True, so the full
    check runs and refuses a peer by name."""
    try:
        return bool(library_assigned(agent_name))
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def peer_of(agent_name: str, requester) -> Optional[str]:
    """The calling agent's name when `requester` is a PEER of `agent_name`,
    else None."""
    caller = getattr(requester, "agent_name", None)
    if getattr(requester, "kind", None) != _AGENT_KIND or not caller:
        return None
    if caller == agent_name or caller == SYSTEM_AGENT_NAME:
        return None
    return caller


def _spawned(caller: str, target: str) -> bool:
    """Did `caller` spawn `target` with the key it holds now? Parent → child
    only — a child calling its parent is a peer like any other. The ent#629
    relation (`dependencies._spawned_by_caller`), which fails closed."""
    import dependencies   # lazy: dependencies imports the service layer
    return dependencies._spawned_by_caller(caller, target)


async def enforce(agent_name: str, *, requester, request_text: Optional[str],
                  context_text: Optional[str] = None,
                  execution_id: Optional[str] = None) -> None:
    """Refuse a peer request that invokes a library-assigned skill it holds no
    grant for. Returns None when the request may proceed to the skill gate."""
    caller = peer_of(agent_name, requester)
    if caller is None or _spawned(caller, agent_name):
        return
    try:
        assigned = library_assigned(agent_name)
        granted = db.granted_peer_skills(caller, agent_name) if assigned else set()
    except Exception as e:  # noqa: BLE001
        logger.exception("[PeerSkills] could not read %s's assignments or grants — refusing", agent_name)
        await _audit_refusal(caller=caller, callee=agent_name, skills=[],
                             mcp_key_id=getattr(requester, "mcp_key_id", None),
                             reason=UNAVAILABLE_CODE, execution_id=execution_id)
        raise SkillGateRefused(
            503, UNAVAILABLE_CODE,
            f"Could not check which skills {caller} may run on {agent_name}; "
            "nothing was run. Try again shortly.") from e
    restricted = [n for n in assigned if n not in granted]
    if not restricted:
        return
    names = find_gated_invocations(request_text, restricted)
    names += [n for n in find_gated_invocations(context_text, restricted) if n not in names]
    if not names:
        return
    await _audit_refusal(caller=caller, callee=agent_name, skills=names,
                         mcp_key_id=getattr(requester, "mcp_key_id", None),
                         reason=REFUSED_CODE, execution_id=execution_id)
    one = len(names) == 1
    noun = "the skill" if one else "the skills"
    # No slash before a name: this text is echoed into rows and replies and
    # must never read as a request to run a skill (the gate's own rule). The
    # matcher reads a name anywhere, so a mere mention lands here too: the text
    # says how to send one, and never sends the caller to widen a standing
    # permission — the calling model is told to do what it says (trinity#3949).
    raise SkillGateRefused(
        403, REFUSED_CODE,
        f"Not run, nothing was delivered: this message names {noun} {', '.join(names)}, "
        f"which {caller} may not run on {agent_name} "
        f"({'it was' if one else 'they were'} assigned from the skills library and "
        f"need{'s' if one else ''} a grant). If you only meant to mention "
        f"{'it' if one else 'them'}, write {'its name' if one else 'their names'} without "
        f"the leading slash{'' if one else 'es'} and send again. Do not carry out "
        f"{'its' if one else 'their'} steps another way. Asking for {'it' if one else 'them'} "
        f"by name without the slash{'' if one else 'es'} is the same request and is not "
        f"permitted.",
        skills=names, caller=caller, agent=agent_name)


async def _audit_refusal(*, caller: str, callee: str, skills: List[str],
                         mcp_key_id: Optional[str], reason: str,
                         execution_id: Optional[str]) -> None:
    """One `authorization` audit row per refusal. Best effort."""
    try:
        from services.platform_audit_service import AuditEventType, platform_audit_service
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action="peer_skill_refused",
            source="gateway",
            actor_agent_name=caller,
            mcp_key_id=mcp_key_id,
            target_type="agent",
            target_id=callee,
            details={"skills": list(skills), "reason": reason, "execution_id": execution_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning("[PeerSkills] audit of a refusal %s -> %s failed", caller, callee)


# ---------------------------------------------------------------------------
# The grant surface
# ---------------------------------------------------------------------------

class PeerSkillGrantRefused(Exception):
    """A named refusal the router maps to an HTTP status."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def _agent_exists(agent_name: str) -> bool:
    return bool(db.get_agent_owner(agent_name))   # None for nonexistent AND soft-deleted


def consent_line(caller: str, skill: str, target: str) -> str:
    return (f"{caller} may now run the skill {skill} on {target}, with {target}'s "
            "identity, credentials and write access.")


def set_grant(caller: str, target: str, skill: str, granted: bool, actor: str) -> dict:
    """Grant or revoke. Idempotent both ways; `changed` says whether it moved.

    A revoke needs no validation — taking a grant away is always safe, and
    refusing one for a since-deleted agent would strand a row."""
    if not granted:
        changed = db.revoke_peer_skill(caller, target, skill)
        return {"caller": caller, "target": target, "skill": skill,
                "granted": False, "changed": changed}
    if caller == target:
        raise PeerSkillGrantRefused(
            "self_grant", "An agent always runs its own skills; it needs no grant.", 422)
    if caller == SYSTEM_AGENT_NAME:
        raise PeerSkillGrantRefused(
            "system_agent_not_grantable",
            "The system agent holds no permission edge, so a skill grant would mean nothing.", 422)
    if not (_agent_exists(caller) and _agent_exists(target)):
        raise PeerSkillGrantRefused("agent_not_found", "Agent not found", 404)
    if skill not in library_assigned(target):
        raise PeerSkillGrantRefused(
            "skill_not_assigned",
            f"{skill} is not a library skill assigned to {target}. A skill {target} "
            "declares itself is already callable over the edge; only library skills need a grant.",
            422)
    changed = db.grant_peer_skill(caller, target, skill, actor)
    return {"caller": caller, "target": target, "skill": skill, "granted": True,
            "changed": changed, "consent": consent_line(caller, skill, target)}


DRIFT_WINDOW_DAYS = 30


def _recent_uses(caller: str, target: str, names: List[str]) -> Dict[str, dict]:
    """Which of `names` `caller` invoked on `target` in the last
    DRIFT_WINDOW_DAYS, read off the execution ledger with the gate's own
    matcher: ``{name: {uses, last_used_at}}``."""
    if not names:
        return {}
    from utils.helpers import iso_cutoff
    out: Dict[str, dict] = {}
    for row in db.recent_peer_messages(caller, target, iso_cutoff(DRIFT_WINDOW_DAYS * 24)):
        for name in find_gated_invocations(row.get("message"), names):
            hit = out.setdefault(name, {"uses": 0, "last_used_at": None})
            hit["uses"] += 1
            at = row.get("started_at")
            if at and (hit["last_used_at"] is None or at > hit["last_used_at"]):
                hit["last_used_at"] = at
    return out


def drift_report() -> dict:
    """The honest upgrade: every (edge × library skill) pair the caller USED in
    the last DRIFT_WINDOW_DAYS and that is now refused — no grant, not its own
    spawned agent. Admin-only at the router; a grant removes the pair."""
    pairs = []
    for edge in db.get_all_permission_edges():
        caller, target = edge["source"], edge["target"]
        if caller == target or caller == SYSTEM_AGENT_NAME or _spawned(caller, target):
            continue
        assigned = library_assigned(target)
        if not assigned:
            continue
        granted = db.granted_peer_skills(caller, target)
        restricted = [n for n in assigned if n not in granted]
        for name, hit in sorted(_recent_uses(caller, target, restricted).items()):
            pairs.append({"caller": caller, "target": target, "skill": name, **hit})
    return {"window_days": DRIFT_WINDOW_DAYS, "pairs": pairs}


def peer_skills_view(caller: str, *, viewer) -> dict:
    """Per permitted target of `caller` that `viewer` may open: the library
    skills on it and whether `caller` may run each. Also the upgrade report —
    `not_granted` counts what the edge no longer reaches."""
    from dependencies import agent_may_reach   # lazy: dependencies imports the service layer

    grants = db.list_peer_skill_grants(caller)
    username = getattr(viewer, "username", None)
    targets = []
    for target in db.get_permitted_agents(caller):
        # The viewer must be able to open the target, and an agent key must be
        # able to reach it (ent#629) — the skill names are the target's data.
        if not username or not (db.can_user_access_agent(username, target)
                                and agent_may_reach(viewer, target)):
            continue
        held = grants.get(target, {})
        assigned = library_assigned(target)
        used = _recent_uses(caller, target, list(assigned))
        skills = []
        for name, row in sorted(assigned.items()):
            g = held.get(name)
            skills.append({
                "name": name,
                "individual": bool(getattr(row, "individual", True)),
                "assigned_by": row.assigned_by,
                "granted": g is not None,
                "granted_by": g["granted_by"] if g else None,
                "granted_at": g["granted_at"] if g else None,
                "last_used_at": used.get(name, {}).get("last_used_at"),
            })
        # A parent runs every skill on an agent it spawned (no grant needed).
        spawned = _spawned(caller, target)
        targets.append({"target": target, "skills": skills, "spawned": spawned,
                        "not_granted": 0 if spawned else sum(1 for s in skills if not s["granted"])})
    return {"caller": caller, "targets": targets}
