"""Skill sets — pure rules (trinity-enterprise#530).

A library source's ``catalog.yaml`` may declare named sets of its own skills:

```yaml
sets:
  project-management: [project-init, project-task]        # short form
  dev-backlog:                                              # long form
    skills: [backlog, groom]
    requires: {env: [GITHUB_TOKEN]}                         # prerequisite keys (#342)
    schedules: [{name: Weekly groom, cron: "0 9 * * 1", message: /groom}]   # SUGGESTED, never created
```

Assigning ``set:<name>`` to an agent materialises one ``agent_skills`` row per
member (``individual = 0`` unless the skill was also assigned on its own). This
module holds the two decisions that must not depend on I/O:

* ``parse_catalog_sets`` — total, never raises; every problem is a CODE, never
  author text, and a set with missing members stays listed as ``partial``.
* ``plan_member_rows`` — what the materialised rows should become. It FAILS
  CLOSED: while any assigned set cannot be resolved (source disabled or deleted,
  catalog unreadable, clone missing) no set-derived row is removed — a catalog
  hiccup must never strip a fleet's skills.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

#: The skill-name rule (`skill_packaging.SKILL_NAME_RE`), reused for set names
#: and members so a set can never name something no skill could be.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: The prefix a set carries wherever a skill name is accepted (`set:<name>`).
#: `:` can never occur in a skill name, so the two namespaces cannot collide.
SET_PREFIX = "set:"

MAX_SETS = 50
MAX_MEMBERS = 100
MAX_SCHEDULES = 10
MAX_ENV = 20

# Problem codes — the library status and the API carry these, never raw text.
MEMBER_MISSING = "member_missing"
INVALID_MEMBER_NAME = "invalid_member_name"
NAME_COLLIDES_WITH_SKILL = "name_collides_with_skill"
INVALID_SET = "invalid_set"
INVALID_SCHEDULE = "invalid_schedule"
INVALID_ENV = "invalid_env"
TOO_MANY = "too_many"
#: Problems that make the member list untrustworthy — such a set never resolves.
INVALIDATING = {INVALID_SET, INVALID_MEMBER_NAME, TOO_MANY}


@dataclass
class SetDef:
    name: str
    members: List[str]                       # valid member names, catalog order
    missing: List[str] = field(default_factory=list)     # valid names absent from the source
    requires_env: List[str] = field(default_factory=list)
    schedules: List[dict] = field(default_factory=list)  # {name, cron, message} — suggestions only
    problems: List[str] = field(default_factory=list)    # codes

    @property
    def status(self) -> str:
        """``invalid`` (the definition itself is broken — a typo'd key, a bad or
        dropped member name, too many members, or no members at all), ``partial``
        (a member its source does not ship), else ``ok``. Only ``ok`` resolves:
        a set in any other state must never drive a prune (review finding 1)."""
        if not self.members or set(self.problems) & INVALIDATING:
            return "invalid"
        return "partial" if self.missing else "ok"


def split_set_ref(name: str) -> Optional[str]:
    """`set:dev-backlog` → `dev-backlog`; anything else → None."""
    if isinstance(name, str) and name.startswith(SET_PREFIX):
        rest = name[len(SET_PREFIX):]
        return rest if NAME_RE.match(rest) else None
    return None


def _schedules(raw) -> Tuple[List[dict], bool]:
    if raw is None:
        return [], False
    if not isinstance(raw, list):
        return [], True
    out, bad = [], False
    for item in raw[:MAX_SCHEDULES]:
        if not isinstance(item, dict):
            bad = True
            continue
        name, cron, message = item.get("name"), item.get("cron"), item.get("message")
        if not (isinstance(name, str) and 0 < len(name.strip()) <= 120
                and isinstance(cron, str) and len(cron.split()) == 5 and len(cron) <= 100
                and isinstance(message, str) and 0 < len(message) <= 2000):
            bad = True
            continue
        out.append({"name": name.strip(), "cron": cron.strip(), "message": message})
    return out, bad or len(raw) > MAX_SCHEDULES


def parse_catalog_sets(raw, skill_names: Set[str]) -> List[SetDef]:
    """The catalog's ``sets:`` → validated set definitions. Never raises.

    ``skill_names`` is the SAME source's skills at its current commit: a set
    names its own source's skills, and a member that source does not ship is
    reported (the set stays listed as ``partial``) rather than silently resolved
    from another source.
    """
    if not isinstance(raw, dict):
        return []
    out: List[SetDef] = []
    for set_name, body in list(raw.items())[:MAX_SETS]:
        if not isinstance(set_name, str) or not NAME_RE.match(set_name):
            continue   # an unaddressable set cannot be assigned or reported by name
        problems: List[str] = []
        if isinstance(body, list):
            members_raw, env_raw, sched_raw = body, None, None
        elif isinstance(body, dict):
            members_raw = body.get("skills")
            requires = body.get("requires")
            env_raw = requires.get("env") if isinstance(requires, dict) else None
            sched_raw = body.get("schedules")
        else:
            out.append(SetDef(set_name, [], problems=[INVALID_SET]))
            continue
        if not isinstance(members_raw, list):
            out.append(SetDef(set_name, [], problems=[INVALID_SET]))
            continue
        if len(members_raw) > MAX_MEMBERS:
            problems.append(TOO_MANY)
        members: List[str] = []
        for m in members_raw[:MAX_MEMBERS]:
            if not isinstance(m, str) or not NAME_RE.match(m):
                if INVALID_MEMBER_NAME not in problems:
                    problems.append(INVALID_MEMBER_NAME)
                continue
            if m not in members:
                members.append(m)
        missing = [m for m in members if m not in skill_names]
        if missing:
            problems.append(MEMBER_MISSING)
        if set_name in skill_names:
            problems.append(NAME_COLLIDES_WITH_SKILL)
        env: List[str] = []
        if env_raw is not None:
            items = env_raw if isinstance(env_raw, list) else []
            for k in items[:MAX_ENV]:
                if isinstance(k, str) and ENV_RE.match(k):
                    if k not in env:
                        env.append(k)
                elif INVALID_ENV not in problems:
                    problems.append(INVALID_ENV)
            if not isinstance(env_raw, list) and INVALID_ENV not in problems:
                problems.append(INVALID_ENV)
        schedules, bad_sched = _schedules(sched_raw)
        if bad_sched:
            problems.append(INVALID_SCHEDULE)
        out.append(SetDef(set_name, members, missing, env, schedules, problems))
    return out


def plan_member_rows(
    rows: Dict[str, bool],
    assigned_sets: Iterable[str],
    resolved: Dict[str, Optional[List[str]]],
) -> Tuple[Set[str], Set[str]]:
    """What the materialised ``agent_skills`` rows should become.

    ``rows``: skill → individual? · ``assigned_sets``: the agent's set names ·
    ``resolved``: set → present members, or None when the set could not be
    resolved right now. Returns ``(add, remove)``: ``add`` are set members with
    no row yet (inserted as individual=0); ``remove`` are set-derived rows
    (individual=0) no assigned set names any more. Fails closed: while any
    assigned set is unresolved, ``remove`` is empty.
    """
    assigned = list(assigned_sets)
    expected: Set[str] = set()
    any_unresolved = False
    for s in assigned:
        members = resolved.get(s)
        if members is None:
            any_unresolved = True
            continue
        expected.update(members)
    add = {m for m in expected if m not in rows}
    if any_unresolved:
        return add, set()
    remove = {name for name, individual in rows.items() if not individual and name not in expected}
    return add, remove
