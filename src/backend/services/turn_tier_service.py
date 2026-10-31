"""Which tier is a running turn acting for? (trinity-enterprise#926)

An agent acts for whoever chats with it. When the person is only shared the
agent, what the agent's key reads during that turn must not exceed what the API
gives that person.

The answer comes from the execution row (who started the turn), never from the
agent.

  owner    the person is the agent's owner or an admin, or the turn was started
           by something the owner configured (a schedule the owner created, a
           reminder, a loop)
  share    anyone else: a shared person, a run of a schedule a shared person
           created, or a turn from a channel, a public link or another agent
           that carries no platform user
  unknown  the execution does not exist, or belongs to another agent

A schedule run carries no `source_user_id`: the person behind it is the
schedule's creator (`agent_schedules.owner_id`). A share holder may create a
schedule (and mint its webhook), so rating every schedule run as `owner` would
let them run their own prompt as an owner-tier turn. The creator is rated like
any other person; a schedule that cannot be found fails closed to `share`.
"""
from typing import Optional

OWNER = "owner"
SHARE = "share"
UNKNOWN = "unknown"

# The `schedule_id` of a run that belongs to no schedule.
_NO_SCHEDULE_IDS = frozenset({"", "__manual__"})

# Turns with no platform user and no schedule that the OWNER configured.
# Anything else without a user (a public link, a channel, a paid call, another
# agent, voice) is an outside caller and gets the restricted tier.
OWNER_CONFIGURED_TRIGGERS = frozenset({
    "schedule", "system", "reminder", "loop", "self_task", "validation",
    "webhook", "manual", "event",
})


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


def turn_person(db, agent_name: str, execution) -> Optional[int]:
    """The platform user a turn is for: its `source_user_id`, or for a schedule
    run with none, the schedule's creator. None when the turn has no person."""
    uid = _field(execution, "source_user_id")
    if uid is None:
        has_schedule, schedule = _schedule_of(db, agent_name, execution)
        if has_schedule and schedule is not None:
            uid = _field(schedule, "owner_id")
    try:
        uid = int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None
    return uid if uid is not None and uid > 0 else None


def turn_tier(db, agent_name: str, execution_id: Optional[str] = None, *, execution=None) -> str:
    """Rate a turn by its row: pass `execution_id`, or the row itself as
    `execution` (any object or dict carrying `agent_name`, `source_user_id`,
    `schedule_id` and `triggered_by`)."""
    if execution is None:
        execution = db.get_execution(execution_id) if execution_id else None
    if execution is None or _field(execution, "agent_name") != agent_name:
        return UNKNOWN
    user_id = _field(execution, "source_user_id")
    if user_id is None:
        has_schedule, schedule = _schedule_of(db, agent_name, execution)
        if has_schedule:
            if schedule is None:
                return SHARE
            user_id = _field(schedule, "owner_id")
            if user_id is None:
                # No recorded creator: the owner's alone (the webhook rule).
                return OWNER
        else:
            trigger = (_field(execution, "triggered_by") or "").lower()
            return OWNER if trigger in OWNER_CONFIGURED_TRIGGERS else SHARE
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


def running_share_people(db, agent_name: str) -> set:
    """The people (user ids; None for a turn with no person) of every RUNNING
    share-tier turn on `agent_name`. Read from the ledger, never from a request,
    so no header the agent sends can change it."""
    people = set()
    for row in db.get_running_turn_attribution(agent_name):
        if turn_tier(db, agent_name, execution=row) == SHARE:
            people.add(turn_person(db, agent_name, row))
    return people
