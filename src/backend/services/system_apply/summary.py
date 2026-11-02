"""`SystemApplyResponse.summary` and each member's `unobserved` (trinity-enterprise#1030 CP6).

One count per config group — `skills`, `grants`, `gates`, `roles` — and one
for `members`, each `{write, report, unobserved}`:

* `write` — a change apply makes (or, on a dry run, would make). On a real
  run only a write that LANDED is counted, by its own key (#4130): a skill or
  set held back for its gate, a writer that raised and every write of a
  refused run are not, even beside a write of the same kind that landed;
* `report` — a change apply only reports (an extra, a held-back grant or seat, a
  vacant person seat, a declared-only member);
* `unobserved` — a group the platform could not observe for the member: the
  install has no hook that answers it, so it is neither "in sync" nor a
  blocker. Today only `roles` can be unobserved.

A READ ERROR is not unobserved: it blocks the member with the retryable
`member_reads_unavailable`, and a blocked member is counted in no group —
nothing about it runs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

#: Config group -> the `SystemApplyChange.field` values it covers.
GROUP_FIELDS: Dict[str, tuple] = {
    "skills": ("skills", "skill_sets"),
    "grants": ("grants",),
    "gates": ("gates",),
    "roles": ("seat", "person_seats"),
}
UNOBSERVED_SUFFIX = "_unobserved"


def _group_of(field_name: str):
    for group, fields in GROUP_FIELDS.items():
        if field_name in fields:
            return group
    return None


def member_unobserved(mp) -> List[str]:
    """The config groups this member's plan could not observe, sorted."""
    return sorted({g for c in mp.changes if c.code.endswith(UNOBSERVED_SUFFIX)
                   for g in [_group_of(c.field)] if g})


def write_keys(change) -> List[Any]:
    """The keys a `write` change's ops write — what the executor records in
    `landed`: each skill / set / capability of a list, a gate's skill, a seat's
    role."""
    desired = change.desired
    if isinstance(desired, dict):
        return [desired.get("skill")]
    if isinstance(desired, (list, tuple)):
        return list(desired)
    return [desired]


def build(members: Dict, *, extras: Dict[str, Dict],
          landed: Optional[Set[Tuple[str, str, Any]]] = None,
          extra_changes: Optional[Dict[str, List]] = None) -> Dict[str, Dict]:
    """The summary over every member's plan. `extras` adds group-specific
    keys (`grants.held_back`, `roles.held_back`) beside the three counts.

    `landed` is a real run's `(member, step, key)` set of writes that landed
    (#4130); with it a `write` change counts only when a write of ITS OWN key
    landed for that member (a group field IS the executor's step name), so two
    sets or two gates on one member with one failure count 1; a created or
    updated member counts only when any of its writes landed. None (a dry
    run) counts the plan. `report` and `unobserved` are plan counts always.

    `extra_changes` is a real run's own changes per member: a `write` among
    them counts by the same landed-key rule — today only the unassign a failed
    gate leads to (trinity-enterprise#1068), a write the plan could not list."""
    landed_members = None if landed is None else {entry[0] for entry in landed}
    out = {g: {"write": 0, "report": 0, "unobserved": 0} for g in GROUP_FIELDS}
    out["members"] = {"write": 0, "report": 0, "unobserved": 0}
    for mp in members.values():
        if mp.verdict == "blocked":
            continue
        if mp.verdict in ("create", "update"):
            if landed_members is None or mp.name in landed_members:
                out["members"]["write"] += 1
        elif mp.verdict == "declared_only":
            out["members"]["report"] += 1
        if member_unobserved(mp):
            out["members"]["unobserved"] += 1
        for c in mp.changes:
            group = _group_of(c.field)
            if group is None:
                continue
            if c.action == "write":
                if landed is None or any((mp.name, c.field, k) in landed
                                         for k in write_keys(c)):
                    out[group]["write"] += 1
            elif c.code.endswith(UNOBSERVED_SUFFIX):
                out[group]["unobserved"] += 1
            else:
                out[group]["report"] += 1
    if landed is not None:
        for member, changes in (extra_changes or {}).items():
            for c in changes:
                group = _group_of(c.field)
                if (group is not None and c.action == "write"
                        and any((member, c.field, k) in landed for k in write_keys(c))):
                    out[group]["write"] += 1
    for group, keys in extras.items():
        out[group].update(keys)
    return out
