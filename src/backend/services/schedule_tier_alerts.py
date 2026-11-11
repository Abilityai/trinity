"""A schedule whose runs are a shared person's: on the run, and to the operator (#4281).

A schedule's runs are rated by its creator (`turn_tier_service.schedule_run_tier`).
When the creator is neither an admin nor the agent's owner, every run is a shared
person's turn: no shell, no tools the owner added. Two readers need to know:

* **whoever reads the run.** The terminal write records `restricted_turn` in the
  run's `turn_integrity` JSON (`run_restriction` → `execution_integrity`). The
  run's status does not change.
* **the agent's operator.** One operator-queue row per agent x creator, kind
  `schedule_runs_restricted`, on the #3246 one-row-per-subject seam (`sync`).

`sync(agent_name, creator_id)` is the one owner of the observe/clear decision.
It reads that creator's live enabled schedules on the agent, rates them, and
files (or updates) the row when any runs as a shared person, or ends it when
none does. Because it always reads the live set, it is safe to call after any
write: a run that ends after its schedule was disabled reads the schedule as
disabled and files nothing (review X7). It never raises.

Its callers, each after its own write committed: the schedule routes (create,
update, enable, disable, delete), the admin pause / resume / emergency stop, the
dispatch of a schedule-backed run (`/api/internal/execute-task`, which also
covers runs whose terminal never reaches `apply_result`), the winning terminal
write in `apply_result`, and take-over (it must call `sync` for the former and
the new creator once the re-home commits). No background loop calls it, so a
reading has one writer; two that race (two requests) meet the seam's per-agent
lock and its partial unique index on the pending subject, so they stay one row.

The row names a person, so its prefix is owner-only and about-a-person
(`operator_queue_service`): a share holder and a machine key do not see it.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database import db
from services import execution_integrity, platform_alerts, turn_tier_service as tts

logger = logging.getLogger(__name__)

KIND = "schedule_runs_restricted"
PRIORITY = "medium"
MAX_CONTEXT_IDS = 20   # schedule ids the row's context lists (plus a count)
MAX_NAMED = 5          # schedules the row's text names

CLEARED = "cleared"

# Keyed by the remedy codes as they go over the wire, not by turn_tier_service's
# constants: `recreate` is the code until take-over exists, and the slice that
# adds take-over removes that constant (a clean merge must still import).
_REMEDY_TEXT = {
    "recreate": ("To give them the owner's access, the agent's owner can create them again "
                          "as their own and disable these."),
    "take_over": "To give them the owner's access, the agent's owner can take them over.",
    tts.REMEDY_REPAIR_AGENT_OWNER: "An admin needs to restore the agent's owner.",
}
_REMEDY_FALLBACK = "The agent's owner can decide whether they should run with the owner's access."


def _int_or_none(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ---- the run ------------------------------------------------------------------------------

def run_restriction(db_, agent_name: str, execution) -> Tuple[Optional[tts.RunTier], Optional[Dict]]:
    """`(rating, restricted_turn record)` for a run.

    The rating is None for a run that is not schedule-backed (a person started
    it, or it names no schedule). The record is None unless the rating is not
    owner. Never raises: a rating that cannot be computed gives `(None, None)`,
    so it never changes the run's own outcome (the #2467 belt)."""
    try:
        rated = tts.schedule_backed_run_tier(db_, agent_name, execution)
    except Exception as exc:  # noqa: BLE001 - a stamp must never break the terminal write
        logger.warning("[#4281] could not rate run %s on %s: %s",
                       getattr(execution, "id", None), agent_name, exc)
        return None, None
    if rated is None or rated.tier == tts.OWNER:
        return rated, None
    schedule_id = (execution.get("schedule_id") if isinstance(execution, dict)
                   else getattr(execution, "schedule_id", None))
    record = execution_integrity.restricted_turn_record(
        tier=rated.tier, kind=rated.kind, cause=rated.cause,
        schedule_id=schedule_id, creator_id=rated.creator_id)
    return rated, record


def run_restriction_by_id(db_, agent_name: str, execution_id: Optional[str]):
    """`run_restriction` for the run `execution_id`. Never raises."""
    if not execution_id:
        return None, None
    try:
        execution = db_.get_execution(execution_id)
    except Exception as exc:  # noqa: BLE001 - see run_restriction
        logger.warning("[#4281] could not read run %s on %s: %s", execution_id, agent_name, exc)
        return None, None
    return run_restriction(db_, agent_name, execution)


def after_run(agent_name: str, rated: Optional[tts.RunTier]) -> Optional[str]:
    """Decide the row again after a run's terminal write won. Never raises."""
    if rated is None or rated.creator_id is None:
        return None
    return sync(agent_name, rated.creator_id)


def at_dispatch(agent_name: str, execution_id: Optional[str]) -> Optional[str]:
    """Decide the row again when a schedule-backed run is dispatched. This is
    the one point every schedule run passes, whatever terminal path it takes
    later (the watchdog and the scheduler's own failure writers never reach
    `apply_result`). Never raises."""
    try:
        execution = db.get_execution(execution_id) if execution_id else None
        is_run, schedule = tts.run_schedule(db, agent_name, execution)
    except Exception as exc:  # noqa: BLE001 - an alert must never block a dispatch
        logger.warning("[#4281] could not read run %s on %s: %s", execution_id, agent_name, exc)
        return None
    if not is_run or schedule is None:
        return None
    return sync(agent_name, getattr(schedule, "owner_id", None))


# ---- the operator's row ------------------------------------------------------------------------

def _names(schedules: List[Any]) -> str:
    named = [tts.bounded_label(getattr(s, "name", None), tts.SCHEDULE_LABEL_MAX) or "(unnamed)"
             for s in schedules[:MAX_NAMED]]
    text = ", ".join(f"'{n}'" for n in named)
    more = len(schedules) - len(named)
    return f"{text} and {more} more" if more > 0 else text


def _creator_text(lookups, creator_id: int, cause: str) -> str:
    if cause == tts.CREATOR_MISSING:
        return "an account that no longer exists"
    try:
        label = tts.creator_label(lookups.get_user_by_id(creator_id), for_turn=False)
    except Exception as exc:  # noqa: BLE001 - a label is optional; the row stands
        logger.warning("[#4281] creator label lookup failed: %s", exc)
        label = None
    return label or f"user #{creator_id}"


def _why(cause: str) -> str:
    if cause == tts.CREATOR_MISSING:
        return "the account that created them no longer exists"
    if cause == tts.AGENT_HAS_NO_OWNER:
        return "the agent has no owner on record, so only an admin's schedules run with the owner's access"
    return "the person who created them is not this agent's owner or an admin"


def _text(agent_name: str, creator: str, cause: str, schedules: List[Any]) -> Tuple[str, str]:
    count = len(schedules)
    plural = "s" if count != 1 else ""
    title = f"{count} schedule{plural} on {agent_name} run without the shell"
    question = (
        f"{count} enabled schedule{plural} on {agent_name} created by {creator} "
        f"{'run' if count != 1 else 'runs'} with the access of a person the agent is shared with: "
        f"no shell commands and no tools the owner added. Why: {_why(cause)}. "
        f"Schedule{plural}: {_names(schedules)}. "
        f"{_REMEDY_TEXT.get(tts.schedule_remedy(cause), _REMEDY_FALLBACK)}"
    )
    return title, question


def sync(agent_name: str, creator_id) -> Optional[str]:
    """File, update or end the `schedule_runs_restricted` row for one agent x
    creator, from the creator's live enabled schedules on the agent.

    Returns the seam's outcome (`platform_alerts.OBSERVED_*`), `CLEARED` when
    no such schedule is left, or None when nothing was decided (no creator, a
    rating that could not be computed, or a cause that could not be looked
    up — an incomplete reading writes nothing). Never raises."""
    creator = _int_or_none(creator_id)
    if not agent_name or creator is None:
        return None
    try:
        lookups = tts.RunTierLookups(db)
        restricted: List[Tuple[Any, tts.RunTier]] = []
        for schedule in db.list_agent_schedules(agent_name):
            if not schedule.enabled or _int_or_none(schedule.owner_id) != creator:
                continue
            rated = tts.schedule_run_tier(lookups, agent_name, schedule)
            if rated.tier != tts.OWNER:
                restricted.append((schedule, rated))
        if not restricted:
            platform_alerts.clear(agent_name, KIND, creator)
            return CLEARED
        causes = [r.cause for _s, r in restricted if r.cause != tts.CAUSE_UNKNOWN]
        if not causes:
            logger.warning("[#4281] why %s's schedules on %s run restricted could not be looked up; "
                           "the alert is left as it is", creator, agent_name)
            return None
        cause = causes[0]
        schedules = [s for s, _r in restricted]
        title, question = _text(agent_name, _creator_text(lookups, creator, cause), cause, schedules)
        return platform_alerts.observe(
            agent_name, KIND, creator,
            title=title, question=question, priority=PRIORITY,
            context={
                "cause": cause,
                "remedy": tts.schedule_remedy(cause),
                "creator_id": creator,
                "schedule_ids": [s.id for s in schedules[:MAX_CONTEXT_IDS]],
                "schedule_count": len(schedules),
            },
        )
    except Exception:  # noqa: BLE001 - an alert must never break its caller
        logger.error("[#4281] could not decide the restricted-schedule alert for %s on %s",
                     creator, agent_name, exc_info=True)
        return None


def sync_for(agent_name: str, schedule) -> Optional[str]:
    """`sync` for the creator of `schedule`, a row the caller has just written.
    Never raises: the caller's write has committed, and an alert must not turn
    it into an error."""
    try:
        creator = schedule.owner_id
    except AttributeError:
        logger.error("[#4281] schedule %r on %s carries no creator; its alert is not decided",
                     getattr(schedule, "id", None), agent_name)
        return None
    return sync(agent_name, creator)


def sync_schedules(schedules: Iterable[Any]) -> None:
    """`sync` once per distinct agent x creator among `schedules` (a bulk
    `enabled` writer: the admin pause, resume and emergency stop). Never raises."""
    seen = set()
    for schedule in schedules:
        pair = (getattr(schedule, "agent_name", None), _int_or_none(getattr(schedule, "owner_id", None)))
        if pair in seen:
            continue
        seen.add(pair)
        sync(*pair)
