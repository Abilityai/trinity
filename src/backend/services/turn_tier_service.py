"""Which tier is a running turn acting for? (trinity-enterprise#926)

An agent acts for whoever chats with it. When the person is only shared the
agent, the agent's own tools must not do for them what the API refuses them.
The guard hook in the agent image asks this question, per turn, before it lets
a file tool reach a path the shared policy keeps at owner tier or lets a shell
command run; the Gemini/Codex runtimes ask it before they start at all.

The answer comes from the execution row (who started the turn), never from the
agent: the hook identifies the turn by `TRINITY_EXECUTION_ID` in the claude
process's launch environment, which the agent cannot change once it is running.
The agent key's own reads start from the ledger instead (`running_share_people`,
trinity-enterprise#875), so a header it omits or forges can only narrow them.

  owner    the person is the agent's owner or an admin, or the turn was started
           by something set up at owner tier (a schedule rates as its creator)
  share    anyone else, in one of two kinds:
             person   a person the agent is shared with
             visitor  a turn with no platform user: a channel (Slack, Telegram,
                      WhatsApp), a public link, voice, a paid call
  unknown  the execution does not exist, or belongs to another agent

The shell (and running on Gemini/Codex, which no hook can scope) is owner tier,
with one owner-controlled exception: visitors, by the per-agent guardrail
`visitor_shell` (default on, so channel agents keep working). A shared person's
turn never gets it (ruling 2026-10-19).

A schedule run carries no `source_user_id`: the person behind it is the
schedule's creator (`agent_schedules.owner_id`). A share holder may create a
schedule (and mint its webhook), so rating every schedule run as `owner` would
let them run their own prompt as an owner-tier turn. The creator is rated like
any other person; a schedule that cannot be found, or that belongs to another
agent, fails closed to `share`; one with no recorded creator is the owner's.
"""
from typing import Optional, Tuple

OWNER = "owner"
SHARE = "share"
UNKNOWN = "unknown"

PERSON = "person"
VISITOR = "visitor"

# Turns with no platform user that only something set up at owner tier starts.
# A schedule-backed row (schedule, webhook, retry, validation, manual) is rated
# by the schedule's creator first, below; these are what is left. `self_task` is
# not here: a self-task row always carries its caller, and a row that does not
# (a visitor's turn dispatching one) must not read as the owner's.
OWNER_CONFIGURED_TRIGGERS = frozenset({
    "schedule", "system", "reminder", "loop", "validation", "webhook", "manual", "event",
})

# The `schedule_id` of a run that belongs to no schedule (reminders, replays):
# there is no creator to look up.
_NO_SCHEDULE_IDS = frozenset({"", "__manual__"})

VISITOR_SHELL_KEY = "visitor_shell"


def _user_tier(db, user_id, agent_name: str) -> str:
    try:
        user = db.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        user = None
    if not user:
        return SHARE
    if user.get("role") == "admin":
        return OWNER
    if db.can_user_share_agent(user.get("username") or "", agent_name):
        return OWNER
    return SHARE


def _int_or_none(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _field(execution, name: str):
    if isinstance(execution, dict):
        return execution.get(name)
    return getattr(execution, name, None)


def _schedule_of(db, agent_name: str, execution):
    """`(has_schedule, schedule-or-None)` for a run's schedule on `agent_name`."""
    sid = _field(execution, "schedule_id")
    if not isinstance(sid, str) or sid in _NO_SCHEDULE_IDS:
        return False, None
    try:
        schedule = db.get_schedule(sid)
    except Exception:  # noqa: BLE001 — fail closed: an unreadable schedule rates as share
        schedule = None
    if schedule is None or _field(schedule, "agent_name") != agent_name:
        return True, None
    return True, schedule


def turn_actor(db, agent_name: str, execution_id: Optional[str] = None, *,
               execution=None) -> Tuple[str, Optional[str], Optional[int]]:
    """(tier, kind, person id) for a turn, by `execution_id` or by the row itself
    (`execution`: any object or dict carrying `agent_name`, `source_user_id`,
    `schedule_id` and `triggered_by`). `kind` is `person` or `visitor` for a
    share turn and None otherwise; the person id is the user a share turn is
    for, if any."""
    if execution is None:
        execution = db.get_execution(execution_id) if execution_id else None
    if execution is None or _field(execution, "agent_name") != agent_name:
        return UNKNOWN, None, None
    user_id = _field(execution, "source_user_id")
    if user_id is None:
        # A schedule-backed turn is its creator's: a person the agent is shared
        # with may create a schedule, and its runs must not be the owner's.
        has_schedule, schedule = _schedule_of(db, agent_name, execution)
        if has_schedule:
            if schedule is None:
                return SHARE, VISITOR, None
            creator = _field(schedule, "owner_id")
            if creator is None:
                # No recorded creator: the owner's alone (the webhook rule, ent#875).
                return OWNER, None, None
            if _user_tier(db, creator, agent_name) == OWNER:
                return OWNER, None, None
            return SHARE, PERSON, _int_or_none(creator)
        trigger = (_field(execution, "triggered_by") or "").lower()
        if trigger in OWNER_CONFIGURED_TRIGGERS:
            return OWNER, None, None
        return SHARE, VISITOR, None
    if _user_tier(db, user_id, agent_name) == OWNER:
        return OWNER, None, None
    return SHARE, PERSON, _int_or_none(user_id)


def turn_tier(db, agent_name: str, execution_id: Optional[str] = None, *, execution=None) -> str:
    return turn_actor(db, agent_name, execution_id, execution=execution)[0]


def turn_person(db, agent_name: str, execution) -> Optional[int]:
    """The platform user a turn is for: its `source_user_id`, or for a schedule
    run with none, the schedule's creator. None when the turn has no person."""
    uid = _field(execution, "source_user_id")
    if uid is None:
        has_schedule, schedule = _schedule_of(db, agent_name, execution)
        if has_schedule and schedule is not None:
            uid = _field(schedule, "owner_id")
    return _int_or_none(uid)


def running_share_people(db, agent_name: str) -> set:
    """The people (user ids; None for a turn with no person) of every RUNNING
    share-tier turn on `agent_name`. Read from the ledger, never from a request,
    so no header the agent sends can change it."""
    people = set()
    for row in db.get_running_turn_attribution(agent_name):
        if turn_tier(db, agent_name, execution=row) == SHARE:
            people.add(turn_person(db, agent_name, row))
    return people


def visitor_shell_allowed(db, agent_name: str) -> bool:
    """The owner's choice for channel and public-link visitors (default on)."""
    try:
        value = (db.get_guardrails_config(agent_name) or {}).get(VISITOR_SHELL_KEY, True)
    except Exception:  # noqa: BLE001 - an unreadable setting keeps the default
        value = True
    return value is not False


def shell_allowed(db, agent_name: str, tier: str, kind: Optional[str]) -> bool:
    """May this turn run shell commands (and run on Gemini/Codex at all)?"""
    if tier == OWNER:
        return True
    if tier == SHARE and kind == VISITOR:
        return visitor_shell_allowed(db, agent_name)
    return False


def restricts_standing_work(db, agent_name: str, tier: str, kind: Optional[str]) -> bool:
    """May this turn NOT set up work that later runs as the agent's owner
    (schedules, loops, reminders, events)? Same line as the shell: a shared
    person never; a visitor when the owner turned `visitor_shell` off."""
    return tier != OWNER and not shell_allowed(db, agent_name, tier, kind)


# --- the turn an agent's key is acting in (#2392 header, ent#926) ------------

MANUAL_EXECUTION_ID = "manual"


def acting_turn(db, principal) -> Optional[Tuple[str, Optional[str], Optional[int]]]:
    """`turn_actor` for the turn an AGENT-scoped key is acting in, or None when
    it is not acting in one (another principal, no header, `manual`). Computed
    once per request: the principal object is per request."""
    agent = getattr(principal, "agent_name", None)
    if getattr(principal, "mcp_scope", None) != "agent" or not (isinstance(agent, str) and agent):
        return None
    run = getattr(principal, "_acting_execution_id", None)
    if not run or run == MANUAL_EXECUTION_ID:
        return None
    cached = getattr(principal, "_acting_turn_cache", None)
    if cached is not None:
        return cached
    result = turn_actor(db, agent, run)
    try:
        principal._acting_turn_cache = result
    except (AttributeError, ValueError):  # a stand-in principal that will not take it
        pass
    return result


def turn_origin_user_id(db, principal) -> Optional[int]:
    """The `source_user_id` a turn this caller dispatches carries.

    An agent's key resolves to the agent's OWNER, so a turn it dispatches while
    acting for a shared person (a self-task, a call to another agent, a fan-out)
    would run as the owner. It carries that person instead, or no user for a
    visitor, so the child turn is no higher than its parent (ruling 2026-10-19)."""
    acting = acting_turn(db, principal)
    if acting is None or acting[0] == OWNER:
        return getattr(principal, "id", None)
    return acting[2]


def turn_origin_email(db, principal, default: Optional[str]) -> Optional[str]:
    """The `source_user_email` matching `turn_origin_user_id`."""
    acting = acting_turn(db, principal)
    if acting is None or acting[0] == OWNER:
        return default
    person = acting[2]
    if not person:
        return None
    user = db.get_user_by_id(person) or {}
    return user.get("email") or user.get("username")
