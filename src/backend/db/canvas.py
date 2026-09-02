"""Agent canvas database operations (ent#438).

A **canvas** is a durable surface an agent renders onto and keeps current. The
row is keyed on ``(agent_name, canvas_id)``, so a write is an upsert and the
surface is addressable — that composite key is the whole difference from
``agent_reports`` (§5.14), where each publish is a new immutable row that
accumulates.

SQLAlchemy Core over ``db/tables.py::agent_canvases`` so it runs unchanged on
SQLite and PostgreSQL. Two response shapes, mirroring the reports split:

- **summary** (list views): metadata only, never decodes ``blocks``.
- **full** (detail view): includes the decoded block list.
"""

import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import and_, delete, func, insert, select, update

from .engine import get_engine
from .tables import agent_canvases, schedule_executions
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# The two audiences a canvas can carry. `operator` is the default and the
# fail-closed one: a canvas reaches a Workspace client only because the agent
# said so (ent#438 FR-4). An UNRECOGNISED stored value reads as `operator`
# rather than being trusted — an allowlist, never a blocklist (#2396's rule).
AUDIENCE_OPERATOR = "operator"
AUDIENCE_ROSTER = "roster"
VALID_AUDIENCES = (AUDIENCE_OPERATOR, AUDIENCE_ROSTER)

# Metadata columns in DDL order — the list/summary projection (no blocks).
_SUMMARY_COLUMNS = (
    agent_canvases.c.agent_name,
    agent_canvases.c.canvas_id,
    agent_canvases.c.title,
    agent_canvases.c.audience,
    agent_canvases.c.schema_version,
    agent_canvases.c.created_at,
    agent_canvases.c.updated_at,
    agent_canvases.c.updated_by_execution_id,
)


def normalize_audience(value) -> str:
    """Coerce a stored or supplied audience to a known one, defaulting closed."""
    return value if value in VALID_AUDIENCES else AUDIENCE_OPERATOR


class CanvasOperations:
    """Agent canvas database operations (ent#438)."""

    @staticmethod
    def _row_to_summary(row) -> Dict:
        """Metadata-only dict (no blocks) from a `_SUMMARY_COLUMNS` row."""
        return {
            "agent_name": row[0],
            "canvas_id": row[1],
            "title": row[2],
            "audience": normalize_audience(row[3]),
            "schema_version": row[4],
            "created_at": row[5],
            "updated_at": row[6],
            "updated_by_execution_id": row[7],
        }

    @classmethod
    def _row_to_full(cls, row) -> Dict:
        """Summary plus the decoded blocks.

        A blocks value that will not decode degrades to an EMPTY list rather
        than raising: the row is still a real canvas with a real timestamp, and
        a list view that 500s because one agent wrote malformed JSON is worse
        than one canvas rendering empty. The caller sees `blocks: []` and the
        `updated_at` that proves something was written.
        """
        summary = cls._row_to_summary(row)
        raw = row[8]
        try:
            blocks = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            logger.warning(
                "canvas %s/%s has undecodable blocks; rendering empty",
                summary["agent_name"], summary["canvas_id"],
            )
            blocks = []
        summary["blocks"] = blocks if isinstance(blocks, list) else []
        return summary

    # ------------------------------------------------------------------ read

    def list_canvases(self, agent_name: str, audience: Optional[str] = None) -> List[Dict]:
        """Canvas metadata for one agent, newest-updated first.

        ``audience`` narrows to one audience — the Workspace passes
        ``roster``; operator surfaces pass nothing and see every canvas.
        """
        stmt = select(*_SUMMARY_COLUMNS).where(agent_canvases.c.agent_name == agent_name)
        if audience is not None:
            stmt = stmt.where(agent_canvases.c.audience == audience)
        stmt = stmt.order_by(agent_canvases.c.updated_at.desc())
        with get_engine().connect() as conn:
            return [self._row_to_summary(row) for row in conn.execute(stmt)]

    def get_canvas(
        self, agent_name: str, canvas_id: str, audience: Optional[str] = None
    ) -> Optional[Dict]:
        """One canvas with its blocks, or None.

        ``audience`` is a REQUIRED narrowing for the client read rather than a
        filter applied afterwards: a caller that fetches first and checks later
        has already loaded the blocks, and the ent#365 lesson is that the gate
        belongs in the query.
        """
        stmt = select(*_SUMMARY_COLUMNS, agent_canvases.c.blocks).where(
            and_(
                agent_canvases.c.agent_name == agent_name,
                agent_canvases.c.canvas_id == canvas_id,
            )
        )
        if audience is not None:
            stmt = stmt.where(agent_canvases.c.audience == audience)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return self._row_to_full(row) if row else None

    def last_completed_execution_at(self, agent_name: str) -> Optional[str]:
        """When this agent last FINISHED a run, or None (ent#438 FR-5).

        The input to the derived staleness claim. Deliberately `MAX` over the
        whole column rather than a bounded scan of recent rows: the question is
        "has ANY run finished since the canvas was written", and a windowed
        read answers it wrong in exactly the direction that matters — a fleet of
        queued/running rows at the head would push the newest COMPLETED row out
        of the window and report a stale canvas as current, which is the failure
        AC 7 exists to prevent.

        `completed_at` is an ISO-Z string written by `utc_now_iso`, so MAX is a
        lexicographic max over a fixed-width format — the Invariant #16
        precondition holds because we never compare it to `datetime('now')`,
        only to another `utc_now_iso` value.
        """
        stmt = select(func.max(schedule_executions.c.completed_at)).where(
            and_(
                schedule_executions.c.agent_name == agent_name,
                schedule_executions.c.completed_at.isnot(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return row[0] if row else None

    # ----------------------------------------------------------------- write

    def upsert_canvas(
        self,
        agent_name: str,
        canvas_id: str,
        *,
        blocks: List[Dict],
        title: Optional[str] = None,
        audience: str = AUDIENCE_OPERATOR,
        execution_id: Optional[str] = None,
    ) -> Dict:
        """Replace a canvas's blocks, creating it if absent.

        `created_at` is preserved across updates — it is the age of the
        SURFACE, and the thing that changes is `updated_at`. Not a database
        upsert construct: the two dialects spell it differently and the
        read-then-write here is inside one connection with a last-writer-wins
        contract that is correct for this surface (a canvas has exactly one
        writer, the agent itself, and its executions are serialized by the
        agent's own slot budget).
        """
        now = utc_now_iso()
        payload = json.dumps(blocks)
        with get_engine().begin() as conn:
            existing = conn.execute(
                select(agent_canvases.c.created_at).where(
                    and_(
                        agent_canvases.c.agent_name == agent_name,
                        agent_canvases.c.canvas_id == canvas_id,
                    )
                )
            ).first()
            if existing:
                conn.execute(
                    update(agent_canvases)
                    .where(
                        and_(
                            agent_canvases.c.agent_name == agent_name,
                            agent_canvases.c.canvas_id == canvas_id,
                        )
                    )
                    .values(
                        title=title,
                        blocks=payload,
                        audience=normalize_audience(audience),
                        updated_at=now,
                        updated_by_execution_id=execution_id,
                    )
                )
                created_at = existing[0]
            else:
                created_at = now
                conn.execute(
                    insert(agent_canvases).values(
                        agent_name=agent_name,
                        canvas_id=canvas_id,
                        title=title,
                        blocks=payload,
                        audience=normalize_audience(audience),
                        schema_version=1,
                        created_at=created_at,
                        updated_at=now,
                        updated_by_execution_id=execution_id,
                    )
                )
        return {
            "agent_name": agent_name,
            "canvas_id": canvas_id,
            "title": title,
            "audience": normalize_audience(audience),
            "schema_version": 1,
            "created_at": created_at,
            "updated_at": now,
            "updated_by_execution_id": execution_id,
            "blocks": blocks,
        }

    def delete_canvas(self, agent_name: str, canvas_id: str) -> bool:
        """Remove a canvas. Returns whether a row was actually deleted."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_canvases).where(
                    and_(
                        agent_canvases.c.agent_name == agent_name,
                        agent_canvases.c.canvas_id == canvas_id,
                    )
                )
            )
        return bool(result.rowcount)
