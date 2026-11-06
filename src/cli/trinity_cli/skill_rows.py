"""The rows `trinity skills list` prints as a table.

Stdlib only, on purpose: the backend's unit job does not install the CLI's own
dependencies (click, rich), so a rule that lives here can be run by that job
(tests/unit/test_ent736_deprecated_skill_markers.py loads this file by path).
"""

DESCRIPTION_CHARS = 60
SUCCESSOR_CHARS = 40


def _deprecated_cell(skill: dict) -> str:
    """`yes`, `→ <successor>` or blank (trinity-enterprise#736).

    The successor is the library author's text (`superseded_by`, set only on a
    deprecated skill), shown as written and cut to fit the column.
    """
    if skill.get("deprecated") is not True:
        return ""
    successor = skill.get("superseded_by")
    if isinstance(successor, str) and successor:
        return f"→ {successor[:SUCCESSOR_CHARS]}"
    return "yes"


def skill_table_rows(skills: list) -> list:
    """One row per library skill: name, description, category, deprecated."""
    return [
        {
            "name": s.get("name") or "",
            "description": (s.get("description") or "")[:DESCRIPTION_CHARS],
            "category": s.get("category") or "",
            "deprecated": _deprecated_cell(s),
        }
        for s in skills
    ]
