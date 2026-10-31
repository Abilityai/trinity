"""A manifest member's `skills:` and `skill_sets:` (trinity-enterprise#1030 CP1).

Additive, like tags: a declared skill or set the member does not hold is
assigned; one it holds that the manifest does not name is reported and left.
Nothing here unassigns.

* `read_library` / `read_member` — the snapshot's reads. A failed read is a
  retryable blocker, never an empty value (P-B).
* `plan` — pure over what was read: changes, ops and blockers.
* `run` — the executor's writes, through `skill_assignment_service`, so a
  manifest assigns exactly as the assign routes do: row, gates (ent#753),
  delivery (#2703), broadcast.

Governance is the caller's: apply writes only on members whose owner gate it
already passed (`can_user_share_agent`, the skills fence's owner arm for a
person); deploy checks the skills.manage capability for an agent key (T8).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from database import db
from services import skill_assignment_service, skill_set_service
from services.skill_packaging import validate_skill_name
from services.skill_service import skill_service

SKILLS_STEP = "skills"
SKILL_SETS_STEP = "skill_sets"

# The executor's op kinds, in the order they run (after the gate-bearing
# fields, before the created members start).
OP_KINDS = {"skill_assign": SKILLS_STEP, "skill_set_assign": SKILL_SETS_STEP}


def declares(cfg) -> bool:
    """True when the member declares either field — an empty list included:
    `skills: []` asks for the extras report."""
    return cfg.skills is not None or cfg.skill_sets is not None


def declared(cfg) -> Tuple[List[str], List[str]]:
    """The member's declared skills and sets, first occurrence wins."""
    return (list(dict.fromkeys(cfg.skills or [])),
            list(dict.fromkeys(cfg.skill_sets or [])))


def library_verdict_skill(name: str) -> Optional[Tuple[str, str]]:
    """None when `name` can be assigned; else `(code, detail)` — the assign
    route's own refusals (the ent#183 name guard, then its 404)."""
    if not validate_skill_name(name):
        return "skill_name_invalid", f"'{name}' is not a valid skill name."
    if not skill_service.get_skill(name):
        return "skill_unknown", f"Skill '{name}' not found in library."
    return None


def library_verdict_set(name: str) -> Optional[Tuple[str, str]]:
    """None when set `name` can be assigned; else `(code, detail)` — the set
    route's refusals (`invalid_set_name`, then `require_set`'s SetError)."""
    if not validate_skill_name(name):
        return "skill_set_unknown", f"'{name}' is not a valid skill set name."
    try:
        skill_set_service.require_set(name)
    except skill_set_service.SetError as e:
        return "skill_set_unknown", f"{e.detail.get('message', name)} ({e.detail.get('code')})"
    return None


def read_library(manifest) -> Tuple[Dict[str, Optional[Tuple[str, str]]],
                                     Dict[str, Optional[Tuple[str, str]]]]:
    """One verdict per declared skill and set name across the manifest.
    Raises on an unreadable library; the snapshot turns that into a blocker."""
    skills: Dict[str, Optional[Tuple[str, str]]] = {}
    sets: Dict[str, Optional[Tuple[str, str]]] = {}
    for cfg in manifest.agents.values():
        names, set_names = declared(cfg)
        for n in names:
            if n not in skills:
                skills[n] = library_verdict_skill(n)
        for n in set_names:
            if n not in sets:
                sets[n] = library_verdict_set(n)
    return skills, sets


def read_member(name: str) -> Tuple[List[str], List[str]]:
    """(individually assigned skills, held sets) of a live member. Raises on
    a failed read. Set-derived rows are not `skills:` — the set brought them."""
    individual = sorted(r.skill_name for r in db.get_agent_skills(name)
                        if getattr(r, "individual", True))
    return individual, sorted(skill_set_service.held_sets(name))


def plan(mp, *, held_skills: Optional[List[str]], held_sets: Optional[List[str]],
         skill_verdicts: Dict, set_verdicts: Dict, blockers: List,
         apply_change, apply_blocker, make_op) -> None:
    """Changes, ops and blockers for one member. `held_*` is None for a member
    apply will create (it holds nothing yet)."""
    cfg = mp.config
    if cfg.skills is None and cfg.skill_sets is None:
        return
    names, set_names = declared(cfg)
    name, short = mp.name, mp.short_name
    ok_names: List[str] = []
    for n in names:
        verdict = skill_verdicts.get(n)
        if verdict is not None:
            blockers.append(apply_blocker(verdict[0], name, verdict[1],
                                          "Fix the skill name in the manifest, or add the "
                                          "skill to the library."))
        else:
            ok_names.append(n)
    ok_sets: List[str] = []
    for n in set_names:
        verdict = set_verdicts.get(n)
        if verdict is not None:
            blockers.append(apply_blocker(verdict[0], name, verdict[1],
                                          "Fix the set name in the manifest, or fix the set "
                                          "in its source's catalog.yaml."))
        else:
            ok_sets.append(n)

    have = set(held_skills or [])
    missing = [n for n in ok_names if n not in have]
    if missing:
        mp.changes.append(apply_change(SKILLS_STEP, "write", "skill_missing",
                                       current=sorted(have), desired=missing))
        mp.ops.append(make_op("skill_assign", name, short, {"skills": missing}))
    extra = sorted(have - set(names))
    if held_skills is not None and cfg.skills is not None and extra:
        mp.changes.append(apply_change(
            SKILLS_STEP, "report", "skill_extra", current=extra,
            note="apply never unassigns a skill; unassign it by hand if it is unwanted"))

    have_sets = set(held_sets or [])
    for n in (s for s in ok_sets if s not in have_sets):
        mp.changes.append(apply_change(SKILL_SETS_STEP, "write", "skill_set_missing",
                                       current=sorted(have_sets), desired=[n]))
        mp.ops.append(make_op("skill_set_assign", name, short, {"set": n}))
    extra_sets = sorted(have_sets - set(set_names))
    if held_sets is not None and cfg.skill_sets is not None and extra_sets:
        mp.changes.append(apply_change(
            SKILL_SETS_STEP, "report", "skill_set_extra", current=extra_sets,
            note="apply never unassigns a skill set; unassign it by hand if it is unwanted"))


def set_members(set_name: str) -> Optional[List[str]]:
    """The skills a set would bring; None when the library cannot say."""
    try:
        return skill_set_service.resolved_members([set_name]).get(set_name)
    except Exception:  # noqa: BLE001 — unknown, and the caller fails closed on it
        return None


def set_held_for_gates(member: str, set_name: str, unlanded) -> List[str]:
    """Why a set must not be assigned to `member`: the skills it carries whose
    declared gate did not land. A set the library cannot resolve is held while
    any of the member's gates is outstanding, since it could carry one."""
    from services.system_apply import gates as apply_gates
    outstanding = sorted(skill for who, skill in unlanded if who == member)
    if not outstanding:
        return []
    members = set_members(set_name)
    return outstanding if members is None else apply_gates.unlanded_in(member, members, unlanded)


def delivery_change(apply_change, field_name: str, names: List[str], delivery):
    """The delivery outcome of one write, as a report change: the row is the
    write; delivery is best-effort and named (#2703), never a failure."""
    status = (delivery or {}).get("status") or "not_delivered"
    note = None
    if status == "pending_start":
        note = "the agent is not running; the skills are delivered at its next start"
    elif status not in ("injected", "delivered", "unchanged"):
        note = (f"assigned, not delivered ({(delivery or {}).get('reason') or status}); "
                "the next start or a skills sync delivers them")
    return apply_change(field_name, "report", "skill_delivery", current=status,
                        desired=names, note=note)


async def run(op, actor, out, attempt, *, apply_change, acting_agent: Optional[str]) -> None:
    """One `skill_assign` / `skill_set_assign` op."""
    from services.system_apply import gates as apply_gates
    name, short = op.member, op.short_name
    result: Dict = {}

    async def hold(step: str, skills: List[str]) -> None:
        def refuse():
            raise RuntimeError(apply_gates.not_written_reason(skills))
        await attempt(name, short, step, refuse)

    if op.kind == "skill_assign":
        # ent#753: a skill whose declared gate did not land is not delivered.
        held = apply_gates.unlanded_in(name, op.args["skills"], out.gates_unlanded)
        names = [n for n in op.args["skills"] if n not in held]
        if held:
            await hold(SKILLS_STEP, held)
        if not names:
            return

        async def write():
            result.update(await skill_assignment_service.assign_and_deliver(
                name, names, actor, acting_agent=acting_agent))
            return True
        if await attempt(name, short, SKILLS_STEP, write):
            out.extra_changes.setdefault(name, []).append(delivery_change(
                apply_change, SKILLS_STEP, names, result.get("delivery")))
    elif op.kind == "skill_set_assign":
        held = set_held_for_gates(name, op.args["set"], out.gates_unlanded)
        if held:
            await hold(SKILL_SETS_STEP, held)
            return

        async def write():
            result.update(await skill_assignment_service.assign_set_and_deliver(
                name, op.args["set"], actor, acting_agent=acting_agent))
            return True
        if await attempt(name, short, SKILL_SETS_STEP, write) and result.get("added"):
            out.extra_changes.setdefault(name, []).append(delivery_change(
                apply_change, SKILL_SETS_STEP, list(result["added"]), result.get("delivery")))
