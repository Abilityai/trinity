"""
Agent self-reminder database operations (#1296).

An agent schedules a durable one-shot deferred self-trigger (§10.14). This layer
is the source of truth; the standalone scheduler arms an APScheduler DateTrigger
per pending row and fires it. Backend create/list/cancel go through here.

SQLAlchemy Core over the ``agent_reminders`` table in ``db/tables.py`` so it runs
unchanged on SQLite and PostgreSQL (Invariant #2). Every by-id operation is
**tenant-scoped** (carries ``agent_name`` in the predicate) so an id belonging to
another agent can never be read or cancelled (defense-in-depth IDOR / id-oracle).
"""

import json
import uuid
from typing import Dict, List, Optional

from sqlalchemy import select, insert, update, delete, and_, func

from .engine import get_engine
from .tables import agent_reminders
from utils.helpers import utc_now_iso, iso_cutoff


# Status state machine: pending → firing → fired (delivered);
# firing → pending (transient-failure release) / firing → failed (bounded);
# pending → cancelled.
STATUS_PENDING = "pending"
STATUS_FIRING = "firing"
STATUS_FIRED = "fired"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

# Terminal states the retention sweep may delete (pending/firing never deleted).
_TERMINAL_STATUSES = (STATUS_FIRED, STATUS_CANCELLED, STATUS_FAILED)


def _agent_reminders_prune_predicate(cutoff: str):
    """WHERE clause for the #1296 retention sweep — shared by the count and the
    delete so the #1644 guard can never describe a different row set than the
    prune. Terminal rows only; ``pending``/``firing`` are never swept."""
    return and_(
        agent_reminders.c.status.in_(_TERMINAL_STATUSES),
        agent_reminders.c.created_at < cutoff,
    )


class RemindersOperations:
    """Agent self-reminder database operations (#1296)."""

    @staticmethod
    def _row_to_dict(row) -> Dict:
        """Full reminder dict from a name-accessible mapping row.

        ``allowed_tools`` is stored as a JSON string and decoded back to a list
        (None when absent) for the API response.
        """
        allowed = row["allowed_tools"]
        try:
            allowed_tools = json.loads(allowed) if allowed else None
        except (TypeError, ValueError):
            allowed_tools = None
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "message": row["message"],
            "fire_at": row["fire_at"],
            "status": row["status"],
            "model": row["model"],
            "timeout_seconds": row["timeout_seconds"],
            "allowed_tools": allowed_tools,
            "owner_id": row["owner_id"],
            "created_by_email": row["created_by_email"],
            "source_agent_name": row["source_agent_name"],
            "source_mcp_key_id": row["source_mcp_key_id"],
            "execution_id": row["execution_id"],
            "fire_attempts": row["fire_attempts"],
            "firing_at": row["firing_at"],
            "error": row["error"],
            "created_at": row["created_at"],
            "fired_at": row["fired_at"],
            "cancelled_at": row["cancelled_at"],
        }

    def insert_reminder(
        self,
        agent_name: str,
        message: str,
        fire_at: str,
        *,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        allowed_tools: Optional[List[str]] = None,
        owner_id: Optional[int] = None,
        created_by_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
    ) -> Dict:
        """Insert a pending reminder row and return the full reminder dict."""
        reminder_id = f"rem_{uuid.uuid4().hex}"
        now = utc_now_iso()
        stmt = insert(agent_reminders).values(
            id=reminder_id,
            agent_name=agent_name,
            message=message,
            fire_at=fire_at,
            status=STATUS_PENDING,
            model=model,
            timeout_seconds=timeout_seconds,
            allowed_tools=json.dumps(allowed_tools) if allowed_tools else None,
            owner_id=owner_id,
            created_by_email=created_by_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            execution_id=None,
            fire_attempts=0,
            firing_at=None,
            error=None,
            created_at=now,
            fired_at=None,
            cancelled_at=None,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return {
            "id": reminder_id,
            "agent_name": agent_name,
            "message": message,
            "fire_at": fire_at,
            "status": STATUS_PENDING,
            "model": model,
            "timeout_seconds": timeout_seconds,
            "allowed_tools": allowed_tools,
            "owner_id": owner_id,
            "created_by_email": created_by_email,
            "source_agent_name": source_agent_name,
            "source_mcp_key_id": source_mcp_key_id,
            "execution_id": None,
            "fire_attempts": 0,
            "firing_at": None,
            "error": None,
            "created_at": now,
            "fired_at": None,
            "cancelled_at": None,
        }

    def list_reminders(
        self, agent_name: str, status: Optional[str] = None, limit: int = 100
    ) -> List[Dict]:
        """List one agent's reminders (WHERE agent_name=?), soonest fire first."""
        conditions = [agent_reminders.c.agent_name == agent_name]
        if status:
            conditions.append(agent_reminders.c.status == status)
        stmt = (
            select(agent_reminders)
            .where(and_(*conditions))
            .order_by(agent_reminders.c.fire_at.asc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_dict(r) for r in rows]

    def get_reminder(self, agent_name: str, reminder_id: str) -> Optional[Dict]:
        """Tenant-scoped fetch: the id AND agent_name must match, else None."""
        stmt = select(agent_reminders).where(
            and_(
                agent_reminders.c.id == reminder_id,
                agent_reminders.c.agent_name == agent_name,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def count_pending_reminders(self, agent_name: str) -> int:
        """Real count of pending reminders for an agent (the concurrency cap)."""
        stmt = select(func.count()).select_from(agent_reminders).where(
            and_(
                agent_reminders.c.agent_name == agent_name,
                agent_reminders.c.status == STATUS_PENDING,
            )
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def count_reminders_created_since(self, agent_name: str, since_iso: str) -> int:
        """Durable rolling-window create count (the daily self-perpetuation cap)."""
        stmt = select(func.count()).select_from(agent_reminders).where(
            and_(
                agent_reminders.c.agent_name == agent_name,
                agent_reminders.c.created_at >= since_iso,
            )
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def cancel_reminder(self, agent_name: str, reminder_id: str) -> str:
        """CAS ``pending → cancelled``, tenant-scoped. Returns an outcome string:

        - ``"cancelled"``          — was pending, now cancelled
        - ``"already_cancelled"``  — already cancelled (idempotent no-op)
        - ``"conflict"``           — firing/fired/failed (not cancellable)
        - ``"not_found"``          — no row with (id, agent_name)
        """
        existing = self.get_reminder(agent_name, reminder_id)
        if existing is None:
            return "not_found"
        if existing["status"] == STATUS_CANCELLED:
            return "already_cancelled"
        if existing["status"] != STATUS_PENDING:
            return "conflict"
        now = utc_now_iso()
        stmt = (
            update(agent_reminders)
            .where(
                and_(
                    agent_reminders.c.id == reminder_id,
                    agent_reminders.c.agent_name == agent_name,
                    agent_reminders.c.status == STATUS_PENDING,
                )
            )
            .values(status=STATUS_CANCELLED, cancelled_at=now)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            return "cancelled"
        # Lost the CAS (a concurrent fire flipped pending→firing between the read
        # and the update) — re-read for the precise outcome.
        after = self.get_reminder(agent_name, reminder_id)
        if after is None:
            return "not_found"
        if after["status"] == STATUS_CANCELLED:
            return "already_cancelled"
        return "conflict"

    # ------------------------------------------------------------------
    # Retention (#1296 / #1638–#1644 discipline)
    # ------------------------------------------------------------------

    def count_agent_reminders_candidates(self, retention_days: int, limit: int) -> int:
        """#1644: how many rows ``prune_agent_reminders(retention_days)`` would DELETE."""
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        inner = (
            select(agent_reminders.c.id)
            .where(_agent_reminders_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(inner)).scalar() or 0
            )

    def prune_agent_reminders(self, retention_days: int = 90, chunk_size: int = 1000) -> int:
        """Delete TERMINAL reminders older than ``retention_days`` (#1296 sweep).

        Chunked DELETE (mirrors ``prune_agent_reports``) so a large table doesn't
        hold the write lock for the full purge. ``pending``/``firing`` rows are
        never deleted. ``iso_cutoff()`` keeps the comparison Invariant-#16 safe.
        ``0`` disables the sweep.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        while True:
            with get_engine().begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(agent_reminders.c.id)
                        .where(_agent_reminders_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(agent_reminders).where(agent_reminders.c.id.in_(ids))
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total
