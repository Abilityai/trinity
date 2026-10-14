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
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from database import db
from services.assignment_provider import ROLE_ID_RE  # one regex: the seam validates with it too

logger = logging.getLogger(__name__)

ROLES = ("primary", "approver", "viewer", "operator")

#: trinity-enterprise#848: a skill gate's approver may be a canon role, stored
#: as ``role:<id>`` beside the kinds. Only the gate speaks this form — ``ROLES``
#: and :func:`resolve` (what an agent's ask / report / message may address) do
#: not know it, so an agent cannot address a role id.
ROLE_ID_PREFIX = "role:"


def parse_role_id(value: Any) -> Optional[str]:
    """The lower-cased id in a ``role:<id>`` value, or ``None`` for anything
    else (a kind, a bare id, a malformed one). Matched with ``fullmatch`` so a
    trailing newline is not an id; lower-cased because the stored value is the
    gate's identity (`mixed_approvers` compares the string) and canon ids are
    written in either case."""
    if not isinstance(value, str) or not value.startswith(ROLE_ID_PREFIX):
        return None
    raw = value[len(ROLE_ID_PREFIX):]
    return raw.lower() if ROLE_ID_RE.fullmatch(raw) else None


def role_label(value: Any) -> str:
    """What a stored approver shows as. A kind is its own name; a role id is
    humanised (``role:head-of-sales`` → "Head of sales") until canon titles
    are cached platform-side. Anything else is shown as given. The output of a
    regex-validated id carries no markup."""
    role_id = parse_role_id(value)
    if role_id is None:
        return str(value)
    words = " ".join(re.split(r"[._-]+", role_id)).split()
    text = " ".join(words)
    return text[:1].upper() + text[1:]


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


def resolve_role_id(agent_name: str, role_id: str) -> Resolution:
    """Resolve canon role ``role_id`` (the parsed, lower-cased id) for
    ``agent_name`` — the skill gate's rule for a ``role:<id>`` approver
    (trinity-enterprise#848). Raises ``RoleRefused``.

    Only the assignments provider answers (``resolve_role_id_people``); there
    is no owner or operator fallback for a role id, so no provider, no answer
    and "nobody" all refuse ``role_unassigned`` — the fail-closed direction.
    """
    from services import assignment_provider

    role = f"{ROLE_ID_PREFIX}{role_id}"
    people = assignment_provider.resolve_role_id_people(agent_name, role_id)
    if people:
        return Resolution(role, people, people[0] if len(people) == 1 else None, True)
    raise RoleRefused(
        "role_unassigned", role,
        f"Nobody fills the {role_label(role)} role for this agent yet.",
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
