"""Role addressing — an agent names a role, the platform resolves the person
(abilityai/trinity-enterprise#606).

Every outbound object an agent produces for a human — an ask, a report, a
message — carries one field, ``to``, whose value is a **role**, never a person:
``primary | approver | viewer | operator``. This module is the ONE resolution
rule behind all three, so an ask and a report addressed ``to: approver`` cannot
reach different people. An ask — and only an ask — may also name a canon role
by id, ``role:<id>`` (:func:`resolve_role_id_for_ask`, trinity-enterprise#1007).

Resolution order:

1. A registered assignment provider answers first
   (``assignment_provider.resolve_role_people``). A non-empty answer is the
   people who fill the role. An empty answer is real: nobody fills it.
2. No provider answer ⇒ the core defaults: ``primary`` → the agent's owner;
   ``operator`` → the operators (no person recorded); ``approver`` / ``viewer``
   → refused until someone fills them.
3. A provider that answers "nobody" for ``primary`` is the operators HERE —
   :func:`resolve` is also the rule that decides who may approve a gated skill
   (``skill_gate_service``), and an address is not a grant (ent#904). The
   ADDRESSING surfaces — an ask, a message, a voice note, a proposal — read
   :func:`resolve_addressee` instead, which stands the agent's owner in for
   that one arm (abilityai/trinity-enterprise#1134, ruling 1 of 2026-11-07:
   no primary assigned → the owner, never silently the operator queue). The
   owner is the core default when no provider answers at all.

An ask — and only an ask — may also name an addressing TARGET the platform
resolves from the turn, ``project`` (:data:`ASK_TARGETS`, ent#1134 ruling 2):
who a project reaches is enterprise knowledge, so a resolver is registered
through :func:`register_target_resolver` and :func:`resolve_target` refuses
``project_unavailable`` when none is.

An unknown role, or a role nobody fills, is refused LOUDLY (``RoleRefused``) —
never dropped, never defaulted to "whoever".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from database import db
from services.assignment_provider import ROLE_ID_RE  # one regex: the seam validates with it too

logger = logging.getLogger(__name__)

ROLES = ("primary", "approver", "viewer", "operator")

#: A canon role, stored and addressed as ``role:<id>`` beside the four kinds
#: (trinity-enterprise#848). Two readers speak the form: the skill gate, whose
#: approver may be a role id (:func:`resolve_role_id`), and — since
#: trinity-enterprise#1007 — the ask sink, where an agent's own ``to`` may name
#: one (:func:`resolve_role_id_for_ask`: the raising agent's own roster,
#: reachability applied, every arm refused by name). ``ROLES`` and
#: :func:`resolve` — what a report or a message may address — still do not
#: know it, so those two surfaces refuse a role id ``invalid_to``.
ROLE_ID_PREFIX = "role:"

#: Addressing TARGETS an ask may name beside the kinds and ``role:<id>``
#: (abilityai/trinity-enterprise#1134). A target is resolved by the PLATFORM
#: from the raising turn — never from anything the agent supplies — through a
#: resolver registered per word (:func:`register_target_resolver`). ``project``
#: reaches the steward, then the creator, of the project the turn's chat or
#: room is linked to. Not a role: :func:`resolve` refuses it ``invalid_to``, so
#: a report or a message cannot address one.
ASK_TARGETS = ("project",)


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
    ``invalid_to`` (not a role) or ``role_unassigned`` (nobody fills it); an
    ask to a canon role id (:func:`resolve_role_id_for_ask`) also names
    ``role_unavailable`` (no provider can answer) and ``role_cannot_open_agent``
    (nobody seated can open the agent); an ask to the ``approver`` kind
    (:func:`resolve_approver_for_ask`) names ``role_cannot_open_agent`` too,
    and so does a ``credential`` ask (:func:`resolve_credential_reader`).
    ``message`` names the role's label, never a person.

    ``cause`` says WHY a ``role_cannot_open_agent`` refusal fired when that
    changes the remedy (#4599): one of the ``CAUSE_*`` words, or None for the
    plain arm (the seated people cannot open the agent: share it). It is read
    by callers that word their own refusal (an admin's re-address) and is
    never sent to an agent: the agent's remedy is in ``message``. It names a
    shape of the roster, never a person's account state."""

    def __init__(self, code: str, role: str, message: str, *, cause: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.role = role
        self.message = message
        self.cause = cause


#: The platform could not read who can open the agent. Clears by itself.
CAUSE_UNCHECKED = "unchecked"
#: Nobody is seated as primary and the owner, standing in, cannot open the agent.
CAUSE_NO_PRIMARY = "no_primary"
#: The owner IS the primary person (seated, or by default) and cannot open it.
CAUSE_OWNER_IS_PRIMARY = "owner_is_primary"
#: A system agent is on nobody's Workspace roster.
CAUSE_SYSTEM_AGENT = "system_agent"


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
    ``stood_in`` — True only from :func:`resolve_addressee`, when the provider
    answered "nobody fills primary" and the agent's owner was stood in
    (abilityai/trinity-enterprise#1134); never set by :func:`resolve`.
    """

    role: str
    people: List[str] = field(default_factory=list)
    single: Optional[str] = None
    resolved: bool = True
    stood_in: bool = False


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


def operators_reason(agent_name: str) -> Optional[str]:
    """Why a `primary` address fell to the operators once the owner could not
    stand in (trinity-enterprise#1134, AC4): ``owner_no_email`` — the agent has
    an owner, who has no email; ``no_owner`` — it has none. None when the
    ownership row cannot be read: the notice then says less, never a guess."""
    try:
        owner = db.get_agent_owner(agent_name)
    except Exception:  # noqa: BLE001
        logger.warning("[role_addressing] owner lookup failed for %s", agent_name, exc_info=True)
        return None
    return "owner_no_email" if (owner or {}).get("owner_username") else "no_owner"


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


def resolve_addressee(
    agent_name: str,
    role: str,
    *,
    owner_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Resolution:
    """:func:`resolve` for an ADDRESSING surface — an ask, a message, a voice
    note, a proposal's approver (abilityai/trinity-enterprise#1134, ruling 1):
    the one rule those surfaces share, so an ask and a message addressed
    ``to: primary`` cannot reach different people (one exception: a
    ``credential`` ask, whose reader must be able to open its one door —
    :func:`resolve_credential_reader`, #4579).

    Identical to :func:`resolve` on every arm but one: a provider that answers
    "nobody fills primary" stands the agent's OWNER in (``stood_in`` True)
    instead of the operators, because the person in the chat never sees an
    ask that went to the Operating Room. An owner with no email is still the
    operators (``resolved`` False). The skill gate must keep reading
    :func:`resolve`: with the stand-in there, an owner requesting a gated
    skill with no primary seated would approve their own run (ent#904).
    Raises ``RoleRefused`` exactly as :func:`resolve` does.
    """
    r = resolve(agent_name, role, owner_lookup=owner_lookup)
    if role != "primary" or r.people:
        return r
    owner = (owner_lookup or owner_email)(agent_name)
    if not owner:
        return r
    return Resolution(role, [owner], owner, True, stood_in=True)


# ---------------------------------------------------------------------------
# Addressing targets (ent#1134): the word is OSS, the resolution is registered
# ---------------------------------------------------------------------------

@dataclass
class TargetResolution:
    """Who an addressing target reaches: ``people`` (one, for ``project``),
    ``single`` when exactly one, the ``project_id`` the sink stamps on the row
    under the platform's project key, and ``seat`` — which rule picked the
    person (``steward`` | ``creator``), carried for the Workspace card, never
    for the agent."""

    people: List[str] = field(default_factory=list)
    single: Optional[str] = None
    project_id: Optional[str] = None
    seat: Optional[str] = None


TargetResolver = Callable[[str, Optional[str]], TargetResolution]

_target_resolvers: Dict[str, TargetResolver] = {}


def register_target_resolver(target: str, fn: TargetResolver) -> None:
    """Register the resolver for ``target`` (one of :data:`ASK_TARGETS`). One
    per word; a later registration replaces the earlier (a test's seam). The
    enterprise projects module registers ``project`` when it joins the seams.
    An unknown word is a programming error."""
    if target not in ASK_TARGETS:
        raise ValueError(f"register_target_resolver: target must be one of {ASK_TARGETS}, got {target!r}")
    _target_resolvers[target] = fn


def clear_target_resolvers() -> None:
    """Forget every registered target resolver (tests)."""
    _target_resolvers.clear()


def resolve_target(target: str, agent_name: str, turn: Optional[str]) -> TargetResolution:
    """Resolve addressing ``target`` for an ask ``agent_name`` raised in
    ``turn`` — the PLATFORM's id of the raising execution, already validated
    as the agent's own (``ask_service._platform_turn``), or None when the
    platform knows no turn. Raises ``RoleRefused`` on every miss:

    * ``project_unavailable`` — no resolver is registered (an OSS-only
      install), or the resolver failed: refused, never a 500, nothing filed;
    * the resolver's own refusals pass through by name (the enterprise
      projects module names ``not_in_project_turn`` and
      ``project_unreachable``), each carrying its remedy.
    """
    if target not in ASK_TARGETS:
        raise RoleRefused("invalid_to", str(target), "Unknown addressing target.")
    unavailable = RoleRefused(
        "project_unavailable", target,
        f"The {target} target cannot be addressed on this install right now: the module "
        "that resolves it is not installed or could not answer. Address the ask to "
        "primary or operator.",
    )
    fn = _target_resolvers.get(target)
    if fn is None:
        raise unavailable
    try:
        return fn(agent_name, turn)
    except RoleRefused:
        raise
    except Exception:  # noqa: BLE001 — refused by name, never a 500 on the agent's path
        logger.warning("[role_addressing] the %s target resolver failed for %s",
                       target, agent_name, exc_info=True)
        raise unavailable from None


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


def _client_blocked(email: str) -> bool:
    """Is this email blocked at the Workspace door (the client block)? Raises
    when the block cannot be read: the caller decides what that means."""
    from client_portal import db as portal_db
    return bool(portal_db.is_client_blocked(email))


def _workspace_only(user: dict) -> bool:
    """Can this account decide in the Workspace only (below the operator
    floor, ``dependencies.is_workspace_only_role``)?"""
    from dependencies import is_workspace_only_role
    return is_workspace_only_role(user.get("role"))


def agent_reach(agent_name: str, email: str) -> Optional[bool]:
    """Can this person reach a decision raised on ``agent_name``? True, False,
    or None when the platform could not check (#4599).

    A decision is made in the Workspace (the agent on the person's roster:
    owned or shared) or in Operations (an operator or admin with access to the
    agent). A platform account is asked ``can_user_access_agent`` — owner,
    shared or admin, which covers both doors; an email with no account only has
    the Workspace, so the agent must be shared with it.

    Two account states close a door whatever is shared (#4599):

    * a deactivated account signs in nowhere (``get_current_user`` refuses it
      on every credential kind), so it reaches nothing;
    * the client block closes the Workspace only (``get_current_user`` does
      not read it). So it stops an email with no account and an account that
      can only use the Workspace; a blocked operator or admin still decides in
      Operations and stays reachable.

    None is "could not check": a lookup raised. :func:`can_open_agent` reads
    it as "cannot open" (fail closed); a resolver that found nobody reachable
    (:func:`reach_of`) refuses it with "try again" instead of "share the
    agent".
    """
    try:
        user = db.get_user_by_email(email)
        if user:
            if user.get("suspended_at"):
                return False
            if _workspace_only(user) and _client_blocked(email):
                return False
            return bool(db.can_user_access_agent(user["username"], agent_name))
        if _client_blocked(email):
            return False
        from client_portal.service import agent_on_roster
        return bool(agent_on_roster(agent_name, email, include_owned=False))
    except Exception:  # noqa: BLE001
        logger.warning("[role_addressing] could not tell whether a person can open %s",
                       agent_name, exc_info=True)
        return None


def can_open_agent(agent_name: str, email: str) -> bool:
    """Can this person reach a decision raised on ``agent_name``?
    (trinity-enterprise#881)

    :func:`agent_reach` as a yes or no, for a caller that only needs the
    decision. Fails closed: an unreadable lookup is "cannot open", which
    refuses by name rather than waits silently. :func:`agent_reach` is the one
    rule for a gate's approver (``skill_gate_service._can_open``), for an ask
    to a canon role (trinity-enterprise#1007) and for an ask to the
    ``approver`` kind (#4501).
    """
    return agent_reach(agent_name, email) is True


def reach_of(agent_name: str, people: List[str]) -> Dict[str, Optional[bool]]:
    """:func:`agent_reach` for each of ``people``, read ONCE (#4599). A
    resolver takes both its audience (the people who can open the agent) and
    its refusal's cause (nobody can, or the platform could not check) from
    this one read: asked twice, a lookup that failed and then recovered would
    answer "cannot open" for a person who can, and that refusal is one a room
    records for good."""
    return {p: agent_reach(agent_name, p) for p in people}


def could_not_check(agent_name: str, role: str, *, workspace: bool = False) -> RoleRefused:
    """The ONE refusal for "the platform could not check who can open the
    agent" (#4599). The code stays ``role_cannot_open_agent``, so a caller
    that reads codes is unchanged; the sentence tells the agent to try again
    rather than to get the agent shared. ``workspace`` is the credential door,
    which is the Workspace alone."""
    where = " in their Workspace" if workspace else ""
    return RoleRefused(
        "role_cannot_open_agent", role,
        f"The platform could not check right now who can open {agent_name}{where}, so this ask "
        "was not filed. Nothing is wrong with your request. Try again in a few minutes.",
        cause=CAUSE_UNCHECKED,
    )


def workspace_reach(agent_name: str, email: str) -> Optional[bool]:
    """Can this person reach the Workspace secret field of a ``credential``
    ask raised on ``agent_name``? True, False, or None when the platform could
    not check (#4579, #4599).

    That field is the ONE door that can end a credential ask
    (``ask_service.answer`` refuses every other answer), and it opens for the
    ask's reader only when the agent is on their Workspace roster: shared with
    them, or owned by them (``client_portal.asks.service._owned_ask``).
    Narrower than :func:`agent_reach` on purpose: that rule also counts an
    admin's access to any agent, because an approval can be decided in
    Operations. A credential cannot.

    ``include_owned=True`` for everyone: the owned read joins the account by
    email, so an address with no account owns nothing. Two account states keep
    a person out of the Workspace whatever is shared with them, so neither is
    a reader: a deactivated account (trinity-enterprise#1258) and a blocked
    email (#4599) — the Workspace door refuses a blocked platform account too,
    so the block is read for everyone here.

    The account is read with ``db.is_email_account_suspended``, which raises
    when it cannot answer; ``client_portal.service.email_account_suspended``
    answers "deactivated" instead, which would hide an outage as "not a
    reader". None is that outage: :func:`resolve_credential_reader` refuses it
    with "try again", never files the ask for a reader nobody checked.
    """
    try:
        from client_portal.service import agent_on_roster
        if db.is_email_account_suspended(email):
            return False
        if _client_blocked(email):
            return False
        return bool(agent_on_roster(agent_name, email, include_owned=True))
    except Exception:  # noqa: BLE001
        logger.warning("[role_addressing] could not check whether a person has %s on their "
                       "Workspace roster", agent_name, exc_info=True)
        return None


def on_workspace_roster(agent_name: str, email: str) -> bool:
    """:func:`workspace_reach` as a yes or no (#4579). Fails closed: "could
    not check" is "not on the roster"."""
    return workspace_reach(agent_name, email) is True


def _is_system_agent(agent_name: str) -> bool:
    """A system agent is excluded from both Workspace rosters, so nobody ever
    has one. Unreadable ⇒ False: the refusal then keeps its plain remedy."""
    try:
        return bool(db.is_system_agent(agent_name))
    except Exception:  # noqa: BLE001
        logger.warning("[role_addressing] could not tell whether %s is a system agent",
                       agent_name, exc_info=True)
        return False


def resolve_credential_reader(
    agent_name: str,
    *,
    owner_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> Resolution:
    """Resolve the ONE reader of a ``credential`` ask raised by ``agent_name``
    (#4579) — a chain, with the reach of the secret field checked at each step
    (:func:`workspace_reach`):

    1. the agent's primary person (:func:`resolve_addressee`: the seated
       primary, else the owner standing in), when they can reach the field;
    2. else the agent's owner, when the owner can;
    3. else ``RoleRefused("role_cannot_open_agent")`` — nothing is filed.

    Nobody resolved, or several people, is returned as it is: the ask sink
    refuses that ``no_person``, as before. This is the one place an ask
    ``to: primary`` may reach someone other than a message ``to: primary``
    does: a credential ask has a single door, so a seated primary who cannot
    open it is passed over rather than recorded as a reader who never sees
    the ask while nobody else may fill it. The native raise, the queue-file
    channel and a re-address (``ask_service.readdress_role``) all read this
    rule, so they cannot drift. (A queue-file row carries no role: it is
    addressed once, at ingest, and no re-address lists it.)

    The refusal keeps one code and names its real cause (#4599), because the
    remedy differs — ``cause`` on the exception, the sentence for the agent:

    * a reach that could not be checked (:func:`could_not_check`) — try
      again. A primary who could not be checked is NOT passed over for the
      owner: nothing moves a filed ask back to them;
    * a system agent — nobody has one in their Workspace;
    * nobody is seated and the owner, standing in, cannot open it — assign a
      primary person;
    * the owner is the primary person (seated, or by default where no
      assignments module answers) — an agent cannot be shared with its own
      owner, so the owner's own access is the thing to check;
    * otherwise a seated primary and the owner both cannot — share it.

    The message names roles, never a person, and never which account state
    stops them.
    """
    lookup = owner_lookup or owner_email
    r = resolve_addressee(agent_name, "primary", owner_lookup=lookup)
    if not r.single:
        return r
    reach = workspace_reach(agent_name, r.single)
    if reach:
        return r
    if reach is None:
        raise could_not_check(agent_name, "primary", workspace=True)
    owner = lookup(agent_name)
    owner_is_primary = bool(owner) and owner == r.single
    if owner and not owner_is_primary:
        owner_reach = workspace_reach(agent_name, owner)
        if owner_reach:
            logger.info("[role_addressing] the primary person for %s cannot open it in their "
                        "Workspace; a credential ask goes to the agent's owner", agent_name)
            return Resolution("primary", [owner], owner, True, stood_in=True)
        if owner_reach is None:
            raise could_not_check(agent_name, "primary", workspace=True)
    code = "role_cannot_open_agent"
    if _is_system_agent(agent_name):
        raise RoleRefused(
            code, "primary",
            f"{agent_name} is a system agent, and a system agent is in nobody's Workspace, "
            "so nobody could fill this ask. Ask an admin to set the credential on "
            f"{agent_name} directly.",
            cause=CAUSE_SYSTEM_AGENT,
        )
    if owner_is_primary and r.stood_in:
        raise RoleRefused(
            code, "primary",
            f"{agent_name} has no primary person, and its owner cannot open {agent_name} in "
            "their Workspace, so nobody would see this ask. Ask an admin to assign a primary "
            f"person and share {agent_name} with them.",
            cause=CAUSE_NO_PRIMARY,
        )
    if owner_is_primary:
        raise RoleRefused(
            code, "primary",
            f"The primary person for {agent_name} is its owner, who cannot open {agent_name} "
            "in their Workspace, so nobody would see this ask. Ask an admin to check the "
            "owner's access to the Workspace.",
            cause=CAUSE_OWNER_IS_PRIMARY,
        )
    raise RoleRefused(
        code, "primary",
        f"Neither the primary person for {agent_name} nor its owner can open {agent_name} "
        "in their Workspace, so nobody would see this ask. Ask an admin to share "
        f"{agent_name} with its primary person.",
    )


def resolve_approver_for_ask(agent_name: str) -> Resolution:
    """Resolve the ``approver`` kind for an ASK raised by ``agent_name``
    (#4501): the people seated as approver who can open the agent. Raises
    ``RoleRefused``:

    * ``role_unassigned`` — nobody fills the seat (:func:`resolve`'s own arm);
    * ``role_cannot_open_agent`` — everyone seated is outside the agent
      (:func:`can_open_agent`), so they would never see the ask; or the
      platform could not check (:func:`could_not_check`, #4599), which says
      "try again" under the same code.

    The answer to an ``approver`` ask is fenced to the people recorded at the
    raise (trinity-enterprise#1053), so a seated approver who cannot open the
    agent must not be recorded: nobody else may decide the ask, and that
    person never sees it. The same rule as a canon role
    (:func:`resolve_role_id_for_ask`) and a gate's approver
    (``skill_gate_service._approvers``). The other kinds are not fenced and
    keep :func:`resolve_addressee`. Several reachable people are all recorded;
    ``single`` only when exactly one. The message names the role, never a
    person, and its remedy is to share the agent — not to address someone
    else, which would hand a separate approval to the owner.
    """
    r = resolve(agent_name, "approver")
    reach = reach_of(agent_name, r.people)
    reachable = [p for p in r.people if reach[p]]
    if not reachable:
        if None in reach.values():
            raise could_not_check(agent_name, "approver")
        raise RoleRefused(
            "role_cannot_open_agent", "approver",
            f"The approver for {agent_name} cannot open {agent_name}, so they would "
            f"never see this ask. Ask the agent's owner to share {agent_name} with them.",
        )
    return Resolution("approver", reachable, reachable[0] if len(reachable) == 1 else None, True)


def resolve_role_id_for_ask(agent_name: str, role_id: str) -> Resolution:
    """Resolve canon role ``role_id`` (the parsed, lower-cased id) for an ASK
    raised by ``agent_name`` (trinity-enterprise#1007): the people seated in it
    on this agent's OWN roster who can open the agent. Raises ``RoleRefused``
    on every arm, each named for its remedy — unlike :func:`resolve_role_id`,
    which folds the gate's arms into one ``role_unassigned``:

    * ``role_unavailable`` — no assignments provider answers for the role
      (none registered, no answer, or it failed): address ``primary`` or
      ``operator`` instead.
    * ``role_unassigned`` — the provider answered: nobody is seated.
    * ``role_cannot_open_agent`` — everyone seated is outside the agent
      (:func:`can_open_agent`), so they would never see the ask; or the
      platform could not check (:func:`could_not_check`, #4599).

    No owner or operator fallback: a role id reaches the people it names, or
    nobody. Several reachable people are all recorded; ``single`` only when
    exactly one. The message names the role's label, never a person.
    """
    from services import assignment_provider

    role = f"{ROLE_ID_PREFIX}{role_id}"
    label = role_label(role)
    people = assignment_provider.resolve_role_id_people(agent_name, role_id)
    if people is None:
        raise RoleRefused(
            "role_unavailable", role,
            f"The {label} role cannot be addressed on this install right now: the "
            "assignments module that answers canon roles is not installed, has no "
            "answer for this role, or could not answer. Address the ask to primary "
            "or operator.",
        )
    if not people:
        raise RoleRefused(
            "role_unassigned", role,
            f"Nobody fills the {label} role for this agent yet. Ask the agent's owner "
            "to seat someone, or address the ask to primary or operator.",
        )
    reach = reach_of(agent_name, people)
    reachable = [p for p in people if reach[p]]
    if not reachable:
        if None in reach.values():
            raise could_not_check(agent_name, role)
        raise RoleRefused(
            "role_cannot_open_agent", role,
            f"The {label} for {agent_name} cannot open {agent_name}, so they would "
            f"never see this ask. Ask the agent's owner to share {agent_name} with them.",
        )
    return Resolution(role, reachable, reachable[0] if len(reachable) == 1 else None, True)


class RecipientRefused(Exception):
    """A one-recipient send (a message, a voice note) cannot be addressed.
    ``detail`` is the caller-facing body — ``code`` / ``role`` / ``message``
    (+ ``count`` for several) — and names the ROLE only, never a resolved
    person (ent#606: an agent that named a role learns nobody's email)."""

    def __init__(self, detail: dict):
        super().__init__(detail.get("code"))
        self.detail = detail


def resolve_single_recipient(
    agent_name: str,
    *,
    to: Optional[str],
    recipient_email: Optional[str],
    noun: str = "message",
    surface: str = "message",
    by_agent_key: bool = False,
) -> str:
    """The one person a one-recipient send goes to (ent#606, shared by
    ``send_message`` and ``send_voice_reply`` — abilityai/trinity-enterprise#700).

    ``to`` names a role, resolved by :func:`resolve_addressee`. A role nobody
    fills, or that several people fill, raises :class:`RecipientRefused` — a
    send has one recipient, and silently picking one of several is exactly
    what the ruling forbids. ``recipient_email`` still works for two releases;
    an agent key choosing a person by email is logged as deprecated
    (``by_agent_key``).

    trinity-enterprise#904 ruled that ``primary`` with no seat written falls
    back to the agent's owner on the send surface; ent#1134 made that the one
    shared rule of every addressing surface (:func:`resolve_addressee`), so
    the stand-in no longer lives here. :func:`resolve` keeps refusing for the
    gate — an address, not a grant.
    """
    if not to:
        if by_agent_key:
            log_email_addressing(agent_name, surface)
        return recipient_email
    try:
        r = resolve_addressee(agent_name, to)
    except RoleRefused as e:
        raise RecipientRefused({
            "code": e.code, "role": e.role, "message": e.message + " Message primary instead."})
    if len(r.people) > 1:
        raise RecipientRefused({
            "code": "role_resolves_to_several", "role": to, "count": len(r.people),
            "message": f"{len(r.people)} people fill the {to} role and a {noun} has "
                       "one recipient — message primary, or raise an ask."})
    if not r.single and to == "primary":
        raise RecipientRefused({
            "code": "role_unassigned", "role": to,
            "message": "Nobody fills the primary role for this agent yet, and its owner has "
                       "no email address. An admin can assign a primary in the agent's "
                       "Access tab."})
    if not r.single:
        raise RecipientRefused({
            "code": "role_unassigned", "role": to,
            "message": f"Nobody fills the {to} role for this agent yet."})
    return r.single


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
