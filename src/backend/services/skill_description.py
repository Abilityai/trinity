"""The one blank rule for a capability description (trinity-enterprise#584).

A capability is described once, in its skill's ``SKILL.md`` frontmatter
``description:`` (requirements/skills.md §23). Every surface agrees on whether a
skill HAS a description only if every place a description enters the backend
applies the same rule, so this leaf is called at those chokepoints and nowhere
else needs one:

* ``connector_service.resolve_exposed_playbooks`` — the MCP connector and the
  Workspace briefing;
* ``agent_skills_listing`` — the agent's live listing and its kept copy (Skills
  tab, chat ``/`` menu, public link);
* ``skill_packaging.extract_contract`` — the skills library.

The text rule mirrors the agent server's ``_coerce_optional_str``
(``docker/base-image/agent_server/routers/skills.py``): a string passes, a YAML
1.1 scalar (bool, number, date) becomes its string form, a collection cannot be
text. On top of that, blank means absent: an empty or whitespace-only value is
``None``. Agent images that still serve blanks are cleaned on the way through,
so no base-image rebuild is needed.
"""
from typing import Any, Optional


def clean_description(value: Any) -> Optional[str]:
    """``value`` as display text, or ``None`` when there is no description."""
    if value is None or isinstance(value, (list, dict, set, tuple)):
        return None
    text = (value if isinstance(value, str) else str(value)).strip()
    return text or None
