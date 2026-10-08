"""The per-agent skill gate map — every write to it (trinity-enterprise#753).

The map says, per agent, which skills need approval before they run and which
kind of person approves (`primary` | `approver`). The dispatch check (#751) and
the in-container hook (#752) read it through `skill_gate_service.list_skill_gates`;
this module is the only thing that writes it.

Two kinds of row, two lifetimes:

* **Explicit** (`set`) — a person or an orchestrator set it. It stays until a
  person or a `skills.manage` holder clears it, or a PERSON unassigns the
  library skill it is on and the skill's package has left the agent. An agent
  unassigning a skill — itself or a sibling — the system key, the start path
  and the library sweep leave it in place (reported as `gates_kept`), so
  unassign-then-reassign cannot launder a gate away. An own skill's gate is
  never touched by an assignment path at all: own skills have no assignment
  row (decision 1, sticky until cleared).
* **Library default** (`library_default`) — the library recommends approval
  (`approval: recommended`) for a skill the agent is assigned. Driven from
  STATE by `reconcile_library_gates`: applied BEFORE an assigned package is
  delivered (agents that already held the skill included — the backfill), and
  removed once the skill is unassigned AND its package is gone (a removal the
  route saw complete, or the start path's prune) — never while a deferred
  removal may leave the files on the agent. It never loosens because the
  metadata changed. Clearing a default on an assigned recommended skill leaves
  a `cleared` tombstone so the reconcile does not re-apply it; the tombstone
  goes with the assignment.

Every write keeps the marker ordering contract of #752
(`/opt/trinity/skill-gates-active`, which makes the hook fail closed when the
platform cannot answer): written BEFORE an agent's first gate is inserted,
removed only AFTER its last gate is gone — under the same per-agent lock as
every other marker write in this worker. A stopped agent is never exec'd; its
next start syncs it. Ephemeral agents hold no gates (decision 2).

Who may call: the routers decide (`routers/skill_gate.py`, `routers/skills.py`).
The service records who did it and from where (`GateContext`).
"""
# A package whose removal reported one of these has left the agent.
_GONE_STATUSES = frozenset({"removed", "not_present"})
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from database import db
from db.skill_gates import ORIGIN_CLEARED, ORIGIN_LIBRARY_DEFAULT, ORIGIN_SET
from services import role_addressing, skill_gate_service
from services.agent_auth import agent_httpx_client
from services.skill_gate_service import SkillGate  # noqa: F401 — re-exported for callers
from services.skill_packaging import SKILL_NAME_RE

logger = logging.getLogger(__name__)

APPROVER_KINDS = ("primary", "approver")
MIN_DEADLINE_HOURS = 1
MAX_DEADLINE_HOURS = 168
_ABSENT = object()


class SkillGateMapRefused(Exception):
    """A gate change refused by name. Nothing was written."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_detail(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class GateContext:
    """Who changed the map and from where — the audit's "who, from where" (AC 7).

    `via` comes from the principal (`via_for`): `ui` (a signed-in session), `api`
    (a person's user-scoped key), `orchestrator` (an agent key), `system`.
    `trigger` is what caused the write: `direct` (a gate route), `assignment`,
    `start` or `library_sync`.
    """

    actor: Any = None
    via: str = "system"
    trigger: str = "direct"
    ip: Optional[str] = None
    endpoint: Optional[str] = None
    request_id: Optional[str] = None


def via_for(principal) -> str:
    """Where a change came from, read off the AUTHENTICATED principal only. A
    principal with no scope attribute is never the JWT value."""
    scope = getattr(principal, "mcp_scope", _ABSENT) if principal is not None else _ABSENT
    if scope is _ABSENT:
        return "system"
    return {None: "ui", "user": "api", "agent": "orchestrator"}.get(scope, str(scope))


def gate_context(principal, *, trigger: str = "direct", ip: Optional[str] = None,
                 endpoint: Optional[str] = None, request_id: Optional[str] = None) -> GateContext:
    return GateContext(actor=principal, via=via_for(principal), trigger=trigger, ip=ip,
                       endpoint=endpoint, request_id=request_id)


def approver_kinds() -> List[str]:
    """The approver kinds this install can resolve. `approver` needs a
    registered assignments provider; without one only the agent's primary
    approves."""
    from services import assignment_provider
    return list(APPROVER_KINDS) if assignment_provider.get_provider() is not None else ["primary"]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _public(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k) for k in ("skill_name", "approver", "deadline_hours", "origin",
                                    "set_by", "set_by_agent", "set_at")}


def _reachable(agent_name: str, kind: str) -> bool:
    """Does `kind` reach anyone on this agent right now? The default admin often
    has no email, so `primary` can reach nobody — every gated request is then
    refused `role_unassigned` (skill_gate_service._approvers)."""
    try:
        return bool(role_addressing.resolve(agent_name, kind).people)
    except Exception:  # noqa: BLE001 — RoleRefused or an unreadable lookup: nobody, as far as we can tell
        return False


def list_gates(agent_name: str) -> Dict[str, Any]:
    """The agent's gate map as the API shows it."""
    rows = db.list_agent_skill_gates(agent_name)
    reach: Dict[str, bool] = {}
    gates = []
    for row in rows:
        if row["origin"] == ORIGIN_CLEARED:
            continue
        if row["approver"] not in reach:
            reach[row["approver"]] = _reachable(agent_name, row["approver"])
        gates.append({**_public(row), "approver_reachable": reach[row["approver"]]})
    return {
        "agent_name": agent_name,
        "gates": gates,
        "cleared_defaults": [r["skill_name"] for r in rows if r["origin"] == ORIGIN_CLEARED],
        "approver_kinds": approver_kinds(),
        "default_deadline_hours": skill_gate_service.DEFAULT_DEADLINE_HOURS,
    }


def approver_status(agent_name: str, principal) -> List[Dict[str, Any]]:
    """trinity-enterprise#754: every approver kind this install resolves, as
    the caller sees it — does it reach anyone now (the picker disables a kind
    nobody fills), and is the caller one of its people (the card's "you
    approve this")?

    `viewer_fills` is decided exactly as `skill_gate_service.enforce` decides
    self-approval — the same requester (`requester_from_principal`, so an
    agent key, a connector or the event loopback never fills a kind) and the
    same casefolded people list — so the card cannot say "you approve this"
    for a run the gate would hold, or the reverse. Booleans only: no person
    data leaves the server."""
    requester = skill_gate_service.requester_from_principal(principal)
    me = requester.email.strip().casefold() if requester.is_person and requester.email else ""
    out = []
    for kind in approver_kinds():
        try:
            people = skill_gate_service.approver_people(agent_name, kind)
        except Exception:  # noqa: BLE001 — a read never fails on one kind's lookup
            logger.warning("[skill_gate_map] approver lookup failed for %s/%s", agent_name, kind,
                           exc_info=True)
            people = []
        out.append({"kind": kind, "reachable": bool(people), "viewer_fills": bool(me) and me in people})
    return out


def self_approved_flags(agent_name: str, execution_ids: Iterable[str],
                        principal) -> Dict[str, Tuple[bool, bool]]:
    """trinity-enterprise#754: `{execution id: (self_approved, by_viewer)}` for
    the runs among `execution_ids` that went through without approval because
    their requester is the approver — read from ent#752's `self_approved`
    records, never from today's gate config (a run is marked by what happened
    to it). `by_viewer` is true only for a person whose casefolded email is
    the record's requester key, so the email itself never leaves the server.
    Runs with no record are absent (false). Never raises: a marker is a hint,
    never a reason to fail the executions read."""
    ids = [e for e in execution_ids if e]
    if not ids:
        return {}
    try:
        runs = db.get_self_approved_runs(agent_name, ids)
    except Exception:  # noqa: BLE001
        logger.warning("[skill_gate_map] self-approved read failed for %s", agent_name, exc_info=True)
        return {}
    requester = skill_gate_service.requester_from_principal(principal)
    mine = (f"person:{requester.email.strip().casefold()}"
            if requester.is_person and requester.email else None)
    return {eid: (True, mine is not None and key == mine) for eid, key in runs.items()}


# trinity-enterprise#754: the in-agent gate check's states, as the agent's
# `/health → skill_gate_hook` reports them (ent#752, `agent_server/routers/
# info.py::_skill_gate_hook`), plus two of the platform's own: `predates` — a
# 200 /health without the field, i.e. an image built before the hook, the
# common case the owner's warning exists for — and `unknown` — no usable
# answer at all, which is NOT "not ok" and warns nobody.
HOOK_STATES = frozenset({"ok", "missing", "not_root_owned", "writable", "unsupported_runtime"})
HOOK_PREDATES = "predates"
HOOK_UNKNOWN = "unknown"
HOOK_PROBE_TIMEOUT_SECONDS = 3.0


async def hook_status(agent_name: str) -> str:
    """Is the gate enforced inside the agent? One direct `GET /health` with a
    short timeout and NO circuit-breaker bookkeeping — a probe from the Skills
    tab must never mark an agent unhealthy (the `gitignore_clone` pattern).
    Never raises."""
    try:
        async with agent_httpx_client(agent_name, timeout=HOOK_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(f"http://agent-{agent_name}:8000/health")
        if response.status_code != 200:
            return HOOK_UNKNOWN
        data = response.json()
    except Exception:  # noqa: BLE001 — stopped, slow, unreachable or not JSON: no answer
        return HOOK_UNKNOWN
    if not isinstance(data, dict):
        return HOOK_UNKNOWN
    if "skill_gate_hook" not in data:
        return HOOK_PREDATES
    value = data["skill_gate_hook"]
    return value if isinstance(value, str) and value in HOOK_STATES else HOOK_UNKNOWN


def explicit_gate_names(agent_name: str, names: Iterable[str]) -> List[str]:
    """Which of `names` carry an explicit (`set`) gate on the agent."""
    wanted = {(n or "").lower() for n in names}
    return sorted(r["skill_name"] for r in db.list_agent_skill_gates(agent_name)
                  if r["origin"] == ORIGIN_SET and r["skill_name"] in wanted)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_name(skill_name: str) -> str:
    if not isinstance(skill_name, str) or not SKILL_NAME_RE.match(skill_name):
        raise SkillGateMapRefused(
            422, "invalid_skill_name",
            "A skill name is 1 to 64 letters, digits, dots, dashes or underscores, "
            "starting with a letter or digit.")
    return skill_name.lower()


def _validate_changes(changes: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "approver" in changes:
        approver = changes["approver"]
        if approver is None:
            approver = "primary"
        if not isinstance(approver, str) or approver not in APPROVER_KINDS:
            raise SkillGateMapRefused(
                422, "invalid_approver", "The approver is 'primary' or 'approver'.")
        if approver not in approver_kinds():
            raise SkillGateMapRefused(
                422, "approver_unavailable",
                "Only the agent's primary can approve on this install.")
        out["approver"] = approver
    if "deadline_hours" in changes:
        hours = changes["deadline_hours"]
        if hours is not None and not (type(hours) is int
                                      and MIN_DEADLINE_HOURS <= hours <= MAX_DEADLINE_HOURS):
            raise SkillGateMapRefused(
                422, "invalid_deadline",
                f"The deadline is a whole number of hours from {MIN_DEADLINE_HOURS} to "
                f"{MAX_DEADLINE_HOURS}, or empty for the {skill_gate_service.DEFAULT_DEADLINE_HOURS}-hour "
                "default.")
        out["deadline_hours"] = hours
    return out


def _refuse_ghost(agent_name: str) -> None:
    info = db.get_agent_ephemeral_info(agent_name)
    if isinstance(info, dict) and info.get("is_ephemeral"):
        raise SkillGateMapRefused(
            409, "ephemeral_agent", "Skill gates can't be set on an ephemeral agent.")


def _is_ghost(agent_name: str) -> bool:
    try:
        _refuse_ghost(agent_name)
    except SkillGateMapRefused:
        return True
    return False


# ---------------------------------------------------------------------------
# The write discipline: marker ordering under one per-agent lock
# ---------------------------------------------------------------------------

async def _container_running(agent_name: str) -> bool:
    """False only when Docker answered that the container is not running; an
    unreadable state is tried (each exec is bounded) rather than skipped."""
    from services import docker_utils
    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception:  # noqa: BLE001
        state = None
    return state not in ("stopped", "missing")


MARKER_SYNCED, MARKER_FAILED, MARKER_SKIPPED = "synced", "failed", "skipped"


def _has_live_gates(agent_name: str) -> bool:
    return any(r["origin"] != ORIGIN_CLEARED for r in db.list_agent_skill_gates(agent_name))


async def _write_under_marker(agent_name: str, mutate, *, may_add: bool) -> Tuple[Any, str]:
    """Run `mutate` (a sync DB write returning `(result, changed)`) under the
    agent's marker lock, keeping #752's ordering contract. Returns
    `(result, marker)`: `synced` when the marker was re-synced from the map,
    `failed` when that exec did not complete, `skipped` when nothing changed or
    the agent is not running (its next start syncs it).

    Whether this write adds the agent's FIRST gate is decided under the lock,
    from the map as it is then — never from a read taken before it, which a
    clear of the last gate could invalidate in between. Never awaits
    `sync_gate_marker` — the lock is not reentrant."""
    running = await _container_running(agent_name)
    marker = MARKER_SKIPPED
    async with skill_gate_service.marker_lock(agent_name):
        if may_add and running and not await asyncio.to_thread(_has_live_gates, agent_name):
            # Before the insert: a gate that takes effect with no marker is a
            # window in which the hook fails OPEN while the platform is down.
            await skill_gate_service.write_marker(agent_name, True)
        result, changed = await asyncio.to_thread(mutate)
        if changed and running:
            synced = await skill_gate_service.sync_marker_locked(agent_name)
            marker = MARKER_FAILED if synced is None else MARKER_SYNCED
    return result, marker


def _who(ctx: GateContext) -> Tuple[str, Optional[str]]:
    actor = ctx.actor
    if actor is None:
        return "system", None
    scope = getattr(actor, "mcp_scope", None)
    agent = None
    if scope == "agent":
        agent = getattr(actor, "agent_name", None) or None
    elif scope == "system":
        from db.agents import SYSTEM_AGENT_NAME
        agent = SYSTEM_AGENT_NAME
    return getattr(actor, "username", None) or "system", agent


async def _audit(ctx: GateContext, action: str, agent_name: str, details: Dict[str, Any]) -> None:
    """Best-effort: the write has committed; an audit failure never fails it.
    An agent actor is the actor, its owner rides as `actor_email` (R29)."""
    try:
        from services.platform_audit_service import AuditEventType, platform_audit_service
        actor = ctx.actor
        if actor is None:
            who: Dict[str, Any] = {}
        elif getattr(actor, "mcp_scope", None) == "agent" and getattr(actor, "agent_name", None):
            who = {"actor_agent_name": actor.agent_name,
                   "actor_email": getattr(actor, "email", None),
                   "mcp_key_id": getattr(actor, "mcp_key_id", None),
                   "mcp_key_name": getattr(actor, "mcp_key_name", None),
                   "mcp_scope": "agent"}
        else:
            who = {"actor_user": actor}
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action=action,
            source="api" if actor is not None else "system",
            **who,
            actor_ip=ctx.ip,
            endpoint=ctx.endpoint,
            request_id=ctx.request_id,
            target_type="agent",
            target_id=agent_name,
            details={**details, "via": ctx.via, "trigger": ctx.trigger},
        )
    except Exception:  # noqa: BLE001
        logger.warning("[SkillGateMap] audit of %s on %s failed", action, agent_name, exc_info=True)


# ---------------------------------------------------------------------------
# Set / clear (the gate routes and MCP tools)
# ---------------------------------------------------------------------------

async def set_gate(agent_name: str, skill_name: str, *, changes: Mapping[str, Any],
                   ctx: GateContext) -> Dict[str, Any]:
    """Gate `skill_name` on `agent_name`, or change its gate. `changes` holds
    only the fields the caller sent: on an update an omitted field keeps its
    stored value; an explicit null deadline resets to the default. Any
    well-formed name may be gated — one the agent does not have gates nothing
    until a skill answers to it (decision 6)."""
    key = _validate_name(skill_name)
    fields = _validate_changes(changes)
    _refuse_ghost(agent_name)
    set_by, set_by_agent = _who(ctx)

    def mutate():
        # The merge with what is stored happens in the DB write, under the
        # lock: two partial PUTs never drop each other's field.
        previous, current, changed = db.write_skill_gate(
            agent_name, key, changes=fields, origin=ORIGIN_SET,
            set_by=set_by, set_by_agent=set_by_agent)
        return (previous, current, changed), changed

    (previous, current, changed), marker = await _write_under_marker(agent_name, mutate, may_add=True)
    if current is None:   # renamed or deleted after the access check
        raise SkillGateMapRefused(404, "agent_not_found", "Agent not found")
    if changed:
        await _audit(ctx, "skill_gate_set", agent_name, {
            "skill": key, "approver": current["approver"], "deadline_hours": current["deadline_hours"],
            "previous": _public(previous) if previous else None,
        })
    warnings = _warnings(agent_name, current["approver"], marker)
    gate = {**_public(current), "approver_reachable": "approver_unassigned" not in warnings}
    return {"agent_name": agent_name, "skill_name": key, "gate": gate,
            "changed": changed, "warnings": warnings}


def _warnings(agent_name: str, approver: str, marker: Optional[str]) -> List[str]:
    """What the caller should know about a gate that was saved: the marker on a
    running agent could not be written (the hook fails open during an outage
    until the check's heal re-syncs it), or the approver kind reaches nobody."""
    out = []
    if marker == MARKER_FAILED:
        out.append("marker_not_written")
    if not _reachable(agent_name, approver):
        out.append("approver_unassigned")
    return out


async def clear_gate(agent_name: str, skill_name: str, *, ctx: GateContext) -> Dict[str, Any]:
    """Clear the gate on `skill_name`. Idempotent. On a library-assigned skill
    the row becomes a `cleared` tombstone so the library default stays off."""
    key = _validate_name(skill_name)
    set_by, set_by_agent = _who(ctx)
    recommended = await asyncio.to_thread(_recommended)

    def mutate():
        previous, action = db.clear_skill_gate(agent_name, key, set_by=set_by,
                                               set_by_agent=set_by_agent, recommended=recommended)
        return (previous, action), action is not None

    (previous, action), _ = await _write_under_marker(agent_name, mutate, may_add=False)
    if action is not None:
        await _audit(ctx, "skill_gate_cleared", agent_name, {
            "skill": key, "cleared": action, "previous": _public(previous)})
    return {"agent_name": agent_name, "skill_name": key, "changed": action is not None,
            "cleared": action}


# ---------------------------------------------------------------------------
# The library-assignment paths
# ---------------------------------------------------------------------------

def _recommended() -> Optional[set]:
    """Lowercased names the library recommends gating; None when unreadable."""
    from services.skill_service import skill_service
    try:
        return {str(s.get("name")).lower() for s in skill_service.list_skills()
                if s.get("approval") == "recommended" and s.get("name")}
    except Exception:  # noqa: BLE001 — retried by the next reconcile (start, sync, assignment)
        logger.warning("[SkillGateMap] library unreadable; gate defaults deferred", exc_info=True)
        return None


@dataclass(frozen=True)
class AllExcept:
    """`drop_defaults` for a caller that saw EVERY package an unassigned skill
    left behind go — the start path's prune — except the names in `keep`."""

    keep: frozenset = frozenset()


def packages_gone(removal: Optional[Mapping[str, Any]]) -> Set[str]:
    """The names a package removal report (`skill_service.remove_skills`, as a
    route relays it) says have left the agent. A deferred or failed removal
    names none — its files may still be there."""
    if not isinstance(removal, Mapping):
        return set()
    return {str(name).lower() for name, r in (removal.get("results") or {}).items()
            if isinstance(r, Mapping) and r.get("status") in _GONE_STATUSES}


def drop_after_prune(prune: Optional[Mapping[str, Any]]) -> Optional[AllExcept]:
    """What the start path's prune (`reconcile_agent_skills`) lets the reconcile
    drop: every unassigned default when the agent's skills were inventoried —
    except a name whose package the prune could not remove (or that is the
    agent's own, unmanaged copy). Nothing when the prune did not run."""
    if not isinstance(prune, Mapping) or prune.get("status") not in ("clean", "reconciled"):
        return None
    keep = {str(name).lower() for name, r in (prune.get("results") or {}).items()
            if not (isinstance(r, Mapping) and r.get("status") in _GONE_STATUSES)}
    return AllExcept(frozenset(keep))


def _may_drop(drop_defaults) -> Callable[[str], bool]:
    if isinstance(drop_defaults, AllExcept):
        return lambda name: name not in drop_defaults.keep
    allowed = {str(n).lower() for n in (drop_defaults or ())}
    return lambda name: name in allowed


async def reconcile_library_gates(agent_name: str, *, ctx: GateContext, add: bool = True,
                                  drop_defaults=None) -> Optional[Dict[str, Any]]:
    """Bring the agent's library-default gates in line with what it is assigned.

    `add` applies the default to every assigned recommended skill — call it
    BEFORE a package is delivered. `drop_defaults` says which unassigned
    skills' defaults may go: the names whose package removal completed (a
    route), `AllExcept(...)` after the start path's prune, or None (keep them —
    the files may still be on the agent). `cleared` tombstones of unassigned
    skills always go.

    Returns None when nothing changed (and the library was readable), else
    `{applied, removed, unreachable, library_unreadable}`. Never raises: an
    assignment has already committed, and the next reconcile retries.
    """
    try:
        return await _reconcile(agent_name, ctx, add=add, drop_defaults=drop_defaults)
    except Exception:  # noqa: BLE001
        logger.warning("[SkillGateMap] gate reconcile failed for %s", agent_name, exc_info=True)
        return {"applied": [], "removed": [], "unreachable": [], "library_unreadable": False,
                "status": "unavailable"}


async def _reconcile(agent_name: str, ctx: GateContext, *, add: bool, drop_defaults) -> Optional[Dict[str, Any]]:
    # Cheap pre-read: an agent with no assignments and no library rows costs
    # two queries — no library read, no container state, no lock. It only
    # decides whether there is work; the write re-decides under the lock.
    may_drop = _may_drop(drop_defaults)
    assigned = {n.lower() for n in db.get_agent_skill_names(agent_name)}
    rows = {r["skill_name"]: r for r in db.list_agent_skill_gates(agent_name)}
    stale = [n for n, r in rows.items() if n not in assigned and (
        r["origin"] == ORIGIN_CLEARED or (r["origin"] == ORIGIN_LIBRARY_DEFAULT and may_drop(n)))]
    if not (add and assigned) and not stale:
        return None
    recommended: Optional[Set[str]] = set()
    if add and assigned and not _is_ghost(agent_name):
        recommended = await asyncio.to_thread(_recommended)
    missing = sorted((assigned & recommended) - set(rows)) if recommended else []
    if not stale and not missing:
        return None if recommended is not None else _report([], [], [], unreadable=True)

    set_by, _ = _who(ctx)

    def mutate():
        removed, inserted = db.reconcile_library_skill_gates(
            agent_name, recommended, set_by=set_by, may_drop=may_drop)
        return (removed, inserted), bool(removed or inserted)

    (removed, inserted), _ = await _write_under_marker(agent_name, mutate, may_add=bool(missing))
    applied = [r["skill_name"] for r in inserted]
    gone = [r["skill_name"] for r in removed]
    if applied:
        await _audit(ctx, "skill_gate_default_applied", agent_name, {"skills": applied})
    if gone:
        await _audit(ctx, "skill_gate_default_removed", agent_name, {
            "skills": gone, "origins": {r["skill_name"]: r["origin"] for r in removed}})
    unreachable = applied if applied and not _reachable(agent_name, "primary") else []
    if not applied and not gone and recommended is not None:
        return None
    return _report(applied, gone, unreachable, unreadable=recommended is None)


def _report(applied, removed, unreachable, *, unreadable: bool) -> Dict[str, Any]:
    return {"applied": list(applied), "removed": list(removed), "unreachable": list(unreachable),
            "library_unreadable": unreadable}


async def drop_explicit_gates_on_unassign(agent_name: str, names: Iterable[str], *,
                                          ctx: GateContext, keep: Iterable[str] = ()) -> List[str]:
    """A PERSON unassigned `names` and their packages have left the agent: their
    explicit gates go with them (AC 6). Only names still unassigned when the
    write runs (a concurrent re-assign keeps its gate), and never one in `keep`
    — a name whose library row was in `conflict`, where the agent's own skill of
    that name is what runs and its gate is the own skill's (decision 1). The
    caller decides it was a person and that the package is gone."""
    names = [n for n in names if n]
    keep = list(keep)
    # Nothing explicit to drop costs no lock, no container state, no exec.
    if not names or not set(explicit_gate_names(agent_name, names)) - {k.lower() for k in keep}:
        return []

    def mutate():
        removed = db.drop_unassigned_skill_gates(agent_name, names, keep=keep)
        return removed, bool(removed)

    removed, _ = await _write_under_marker(agent_name, mutate, may_add=False)
    gone = [r["skill_name"] for r in removed]
    if gone:
        await _audit(ctx, "skill_gate_removed_with_skill", agent_name, {
            "skills": gone,
            "previous": {r["skill_name"]: {"approver": r["approver"],
                                           "deadline_hours": r["deadline_hours"]} for r in removed},
        })
    return gone
