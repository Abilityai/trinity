"""Business logic for Workspace work (trinity-enterprise#525). OSS core.

Reads the execution ledger for a chat's participants and projects it for the
person who asked — nothing here writes. Two selectors, one projection:

  * by AGENT — the participants' own rows: what is running now, and the
    bounded, windowed history behind "N in the last 30 days · latest 3 shown";
  * by CHAT — the in-flight rows bound to this conversation, which is the only
    way a DELEGATED child is found (its ``agent_name`` is the delegate, so the
    agent selector never returns it; ``source_channel_chat_id`` is copied from
    the parent at creation, ent#265 D0 / #2386).

Both run through the portal ROSTER (`client_portal.service.roster_agent_names`,
a DB fact) and never the operator fleet ACL: that one resolves through
`list_all_agents_fast()`, a Docker read that answers ``[]`` on any daemon fault
— the #2196 class the Workspace forbids (membership is a DB fact; container
state is a projection). The DB queries themselves are the fleet dashboard's
(`get_fleet_executions`, `get_fleet_execution_stats`) — one query, two readers.

Every name that leaves this module is roster-masked (ent#467's disclosure
class): a delegated child on an agent the caller cannot see is still a step —
"another agent" — but never a name.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from config import PORTAL_SOURCE_CHANNEL
from database import db as core_db
from utils.helpers import parse_iso_timestamp

from .. import db as portal_db
from ..service import roster_agent_names
from .models import PortalWork, WorkItem, WorkKind, WorkOutcome, WorkSteps
from . import pipeline_state

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
EARLIER_LIMIT = 30
#: `agents=` cap — the `/briefings` precedent: a named 422, never silent truncation.
MAX_AGENTS = 50
TITLE_MAX = 120
ERROR_MAX = 200

#: Past this multiple of the agent's own turn bound a RUNNING row is not live:
#: nothing is watching it (a hard restart skipped the `finally` that would
#: have closed it), the 120-minute sweep will fail it, and a card counting up
#: from its `started_at` would be the stuck "running" AC 1 forbids.
STALE_MULTIPLIER = 1.5
STALE_FLOOR_SECONDS = 30 * 60

IN_FLIGHT = frozenset({"running", "queued", "pending_retry"})
_TERMINAL = frozenset({"success", "failed", "error", "cancelled", "skipped"})
_TIMEOUT_RE = re.compile(r"timed?\s*-?\s*out|timeout", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


class WorkError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------- pure rules

def work_kind(row: dict) -> WorkKind:
    """The client's word for a ledger row — from `triggered_by` + the channel stamp.

    `turn` is the person's own Workspace message (a `public` trigger stamped
    `portal`); `delegated` is work an agent handed on from such a turn (an
    `mcp` trigger carrying the same stamp); the rest name the trigger. An
    unknown trigger is `other`, never a guess.
    """
    trigger = (row.get("triggered_by") or "").strip().lower()
    channel = (row.get("source_channel") or "").strip().lower()
    if trigger == "loop" or row.get("loop_id"):
        return "loop"
    if trigger == "room":
        return "room"
    if trigger in ("schedule", "scheduled"):
        return "schedule"
    if channel == PORTAL_SOURCE_CHANNEL:
        if trigger == "public":
            return "turn"
        if trigger in ("mcp", "agent", "fan_out", "a2a"):
            return "delegated"
    return "other"


def work_outcome(row: dict, *, stale: bool = False) -> WorkOutcome:
    """The honest word: `timeout` is a failure whose error says so; `lost` is a
    running row nothing is watching any more."""
    status = (row.get("status") or "").strip().lower()
    if status in ("running", "pending_retry"):
        return "lost" if stale else "running"
    if status == "queued":
        return "lost" if stale else "queued"
    if status == "success":
        return "success"
    if status == "cancelled":
        return "cancelled"
    if status == "skipped":
        return "skipped"
    if status in ("failed", "error"):
        err = row.get("error_summary") or row.get("error") or ""
        return "timeout" if _TIMEOUT_RE.search(str(err)) else "failed"
    return "failed"


def clean_title(message: Optional[str]) -> str:
    """One line, secrets masked, bounded. The message is what the person (or an
    agent) wrote — it can hold anything, including a pasted token."""
    from utils.credential_sanitizer import sanitize_text
    text = _WS_RE.sub(" ", sanitize_text(str(message or ""))).strip()
    if not text:
        return "(no message)"
    return text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1].rstrip() + "…"


def clean_error(error_summary: Optional[str]) -> Optional[str]:
    """The failed row's one-liner, masked and bounded; None when there is none."""
    from utils.credential_sanitizer import sanitize_text
    text = _WS_RE.sub(" ", sanitize_text(str(error_summary or ""))).strip()
    if not text:
        return None
    return text if len(text) <= ERROR_MAX else text[: ERROR_MAX - 1].rstrip() + "…"


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parse_iso_timestamp(str(value))
    except Exception:  # noqa: BLE001
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def elapsed_seconds(started_at: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    start = _ts(started_at)
    if start is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - start).total_seconds()))


def stale_bound_seconds(turn_timeout_seconds: int) -> int:
    return max(STALE_FLOOR_SECONDS, int(turn_timeout_seconds * STALE_MULTIPLIER))


def is_stale(elapsed: Optional[int], turn_timeout_seconds: int) -> bool:
    return elapsed is not None and elapsed > stale_bound_seconds(turn_timeout_seconds)


def can_stop(item_kind: WorkKind, status: str, *, mine: bool, on_roster: bool, stale: bool) -> bool:
    """What `POST .../executions/{id}/terminate` will accept, decided once here
    so the button is never a lie: the route requires the agent on the roster
    and the row started by this caller, and cancelling a lost row does nothing
    a person can see."""
    return (mine and on_roster and not stale
            and status in ("running", "queued")
            and item_kind in ("turn", "delegated"))


def mask(name: Optional[str], roster: Iterable[str]) -> Optional[str]:
    """A name the caller may see, or None (ent#467: never an off-roster name)."""
    return name if name and name in roster else None


def parse_agents(raw: Optional[str]) -> List[str]:
    """`a,b, a` → `['a', 'b']`; the `/briefings?agents=` rule."""
    names: List[str] = []
    seen = set()
    for part in (raw or "").split(","):
        n = part.strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names


# ---------------------------------------------------------------- projection

def _project(row: dict, *, email: str, roster: set, turn_timeout: int,
             now: datetime) -> WorkItem:
    status = (row.get("status") or "").strip().lower()
    kind = work_kind(row)
    agent = row.get("agent_name")
    on_roster = bool(agent) and agent in roster
    in_flight = status in IN_FLIGHT
    elapsed = elapsed_seconds(row.get("started_at"), now) if in_flight else None
    stale = in_flight and is_stale(elapsed, turn_timeout)
    mine = bool(email) and (row.get("source_user_email") or "").strip().lower() == email.lower()
    channel = (row.get("source_channel") or "").strip().lower()
    return WorkItem(
        id=str(row.get("id")),
        agent_name=agent if on_roster else None,
        status=status,
        outcome=work_outcome(row, stale=stale),
        kind=kind,
        title=clean_title(row.get("message")),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        duration_ms=row.get("duration_ms"),
        elapsed_seconds=None if stale else elapsed,
        stale=stale,
        # Only a PORTAL stamp is a chat id the client can open; a Telegram or
        # Slack destination is not the client's business.
        chat_id=row.get("source_channel_chat_id") if channel == PORTAL_SOURCE_CHANNEL else None,
        mine=mine,
        can_stop=can_stop(kind, status, mine=mine, on_roster=on_roster, stale=stale),
        delegated_by=mask(row.get("source_agent_name"), roster),
        loop_id=row.get("loop_id") if kind == "loop" else None,
        error=clean_error(row.get("error_summary") or row.get("error")) if status in ("failed", "error") else None,
        steps=None,
    )


def _resolve_timeout(agent_name: Optional[str]) -> int:
    from services.session_turn_service import resolve_turn_timeout
    try:
        return int(resolve_turn_timeout(agent_name)) if agent_name else 3600
    except Exception:  # noqa: BLE001 — fail open to the platform default
        return 3600


def _chat_is_callers(chat_id: Optional[str], agents: List[str], email: str) -> bool:
    """A chat id is honoured only when it names a thread THIS caller holds with
    one of the requested agents — the scoped read, never the unscoped one."""
    if not chat_id:
        return False
    for agent in agents:
        try:
            if portal_db.get_portal_session(chat_id, agent, email):
                return True
        except Exception:  # noqa: BLE001 — fail closed: no children rather than someone else's
            logger.warning("[ent#525] chat ownership check failed for %s", chat_id, exc_info=True)
            return False
    return False


async def get_work(email: str, names: List[str], chat_id: Optional[str] = None) -> PortalWork:
    """The chat's work, three ways. Platform principals only — the router gates.

    Never raises for a stopped or unreachable agent; raises `WorkError(503)`
    only when the LEDGER cannot be read, which is the one failure the client
    must show as "couldn't load" rather than "nothing running".
    """
    roster = roster_agent_names(email, include_owned=True)
    # Set-membership only: an off-roster or unknown name is dropped, never
    # answered (Invariant #8 — no existence oracle across the set).
    agents = [n for n in names if n in roster]
    now = datetime.now(timezone.utc)
    hours = WINDOW_DAYS * 24

    if not agents:
        return PortalWork(agents=[], now=[], earlier=[], earlier_total=0,
                          window_days=WINDOW_DAYS, earlier_limit=EARLIER_LIMIT)

    try:
        running_rows = core_db.get_fleet_executions(agents, status="running", hours=0, limit=50)
        queued_rows = core_db.get_fleet_executions(agents, status="queued", hours=0, limit=50)
        recent_rows = core_db.get_fleet_executions(agents, hours=hours, limit=EARLIER_LIMIT + 20)
        stats = core_db.get_fleet_execution_stats(agents, hours=hours) or {}
        children = core_db.get_running_for_chat(chat_id) if _chat_is_callers(chat_id, agents, email) else []
    except Exception:  # noqa: BLE001 — the ledger is the one thing this read cannot do without
        logger.warning("[ent#525] work read failed", exc_info=True)
        raise WorkError(503, "Couldn't load what's running. Try again in a moment.")

    timeouts: Dict[str, int] = {}

    def timeout_for(agent: Optional[str]) -> int:
        key = agent or ""
        if key not in timeouts:
            timeouts[key] = _resolve_timeout(agent)
        return timeouts[key]

    # Now: the participants' in-flight rows, plus the chat's children (a child
    # may live on an agent outside the roster — it is still a step, unnamed).
    seen = set()
    now_items: List[WorkItem] = []
    for row in list(running_rows) + list(queued_rows) + list(children):
        rid = str(row.get("id"))
        if rid in seen:
            continue
        seen.add(rid)
        now_items.append(_project(row, email=email, roster=roster,
                                  turn_timeout=timeout_for(row.get("agent_name")), now=now))
    now_items.sort(key=lambda it: it.started_at or "", reverse=True)

    # Earlier: terminal rows inside the window, bounded.
    earlier_items: List[WorkItem] = []
    for row in recent_rows:
        if (row.get("status") or "").lower() in IN_FLIGHT:
            continue
        earlier_items.append(_project(row, email=email, roster=roster,
                                      turn_timeout=timeout_for(row.get("agent_name")), now=now))
        if len(earlier_items) >= EARLIER_LIMIT:
            break

    # The window total counts every row STARTED inside it, in-flight included;
    # "N in the last 30 days" is about finished work, so take those out.
    total = int(stats.get("total") or 0)
    cutoff = now.timestamp() - hours * 3600
    inflight_in_window = 0
    for row in list(running_rows) + list(queued_rows):
        ts = _ts(row.get("started_at"))
        if ts is not None and ts.timestamp() > cutoff:
            inflight_in_window += 1
    earlier_total = max(len(earlier_items), total - inflight_in_window)

    # Steps: the #919 read, only for a rostered agent with exactly ONE in-flight
    # row — an agent-written `updated_at` cannot say which of two runs an
    # instance belongs to (review), so two running rows read `unknown`.
    by_agent: Dict[str, List[WorkItem]] = {}
    for it in now_items:
        if it.agent_name:
            by_agent.setdefault(it.agent_name, []).append(it)

    async def steps_for(agent: str, item: WorkItem) -> None:
        item.steps = await pipeline_state.read_pipeline_steps(agent, item.started_at, roster)

    jobs = []
    for agent, items in by_agent.items():
        if len(items) == 1 and not items[0].stale:
            jobs.append(steps_for(agent, items[0]))
        else:
            for it in items:
                it.steps = WorkSteps(state="unknown")
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)
    for it in now_items:
        if it.steps is None:
            it.steps = WorkSteps(state="unknown")

    return PortalWork(agents=agents, now=now_items, earlier=earlier_items,
                      earlier_total=earlier_total, window_days=WINDOW_DAYS,
                      earlier_limit=EARLIER_LIMIT)
