"""`SystemApplyResponse.summary` and each member's `unobserved` (trinity-enterprise#1030 CP6).

One count per config group — `skills`, `grants`, `gates`, `roles` — and one
for `members`, each `{write, report, unobserved}`:

* `write` — a change apply makes (or, on a dry run, would make);
* `report` — a change apply only reports (an extra, a held-back grant, a
  vacant person seat, a declared-only member);
* `unobserved` — a group the platform could not observe for the member: the
  install has no hook that answers it, so it is neither "in sync" nor a
  blocker. Today only `roles` can be unobserved.

A READ ERROR is not unobserved: it blocks the member with the retryable
`member_reads_unavailable`, and a blocked member is counted in no group —
nothing about it runs.
"""
from __future__ import annotations

from typing import Dict, List

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


def build(members: Dict, *, extras: Dict[str, Dict]) -> Dict[str, Dict]:
    """The summary over every member's plan. `extras` adds group-specific
    keys (`grants.held_back`) beside the three counts."""
    out = {g: {"write": 0, "report": 0, "unobserved": 0} for g in GROUP_FIELDS}
    out["members"] = {"write": 0, "report": 0, "unobserved": 0}
    for mp in members.values():
        if mp.verdict == "blocked":
            continue
        if mp.verdict in ("create", "update"):
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
                out[group]["write"] += 1
            elif c.code.endswith(UNOBSERVED_SUFFIX):
                out[group]["unobserved"] += 1
            else:
                out[group]["report"] += 1
    for group, keys in extras.items():
        out[group].update(keys)
    return out
