"""Shared-sessions (rooms) business logic — ent#169. OSS core since ent#443.

Design: ``docs/planning/SHARED_SESSIONS_DESIGN.md``. The one idea:

    A room is a shared persistent RECORD, never a shared CONTEXT.

Each agent keeps its own isolated Claude session; before it speaks it is handed
only the transcript it has not seen (``last_read_seq``). That is why a room does
not cost 15x tokens and why no LLM has to decide who talks next — turn-taking is
mechanical: **you are woken iff you were @mentioned**.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.helpers import utc_now_iso

from . import db

logger = logging.getLogger(__name__)

FEATURE_ID = "shared_sessions"

# Budgets — bounded by construction (the epic's measured reason: broadcast rooms
# are quadratic in agents x rounds).
DEFAULT_MAX_MESSAGES = 60
DEFAULT_TTL_HOURS = 24

# --- Operator budget defaults (ent#387) -----------------------------------
#
# ent#381 retired the Sessions page, and with it `NewRoomDialog` — the only
# surface that ever SET a room budget. Rooms started from the Workspace fell back
# to the constants above, with no way for an operator to change them short of
# creating rooms over MCP.
#
# The deeper half was not a UI gap: `POST /api/rooms` took `max_messages` /
# `max_cost_usd` / `ttl_hours` from ANY principal, including a workspace client
# (ent#362). The dialog never showed the fields, but the endpoint accepted them,
# so the party being bounded could set their own bound — a budget is an operator
# control by definition, and a client-settable one is not a control at all.
#
# So budgets now come from the operator: three `system_settings` rows read at
# creation time (write-through, no restart, no private table — the ent#375
# precedent), and a non-platform caller's supplied budgets are ignored rather
# than trusted.
ROOM_DEFAULT_MAX_MESSAGES_KEY = "room_default_max_messages"
ROOM_DEFAULT_MAX_COST_KEY = "room_default_max_cost_usd"
ROOM_DEFAULT_TTL_HOURS_KEY = "room_default_ttl_hours"

# The same bounds the `RoomCreate` validators enforce, so an operator default can
# never be a value a caller would have been refused.
MAX_MESSAGES_CEILING = 500
MAX_TTL_HOURS = 168
MAX_PARTICIPANTS = 12

# How much transcript a COLD agent gets when it has no resumable session.
ROOM_COLD_CONTEXT_MESSAGES = 30
# ent#362 AC#4: how many agent wakes one participant may trigger in a room per
# window. The chain-depth cap below bounds an agent->agent cascade from a SINGLE
# message; it does nothing about a human sending twenty messages that each
# mention three agents. With customers in rooms that is a spend amplifier, so
# each participant gets its own budget of wakes.
ROOM_WAKE_LIMIT_PER_PARTICIPANT = 30
ROOM_WAKE_LIMIT_WINDOW_SECONDS = 300

# How deep an agent->agent mention chain may go from one human message.
ROOM_MAX_CHAIN_DEPTH = 8

MAX_CONTENT_CHARS = 8000
ROOM_TURN_TIMEOUT_SECONDS = 300

_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_-]{0,99})")


class RoomError(Exception):
    """Carries an HTTP status AND a stable machine-readable ``code`` — callers
    include agents, which need to tell 'closed' from 'not permitted'."""

    def __init__(self, status_code: int, code: str, detail: str, **extra):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


# --- identity helpers --------------------------------------------------------

def _user_identity(current_user) -> str:
    """Participant identity for a human is the USERNAME, not the numeric id.

    It is what the OSS ACL keys on (``can_user_access_agent(username, ...)``),
    and it is what agents see as the sender label in the transcript — an opaque
    id would make the shared record unreadable to its own participants.
    """
    return str(getattr(current_user, "username", "") or "")


# ent#362: a THIRD participant kind. A workspace client is a verified email with
# no `users` row, so it cannot be a `user` participant — that identity space is
# usernames, and the OSS ACL keys on them. Collapsing the two would mean an email
# that happens to equal a username inherits that account's access, which is a
# privilege escalation dressed as a convenience.
#
# `kind` is free TEXT with no CHECK, so this needs no migration.
WORKSPACE_KIND = "workspace_user"


def _workspace_identity(principal) -> str:
    """Participant identity for a workspace client is its VERIFIED email.

    Lowercased, because that is how the portal roster stores and compares it —
    a case difference must not mint a second participant for the same person.
    """
    return str(getattr(principal, "email", "") or "").strip().lower()


def _caller(current_user) -> tuple[str, str]:
    """(kind, identity) for the acting principal, taken from the AUTH CONTEXT.

    An agent-scoped key acts as its agent, a workspace principal as its verified
    email, anything else as the human user. Never derived from a request body —
    that would let a caller post as someone else in a shared transcript.
    """
    agent_name = getattr(current_user, "agent_name", None)
    if agent_name:
        return "agent", agent_name
    # A portal principal carries `is_portal`; a platform User never does, so the
    # two can't be confused even though both have an `email`.
    if getattr(current_user, "is_portal", False):
        return WORKSPACE_KIND, _workspace_identity(current_user)
    return "user", _user_identity(current_user)


# --- ACL ---------------------------------------------------------------------

def _assert_can_reach_agent(current_user, agent_name: str) -> None:
    """Creating a room requires access to EVERY agent participant. This is the
    only place the N^2 question is asked; membership is the grant thereafter."""
    from fastapi import HTTPException

    from dependencies import assert_agent_access

    # ent#362 AC#5: a workspace client may only room with agents shared to it.
    # Resolved through the PORTAL roster — the same predicate the rest of the
    # Workspace uses — rather than the platform ACL, which needs a `users` row
    # this principal does not have. One rule, so the two cannot drift: widening
    # a share widens rooms, and nothing else has to be taught about it.
    if getattr(current_user, "is_portal", False):
        from client_portal.service import agent_on_roster
        email = _workspace_identity(current_user)
        # `include_owned` mirrors the roster: a platform-session workspace user
        # sees the agents it owns, an external client sees only what was shared.
        if not agent_on_roster(agent_name, email,
                               bool(getattr(current_user, "is_platform", False))):
            raise RoomError(403, "agent_not_accessible",
                            f"You do not have access to agent {agent_name!r}")
        return

    # Access first, then existence — and ONE uniform refusal for both branches.
    # A 404 "agent_not_found" followed by a 403 "agent_not_accessible" is the
    # exact existence/access differential Invariant #8 forbids: room creation is
    # open to any authenticated principal, so the split would let any logged-in
    # user enumerate the fleet's live agent names by probing POST /api/rooms.
    # The existence check stays (an admin's access check passes unconditionally,
    # #1445 class) but its failure is indistinguishable from a denial.
    try:
        # The shared imperative guard (Invariant #8 family) — it also enforces
        # the connector-key fence, so a connector principal can't assemble a room.
        assert_agent_access(current_user, agent_name)
    except HTTPException as e:
        raise RoomError(403, "agent_not_accessible",
                        f"You do not have access to agent {agent_name!r}") from e
    if not db.agent_exists(agent_name):
        raise RoomError(403, "agent_not_accessible",
                        f"You do not have access to agent {agent_name!r}")


def _require_membership(room_id: str, current_user) -> dict:
    """Load a room the caller may see, else a UNIFORM 404.

    Never 403 on a room the caller isn't in — that would confirm the room exists
    (Invariant #8, enumeration-safety). Admins can see any room.
    """
    kind, identity = _caller(current_user)
    is_admin = getattr(current_user, "role", None) == "admin" and kind == "user"

    # ent#220 item 6: BOTH reads happen before either verdict, so a room that
    # does not exist and a room the caller is not in cost the same number of
    # queries. Branching early made the 404 arrive measurably sooner for a
    # nonexistent room — a timing oracle that hands back exactly the existence
    # bit the uniform 404 exists to withhold. Same rule as OSS Invariant #8's
    # dependency helpers: evaluate, then branch.
    room = db.get_room(room_id)
    participant = None if is_admin else db.get_participant(room_id, kind, identity)

    if not room:
        raise RoomError(404, "room_not_found", "Room not found")
    if is_admin:
        return room
    if not participant:
        raise RoomError(404, "room_not_found", "Room not found")
    return room


def _require_moderator(room_id: str, current_user) -> dict:
    """Lifecycle + membership mutations (close, add/remove participant) are
    restricted to a human **moderator** (the room creator) or a platform admin.

    A member AGENT is reachable via its own MCP key and is a prompt-injection
    surface: it must not be able to close a room or rewrite its roster using the
    resolved owner's ACL. Agents participate by TALKING (@mention), not by
    managing the room (ent#220). Membership is checked first, so a non-member
    still gets the uniform 404; a non-moderator *member* (an agent) gets a 403,
    which discloses nothing it can't already see (access-first, Invariant #8).
    """
    room = _require_membership(room_id, current_user)
    kind, identity = _caller(current_user)
    if getattr(current_user, "role", None) == "admin" and kind == "user":
        return room
    participant = db.get_participant(room_id, kind, identity)
    # ent#362: a workspace client moderates the room it created — otherwise it
    # could not add an agent mid-chat (ent#361 AC#3) or close its own room.
    # The kind check still excludes AGENTS, which is what this gate is for: an
    # agent is reachable via its own MCP key and is a prompt-injection surface,
    # so it must never rewrite a roster (ent#220).
    if (kind in ("user", WORKSPACE_KIND) and participant
            and participant.get("role") == "moderator"):
        return room
    raise RoomError(403, "not_moderator", "Only a room moderator can manage this room")


# --- rooms -------------------------------------------------------------------

def _expiry(ttl_hours: Optional[int]) -> Optional[str]:
    hours = DEFAULT_TTL_HOURS if ttl_hours is None else ttl_hours
    if hours <= 0:
        return None  # explicit opt-out
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _setting(key: str):
    """Read one operator setting. Fail-open to None (the code default)."""
    try:
        from database import db
        return db.get_setting_value(key, None)
    except Exception:  # noqa: BLE001 — a settings read must never block a room
        logger.warning("[rooms] could not read %s; using the code default", key)
        return None


def _coerce_int(raw, *, lo: int, hi: int):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if lo <= value <= hi else None


def _coerce_cost(raw):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # 0 (and anything below) means "no cap" rather than "a room that closes on
    # its first message" — the `RoomCreate` validator refuses <= 0 for the same
    # reason, and a stored 0 must not become a stricter cap than the operator can
    # express through the API.
    return value if value > 0 else None


def budget_defaults() -> dict:
    """The operator's defaults for a room nobody explicitly bounded.

    Resolution is `system_settings` row -> code default, per key. Every value is
    re-validated against the same range the API enforces, so a hand-edited row
    cannot install a budget a caller would have been refused; an out-of-range or
    unparseable row degrades to the code default rather than failing the create.
    `sources` reports which of the two answered, so an operator reading the panel
    can tell a configured value from an inherited one.
    """
    raw_messages = _setting(ROOM_DEFAULT_MAX_MESSAGES_KEY)
    raw_cost = _setting(ROOM_DEFAULT_MAX_COST_KEY)
    raw_ttl = _setting(ROOM_DEFAULT_TTL_HOURS_KEY)

    max_messages = _coerce_int(raw_messages, lo=1, hi=MAX_MESSAGES_CEILING)
    max_cost = _coerce_cost(raw_cost)
    ttl_hours = _coerce_int(raw_ttl, lo=0, hi=MAX_TTL_HOURS)

    return {
        "max_messages": DEFAULT_MAX_MESSAGES if max_messages is None else max_messages,
        "max_cost_usd": max_cost,
        "ttl_hours": DEFAULT_TTL_HOURS if ttl_hours is None else ttl_hours,
        "sources": {
            ROOM_DEFAULT_MAX_MESSAGES_KEY: "db-row" if max_messages is not None else "code-default",
            ROOM_DEFAULT_MAX_COST_KEY: "db-row" if max_cost is not None else "code-default",
            ROOM_DEFAULT_TTL_HOURS_KEY: "db-row" if ttl_hours is not None else "code-default",
        },
    }


def set_budget_defaults(*, max_messages=None, max_cost_usd=None, ttl_hours=None,
                        clear=None) -> dict:
    """Write the operator defaults through to `system_settings`. Returns the new state.

    Partial update: an omitted field is left alone, and `clear` names keys to
    revert to the code default — the same shape as the Brain Orb flags, and the
    only way to express "no cost cap" once one has been set (a cost of 0 is not a
    cap of zero, it is the absence of one).
    """
    from database import db

    clear = set(clear or ())
    writes: dict = {}
    if max_messages is not None:
        writes[ROOM_DEFAULT_MAX_MESSAGES_KEY] = str(int(max_messages))
    if max_cost_usd is not None:
        writes[ROOM_DEFAULT_MAX_COST_KEY] = str(float(max_cost_usd))
    if ttl_hours is not None:
        writes[ROOM_DEFAULT_TTL_HOURS_KEY] = str(int(ttl_hours))

    for key, value in writes.items():
        db.set_setting(key, value)
    for name in clear:
        db.delete_setting(name)

    return budget_defaults()


def _resolve_budget(current_user, max_messages, max_cost_usd, ttl_hours):
    """Decide the room's budget, and who was allowed to decide it (ent#387).

    A **platform** principal (JWT user, MCP key, agent key) may bound a room
    explicitly — that is the operator, and an unspecified field falls through to
    the operator default. A **workspace client** may not: their values are
    dropped, not refused, because the Workspace never offers the fields and a 4xx
    would only advertise a control they cannot have.

    Ignoring rather than clamping is deliberate. A clamp still lets the bounded
    party choose anything up to the ceiling — including "the ceiling" — which is
    the same defect with a smaller blast radius.
    """
    try:
        defaults = budget_defaults()
    except Exception:  # noqa: BLE001 — belt: `_setting` already guards the read,
        # but a room create must not fail because an operator preference could
        # not be resolved. Falling back to the constants is always safe: they are
        # the tightest of the three sources.
        logger.warning("[rooms] budget defaults unavailable; using code defaults", exc_info=True)
        defaults = {"max_messages": DEFAULT_MAX_MESSAGES, "max_cost_usd": None,
                    "ttl_hours": DEFAULT_TTL_HOURS}
    # Absent attribute defaults to platform, because every PLATFORM principal
    # (`User` from a JWT / MCP key) lacks the field entirely — but a portal
    # principal must never inherit that default. `is_portal` is the marker the
    # rest of this service resolves participant kind from, so a future principal
    # type that carries it and forgets `is_platform` fails CLOSED here rather
    # than silently gaining the operator's authority.
    is_platform = bool(
        getattr(current_user, "is_platform", not getattr(current_user, "is_portal", False))
    )

    if not is_platform and any(v is not None for v in (max_messages, max_cost_usd, ttl_hours)):
        logger.info(
            "[rooms] ignoring caller-supplied budget from a workspace client — "
            "operator defaults apply"
        )
        max_messages = max_cost_usd = ttl_hours = None

    return (
        defaults["max_messages"] if max_messages is None else max_messages,
        defaults["max_cost_usd"] if max_cost_usd is None else max_cost_usd,
        defaults["ttl_hours"] if ttl_hours is None else ttl_hours,
    )


def create_room(current_user, name: str, agents: list[str], topic: Optional[str] = None,
                max_messages: Optional[int] = None, max_cost_usd: Optional[float] = None,
                ttl_hours: Optional[int] = None, scribe: Optional[str] = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise RoomError(422, "invalid_name", "A room needs a name")
    agents = [a.strip() for a in (agents or []) if a and a.strip()]
    if not agents:
        raise RoomError(422, "no_participants", "A room needs at least one agent participant")
    if len(agents) > MAX_PARTICIPANTS:
        raise RoomError(422, "too_many_participants",
                        f"At most {MAX_PARTICIPANTS} agent participants")
    if len(set(agents)) != len(agents):
        raise RoomError(422, "duplicate_participants", "Duplicate agent participants")
    if scribe and scribe not in agents:
        raise RoomError(422, "invalid_scribe", "The scribe must be one of the participants")

    for agent in agents:                      # ACL: every one, before anything is created
        _assert_can_reach_agent(current_user, agent)

    room_id = f"room_{uuid.uuid4().hex[:16]}"
    now = utc_now_iso()
    # ent#362: the creator is seeded from the RESOLVED caller, not from
    # `_user_identity`. A workspace client has no username, so the old form
    # recorded an empty `created_by` and seeded a `user` participant with an
    # empty identity — leaving the creator a non-member of their own room, 404'd
    # by `_require_membership` on the very next request.
    creator_kind, creator_identity = _caller(current_user)
    # ent#220 item 6: ONE transaction. Three separate commits could leave a room
    # with no participants — including its creator, whom `_require_membership`
    # then 404s out of their own room — or a roster with no opening line, and
    # nothing repairs a half-created room because it looks legitimate to every
    # reader.
    participants = [(creator_kind, creator_identity, "moderator")]
    participants += [("agent", a, "scribe" if a == scribe else "member") for a in agents]
    # ent#387: the budget is the operator's, not the caller's — a workspace
    # client's values are dropped here, and anything unspecified resolves to the
    # operator default rather than the constant.
    eff_max_messages, eff_max_cost, eff_ttl_hours = _resolve_budget(
        current_user, max_messages, max_cost_usd, ttl_hours
    )
    db.create_room_with_participants(
        room_id, name, topic, creator_identity,
        eff_max_messages,
        eff_max_cost, _expiry(eff_ttl_hours),
        participants,
        uuid.uuid4().hex, f"Room created with {', '.join(agents)}.",
        now,
    )
    logger.info("room %s created by %s:%s with %s",
                room_id, creator_kind, creator_identity, agents)
    return get_room(current_user, room_id)


def get_room(current_user, room_id: str, since_seq: int = 0) -> dict:
    room = _require_membership(room_id, current_user)
    participants = db.list_participants(room_id)
    return {
        **room,
        "participants": participants,
        "messages": db.get_messages(room_id, since_seq),
        "message_count": db.count_messages(room_id),
        "cost": db.room_cost(room_id),
        # Who is mid-turn, so a client that reloaded can show it again.
        "working": working_agents(room_id, participants),
    }


def list_rooms(current_user) -> dict:
    kind, identity = _caller(current_user)
    if kind == "user" and getattr(current_user, "role", None) == "admin":
        rooms = db.list_all_rooms()
    else:
        rooms = db.list_rooms_for_participant(kind, identity)
    # ent#220 item 6: two batched reads for the whole page instead of two per
    # room. The Workspace sidebar renders every room the caller is in, so the
    # old shape was 2N round-trips on the one call that decides how fast the
    # sidebar paints.
    room_ids = [r["id"] for r in rooms]
    participants_by_room = db.list_participants_for_rooms(room_ids)
    counts_by_room = db.count_messages_for_rooms(room_ids)

    for r in rooms:
        participants = participants_by_room.get(r["id"], [])
        r["message_count"] = counts_by_room.get(r["id"], 0)
        r["participant_count"] = len(participants)
        # ent#359: the Workspace sidebar draws a room's participant avatars, so
        # a room row is visually distinct from a 1:1. This list already loads
        # every participant in order to count them and was discarding the
        # identities — leaving the caller to either fetch each room's detail
        # (N+1) or render no avatar at all. Costs nothing extra.
        r["agents"] = [
            p["identity"] for p in participants
            if p.get("kind") == "agent" and not p.get("left_at")
        ]
    return {"rooms": rooms}


def rename_room(current_user, room_id: str, name) -> dict:
    """A person titles a room (ent#473). Membership first — a non-member gets
    the uniform 404, never a 403 that confirms the room exists — then a person
    check: a member AGENT is reachable through its own MCP key and is a
    prompt-injection surface, and a room's name is what every participant
    reads it by, so an agent may talk in the room but not rename it (the
    ent#220 line, one notch below `_require_moderator`, since a rename is not
    a lifecycle or roster change and any human in the room may make it).
    Validated through the shared leaf so a thread and a room refuse the same
    titles for the same reasons, with the same named 400."""
    from services.chat_title import chat_title_problem, normalize_chat_title

    _require_membership(room_id, current_user)
    kind, _identity = _caller(current_user)
    if kind == "agent":
        raise RoomError(403, "not_a_person", "Only a person can rename a room")
    clean, reason = normalize_chat_title(name)
    if clean is None:
        raise RoomError(400, "invalid_title", chat_title_problem(reason, name), reason=reason)
    db.rename_room(room_id, clean)
    # A thin trigger carrying identifiers only (#918): listeners refetch the
    # room through the membership-scoped read, so the name never rides `/ws`.
    _broadcast("room_renamed", {"room_id": room_id})
    return {"room_id": room_id, "name": clean}


def close_room(current_user, room_id: str, reason: str = "user_closed") -> dict:
    _require_moderator(room_id, current_user)
    closed = db.close_room(room_id, reason, utc_now_iso())
    if closed:
        _post_system(room_id, f"Room closed ({reason}).")
        _broadcast("room_closed", {"room_id": room_id, "stop_reason": reason})
    return {"room_id": room_id, "closed": True, "already_closed": not closed}


def add_participant(current_user, room_id: str, agent_name: str,
                    role: str = "member") -> dict:
    room = _require_moderator(room_id, current_user)
    if room["status"] != "open":
        raise RoomError(410, "room_closed", "This room is closed")
    _assert_can_reach_agent(current_user, agent_name)
    # Re-check the cap here, not only at creation: repeated add_participant would
    # otherwise grow a room without bound (ent#220). A no-op re-add of an already
    # active agent is exempt; a rejoin still has to fit under the cap.
    already = db.get_participant(room_id, "agent", agent_name)
    if not (already and not already.get("left_at")):
        active_agents = sum(1 for p in db.list_participants(room_id)
                            if p["kind"] == "agent" and not p.get("left_at"))
        if active_agents >= MAX_PARTICIPANTS:
            raise RoomError(422, "participant_cap",
                            f"At most {MAX_PARTICIPANTS} agent participants")
    db.add_participant(room_id, "agent", agent_name, role, utc_now_iso())
    _post_system(room_id, f"{agent_name} joined the room.")
    return {"room_id": room_id, "agent": agent_name, "added": True}


def remove_participant(current_user, room_id: str, agent_name: str) -> dict:
    _require_moderator(room_id, current_user)
    removed = db.remove_participant(room_id, "agent", agent_name, utc_now_iso())
    if removed:
        _post_system(room_id, f"{agent_name} left the room.")
    return {"room_id": room_id, "agent": agent_name, "removed": removed}


# --- budgets -----------------------------------------------------------------

def _enforce_budgets(room: dict) -> None:
    """Checked at the POST boundary so a doomed turn is never dispatched.

    Deliberately boundary-only: an in-flight turn is never killed, so its
    response still lands and overshoot is bounded by one turn (the loop-budget
    semantics from #1155/#1156).

    The message budget counts CONVERSATIONAL messages only
    (``count_budget_messages``) — system lines are the room's own bookkeeping
    and charging them to the operator's budget made ``max_messages=1`` a room
    that died before anyone spoke (ent#218).
    """
    room_id = room["id"]
    if room["status"] != "open":
        raise RoomError(410, "room_closed", "This room is closed",
                        stop_reason=room.get("stop_reason"))

    expires_at = room.get("expires_at")
    if expires_at and expires_at <= utc_now_iso():
        _close_with(room_id, "expired")
        raise RoomError(410, "room_closed", "This room has expired", stop_reason="expired")

    if db.count_budget_messages(room_id) >= int(room.get("max_messages") or DEFAULT_MAX_MESSAGES):
        _close_with(room_id, "max_messages")
        raise RoomError(410, "room_closed", "This room reached its message budget",
                        stop_reason="max_messages")

    cap = room.get("max_cost_usd")
    if cap is not None and db.room_cost(room_id) >= float(cap):
        _close_with(room_id, "max_cost")
        raise RoomError(410, "room_closed", "This room reached its cost budget",
                        stop_reason="max_cost")


def _budget_exceeded_reason(room: dict) -> Optional[str]:
    """Non-raising budget check for the OVERSHOOT path (ent#218).

    ``_enforce_budgets`` rejects a NEW inbound post to a full room. But an agent
    reply is an *in-flight* turn that already ran — its response must still land
    (the documented overshoot-by-one), so landing it can't be gated by a raise
    that would propagate a 410 to the human and discard a reply we already paid
    for. This returns the stop_reason once that reply has landed and tipped the
    room over budget (so the caller closes the room), or None."""
    room_id = room["id"]
    if db.count_budget_messages(room_id) >= int(room.get("max_messages") or DEFAULT_MAX_MESSAGES):
        return "max_messages"
    cap = room.get("max_cost_usd")
    if cap is not None and db.room_cost(room_id) >= float(cap):
        return "max_cost"
    return None


def _close_with(room_id: str, reason: str) -> None:
    if db.close_room(room_id, reason, utc_now_iso()):
        _post_system(room_id, f"Room closed ({reason}).")
        _broadcast("room_closed", {"room_id": room_id, "stop_reason": reason})


# --- messages + the turn engine ----------------------------------------------

def _post_system(room_id: str, content: str) -> int:
    """System lines are never budget-gated — they explain WHY a room stopped, so
    refusing to write one at the cap would hide the reason."""
    seq = db.append_message(uuid.uuid4().hex, room_id, "system", None,
                            content, [], "system", None, utc_now_iso())
    _broadcast("room_message", {"room_id": room_id, "seq": seq})
    return seq


def _broadcast(event: str, payload: dict) -> None:
    """Thin WS payload — ids only; the client refetches over the access-controlled
    REST. ``/ws`` is unfiltered, and a room transcript is not public (#918)."""
    try:
        import asyncio
        from main import manager  # type: ignore

        coro = manager.broadcast({"type": event, **payload})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro)
        _WS_TASKS.add(task)
        task.add_done_callback(_WS_TASKS.discard)
    except Exception as e:  # noqa: BLE001 — a broadcast must never fail a post
        logger.debug("room ws broadcast skipped: %s", e)


_WS_TASKS: set = set()


def resolve_mentions(content: str, participants: list[dict]) -> list[str]:
    """@names that are agent participants of THIS room. An @name that isn't in
    the room is left as plain text — a mention can never reach outside."""
    names = {p["identity"] for p in participants
             if p["kind"] == "agent" and not p.get("left_at")}
    found, seen = [], set()
    for raw in _MENTION_RE.findall(content or ""):
        if raw in names and raw not in seen:
            seen.add(raw)
            found.append(raw)
    return found


def _join_mentioned_newcomers(current_user, room_id: str, room: dict,
                              content: str, participants: list[dict]) -> list[str]:
    """Add @mentioned agents that are not yet in this room, and return them.

    Reachability is checked per name and is NON-raising: an @name the caller
    cannot reach — or one that is not an agent at all — stays plain text, which
    is what `resolve_mentions` already does with it. Raising instead would turn
    the composer into an oracle for which agent names exist on the instance, and
    would fail a message whose ONLY problem is a word that looks like a handle.

    The participant cap is re-checked per addition rather than once: three
    newcomers mentioned in one message must not step over it together.
    """
    if room.get("status") != "open":
        return []

    present = {p["identity"] for p in participants
               if p["kind"] == "agent" and not p.get("left_at")}
    candidates, seen = [], set()
    for raw in _MENTION_RE.findall(content or ""):
        if raw not in present and raw not in seen:
            seen.add(raw)
            candidates.append(raw)
    if not candidates:
        return []

    active = len(present)
    joined = []
    for name in candidates:
        if active >= MAX_PARTICIPANTS:
            logger.info(
                "room %s: not joining @%s — participant cap %d reached",
                room_id, name, MAX_PARTICIPANTS,
            )
            break
        try:
            _assert_can_reach_agent(current_user, name)
        except Exception:  # noqa: BLE001 — unreachable / not an agent ⇒ plain text
            continue
        try:
            db.add_participant(room_id, "agent", name, "member", utc_now_iso())
            _post_system(room_id, f"{name} joined the room.")
        except Exception:  # noqa: BLE001 — a failed join must not fail the message
            logger.warning("room %s: could not join @%s", room_id, name, exc_info=True)
            continue
        active += 1
        joined.append(name)
    return joined


def _format_delta(messages: list[dict]) -> str:
    """Sender-labeled transcript — the ONLY thing an agent sees of the room."""
    lines = []
    for m in messages:
        who = m.get("sender_identity") or ("System" if m["sender_kind"] == "system" else "?")
        # ent#362: a workspace client is a human too. Without this an agent
        # reads a bare email as an unlabelled participant kind and cannot tell
        # a customer in the room from another agent.
        if m["sender_kind"] in ("user", WORKSPACE_KIND):
            who = f"{who} (human)"
        lines.append(f"{who}: {m.get('content') or ''}")
    return "\n".join(lines)


def _build_turn_prompt(room: dict, agent_name: str, delta: list[dict], cold: bool) -> str:
    header = (
        f"You are participating in the Trinity room \"{room['name']}\""
        + (f" — topic: {room['topic']}" if room.get("topic") else "")
        + ".\n\n"
        "Other agents and people are in this room. You were @mentioned, so it is "
        "your turn to reply. Reply with your message only — it will be posted to "
        "the room as you. To bring in another participant, @mention them by name.\n\n"
    )
    if cold:
        header += ("[You are joining the conversation now — here is the recent "
                   "transcript for context]\n")
    else:
        header += "[New messages since your last turn]\n"
    return header + _format_delta(delta)


async def post_message(current_user, room_id: str, content: str,
                       _chain_depth: int = 0,
                       _sender_override: Optional[tuple[str, str]] = None,
                       _execution_id: Optional[str] = None) -> dict:
    """Append a message and wake the agents it @mentions.

    ``_sender_override`` / ``_execution_id`` are used when the engine posts an
    agent's reply back into the room; external callers never set them.
    """
    content = (content or "").strip()
    if not content:
        raise RoomError(422, "empty_message", "Message content is required")
    if len(content) > MAX_CONTENT_CHARS:
        raise RoomError(413, "message_too_large",
                        f"Message exceeds {MAX_CONTENT_CHARS} characters")

    room = _require_membership(room_id, current_user)

    # A human/external post is INBOUND — reject it if the room is closed, expired
    # or already at budget. An agent reply (``_sender_override`` set) is an
    # in-flight turn that already ran; its response must still land (the
    # overshoot-by-one this module documents), so it is NEVER gated by a raise —
    # a 410 here would propagate to the human whose post triggered the cascade,
    # for a message that already landed, and discard a reply we already paid for
    # (ent#218). We only skip an agent reply if the room already closed (a prior
    # reply tripped the budget, or a close raced in).
    is_agent_reply = _sender_override is not None
    if is_agent_reply:
        if room["status"] != "open":
            return {"room_id": room_id, "seq": None, "mentions": [], "woke": []}
    else:
        _enforce_budgets(room)

    sender_kind, sender_identity = _sender_override or _caller(current_user)
    participants = db.list_participants(room_id)

    # ent#361 AC#4: a HUMAN @mentioning someone not in the room brings them in.
    # Deliberately not offered to agents (`is_agent_reply`): an agent that could
    # pull arbitrary agents into a room is both a spend amplifier and a
    # prompt-injection lever — a compromised workspace could assemble a room of
    # every agent its operator can reach. A human's mention is a decision; an
    # agent's is text it generated.
    if not is_agent_reply:
        joined = _join_mentioned_newcomers(current_user, room_id, room, content, participants)
        if joined:
            participants = db.list_participants(room_id)

    mentions = resolve_mentions(content, participants)

    seq = db.append_message(uuid.uuid4().hex, room_id, sender_kind, sender_identity,
                            content, mentions, "message", _execution_id, utc_now_iso())
    _broadcast("room_message", {"room_id": room_id, "seq": seq})

    # Landing an agent's overshoot reply may exhaust the budget: close the room
    # now (with the visible system line) and stop the cascade — no raise, so the
    # human's original post still returns normally (ent#218).
    if is_agent_reply:
        reason = _budget_exceeded_reason(room)
        if reason:
            _close_with(room_id, reason)
            return {"room_id": room_id, "seq": seq, "mentions": mentions, "woke": []}

    # An agent never re-wakes itself: that is the cycle-break at the root.
    targets = [m for m in mentions if m != sender_identity]

    # ent#362 AC#6: membership is the grant, but a REVOKED share must stop
    # future wakes — checked here, at wake time, not at join time. The
    # transcript stays (the record is shared and permanent); what goes away is
    # the ability to keep spending the agent's time. Only workspace clients are
    # re-checked: a platform member's access was already resolved by the
    # platform ACL when the room was created.
    if targets and getattr(current_user, "is_portal", False):
        from client_portal.service import agent_on_roster
        email = _workspace_identity(current_user)
        include_owned = bool(getattr(current_user, "is_platform", False))
        still_reachable = [t for t in targets
                           if agent_on_roster(t, email, include_owned)]
        if len(still_reachable) != len(targets):
            revoked = [t for t in targets if t not in still_reachable]
            _post_system(room_id,
                         f"{', '.join(revoked)} could not be reached "
                         f"(no longer shared with the sender).")
            targets = still_reachable

    # ent#362 AC#4: a per-participant wake cap. Counted per ROOM so one noisy
    # conversation cannot exhaust a participant's budget everywhere else, and
    # charged per TARGET, because the cost is one agent turn per target.
    if targets:
        targets = _apply_wake_cap(room_id, sender_kind, sender_identity, targets)

    if targets and _chain_depth >= ROOM_MAX_CHAIN_DEPTH:
        _post_system(room_id,
                     f"Mention chain stopped at depth {ROOM_MAX_CHAIN_DEPTH} "
                     f"(would have woken {', '.join(targets)}).")
        targets = []

    for agent_name in targets:
        # ent#220 item 2 — SHIELDED against client-disconnect cancellation.
        #
        # The chain runs inside the HTTP POST, so when the poster's browser goes
        # away Starlette cancels the request task and `CancelledError` (a
        # BaseException since 3.8) sails past every `except Exception` in this
        # module. Unshielded that killed the chain wherever it happened to be:
        # a turn already dispatched and BILLED lost its reply, the remaining
        # mention targets were never woken, and the room showed no line saying
        # so — the transcript simply stopped, which in a shared room reads as
        # "the agent ignored me".
        #
        # `shield` makes the disconnect stop propagating INTO the wake while the
        # wake itself keeps running to completion on the loop. It is not a
        # promise of immortality: at process shutdown the task is cancelled like
        # any other, and the `finally` in `_wake_agent` still clears the
        # working marker. What it buys is that a closed tab can no longer
        # truncate a conversation other participants are watching.
        #
        # This does NOT make the request short — a deep chain can still hold the
        # connection for minutes, which is the #1083 pinned-coroutine class and
        # needs the fire-and-forget rework tracked separately. Shielding is the
        # half that stops data loss; the held request is a latency problem.
        try:
            await asyncio.shield(
                _wake_agent(current_user, room_id, agent_name, _chain_depth + 1)
            )
        except asyncio.CancelledError:
            # The caller went away. The shielded wake keeps running; stop
            # starting NEW ones, and leave a line so the room explains itself.
            remaining = targets[targets.index(agent_name) + 1:]
            if remaining:
                _post_system(room_id,
                             "The sender disconnected; "
                             f"{', '.join(remaining)} were not woken.")
            raise

    return {"room_id": room_id, "seq": seq, "mentions": mentions,
            "woke": [t for t in targets]}


# --- who is mid-turn, readable after a reload -------------------------------
#
# Every marker carries a TTL just over the turn timeout, so a backend that dies
# mid-turn cannot leave an agent looking busy forever — the room self-heals
# instead of needing a sweep.

def _working_key(room_id: str, agent_name: str) -> str:
    return f"room_working:{room_id}:{agent_name}"


def _working_ttl() -> int:
    return ROOM_TURN_TIMEOUT_SECONDS + 30


def _mark_agent_working(room_id: str, agent_name: str) -> None:
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is not None:
            client.set(_working_key(room_id, agent_name), "1", ex=_working_ttl())
    except Exception as e:  # noqa: BLE001 — a missing indicator must not fail a turn
        logger.warning("room %s: working-marker SET failed for %s: %s", room_id, agent_name, e)


def _clear_agent_working(room_id: str, agent_name: str) -> None:
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is not None:
            client.delete(_working_key(room_id, agent_name))
    except Exception as e:  # noqa: BLE001
        logger.warning("room %s: working-marker DEL failed for %s: %s", room_id, agent_name, e)


def working_agents(room_id: str, participants: list[dict]) -> list[str]:
    """Which agents in this room are mid-turn right now.

    Read per known participant rather than by scanning the keyspace: the caller
    already has the roster, and a SCAN on a shared Redis is a cost paid by every
    poll of every open room. Returns [] when Redis is unavailable — showing no
    indicator is a smaller lie than showing a permanent one.
    """
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is None:
            return []
        names = [p["identity"] for p in participants
                 if p.get("kind") == "agent" and not p.get("left_at")]
        return [n for n in names if client.get(_working_key(room_id, n)) is not None]
    except Exception as e:  # noqa: BLE001
        logger.warning("room %s: working-marker read failed: %s", room_id, e)
        return []


def _apply_wake_cap(room_id: str, sender_kind: str, sender_identity: str,
                    targets: list[str]) -> list[str]:
    """Trim ``targets`` to what this participant may still wake in this window.

    Fail-OPEN on a limiter error: a room going quiet because Redis hiccuped is
    a worse failure than one over-budget wake. The message itself has already
    landed either way — the cap governs whose time gets spent, not what is said.
    """
    from services import rate_limiter

    allowed = []
    for target in targets:
        key = f"room_wake:{room_id}:{sender_kind}:{sender_identity}"
        try:
            ok = rate_limiter.check(key, ROOM_WAKE_LIMIT_PER_PARTICIPANT,
                                    ROOM_WAKE_LIMIT_WINDOW_SECONDS)
        except Exception as e:  # noqa: BLE001
            logger.warning("room %s: wake-cap check failed (%s) — allowing", room_id, e)
            ok = True
        if ok:
            allowed.append(target)
    if len(allowed) != len(targets):
        _post_system(room_id,
                     f"{sender_identity} reached the wake limit "
                     f"({ROOM_WAKE_LIMIT_PER_PARTICIPANT} per "
                     f"{ROOM_WAKE_LIMIT_WINDOW_SECONDS // 60} minutes); "
                     f"{len(targets) - len(allowed)} mention(s) did not wake anyone.")
    return allowed


async def _wake_agent(current_user, room_id: str, agent_name: str, chain_depth: int) -> None:
    """One agent turn: delta -> execute_task -> auto-post the reply.

    Every wake is an ORDINARY execution through the standard path, so slots, the
    circuit breaker, cost and observability all come for free.
    """
    participant = db.get_participant(room_id, "agent", agent_name)
    if not participant or participant.get("left_at"):
        return

    room = db.get_room(room_id)
    if not room or room["status"] != "open":
        return

    cursor = int(participant.get("last_read_seq") or 0)
    cached = participant.get("cached_session_id")
    delta = db.get_messages(room_id, since_seq=cursor)
    cold = not cached
    if cold:
        delta = db.get_recent_messages(room_id, ROOM_COLD_CONTEXT_MESSAGES)
    if not delta:
        return

    top_seq = max(m["seq"] for m in delta)
    # The WS broadcast reaches clients that are CONNECTED right now. A client
    # that reloads mid-turn missed it and has nothing to re-derive the state
    # from — the room looked idle while two agents were thinking. So the state
    # is also recorded where a reloading client can read it back.
    _mark_agent_working(room_id, agent_name)
    _broadcast("room_participant_state",
               {"room_id": room_id, "identity": agent_name, "state": "working"})

    from services.task_execution_service import get_task_execution_service

    try:
        result = await get_task_execution_service().execute_task(
            agent_name=agent_name,
            message=_build_turn_prompt(room, agent_name, delta, cold),
            triggered_by="room",
            source_user_email=getattr(current_user, "email", None),
            timeout_seconds=ROOM_TURN_TIMEOUT_SECONDS,
            resume_session_id=cached,
            persist_session=True,
        )
    except asyncio.CancelledError:
        # ent#220 item 2: cancellation is a BaseException, so it used to slip
        # past the handler below and leave the room with no line at all — the
        # one failure mode that is invisible to everyone watching. The turn is
        # genuinely over (shutdown, or a cancel that outlived the shield), so
        # say so and re-raise: swallowing cancellation is how a process refuses
        # to die.
        logger.warning("room %s: turn for %s was cancelled", room_id, agent_name)
        _post_system(room_id, f"{agent_name}'s turn was interrupted.")
        raise
    except Exception as e:  # noqa: BLE001 — a failed turn is VISIBLE, never silent
        logger.warning("room %s: turn for %s raised: %s", room_id, agent_name, e)
        _post_system(room_id, f"{agent_name} could not respond ({type(e).__name__}).")
        return
    finally:
        _clear_agent_working(room_id, agent_name)
        _broadcast("room_participant_state",
                   {"room_id": room_id, "identity": agent_name, "state": "idle"})

    status = getattr(result, "status", None)
    reply = (getattr(result, "response", "") or "").strip()

    if status in ("failed", "cancelled") or not reply:
        # A dead resume handle is the common cause — drop it so the next wake is
        # cold instead of failing the same way forever (Session-tab idiom).
        if cached:
            db.clear_cached_session(room_id, agent_name)
        err = (getattr(result, "error", "") or "").strip()
        _post_system(room_id,
                     f"{agent_name} could not respond"
                     + (f": {err[:200]}" if err else " (no response)."))
        return

    # POST FIRST, then advance the cursor (ent#220 item 1).
    #
    # The reverse order loses a reply we already paid for: if anything between
    # the advance and the append fails, the cursor says the agent has seen the
    # delta while its reply exists nowhere, and nothing ever re-delivers it —
    # billed, gone, silent. Posting first inverts the failure into a benign one:
    # the reply is durable, and an advance that does not happen merely re-shows
    # the agent a delta on its next wake.
    #
    # Both orders have a failure mode; this one costs a repeated read, the other
    # costs money and a lost answer.
    await post_message(
        current_user, room_id, reply,
        _chain_depth=chain_depth,
        _sender_override=("agent", agent_name),
        _execution_id=getattr(result, "execution_id", None),
    )

    # Cursor advances only on SUCCESS — a failed turn re-delivers its delta.
    db.advance_read_cursor(room_id, agent_name, top_seq, getattr(result, "session_id", None))


def sweep_expired_rooms() -> int:
    """Close rooms past their TTL. Idempotent; safe to call from a loop."""
    closed = 0
    for room in db.open_rooms_needing_sweep(utc_now_iso()):
        if db.close_room(room["id"], "expired", utc_now_iso()):
            _post_system(room["id"], "Room closed (expired).")
            closed += 1
    return closed
