"""`library_skills:` block — tolerant reader for template.yaml (#4387).

Names skills from the platform skills library that an agent created from this
template should hold. Creation assigns them as the owner and delivers them once
the agent is ready (`agent_service.crud._materialize_agent_files`); after that
they are ordinary assignments, managed on the agent's Skills tab. Requirements:
docs/memory/requirements/skills.md §21.3.4.

```yaml
library_skills:
  - plan-interview-research
  - user-research
```

A `template.yaml` is untrusted input (`github:` ones come from arbitrary repos,
`local:` ones can be uploaded), so both functions here are **total**: any input
shape degrades to an empty list plus named errors, and nothing raises. Two
callers rely on that, as with the `schedules:` and `plugins:` readers: the
catalog builders, where one raise empties the template list (#1835), and
creation, where a raise enters the destructive rollback fence.

Names are checked against the library's own skill-name charset
(`skill_packaging.SKILL_NAME_RE`), so one rule decides what a skill name is.
Skill sets (`set:<name>`) are refused with a named error rather than silently
dropped: they carry their own resolution and fail-closed rules (§21.3.2) and are
not part of this declaration yet.

A leaf module: `skill_packaging` (yaml + `utils.safe_yaml`) only.
`template_service` imports this, so nothing here may import it back.
"""

from typing import Any, List, Tuple

from services.skill_packaging import SKILL_NAME_RE

# A template naming hundreds of skills would turn one creation into hundreds of
# assignments and package injections. One bound, shared by catalog and create.
MAX_LIBRARY_SKILLS = 50

_ECHO_LIMIT = 80
_SET_PREFIX = "set:"


def _safe_echo(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _ECHO_LIMIT else text[:_ECHO_LIMIT] + "…"


def _parse(block: Any) -> Tuple[List[str], List[str]]:
    if block is None or block == [] or block == "" or block == {}:
        return [], []
    if not isinstance(block, list):
        return [], [
            f"library_skills must be a list of skill names, got {type(block).__name__}"
        ]

    names: List[str] = []
    errors: List[str] = []
    seen = set()
    for i, entry in enumerate(block):
        if not isinstance(entry, str):
            errors.append(f"library_skills[{i}]: expected a skill name, got {_safe_echo(entry)}")
            continue
        name = entry.strip()
        if name.startswith(_SET_PREFIX):
            errors.append(
                f"library_skills[{i}]: skill sets ({_safe_echo(name)}) are not supported "
                f"here; list the skills by name"
            )
            continue
        if not SKILL_NAME_RE.match(name):
            errors.append(f"library_skills[{i}]: invalid skill name {_safe_echo(name)}")
            continue
        if name in seen:
            continue
        if len(names) >= MAX_LIBRARY_SKILLS:
            errors.append(
                f"library_skills: at most {MAX_LIBRARY_SKILLS} skills; the rest are ignored"
            )
            break
        seen.add(name)
        names.append(name)
    return names, errors


def normalize_declared_library_skills(block: Any) -> List[str]:
    """Valid, de-duplicated skill names in declared order. Never raises."""
    return _parse(block)[0]


def library_skill_errors(block: Any) -> List[str]:
    """Named problems with the block; empty when absent or well-formed. Never raises."""
    return _parse(block)[1]
