"""A manifest member's `gates:` — its approval map (trinity-enterprise#1030 CP3).

`gates: {<skill>: {approver, deadline_hours}}` says which of the member's
skills need a person's approval before they run and who approves. Additive,
like every other group: a declared gate the member does not carry as an
explicit row is written, one stored differently is rewritten, and a stored
gate the manifest does not name is reported and NEVER cleared — no call in
this module clears, drops or tombstones a row.

* `read_member` — the snapshot's read (`db.list_agent_skill_gates`, every row,
  tombstones included: a declared gate on a `cleared` tombstone is a write).
* `plan` — pure over what was read, validated through the gate map's own
  public `validate_gate_changes`, so the dry run reports exactly the refusal
  the write would give (`invalid_skill_name`, `invalid_approver`,
  `approver_unavailable`, `unknown_role`, `invalid_deadline`) as blockers.
  ent#881's reachability answer (`approver_unassigned`, `approver_not_shared`)
  rides as the change's `note`, a warning on the write surface too — never a
  silent pass, never a refusal.
* `run` — the executor's writes, each through `skill_gate_map_service.set_gate`
  with `trigger="apply"`, so gate admission (ent#753, ent#848, ent#881), the
  marker ordering contract (#752) and the audit row are the route's own.
  Runs BEFORE the skill phases (ent#753: gated before delivered).
* `deploy` / `deploy_hold_back_warning` — create mode's phase, before skills.
  `deploy_system` is agent-callable through `agents.manage`, so an agent key
  writes gates only if it holds `skills.manage` — the gate routes' own
  capability (T8) — and is told what was held back; a key the capability
  refuses is held back the same way, never a blocker.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from database import db
from db.skill_gates import ORIGIN_CLEARED, ORIGIN_SET
from services import skill_gate_map_service, skill_gate_service
from services.skill_gate_map_service import SkillGateMapRefused

STEP = "gates"
OP_KIND = "gate_set"
GATE_FIELDS = ("approver", "deadline_hours")
APPLY_TRIGGER = "apply"
DEPLOY_TRIGGER = "deploy"
UNDECLARED_NOTE = ("apply never clears a gate; clear it by hand in the agent's Skills tab "
                   "if it is unwanted")
_REACH_NOTES = {
    "unfilled": ("approver_unassigned: nobody fills this approver on the agent right now, so "
                 "a gated run would be refused until someone does"),
    "not_shared": ("approver_not_shared: the people this approver names can't open the "
                   "agent, so they would never see an approval; share the agent with them"),
}


def declared(cfg) -> Dict[str, Dict[str, Any]]:
    """The member's declared gates, as the parser stored them."""
    return dict(cfg.gates or {})


def read_member(name: str) -> Dict[str, dict]:
    """Every stored gate row of a live member by skill key — tombstones
    included, so the plan can tell a cleared row from an absent one. Raises
    on a failed read."""
    return {r["skill_name"]: dict(r) for r in db.list_agent_skill_gates(name)}


def export_member(name: str) -> Dict[str, Dict[str, Any]]:
    """The member's explicit gates as the manifest declares them: `approver`
    always, `deadline_hours` when set. Library defaults and tombstones are
    not target state. Raises on a failed read."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, row in sorted(read_member(name).items()):
        if row["origin"] != ORIGIN_SET:
            continue
        spec: Dict[str, Any] = {"approver": row["approver"]}
        if row["deadline_hours"] is not None:
            spec["deadline_hours"] = row["deadline_hours"]
        out[key] = spec
    return out


def _public(row: dict) -> Dict[str, Any]:
    return {"skill": row["skill_name"], "approver": row["approver"],
            "deadline_hours": row["deadline_hours"]}


def _reach_note(name: str, approver: str) -> Optional[str]:
    why = skill_gate_service.approver_reason(name, approver)
    return _REACH_NOTES.get(why) if why else None


class _Refused(Exception):
    """A service refusal, as `failed[].reason` names it: `<code>: <message>`."""


def plan(mp, *, stored: Optional[Dict[str, dict]], blockers: List,
         apply_change, apply_blocker, make_op) -> None:
    """Changes, ops and blockers for one member. `stored` is None for a member
    apply will create (it carries nothing yet)."""
    cfg = mp.config
    if cfg.gates is None:
        return
    name, short = mp.name, mp.short_name
    rows = stored or {}
    declared_keys = set()
    for skill, spec in declared(cfg).items():
        changes = {k: spec[k] for k in GATE_FIELDS if k in spec}
        try:
            key, fields = skill_gate_map_service.validate_gate_changes(name, skill, changes)
        except SkillGateMapRefused as e:
            blockers.append(apply_blocker(
                e.code, name, e.message,
                f"Fix the gate on '{skill}' in the manifest: the gate map accepts 'primary', "
                f"'approver' where the install resolves it, or a canon role the roster names, "
                f"and a whole number of hours."))
            continue
        declared_keys.add(key)
        row = rows.get(key)
        live = row if row and row["origin"] != ORIGIN_CLEARED else None
        # What `set_gate` stores: an omitted field keeps the explicit row's value.
        approver = fields.get("approver", live["approver"] if live else "primary")
        deadline = fields["deadline_hours"] if "deadline_hours" in fields else (
            live["deadline_hours"] if live else None)
        desired = {"skill": key, "approver": approver, "deadline_hours": deadline}
        if live and live["origin"] == ORIGIN_SET and (
                live["approver"], live["deadline_hours"]) == (approver, deadline):
            continue
        current = ({"approver": live["approver"], "deadline_hours": live["deadline_hours"],
                    "origin": live["origin"]} if live else None)
        note = _reach_note(name, approver) if stored is not None else None
        mp.changes.append(apply_change(
            STEP, "write", "gate_differs" if live else "gate_missing",
            current=current, desired=desired, note=note))
        mp.ops.append(make_op(OP_KIND, name, short, {"skill": key, "changes": fields}))
    undeclared = [_public(r) for k, r in sorted(rows.items())
                  if k not in declared_keys and r["origin"] != ORIGIN_CLEARED]
    if stored is not None and undeclared:
        mp.changes.append(apply_change(STEP, "report", "gate_undeclared",
                                       current=undeclared, note=UNDECLARED_NOTE))


def _context(actor, trigger: str, request) -> skill_gate_map_service.GateContext:
    return skill_gate_map_service.gate_context(
        actor, trigger=trigger,
        ip=request.client.host if request is not None and request.client else None,
        endpoint=request.scope.get("path") if request is not None else None,
        request_id=getattr(request.state, "request_id", None) if request is not None else None)


async def run(op, actor, out, attempt, *, request, apply_change) -> None:
    """One `gate_set` op, through the gate map's own write."""
    name, short, skill = op.member, op.short_name, op.args["skill"]
    result: Dict = {}

    async def write():
        try:
            result.update(await skill_gate_map_service.set_gate(
                name, skill, changes=dict(op.args["changes"]),
                ctx=_context(actor, APPLY_TRIGGER, request)))
        except SkillGateMapRefused as e:
            raise _Refused(f"{e.code}: {e.message}") from e
        return True

    if not await attempt(name, short, STEP, write):
        return
    out.gates_unlanded.discard((name, skill))
    # ent#881: the write surface's warning is a report here — a gate whose
    # approver reaches nobody was saved, and the run says so.
    for warning in result.get("warnings") or []:
        if warning in ("approver_unassigned", "approver_not_shared"):
            out.extra_changes.setdefault(name, []).append(apply_change(
                STEP, "report", warning, current=result["gate"]["approver"], desired=[skill],
                note=_REACH_NOTES["not_shared" if warning == "approver_not_shared" else "unfilled"]))


def _wanted(agent_names: Dict[str, str], agents_config: Dict) -> Dict[str, Dict[str, Dict]]:
    return {agent_names[short]: declared(cfg) for short, cfg in agents_config.items()
            if short in agent_names and cfg.gates}


def skill_key(member: str, skill: str) -> str:
    """The stored gate key of a skill name; the name itself when it is not one."""
    try:
        return skill_gate_map_service.validate_gate_changes(member, skill, {})[0]
    except SkillGateMapRefused:
        return skill


def declared_keys(agent_names: Dict[str, str], agents_config: Dict) -> Set[Tuple[str, str]]:
    """Every `(member, gate key)` a deploy declares. Deploy starts from this set
    and removes what lands, so anything it never reached still counts as not
    written (ent#753: gated before delivered, failing closed)."""
    return {(name, skill_key(name, skill))
            for name, gates in _wanted(agent_names, agents_config).items() for skill in gates}


def unlanded_in(member: str, skills, unlanded: Set[Tuple[str, str]]) -> List[str]:
    """The names in `skills` whose declared gate on `member` did not land."""
    return [s for s in skills if (member, skill_key(member, s)) in unlanded]


def not_written_reason(skills: List[str]) -> str:
    return (f"gate_not_written: the declared gate on {', '.join(skills)} was not written, "
            "so this would run with no approval; fix the gate and re-apply")


def preview_warnings(agent_names: Dict[str, str], agents_config: Dict) -> List[str]:
    """What the deploy dry run says about gate values, through the write's own
    validator. A warning and not a blocker because a real deploy is best-effort
    here: it creates the member and holds the skill back."""
    warnings: List[str] = []
    for name, gates in _wanted(agent_names, agents_config).items():
        for skill, spec in gates.items():
            changes = {k: spec[k] for k in GATE_FIELDS if k in spec}
            try:
                skill_gate_map_service.validate_gate_changes(name, skill, changes)
            except SkillGateMapRefused as e:
                warnings.append(f"Gate on '{skill}' would not be written for '{name}': {e.code} "
                                f"({e.message}). The skill would not be assigned.")
    return warnings


def deploy_hold_back_warning(agent_names: Dict[str, str], agents_config: Dict,
                             current_user) -> Optional[str]:
    """Create mode's gate (T8): a caller the gate routes' capability refuses
    writes no gate and is told which were held back."""
    from db.capability_grants import CAPABILITY_SKILLS_MANAGE
    from dependencies import capability_refusal
    wanted = _wanted(agent_names, agents_config)
    if not wanted:
        return None
    refusal = capability_refusal(current_user, CAPABILITY_SKILLS_MANAGE)
    if refusal is None:
        return None
    listing = "; ".join(f"{n}: {', '.join(g)}" for n, g in wanted.items())
    return (f"Gates not written: this key does not hold {CAPABILITY_SKILLS_MANAGE} "
            f"({refusal[0]}). Held back: {listing}.")


async def deploy(agent_names: Dict[str, str], agents_config: Dict, current_user,
                 request, *, unlanded: Optional[Set[Tuple[str, str]]] = None) -> List[str]:
    """Create mode's `gates:` phase, before skills: best-effort, one warning
    per refusal, never past the gate above. A gate that lands leaves `unlanded`;
    the skill phase holds back whatever is still in it."""
    warning = deploy_hold_back_warning(agent_names, agents_config, current_user)
    if warning is not None:
        return [warning]
    warnings: List[str] = []
    for name, gates in _wanted(agent_names, agents_config).items():
        for skill, spec in gates.items():
            changes = {k: spec[k] for k in GATE_FIELDS if k in spec}
            try:
                result = await skill_gate_map_service.set_gate(
                    name, skill, changes=changes,
                    ctx=_context(current_user, DEPLOY_TRIGGER, request))
            except SkillGateMapRefused as e:
                warnings.append(f"Gate on '{skill}' not written for '{name}': {e.code} ({e.message})")
                continue
            if unlanded is not None:
                unlanded.discard((name, skill_key(name, skill)))
            for w in result.get("warnings") or []:
                if w in ("approver_unassigned", "approver_not_shared"):
                    warnings.append(f"Gate on '{skill}' for '{name}': {w}.")
    return warnings
