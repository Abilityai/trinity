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
any other person; one with no recorded creator is the owner's. A schedule that
cannot be read (gone, another agent's, or a failed read) has no known creator,
so the run fails closed to a shared `person` with no account: never a visitor,
whom `visitor_shell` would give the shell and the owner-tier routes (#4281).

A Workspace turn carries no `source_user_id` either: it is stamped with the
signed-in person's email (`source_channel="portal"`). That email is the
platform's own record of who is chatting, so the turn is rated as that person —
the owner or an admin is `owner`, anyone else a shared `person`, never a
visitor. Only that channel's email counts: a public-link turn shares
`triggered_by="public"`, but its email is what the visitor typed, and reading
it as an account would let a visitor claim the owner's tier.

Two more answers are `owner` with a kind, so the person stays on record
(trinity-enterprise#1148):

  member     a private module may rate a person the owner of ONE agent, the one
             created for them, through `register_person_tier_provider`. Without
             a registered provider nothing changes. The person's reach on every
             other agent is what it was.
  delegated  a turn another agent dispatched (`source_agent_name` set) for a
             person P who cannot open this agent at all runs at this agent's
             own tier: the callee does the hub's work, and P only ever drove the
             caller's prompt. Bounded by a clamp: only when the calling agent's
             owner is owner or admin of this agent, so a share holder on A never
             gets more on B than A's owner has there. P who CAN open this agent
             keeps P's own tier here. A self-call (caller == this agent) is
             never delegation: it rates P's own tier here. Every lookup fails
             closed to `share`.

A turn such an answer dispatches carries that person forward
(`turn_origin_user_id`), and the delegation rule decides again at every hop.

What tier a schedule's runs get, and why, has one home: `schedule_run_tier`
(#4281). `turn_actor` reads its tier half, so the turn's rating and every
schedule read agree. The cause is a separate, total function that the fences
never call: the fences run `turn_actor` on every agent-key request, and a new
lookup that can raise must not become a 500 there.
"""
import logging
import unicodedata
from typing import Callable, Dict, Iterable, NamedTuple, Optional, Tuple

from config import PORTAL_SOURCE_CHANNEL

logger = logging.getLogger(__name__)

OWNER = "owner"
SHARE = "share"
UNKNOWN = "unknown"

PERSON = "person"
VISITOR = "visitor"
# Kinds of an OWNER answer that is a person's by rule (trinity-enterprise#1148):
# the consumers (the guard hook, the runtime gate) read `tier` and `shell` and
# map any kind but `person`/`visitor` to None, so these change nothing there.
MEMBER = "member"
DELEGATED = "delegated"

# Every execution-row column `turn_actor` reads. `db.get_running_turn_attribution`
# (the ledger path) must select each of them, or a turn rates differently from
# the ledger than from its own row; `test_ent1148_ledger_columns_guard.py` holds
# the SELECT to this tuple.
LEDGER_COLUMNS = (
    "id", "agent_name", "source_user_id", "schedule_id", "triggered_by",
    "source_channel", "source_user_email", "source_agent_name",
)

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


# --- the person-tier provider (an optional private module's answer) ---------

PersonTierProvider = Callable[[object, int, str], bool]

_person_tier_provider: Optional[PersonTierProvider] = None


def register_person_tier_provider(provider: PersonTierProvider) -> None:
    """Register the one provider that may rate a person the owner of an agent
    created for them: `provider(db, user_id, agent_name) -> bool`. Last
    registration wins (a reload re-registers). Only a strict `True` elevates;
    a provider that raises rates nobody."""
    global _person_tier_provider
    _person_tier_provider = provider


def clear_person_tier_providers() -> None:
    global _person_tier_provider
    _person_tier_provider = None


def _provider_rates_owner(db, user_id: int, agent_name: str) -> bool:
    provider = _person_tier_provider
    if provider is None:
        return False
    try:
        return provider(db, user_id, agent_name) is True
    except Exception:  # noqa: BLE001 - fail closed: an unreadable answer elevates nobody
        return False


def _person_tier(db, user_id, agent_name: str) -> Tuple[str, Optional[str], Optional[int]]:
    """(tier, kind, person) for a person's turn: the owner or an admin first
    (`(owner, None, None)`, as before), then the registered provider's member,
    else a shared person."""
    if _user_tier(db, user_id, agent_name) == OWNER:
        return OWNER, None, None
    person = _int_or_none(user_id)
    if person is not None and _provider_rates_owner(db, person, agent_name):
        return OWNER, MEMBER, person
    return SHARE, PERSON, person


def _delegation_runs_as_owner(db, agent_name: str, execution, person: Optional[int]) -> bool:
    """Rule A with its clamp: a row another agent dispatched for `person`, who
    cannot open `agent_name` at all, runs at this agent's own tier — when the
    calling agent's owner is owner or admin here. Every lookup fails closed.
    A self-call (the caller IS this agent) is never delegation: the person
    keeps their own tier here, so a self fan-out never rates above share."""
    caller = _field(execution, "source_agent_name")
    if not isinstance(caller, str) or not caller.strip() or person is None:
        return False
    if caller == agent_name:
        return False
    try:
        user = db.get_user_by_id(person)
    except Exception:  # noqa: BLE001
        user = None
    username = (user or {}).get("username") if isinstance(user, dict) else getattr(user, "username", None)
    if not username:
        return False
    try:
        opens = db.can_user_access_agent(username, agent_name)
    except Exception:  # noqa: BLE001 - fail closed: an unreadable answer keeps the person's tier
        return False
    if opens is not False:
        return False
    try:
        caller_owner = _field(db.get_agent_owner(caller), "owner_id")
    except Exception:  # noqa: BLE001
        return False
    if caller_owner is None:
        return False
    return _user_tier(db, caller_owner, agent_name) == OWNER


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


# --- what tier a schedule's runs get, and why (#4281) ---------------------------

# Why a schedule's runs have the tier they have. Owner tier:
CREATOR_OWNER_TIER = "creator_owner_tier"      # the creator is an admin or the agent's owner
NO_RECORDED_CREATOR = "no_recorded_creator"    # `owner_id` is NULL (no live row has one)
# A shared person:
CREATOR_NOT_OWNER_TIER = "creator_not_owner_tier"  # the creator is someone else
AGENT_HAS_NO_OWNER = "agent_has_no_owner"      # no owner on record (its user row is gone)
CREATOR_MISSING = "creator_missing"            # no user row for the creator
SCHEDULE_UNREADABLE = "schedule_unreadable"    # run time only: the schedule cannot be read
# The cause could not be looked up. The tier is computed without it and stands.
CAUSE_UNKNOWN = "unknown"

# What fixes a shared-person rating. `recreate` until the take-over route
# exists; an agent with no owner on record 404s every owner route, admins
# included, so only restoring its owner helps there.
REMEDY_RECREATE = "recreate"
REMEDY_REPAIR_AGENT_OWNER = "repair_agent_owner"
_REMEDIES = {
    CREATOR_NOT_OWNER_TIER: REMEDY_RECREATE,
    CREATOR_MISSING: REMEDY_RECREATE,
    AGENT_HAS_NO_OWNER: REMEDY_REPAIR_AGENT_OWNER,
}

SCHEDULE_LABEL_MAX = 80   # a schedule name, as the hook may show it to the model
CREATOR_LABEL_MAX = 64    # a creator's name, as the hook may show it to the model
READ_LABEL_MAX = 254      # a creator's name on a schedule read (an email username fits)


class RunTier(NamedTuple):
    """What tier a schedule's runs get, and why.

    `kind` is `person` for a share tier, `member` for an owner tier the
    registered provider gives (trinity-enterprise#1148), and None for the
    owner's own. `creator_id` is the schedule's `owner_id` (None when it has
    none, or the schedule could not be read), on an owner row too: it names the
    creator, not the person a turn is for, which `turn_actor` keeps None on the
    owner's own row."""
    tier: str
    kind: Optional[str]
    cause: str
    creator_id: Optional[int]


def _schedule_tier(db, agent_name: str, schedule) -> Tuple[str, Optional[str], Optional[int]]:
    """The tier half of `schedule_run_tier`: `(tier, kind, creator id)`.

    The one place the creator rule lives; `turn_actor` reads it. It raises when
    a lookup raises, as `turn_actor` always has."""
    if schedule is None or _field(schedule, "agent_name") != agent_name:
        # No known creator: a shared person, never a visitor (#4281).
        return SHARE, PERSON, None
    creator = _field(schedule, "owner_id")
    if creator is None:
        # No recorded creator: the owner's alone (the webhook rule, ent#875).
        return OWNER, None, None
    # Rated like any other person: the owner or an admin, then the registered
    # provider's member (trinity-enterprise#1148), else a shared person.
    tier, kind, _ = _person_tier(db, creator, agent_name)
    return tier, kind, _int_or_none(creator)


def schedule_run_cause(db, agent_name: str, schedule, tier: str) -> str:
    """Why a schedule rated `tier` (from `_schedule_tier`) has that tier.

    Total: a lookup that fails gives `unknown` and never raises, so it cannot
    change the tier or fail a read. Never called on the `turn_actor` path."""
    try:
        if schedule is None or _field(schedule, "agent_name") != agent_name:
            return SCHEDULE_UNREADABLE
        creator = _field(schedule, "owner_id")
        if creator is None:
            return NO_RECORDED_CREATOR
        if tier == OWNER:
            return CREATOR_OWNER_TIER
        uid = _int_or_none(creator)
        user = db.get_user_by_id(uid) if uid is not None else None
        if not user:
            return CREATOR_MISSING
        if user.get("role") == "admin":
            return CAUSE_UNKNOWN  # owner-tier now: a lookup changed since the tier was read
        owner = db.get_agent_owner(agent_name)
        if owner is None:
            return AGENT_HAS_NO_OWNER
        if owner.get("owner_username") == user.get("username"):
            return CAUSE_UNKNOWN  # the same: never name the owner as "someone else"
        return CREATOR_NOT_OWNER_TIER
    except Exception as exc:  # noqa: BLE001 - total by contract; the tier stands
        logger.warning("[#4281] schedule run cause lookup failed for agent %s: %s", agent_name, exc)
        return CAUSE_UNKNOWN


def schedule_run_tier(db, agent_name: str, schedule) -> RunTier:
    """What tier the runs of `schedule` get on `agent_name`, and why.

    `schedule` is a schedule row (anything with `agent_name` and `owner_id`), or
    None for one that cannot be read. Computed live, so a schedule that broke
    after it was created (the creator lost admin, the agent's owner record went)
    rates as it is now. `db` is the database or a `RunTierLookups` over it.

    Raises exactly when `turn_actor` would for the same schedule (the tier
    half is shared); the cause never raises (`schedule_run_cause`)."""
    tier, kind, creator = _schedule_tier(db, agent_name, schedule)
    return RunTier(tier, kind, schedule_run_cause(db, agent_name, schedule, tier), creator)


def run_schedule(db, agent_name: str, execution) -> Tuple[bool, Optional[object]]:
    """`(True, schedule-or-None)` for a schedule-backed run of `agent_name`: no
    person started it (`source_user_id` is empty) and it names a schedule. The
    schedule is None when it cannot be read. `(False, None)` for any other run.
    The same test `turn_actor` applies before it rates a run by its creator."""
    if execution is None or _field(execution, "agent_name") != agent_name:
        return False, None
    if _field(execution, "source_user_id") is not None:
        return False, None
    return _schedule_of(db, agent_name, execution)


def schedule_backed_run_tier(db, agent_name: str, execution) -> Optional[RunTier]:
    """`schedule_run_tier` for a schedule-backed run (`run_schedule`), None for
    any other run. Raises when the rating raises, as `schedule_run_tier` does."""
    is_run, schedule = run_schedule(db, agent_name, execution)
    if not is_run:
        return None
    return schedule_run_tier(db, agent_name, schedule)


def schedule_remedy(cause: Optional[str]) -> Optional[str]:
    """The code of what fixes a shared-person rating, or None."""
    return _REMEDIES.get(cause)


def _is_unprintable(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] == "C" or category in ("Zl", "Zp")


def bounded_label(value, limit: int) -> Optional[str]:
    """`value` with control and format characters removed, whitespace runs
    folded to one space, cut to `limit` characters; None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(" " if _is_unprintable(ch) else ch for ch in value).split())
    return text[:limit].rstrip() or None


def creator_label(user: Optional[Dict], *, for_turn: bool) -> Optional[str]:
    """How to name a schedule's creator: the display name, else the username.

    `for_turn` is the hook's answer, which anything holding the agent's key may
    read: an email (a name or a username) loses its domain there, and the label
    is cut shorter. None when there is no user."""
    if not user:
        return None
    limit = CREATOR_LABEL_MAX if for_turn else READ_LABEL_MAX
    for value in (user.get("name"), user.get("username")):
        if for_turn and isinstance(value, str) and "@" in value:
            value = value.split("@", 1)[0]
        label = bounded_label(value, limit)
        if label:
            return label
    return None


def turn_schedule_detail(db, agent_name: str, execution, tier: str) -> Dict[str, Optional[str]]:
    """The schedule fields of a turn-tier answer: `cause`, `schedule` (its
    name), `creator` and `remedy` for a schedule-backed shared turn; all None
    for any other turn. Never raises."""
    detail: Dict[str, Optional[str]] = {"cause": None, "schedule": None, "creator": None, "remedy": None}
    if tier != SHARE or _field(execution, "source_user_id") is not None:
        return detail
    has_schedule, schedule = _schedule_of(db, agent_name, execution)
    if not has_schedule:
        return detail
    cause = schedule_run_cause(db, agent_name, schedule, tier)
    detail["cause"] = cause
    detail["remedy"] = schedule_remedy(cause)
    if schedule is not None:
        detail["schedule"] = bounded_label(_field(schedule, "name"), SCHEDULE_LABEL_MAX)
    if cause in (CREATOR_NOT_OWNER_TIER, AGENT_HAS_NO_OWNER):
        try:
            detail["creator"] = creator_label(
                db.get_user_by_id(_int_or_none(_field(schedule, "owner_id"))), for_turn=True)
        except Exception as exc:  # noqa: BLE001 - a label is optional; the answer stands
            logger.warning("[#4281] creator label lookup failed for agent %s: %s", agent_name, exc)
    return detail


def turn_tier_answer(db, agent_name: str, execution_id: Optional[str]) -> Optional[Dict]:
    """The turn-tier route's answer, or None for a turn that is not this
    agent's (the route's 404).

    `shell` also answers whether a Gemini/Codex runtime may run the turn. No
    person id is part of the answer. A schedule-backed shared turn names its
    creator by label only (display name, else the username without any email
    domain): the refusal text needs it, and only the agent's own key reads it."""
    execution = db.get_execution(execution_id) if execution_id else None
    tier, kind, _ = turn_actor(db, agent_name, execution=execution)
    if tier == UNKNOWN:
        return None
    answer: Dict = {"tier": tier, "kind": kind, "shell": shell_allowed(db, agent_name, tier, kind)}
    answer.update(turn_schedule_detail(db, agent_name, execution, tier))
    return answer


class RunTierLookups:
    """The four lookups the rating reads, memoised for one request.

    `_user_tier` and `schedule_run_cause` run on it unchanged, so a list of
    schedules costs one user read per distinct creator and one owner read per
    agent. `preload` fills it in two bulk queries for a fleet read (#1265
    removed exactly this N+1 from `GET /api/ops/schedules`).

    `can_user_share_agent` is the database's rule (`db/agent_settings/
    sharing.py`) over the memo: an admin, or the username `get_agent_owner`
    names. A parity test drives both over one table of users."""

    def __init__(self, db):
        self._db = db
        self._users: Dict[int, Optional[Dict]] = {}
        self._by_username: Dict[str, Optional[Dict]] = {}
        self._owners: Dict[str, Optional[Dict]] = {}

    def _remember(self, user_id, user: Optional[Dict]) -> None:
        self._users[user_id] = user
        if user and isinstance(user.get("username"), str):
            self._by_username[user["username"]] = user

    def preload(self, agent_names: Iterable[str], creator_ids: Iterable) -> None:
        agents = [a for a in dict.fromkeys(agent_names) if a and a not in self._owners]
        if agents:
            owners = self._db.get_agent_owners(agents)
            for agent in agents:
                self._owners[agent] = owners.get(agent)
        ids = [i for i in dict.fromkeys(_int_or_none(c) for c in creator_ids)
               if i is not None and i not in self._users]
        if ids:
            users = self._db.get_users_by_ids(ids)
            for uid in ids:
                self._remember(uid, users.get(uid))

    def get_user_by_id(self, user_id):
        if user_id not in self._users:
            self._remember(user_id, self._db.get_user_by_id(user_id))
        return self._users[user_id]

    def get_user_by_username(self, username):
        if username not in self._by_username:
            self._by_username[username] = self._db.get_user_by_username(username)
        return self._by_username[username]

    def get_agent_owner(self, agent_name):
        if agent_name not in self._owners:
            self._owners[agent_name] = self._db.get_agent_owner(agent_name)
        return self._owners[agent_name]

    def can_user_share_agent(self, username, agent_name) -> bool:
        user = self.get_user_by_username(username)
        if not user:
            return False
        if user.get("role") == "admin":
            return True
        owner = self.get_agent_owner(agent_name)
        return bool(owner and owner.get("owner_username") == username)


# What a schedule read says about its runs (`ScheduleResponse.runs_as*`).
RUNS_AS = {OWNER: "owner", SHARE: "shared_person"}
_RESTRICTED = "no shell commands and no tools the owner added"
_RECREATE = ("To give them the owner's access, the agent's owner can create the schedule again "
             "as their own and disable this one.")
RUNS_AS_MESSAGES = {
    CREATOR_OWNER_TIER: "Its runs have the owner's access: it was created by the agent's owner or an admin.",
    NO_RECORDED_CREATOR: "Its runs have the owner's access: it has no recorded creator, so it is the owner's.",
    CREATOR_NOT_OWNER_TIER: (
        "Its runs have the access of the person who created it, who is not the agent's owner or an "
        f"admin: {_RESTRICTED}. {_RECREATE}"),
    CREATOR_MISSING: (
        "The account that created it no longer exists, so its runs have a shared person's access: "
        f"{_RESTRICTED}. {_RECREATE}"),
    AGENT_HAS_NO_OWNER: (
        "The agent has no owner on record, so the runs of a schedule an admin did not create have a "
        f"shared person's access: {_RESTRICTED}. An admin needs to restore the agent's owner."),
    CAUSE_UNKNOWN: (
        f"Its runs have a shared person's access: {_RESTRICTED}. Trinity could not check why."),
}


def schedule_read_fields(lookups, agent_name: str, schedule, *, show_creator: bool,
                         viewer_id) -> Dict:
    """The run-tier fields of a schedule read: `runs_as`, `runs_as_reason`,
    `created_by_you` and `created_by`.

    `runs_as` is None when the tier could not be computed, never a guessed
    `owner`. `created_by` is filled only when `show_creator` (the caller's
    tier decides that, not this function). Never raises."""
    creator = _int_or_none(_field(schedule, "owner_id"))
    fields: Dict = {
        "runs_as": None,
        "runs_as_reason": None,
        "created_by_you": creator is not None and creator == _int_or_none(viewer_id),
        "created_by": None,
    }
    try:
        rated = schedule_run_tier(lookups, agent_name, schedule)
    except Exception as exc:  # noqa: BLE001 - shown as "not computed", never as owner
        logger.warning("[#4281] could not rate schedule %s on %s: %s",
                       _field(schedule, "id"), agent_name, exc)
        rated = None
    if rated is not None and rated.tier in RUNS_AS:
        fields["runs_as"] = RUNS_AS[rated.tier]
        fields["runs_as_reason"] = {"code": rated.cause,
                                    "message": RUNS_AS_MESSAGES.get(rated.cause, RUNS_AS_MESSAGES[CAUSE_UNKNOWN])}
    if show_creator and creator is not None:
        try:
            fields["created_by"] = creator_label(lookups.get_user_by_id(creator), for_turn=False)
        except Exception as exc:  # noqa: BLE001 - a label is optional; the read stands
            logger.warning("[#4281] creator label lookup failed on %s: %s", agent_name, exc)
    return fields


def _workspace_person(db, execution) -> Tuple[bool, Optional[int]]:
    """`(is_workspace_turn, user id)` for a Workspace turn: the platform account
    of the signed-in person it was stamped with, or None when that email has no
    account (an email-only client the agent is shared with). Not a Workspace
    turn → `(False, None)`; an unreadable lookup → no account, which rates the
    turn as a shared person — never higher."""
    if _field(execution, "source_channel") != PORTAL_SOURCE_CHANNEL:
        return False, None
    email = _field(execution, "source_user_email")
    if not isinstance(email, str) or "@" not in email:
        return True, None
    try:
        user = db.get_user_by_email(email.strip().lower())
    except Exception:  # noqa: BLE001 - fail closed to a shared person
        user = None
    return True, _int_or_none((user or {}).get("id"))


def turn_actor(db, agent_name: str, execution_id: Optional[str] = None, *,
               execution=None) -> Tuple[str, Optional[str], Optional[int]]:
    """(tier, kind, person id) for a turn, by `execution_id` or by the row itself
    (`execution`: any object or dict carrying the `LEDGER_COLUMNS`). `kind` is
    `person` or `visitor` for a share turn, `member` or `delegated` for an
    owner turn that is a person's by rule, and None for the owner's own; the
    person id is the user the turn is for, if any."""
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
            tier, kind, creator = _schedule_tier(db, agent_name, schedule)
            # The person a share turn, or a member's owner turn, is for. The
            # owner's own turn names none: the creator is not who it acts for.
            return tier, kind, (creator if kind is not None else None)
        is_workspace, person = _workspace_person(db, execution)
        if is_workspace:
            if person is None:
                return SHARE, PERSON, None
            return _person_tier(db, person, agent_name)
        trigger = (_field(execution, "triggered_by") or "").lower()
        if trigger in OWNER_CONFIGURED_TRIGGERS:
            return OWNER, None, None
        return SHARE, VISITOR, None
    tier, kind, person = _person_tier(db, user_id, agent_name)
    if tier == SHARE and _delegation_runs_as_owner(db, agent_name, execution, person):
        return OWNER, DELEGATED, person
    return tier, kind, person


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
        elif not has_schedule:
            uid = _workspace_person(db, execution)[1]
    return _int_or_none(uid)


def _rated_rows(db, agent_name: str, rows):
    """`(row, turn_actor(row))` for ledger rows, each distinct attribution rated
    once: the ledger readers run on every agent-key request over every RUNNING
    row, and a busy callee's rows mostly share one person and caller."""
    memo = {}
    for row in rows:
        key = tuple(_field(row, c) for c in LEDGER_COLUMNS if c != "id")
        try:
            hit = memo.get(key)
        except TypeError:  # an unhashable stand-in value: rate it alone
            yield row, turn_actor(db, agent_name, execution=row)
            continue
        if hit is None:
            hit = memo[key] = turn_actor(db, agent_name, execution=row)
        yield row, hit


def running_share_people(db, agent_name: str) -> set:
    """The people (user ids; None for a turn with no person) of every RUNNING
    share-tier turn on `agent_name`. Read from the ledger, never from a request,
    so no header the agent sends can change it."""
    people = set()
    for row, (tier, _, _) in _rated_rows(db, agent_name, db.get_running_turn_attribution(agent_name)):
        if tier == SHARE:
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


def running_turn_restricts(db, agent_name: str) -> bool:
    """Is any RUNNING turn on `agent_name` one that `restricts_standing_work`?

    Read from the ledger, never from a request: an agent key that omits the
    turn header (a shell `curl`) or names an owner turn still meets the turns
    actually running, the way its reads do (`running_share_people`, ent#875).
    An unreadable ledger restricts — the fence fails closed."""
    try:
        rows = db.get_running_turn_attribution(agent_name)
    except Exception:  # noqa: BLE001 - fail closed
        return True
    for _, (tier, kind, _) in _rated_rows(db, agent_name, rows):
        if restricts_standing_work(db, agent_name, tier, kind):
            return True
    return False


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
    if acting is None or _is_the_owners_own(acting):
        return getattr(principal, "id", None)
    return acting[2]


def _is_the_owners_own(acting: Tuple[str, Optional[str], Optional[int]]) -> bool:
    """An OWNER answer with no person behind it. An owner turn that is a
    person's by rule (`member`, `delegated`) still dispatches as that person,
    so the next row records who the work is for and the delegation rule
    decides again there (trinity-enterprise#1148)."""
    tier, kind, person = acting[0], acting[1], acting[2]
    return tier == OWNER and not (kind in (MEMBER, DELEGATED) and person)


def turn_origin_email(db, principal, default: Optional[str]) -> Optional[str]:
    """The `source_user_email` matching `turn_origin_user_id`."""
    acting = acting_turn(db, principal)
    if acting is None or _is_the_owners_own(acting):
        return default
    person = acting[2]
    if not person:
        return None
    user = db.get_user_by_id(person) or {}
    return user.get("email") or user.get("username")
