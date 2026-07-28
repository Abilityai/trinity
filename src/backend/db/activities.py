"""
Activity stream database operations.

Handles activity logging for real-time state monitoring and tool-level observability.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``agent_activities`` table
in ``db/tables.py`` (dialect-agnostic expressions, no ``?``/``%s`` placeholders).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Sequence

from sqlalchemy import select, insert, update, and_, case

from .engine import get_engine
from .query_helpers import latest_per_group
from .tables import agent_activities
# ActivityCloseOutcome is defined in `models` (not here) on purpose — it is a
# contract type shared with the service layer and compared by identity; `models`
# is the leaf that survives a test harness evicting `db.*` from sys.modules.
# Re-exported through this module so `db.activities.ActivityCloseOutcome` works.
from models import ActivityState, ActivityType, ActivityCloseOutcome
from utils.helpers import utc_now_iso, to_utc_iso, parse_iso_timestamp


# Dispatch activity types opened by ``track_activity`` at execution start. A
# shared-eid ``tool_call``/collaboration row must never be closed in their place
# (Codex #8, #1083).
_DISPATCH_ACTIVITY_TYPES = (
    ActivityType.CHAT_START.value,
    ActivityType.SCHEDULE_START.value,
)

# #1804: chunk size for the bulk close's `IN (...)`. SQLite caps host params at
# SQLITE_MAX_VARIABLE_NUMBER (999 before 3.32); keep a safe margin. Module
# global so tests can monkeypatch it to exercise the multi-chunk path
# (precedent: db/schedules/git_config.py).
_SQLITE_MAX_IN_VARS = 900


def _close_predicate(status):
    """The activity-close lattice (#1804). MIRRORS the execution-row CAS in
    ``db/schedules/executions.py`` predicate-for-predicate — do NOT "simplify"
    this to a flat ``activity_state == 'started'``; that is the bug.

    ::

                     EXECUTION ROW                             ACTIVITY ROW
                 (schedule_executions)                       (agent_activities)

                        running                                    started
                           |                                          |
       non-success write   | notin_(_TERMINAL)      FAILED close      | = 'started'
                           v                                          v
              failed / cancelled / skipped                   failed / cancelled
                           |                                          |
       SUCCESS write       | != CANCELLED        COMPLETED close      | in (started,
                           v                                          v      failed)
                        success                                   completed

      AUTHORITY:  started  <  failed  <  { completed, cancelled }

      * An authoritative close (COMPLETED) MAY upgrade a provisional FAILED —
        this is the #1083 late-SUCCESS-after-lease-expiry path (see
        ``pull_coordination_service``: "a FAILED row falls through so a late
        SUCCESS can still correct it"). Without it, that path would leave
        ``execution=success, activity=failed`` permanently — #1804 inverted.
      * Nothing may overwrite an authoritative close. This is where the mirror
        is deliberately TIGHTER than the execution CAS rather than literal: the
        execution row's SUCCESS predicate is ``!= CANCELLED``, which also admits
        ``success -> success`` and re-dates ``completed_at``/``duration_ms``. The
        activity must not inherit that edge — its ``duration_ms`` is the exact
        field #1804 exists to keep honest, and a second COMPLETED close would
        rewrite a real 15-minute duration into "now - started_at" by a brand-new
        route (reachable from ``_write_terminal_and_gate``'s lost-CAS branch,
        which passes an explicit ``activity_id`` and closes in the reconciled
        state). So the predicate is the authority lattice above, stated
        literally: an authoritative close accepts ``started`` or ``failed``, and
        nothing else.
    """
    if status == ActivityState.COMPLETED:
        return agent_activities.c.activity_state.in_(
            (ActivityState.STARTED.value, ActivityState.FAILED.value)
        )
    return agent_activities.c.activity_state == ActivityState.STARTED.value


def _norm_ts(value):
    """Normalize a stored ISO timestamp to UTC with an explicit 'Z' suffix (#1474).

    The activity mappers return raw stored strings; a scheduler-written
    ``schedule_start``/``schedule_end`` activity carries a naive timestamp that
    JS parses as local time. Assume-UTC for naive (matching the Python read
    paths), re-emit the 'Z' form. None passes through.
    """
    return to_utc_iso(parse_iso_timestamp(value)) if value else None


# Columns in DDL order so positional row access ([0]..[14]) stays correct.
_ACTIVITY_COLUMNS = (
    agent_activities.c.id,
    agent_activities.c.agent_name,
    agent_activities.c.activity_type,
    agent_activities.c.activity_state,
    agent_activities.c.parent_activity_id,
    agent_activities.c.started_at,
    agent_activities.c.completed_at,
    agent_activities.c.duration_ms,
    agent_activities.c.user_id,
    agent_activities.c.triggered_by,
    agent_activities.c.related_chat_message_id,
    agent_activities.c.related_execution_id,
    agent_activities.c.details,
    agent_activities.c.error,
    agent_activities.c.created_at,
)


class ActivityOperations:
    """Activity stream database operations."""

    @staticmethod
    def _row_to_activity(row) -> Dict:
        """Convert database row to activity dict."""
        return {
            "id": row[0],
            "agent_name": row[1],
            "activity_type": row[2],
            "activity_state": row[3],
            "parent_activity_id": row[4],
            "started_at": _norm_ts(row[5]),
            "completed_at": _norm_ts(row[6]),
            "duration_ms": row[7],
            "user_id": row[8],
            "triggered_by": row[9],
            "related_chat_message_id": row[10],
            "related_execution_id": row[11],
            "details": json.loads(row[12]) if row[12] else None,
            "error": row[13],
            "created_at": _norm_ts(row[14])
        }

    @staticmethod
    def _mapping_to_activity(row) -> Dict:
        """Convert a name-accessible mapping row to an activity dict.

        Used by ``get_latest_activity_for_agents`` (which projects the curated
        ``_ACTIVITY_COLUMNS`` via the shared window helper). Name-based access —
        unlike the positional ``_row_to_activity`` — so reordering a column in
        ``_ACTIVITY_COLUMNS`` can never silently misalign the fields.
        """
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "activity_type": row["activity_type"],
            "activity_state": row["activity_state"],
            "parent_activity_id": row["parent_activity_id"],
            "started_at": _norm_ts(row["started_at"]),
            "completed_at": _norm_ts(row["completed_at"]),
            "duration_ms": row["duration_ms"],
            "user_id": row["user_id"],
            "triggered_by": row["triggered_by"],
            "related_chat_message_id": row["related_chat_message_id"],
            "related_execution_id": row["related_execution_id"],
            "details": json.loads(row["details"]) if row["details"] else None,
            "error": row["error"],
            "created_at": _norm_ts(row["created_at"]),
        }

    def create_activity(self, activity: 'ActivityCreate') -> str:
        """Create a new activity record. Returns activity_id."""
        from models import ActivityType, ActivityState

        activity_id = str(uuid.uuid4())
        now = utc_now_iso()  # Use UTC with 'Z' suffix for frontend compatibility

        stmt = insert(agent_activities).values(
            id=activity_id,
            agent_name=activity.agent_name,
            activity_type=activity.activity_type.value if isinstance(activity.activity_type, ActivityType) else activity.activity_type,
            activity_state=activity.activity_state.value if isinstance(activity.activity_state, ActivityState) else activity.activity_state,
            parent_activity_id=activity.parent_activity_id,
            started_at=now,
            user_id=activity.user_id,
            triggered_by=activity.triggered_by,
            related_chat_message_id=activity.related_chat_message_id,
            related_execution_id=activity.related_execution_id,
            details=json.dumps(activity.details) if activity.details else None,
            error=activity.error,
            created_at=now,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return activity_id

    def complete_activity(self, activity_id: str, status: str = ActivityState.COMPLETED,
                          details: Optional[Dict] = None,
                          error: Optional[str] = None) -> "ActivityCloseOutcome":
        """Close an activity: state, completion time, duration.

        #1804: this is now a **compare-and-set**, not an unconditional UPDATE.
        Every CAS-won terminal writer closes the paired activity, so a second
        closer trying is normal (a recovery path and a late in-process writer
        can both reach the same row) — "the CAS winner owns it" only holds if a
        second attempt is *safe*. An unconditional UPDATE let the later writer
        overwrite the earlier one's ``completed_at``/``duration_ms``/``error``,
        reintroducing the wrong-duration symptom by a new route.

        Returns an :class:`ActivityCloseOutcome`, not a bool — see its docstring
        for why two states are not enough. The predicate is
        :func:`_close_predicate` (read its lattice diagram before touching it).
        """
        with get_engine().begin() as conn:
            # Get start time to calculate duration
            row = conn.execute(
                select(agent_activities.c.started_at, agent_activities.c.details)
                .where(agent_activities.c.id == activity_id)
            ).mappings().first()
            if not row:
                return ActivityCloseOutcome.NOT_FOUND

            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            started_at = parse_iso_timestamp(row["started_at"])
            completed_at = parse_iso_timestamp(utc_now_iso())
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            # Merge existing details with new details
            existing_details = json.loads(row["details"]) if row["details"] else {}
            if details:
                existing_details.update(details)

            # Atomic (not check-then-act): the lattice predicate rides in the
            # same UPDATE as the id match, so two concurrent closers can never
            # both win.
            result = conn.execute(
                update(agent_activities)
                .where(
                    and_(
                        agent_activities.c.id == activity_id,
                        _close_predicate(status),
                    )
                )
                .values(
                    activity_state=status,
                    completed_at=to_utc_iso(completed_at),  # Use UTC with 'Z' suffix
                    duration_ms=duration_ms,
                    details=json.dumps(existing_details) if existing_details else None,
                    error=error,
                )
            )
            return (
                ActivityCloseOutcome.UPDATED
                if result.rowcount > 0
                else ActivityCloseOutcome.ALREADY_CLOSED
            )

    def close_open_activities_for_executions(
        self,
        execution_ids: Sequence[str],
        status: str = ActivityState.FAILED,
        error: Optional[str] = None,
    ) -> int:
        """Close every open dispatch activity belonging to an execution-id set,
        in ONE transaction with no per-row WebSocket broadcast (#1804).

        The bulk watchdog sweeps fail N execution rows at once; routing them
        through the per-row helper would be N transactions AND N WebSocket
        events during post-outage recovery — write amplification plus exactly
        the herd #1085 exists to prevent. This mirrors what the 120-minute
        ``mark_stale_activities_failed`` backstop already does.

        **Set-wise, not a lossy ``eid -> one activity_id`` map**: a re-queued
        execution can own more than one open dispatch activity, and ``limit(1)``
        semantics would silently drop the rest.

        Returns the CAS-won row count.
        """
        ids = [eid for eid in (execution_ids or []) if eid]
        if not ids:
            return 0

        closed = 0
        now = utc_now_iso()
        completed_at = parse_iso_timestamp(now)
        with get_engine().begin() as conn:
            for start in range(0, len(ids), _SQLITE_MAX_IN_VARS):
                chunk = ids[start:start + _SQLITE_MAX_IN_VARS]
                rows = conn.execute(
                    select(agent_activities.c.id, agent_activities.c.started_at)
                    .where(
                        and_(
                            agent_activities.c.related_execution_id.in_(chunk),
                            agent_activities.c.activity_type.in_(_DISPATCH_ACTIVITY_TYPES),
                            _close_predicate(status),
                        )
                    )
                ).mappings().all()

                for row in rows:
                    started_at = parse_iso_timestamp(row["started_at"])
                    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
                    result = conn.execute(
                        update(agent_activities)
                        .where(
                            and_(
                                agent_activities.c.id == row["id"],
                                _close_predicate(status),
                            )
                        )
                        .values(
                            activity_state=status,
                            completed_at=now,
                            duration_ms=duration_ms,
                            error=error,
                        )
                    )
                    closed += 1 if result.rowcount else 0

        return closed

    def get_activity(self, activity_id: str) -> Optional[Dict]:
        """Get a single activity by ID."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(*_ACTIVITY_COLUMNS)
                .where(agent_activities.c.id == activity_id)
            ).first()
            if not row:
                return None
            return self._row_to_activity(row)

    def get_open_activity_id_for_execution(
        self, execution_id: str, include_failed: bool = False
    ) -> Optional[str]:
        """Return the id of the closeable dispatch activity for an execution (#1083).

        Used by the result-callback endpoint, the lease reaper, the terminate
        path and the #1804 shared close helper to close the activity that the
        (now-absent) ``execute_task`` coroutine ``finally`` would have closed
        under fire-and-forget dispatch.

        Filtered (Codex #8): ``related_execution_id`` AND
        ``activity_type IN (chat_start, schedule_start)``. An execution id can
        be referenced by several activity rows (a collaboration/self-task tool
        row shares it); an unfiltered lookup would close the wrong one. We
        restrict to the *dispatch* activity types that ``track_activity`` opens
        at execution start. Uses ``idx_activities_execution``. Returns None when
        nothing is closeable (already authoritatively closed, or never tracked).

        ``include_failed`` (#1804) — **the lookup must agree with the CAS.**
        An *authoritative* close (COMPLETED, from a SUCCESS terminal) may
        upgrade a provisionally-FAILED activity per :func:`_close_predicate`,
        so it must be able to *find* that row; a lookup narrower than the write
        makes the widened predicate inert (the whole fix would silently do
        nothing). A *provisional* close (FAILED) keeps the original
        ``started``-only filter. Callers never reason about this directly —
        ``activity_service.close_execution_activity`` derives it from the
        terminal status.

        Ordering puts still-``started`` rows first, then most-recent: with both
        an open and an already-failed row present, the OPEN one is always the
        right target regardless of ``created_at`` order.
        """
        states = [ActivityState.STARTED.value]
        if include_failed:
            states.append(ActivityState.FAILED.value)
        open_first = case(
            (agent_activities.c.activity_state == ActivityState.STARTED.value, 0),
            else_=1,
        )
        stmt = (
            select(agent_activities.c.id)
            .where(
                and_(
                    agent_activities.c.related_execution_id == execution_id,
                    agent_activities.c.activity_type.in_(_DISPATCH_ACTIVITY_TYPES),
                    agent_activities.c.activity_state.in_(states),
                )
            )
            .order_by(open_first, agent_activities.c.created_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return row[0] if row else None

    def get_agent_activities(self, agent_name: str, activity_type: Optional[str] = None,
                            activity_state: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        Get activities for a specific agent.
        Optionally filter by activity_type and/or activity_state.
        """
        conditions = [agent_activities.c.agent_name == agent_name]

        if activity_type:
            conditions.append(agent_activities.c.activity_type == activity_type)

        if activity_state:
            conditions.append(agent_activities.c.activity_state == activity_state)

        stmt = (
            select(*_ACTIVITY_COLUMNS)
            .where(and_(*conditions))
            .order_by(agent_activities.c.created_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [self._row_to_activity(row) for row in conn.execute(stmt).all()]

    def get_latest_activity_for_agents(self, agent_names: List[str]) -> Dict[str, Dict]:
        """Return the single most-recent activity per agent, in one query.

        #1265: replaces a per-agent ``get_agent_activities(name, limit=1)``
        fan-out (one query per running agent) on the Dashboard context-stats
        path. Uses the shared ``latest_per_group`` window helper (partition by
        agent_name, order by created_at DESC) — served by ``idx_activities_agent``.

        Returns ``{agent_name: activity_dict}``; agents with no activity are
        simply absent from the map.
        """
        rows = latest_per_group(
            _ACTIVITY_COLUMNS,
            agent_activities.c.agent_name,   # partition
            agent_activities.c.created_at,   # order (DESC)
            agent_activities.c.agent_name,   # filter IN
            agent_names,
        )
        return {row["agent_name"]: self._mapping_to_activity(row) for row in rows}

    def get_activities_in_range(self, start_time: Optional[str] = None,
                                end_time: Optional[str] = None,
                                activity_types: Optional[List[str]] = None,
                                limit: int = 100,
                                agent_names: Optional[List[str]] = None) -> List[Dict]:
        """
        Get activities across all agents in a time range.
        Optionally filter by activity types and by an access-control allow-list.

        #1265: ``agent_names`` pushes the per-user access filter into SQL so the
        timeline no longer over-fetches ``limit*2`` rows and filters in Python.
        ``None`` means no agent filter (admin); an empty list returns nothing.
        """
        conditions = []

        if start_time:
            conditions.append(agent_activities.c.created_at >= start_time)

        if end_time:
            conditions.append(agent_activities.c.created_at <= end_time)

        if activity_types:
            conditions.append(agent_activities.c.activity_type.in_(activity_types))

        if agent_names is not None:
            if not agent_names:
                return []
            conditions.append(agent_activities.c.agent_name.in_(agent_names))

        stmt = select(*_ACTIVITY_COLUMNS)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(agent_activities.c.created_at.desc()).limit(limit)

        with get_engine().connect() as conn:
            return [self._row_to_activity(row) for row in conn.execute(stmt).all()]

    def mark_stale_activities_failed(self, timeout_minutes: int = 30) -> int:
        """Mark started activities older than timeout as failed.

        Uses ActivityState.STARTED / .FAILED for status values.

        Args:
            timeout_minutes: Activities running longer than this are considered stale.

        Returns:
            Number of activities marked as failed.
        """
        now = utc_now_iso()
        # Compute threshold in ISO 8601 format to match stored started_at
        # (SQLite's datetime() returns space-separated format which breaks string comparison)
        threshold = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).strftime('%Y-%m-%dT%H:%M:%S')
        with get_engine().begin() as conn:
            # Find stale activities for duration calculation
            stale_rows = conn.execute(
                select(agent_activities.c.id, agent_activities.c.started_at)
                .where(
                    and_(
                        agent_activities.c.activity_state == ActivityState.STARTED,
                        agent_activities.c.started_at < threshold,
                    )
                )
            ).mappings().all()

            if not stale_rows:
                return 0

            completed_at = parse_iso_timestamp(now)
            for row in stale_rows:
                started_at = parse_iso_timestamp(row["started_at"])
                duration_ms = int((completed_at - started_at).total_seconds() * 1000)
                conn.execute(
                    update(agent_activities)
                    .where(agent_activities.c.id == row["id"])
                    .values(
                        activity_state=ActivityState.FAILED,
                        completed_at=now,
                        duration_ms=duration_ms,
                        error='Marked as failed by cleanup: exceeded ' + str(timeout_minutes) + '-minute timeout',
                    )
                )

            return len(stale_rows)

    def get_current_activities(self, agent_name: str) -> List[Dict]:
        """Get all in-progress (started) activities for an agent."""
        return self.get_agent_activities(
            agent_name=agent_name,
            activity_state=ActivityState.STARTED,
            limit=50
        )
