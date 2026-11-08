"""Turn-tier refusal counts on schedule-backed executions, read at read time (#4281).

A schedule whose creator is not owner-tier runs every tick as a shared person's
turn: the turn-tier hook refuses its shell and owner-added MCP calls, and the
run still ends `success`. The agent forwards each refusal as a guardrail event
(trinity-enterprise#165, `guardrail_event_service`), and this module turns
those events into two fields on the execution reads, so such a run no longer
reads as a clean `success`:

* `guardrail_refusals` -- how many of the run's tool calls the turn-tier hook
  refused, or `None` when no honest number exists;
* `guardrail_reporting` -- `None` when that number is complete, otherwise why
  it may not be (`READING_*` below).

Three rules from the event contract (ent#165 plan §6.4) are load-bearing:

* **Read time, never terminal time.** Events land up to ~2 s (p99 <= 5 s) after
  the hook fired, which can be after the run ended, so a count taken once at
  completion would miss them. This is evaluated on every read, one count query
  per page.
* **Only the turn-tier hook counts** (`by_hook_code["turn_tier_guard:*"]`). A
  credential redaction, or another hook's deny, never makes a run unclean.
* **"No events" is zero only when the agent is known to be able to send them**:
  a Claude Code agent that is reporting. An agent that never reported, or one on
  Gemini/Codex (no hooks: its forwarder sends keepalives only, so it reads
  `reporting`; review X12), reads `None`, unknown -- never 0, which would be a
  false "clean".

A `None` count always carries a non-None marker, except on a run with no
schedule, where both are `None`: the fields describe schedule-backed runs only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from services import guardrail_event_service
from utils.helpers import parse_iso_timestamp, utc_now

logger = logging.getLogger(__name__)

# The hook whose refusals make a schedule-backed run unclean.
COUNTED_HOOK = "turn_tier_guard"
# The only runtime whose agent has the hooks that emit those refusals.
HOOK_RUNTIME = "claude-code"
# A run still going, or ended this recently, may still have events in flight:
# the forwarder flushes <= 2 s after an append, p99 <= 5 s hook -> row (§6.2).
PENDING_SECONDS = 10

# `schedule_id` values that name no schedule. The same set as
# `turn_tier_service._NO_SCHEDULE_IDS`: every chat/API/task row is written with
# `__manual__` (`db/schedules/executions.py::create_task_execution`).
NO_SCHEDULE_IDS = frozenset({"", "__manual__"})

READING_UNAVAILABLE = "unavailable"
READING_NEVER = "never"
READING_NOT_CLAUDE_CODE = "not_claude_code"
READING_STALE = "stale"
READING_PENDING = "pending"

Reading = Tuple[Optional[int], Optional[str]]
NOT_SCHEDULE_BACKED: Reading = (None, None)


def is_schedule_backed(schedule_id: Any) -> bool:
    return isinstance(schedule_id, str) and schedule_id not in NO_SCHEDULE_IDS


def _turn_tier_refusals(entry: Optional[Dict[str, Any]]) -> int:
    codes = (entry or {}).get("by_hook_code") or {}
    return sum(n for key, n in codes.items() if key.startswith(f"{COUNTED_HOOK}:"))


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_iso_timestamp(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raise TypeError(f"completed_at of unexpected type {type(value).__name__}")


def reading_for(entry: Optional[Dict[str, Any]], reporting: Dict[str, Any],
                completed_at: Any, now: datetime) -> Reading:
    """`(guardrail_refusals, guardrail_reporting)` for one schedule-backed run.

    `entry` is the run's `count_by_execution` entry (None when the run has no
    events); `reporting` is the agent's reporter state. Markers, most severe
    first: `never` > `not_claude_code` > `stale` > `pending`.
    """
    n = _turn_tier_refusals(entry)
    if reporting.get("state") == "never":
        return None, READING_NEVER
    if reporting.get("runtime") != HOOK_RUNTIME:
        # A refusal that did arrive is a fact; its absence proves nothing here.
        return (n or None), READING_NOT_CLAUDE_CODE
    if reporting.get("state") != "reporting":
        return n, READING_STALE
    ended = _as_utc(completed_at)
    if ended is None or now - ended < timedelta(seconds=PENDING_SECONDS):
        return n, READING_PENDING
    return n, None


def readings(agent_name: str, rows: Iterable[Tuple[str, Any, Any]],
             now: Optional[datetime] = None) -> Dict[str, Reading]:
    """Readings for one page of `agent_name`'s executions, in ONE count query.

    `rows` are `(execution_id, schedule_id, completed_at)`. Every id is in the
    answer; a run with no schedule maps to `NOT_SCHEDULE_BACKED` and is never
    sent to the count. When the counts cannot be read, every schedule-backed
    run reads `(None, "unavailable")` and the cause is logged: the execution
    read itself still answers.
    """
    rows = list(rows)
    out: Dict[str, Reading] = {eid: NOT_SCHEDULE_BACKED for eid, _, _ in rows}
    backed = [(eid, completed_at) for eid, sid, completed_at in rows if is_schedule_backed(sid)]
    if not backed:
        return out
    try:
        counts = guardrail_event_service.count_by_execution(agent_name, [eid for eid, _ in backed])
    except Exception:  # noqa: BLE001 -- surfaced as `unavailable` on every row, and logged
        logger.warning("execution refusals: counts unreadable for %s", agent_name, exc_info=True)
        for eid, _ in backed:
            out[eid] = (None, READING_UNAVAILABLE)
        return out
    now = now or utc_now()
    executions = counts["executions"]
    for eid, completed_at in backed:
        out[eid] = reading_for(executions.get(eid), counts["reporting"], completed_at, now)
    return out
