"""A manifest member's `grants:` — admin capabilities (trinity-enterprise#1030 CP2).

The GRANT half of grant-vs-use, so this group is gated on the CALLER, not on
the member: a declared capability the member does not hold is written only
when the caller passes the grant route's own gate (an admin in a signed-in
session, `dependencies.assert_interactive_admin`). Every other caller gets a
report, a top-level summary entry and one warning line — never a blocker, so
the rest of the plan still applies. A held capability the manifest does not
name is reported and never revoked: no call in this module passes
`granted=False`.

* `read_member` — the snapshot's read (`db.list_agent_capabilities`).
* `plan` — pure over what was read and whether the caller may write.
* `summarize` — the response's `summary.grants` and its one warning line.
* `run` — the executor's writes through `capability_grant_service.set_grant`,
  audited with the routes' own verbs through the shared helper.
* `deploy` / `deploy_hold_back_warning` — create mode's phase and its preview.

The service's refusals (`system_agent_not_grantable`, `ephemeral_agent_not_grantable`,
`calibrating_agent`) land in `failed[]` with their code — the first two only as a
race, since apply's member gates already refuse a system agent and a ghost before
any write; an unknown capability is a blocker, because nothing could ever write it.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from database import db
from db.capability_grants import CAPABILITIES
from services import capability_grant_service
from services.capability_grant_service import CapabilityGrantRefused

STEP = "grants"
OP_KIND = "grant"
HELD_BACK_CODE = "grant_requires_interactive_admin"
DEPLOY_HELD_BACK_CODE = "grants_require_interactive_admin"
HELD_BACK_NOTE = ("granting a capability requires an admin in a signed-in session, the "
                  "grant route's own gate; nothing was written. Run apply from such a "
                  "session, or grant it in the agent's Settings")


def declared(cfg) -> List[str]:
    """The member's declared capabilities, first occurrence wins."""
    return list(dict.fromkeys(cfg.grants or []))


def read_member(name: str) -> List[str]:
    """The capabilities a live member holds. Raises on a failed read."""
    return sorted(g["capability"] for g in db.list_agent_capabilities(name))


class _Refused(Exception):
    """A service refusal, as `failed[].reason` names it: `<code>: <message>`."""


def plan(mp, *, held: Optional[List[str]], grant_writer: bool, blockers: List,
         apply_change, apply_blocker, make_op) -> None:
    """Changes, ops and blockers for one member. `held` is None for a member
    apply will create (it holds nothing yet)."""
    cfg = mp.config
    if cfg.grants is None:
        return
    name, short = mp.name, mp.short_name
    wanted = declared(cfg)
    unknown = [c for c in wanted if c not in CAPABILITIES]
    if unknown:
        blockers.append(apply_blocker(
            "grant_unknown", name,
            f"'{', '.join(unknown)}' is not a capability.",
            f"Use one of: {', '.join(sorted(CAPABILITIES))}."))
        return
    have = set(held or [])
    missing = [c for c in wanted if c not in have]
    if missing and grant_writer:
        mp.changes.append(apply_change(STEP, "write", "grant_missing",
                                       current=sorted(have), desired=missing))
        for cap in missing:
            mp.ops.append(make_op(OP_KIND, name, short, {"capability": cap}))
    elif missing:
        mp.changes.append(apply_change(STEP, "report", HELD_BACK_CODE,
                                       current=sorted(have), desired=missing,
                                       note=HELD_BACK_NOTE))
    extra = sorted(have - set(wanted))
    if held is not None and extra:
        mp.changes.append(apply_change(
            STEP, "report", "grant_extra", current=extra,
            note="apply never revokes a capability; revoke it by hand if it is unwanted"))


def summarize(members: Dict) -> Tuple[Dict, Optional[str]]:
    """`summary.grants` over every member's plan, and the one warning line
    when anything was held back. Blocked members are not counted: nothing
    about them runs."""
    writes = reports = 0
    held_back: List[Dict] = []
    for mp in members.values():
        if mp.verdict == "blocked":
            continue
        for c in mp.changes:
            if c.field != STEP:
                continue
            if c.action == "write":
                writes += 1
            else:
                reports += 1
            if c.code == HELD_BACK_CODE:
                held_back.append({"name": mp.name, "grants": list(c.desired or [])})
    summary = {"write": writes, "report": reports, "held_back": held_back}
    if not held_back:
        return summary, None
    count = sum(len(h["grants"]) for h in held_back)
    listing = "; ".join(f"{h['name']}: {', '.join(h['grants'])}" for h in held_back)
    return summary, (
        f"{count} grant{'s' if count != 1 else ''} not written ({HELD_BACK_CODE}): granting "
        f"a capability requires an admin in a signed-in session, the grant route's own gate. "
        f"Held back: {listing}. Everything else in the manifest still applies.")


async def run(op, actor, out, attempt, *, request) -> None:
    """One `grant` op: the write, then the audit row the routes also write."""
    name, short, cap = op.member, op.short_name, op.args["capability"]
    result: Dict = {}

    def write():
        try:
            result.update(capability_grant_service.set_grant(name, cap, True, actor.username))
        except CapabilityGrantRefused as e:
            raise _Refused(f"{e.code}: {e.message}") from e
        return True

    if await attempt(name, short, STEP, write) and result.get("changed"):
        await capability_grant_service.audit_grant_change(
            name, cap, True, actor, request=request)


def _wanted(agent_names: Dict[str, str], agents_config: Dict) -> Dict[str, List[str]]:
    return {agent_names[short]: declared(cfg) for short, cfg in agents_config.items()
            if short in agent_names and cfg.grants}


def deploy_hold_back_warning(agent_names: Dict[str, str], agents_config: Dict,
                             current_user) -> Optional[str]:
    """Create mode's gate (T8, reversed): `deploy_system` is agent-callable
    through `agents.manage`, so a manifest deployed under an agent key — or any
    caller the grant route would refuse — writes no grant and is told."""
    from dependencies import is_interactive_admin
    wanted = _wanted(agent_names, agents_config)
    if not wanted or is_interactive_admin(current_user):
        return None
    listing = "; ".join(f"{n}: {', '.join(caps)}" for n, caps in wanted.items())
    return (f"Grants not written ({DEPLOY_HELD_BACK_CODE}): granting a capability requires "
            f"an admin in a signed-in session, the grant route's own gate. Held back: {listing}.")


async def deploy(agent_names: Dict[str, str], agents_config: Dict, current_user,
                 request) -> List[str]:
    """Create mode's `grants:` phase: best-effort, one warning per refusal,
    never past the gate above."""
    warning = deploy_hold_back_warning(agent_names, agents_config, current_user)
    if warning is not None:
        return [warning]
    warnings: List[str] = []
    for name, caps in _wanted(agent_names, agents_config).items():
        for cap in caps:
            try:
                result = capability_grant_service.set_grant(name, cap, True, current_user.username)
            except CapabilityGrantRefused as e:
                warnings.append(f"Grant '{cap}' not written for '{name}': {e.code} ({e.message})")
                continue
            if result.get("changed"):
                await capability_grant_service.audit_grant_change(
                    name, cap, True, current_user, request=request)
    return warnings
