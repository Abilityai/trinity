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
from models import CANVAS_MAX_PER_AGENT
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class CanvasLimitExceeded(Exception):
    """An agent is at its canvas cap and tried to create another (ent#553).

    Raised by the db layer because that is where the count and the insert share
    a transaction; `services/canvas_service.py` maps it to the HTTP refusal so
    the router keeps its one error vocabulary.
    """

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
    # ent#537 — appended LAST on purpose: `_row_to_summary` reads by position,
    # so inserting in DDL order would shift every index after it.
    agent_canvases.c.template,
    # ent#553 — same rule, same reason: appended after `template`.
    agent_canvases.c.pinned,
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
            "template": row[8] or None,
            "pinned": bool(row[9]),
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
        raw = row[len(_SUMMARY_COLUMNS)]
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
        """Canvas metadata for one agent, pinned first then newest-updated.

        ``audience`` narrows to one audience — the Workspace passes
        ``roster``; operator surfaces pass nothing and see every canvas.
        """
        stmt = select(*_SUMMARY_COLUMNS).where(agent_canvases.c.agent_name == agent_name)
        if audience is not None:
            stmt = stmt.where(agent_canvases.c.audience == audience)
        # ent#553: pinned first, then newest-updated. Two keys rather than a
        # separate "pinned" list, so one ordered result serves both the rail
        # and the operator tab and neither has to re-sort (or disagree).
        stmt = stmt.order_by(
            agent_canvases.c.pinned.desc(), agent_canvases.c.updated_at.desc()
        )
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
        template: Optional[str] = None,
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
                select(agent_canvases.c.created_at, agent_canvases.c.pinned).where(
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
                        template=template,
                    )
                )
                created_at = existing[0]
                # The pin survives a write: it is the reader's ordering, and an
                # agent updating its canvas must not silently unpin it.
                pinned = bool(existing[1])
            else:
                # ent#553 — the cap is checked HERE, inside the transaction
                # that inserts, and only on this branch. Two properties follow
                # from that placement and both matter:
                #
                # * updating an existing canvas is never refused, however full
                #   the agent is. A cap that blocked updates would freeze an
                #   agent's live surfaces the moment it hit the limit, which
                #   punishes exactly the well-behaved agent that reuses ids.
                # * the count and the insert share one transaction, so this is
                #   not a check-then-act race — the read-then-write shape this
                #   method already had is what makes the bound real.
                #
                # It REFUSES rather than evicting: deleting an agent's work to
                # make room is the failure direction this codebase has been
                # bitten by (#1638), and the named error tells the agent to
                # retire a canvas itself.
                count = conn.execute(
                    select(func.count())
                    .select_from(agent_canvases)
                    .where(agent_canvases.c.agent_name == agent_name)
                ).scalar() or 0
                if count >= CANVAS_MAX_PER_AGENT:
                    raise CanvasLimitExceeded(
                        f"{agent_name} already has {count} canvases "
                        f"(limit {CANVAS_MAX_PER_AGENT}); retire one before "
                        "creating another"
                    )
                created_at = now
                pinned = False
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
                        template=template,
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
            "template": template,
            # Reported, never set, by the agent write path: a new canvas is
            # unpinned and an updated one keeps whatever the reader chose.
            "pinned": pinned,
            "blocks": blocks,
        }

    def count_canvases(self, agent_name: str) -> int:
        """How many canvases this agent holds (ent#553 — the cap's numerator).

        Read-only and outside any transaction: used to SHOW headroom, never to
        authorize a write. The write path counts inside its own transaction
        (`upsert_canvas`), because a count read here and acted on there is the
        check-then-act race the cap exists to close.
        """
        with get_engine().connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(agent_canvases)
                .where(agent_canvases.c.agent_name == agent_name)
            ).scalar() or 0

    def set_canvas_pinned(self, agent_name: str, canvas_id: str, pinned: bool) -> bool:
        """Pin or unpin one canvas. Returns whether a row was updated (ent#553)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_canvases)
                .where(
                    and_(
                        agent_canvases.c.agent_name == agent_name,
                        agent_canvases.c.canvas_id == canvas_id,
                    )
                )
                .values(pinned=1 if pinned else 0)
            )
        return bool(result.rowcount)

    def delete_canvases(self, agent_name: str, canvas_ids: List[str]) -> List[str]:
        """Delete several canvases, returning the ids that actually existed.

        Returning the deleted set rather than a count is what lets the caller
        report "3 of 5 removed" honestly and audit exactly what went — a bare
        rowcount cannot say WHICH, and a bulk delete that misreports its own
        scope is worse than one that deletes less.

        Scoped to one agent by construction: `agent_name` is in the WHERE, so a
        caller authorized for one agent cannot reach another's rows by passing
        foreign ids.
        """
        ids = [c for c in dict.fromkeys(canvas_ids) if c]
        if not ids:
            return []
        with get_engine().begin() as conn:
            present = [
                row[0]
                for row in conn.execute(
                    select(agent_canvases.c.canvas_id).where(
                        and_(
                            agent_canvases.c.agent_name == agent_name,
                            agent_canvases.c.canvas_id.in_(ids),
                        )
                    )
                )
            ]
            if present:
                conn.execute(
                    delete(agent_canvases).where(
                        and_(
                            agent_canvases.c.agent_name == agent_name,
                            agent_canvases.c.canvas_id.in_(present),
                        )
                    )
                )
        return present

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
