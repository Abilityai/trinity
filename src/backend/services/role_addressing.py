"""Role addressing — an agent names a role, the platform resolves the person
(abilityai/trinity-enterprise#606).

Every outbound object an agent produces for a human — an ask, a report, a
message — carries one field, ``to``, whose value is a **role**, never a person:
``primary | approver | viewer | operator``. This module is the ONE resolution
rule behind all three, so an ask and a report addressed ``to: approver`` cannot
reach different people.

Resolution order:

1. A registered assignment provider answers first
   (``assignment_provider.resolve_role_people``). A non-empty answer is the
   people who fill the role. An empty answer is real: nobody fills it.
2. No provider answer ⇒ the core defaults: ``primary`` → the agent's owner;
   ``operator`` → the operators (no person recorded); ``approver`` / ``viewer``
   → refused until someone fills them.
3. A provider that answers "nobody" for ``primary`` falls back to the operators
   (the ruling: an ask to primary goes to operator when no primary is
   assigned). The owner stands in only when no provider answers at all.

An unknown role, or a role nobody fills, is refused LOUDLY (``RoleRefused``) —
never dropped, never defaulted to "whoever".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from database import db

logger = logging.getLogger(__name__)

ROLES = ("primary", "approver", "viewer", "operator")


class RoleRefused(Exception):
    """The role cannot address anyone. ``code`` is the named reason:
    ``invalid_to`` (not a role) or ``role_unassigned`` (nobody fills it)."""

    def __init__(self, code: str, role: str, message: str):
        super().__init__(code)
        self.code = code
        self.role = role
        self.message = message


@dataclass
class Resolution:
    """Who a role reaches.

    ``people`` — every resolved email (lower-case, de-duplicated, in order).
    ``single`` — the one addressee when exactly one person resolved, else None.
    A caller decides what several people mean: a report or a message has one
    reader and refuses (``role_resolves_to_several``); an ask is delivered to
    every one of them (trinity-enterprise#816).
    ``resolved`` — False when the role fell back to the operators (no primary
    assigned, or an owner without an email); True for ``operator`` itself.
    """

    role: str
    people: List[str] = field(default_factory=list)
    single: Optional[str] = None
    resolved: bool = True


def owner_email(agent_name: str) -> Optional[str]:
    """The agent owner's email, or None when there is none (the default admin
    often has none). Unreadable ⇒ None: an operator address, never a guess."""
    try:
        owner = db.get_agent_owner(agent_name)
        username = (owner or {}).get("owner_username")
        user = db.get_user_by_username(username) if username else None
        email = ((user or {}).get("email") or "").strip().lower()
    except Exception:  # noqa: BLE001
        logger.warning("[role_addressing] owner lookup failed for %s", agent_name, exc_info=True)
        return None
    return email if "@" in email else None


def resolve(
    agent_name: str,
    role: str,
    *,
    owner_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Resolution:
    """Resolve ``role`` for ``agent_name``. Raises ``RoleRefused``.

    ``owner_lookup`` defaults to :func:`owner_email`; a caller may pass its own
    (the ask sink passes its module-level lookup, so its tests' patches hold).
    """
    from services import assignment_provider

    if role not in ROLES:
        raise RoleRefused("invalid_to", str(role), "Unknown role.")
    people = assignment_provider.resolve_role_people(agent_name, role)
    if people:
        return Resolution(role, people, people[0] if len(people) == 1 else None, True)
    if role == "primary":
        if people is not None:   # the provider answered: nobody fills primary
            return Resolution(role, [], None, False)
        owner = (owner_lookup or owner_email)(agent_name)
        return Resolution(role, [owner], owner, True) if owner else Resolution(role, [], None, False)
    if role == "operator":
        return Resolution(role, [], None, True)
    raise RoleRefused(
        "role_unassigned", role,
        f"Nobody fills the {role} role for this agent yet.",
    )


_deprecation_logged: set = set()


def log_email_addressing(agent_name: str, surface: str) -> None:
    """An agent picked a person by email instead of naming a role. Still
    honoured for two releases (the queue-file precedent); logged once per
    agent and surface per process so the migration is visible."""
    key = (agent_name, surface)
    if key in _deprecation_logged:
        return
    _deprecation_logged.add(key)
    logger.info(
        "[role_addressing] %s addressed a %s by email; name a role with `to` "
        "(primary | approver | viewer | operator) — addressing by email is "
        "deprecated (trinity-enterprise#606)", agent_name, surface,
    )
