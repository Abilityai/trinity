"""
Agent report database operations (#918).

Agents publish structured reports (telemetry / arbitrary domain reports) via the
MCP ``report`` tool → backend → this layer. Reports are surfaced on the Agent
Detail "Reports" tab and a fleet-wide Reports view.

SQLAlchemy Core over the ``agent_reports`` table in ``db/tables.py`` so it runs
unchanged on SQLite and PostgreSQL. Two response shapes:
- **summary** (list views): metadata only, never decodes ``payload``.
- **full** (detail view): includes the decoded ``payload``.
"""

import json
import uuid
from typing import Dict, List, Optional

from sqlalchemy import select, insert, delete, and_, func, or_

from .engine import get_engine
from .tables import agent_reports
from utils.helpers import utc_now_iso, iso_cutoff


# Metadata columns in DDL order — the list/summary projection (no payload).
_SUMMARY_COLUMNS = (
    agent_reports.c.id,
    agent_reports.c.agent_name,
    agent_reports.c.report_type,
    agent_reports.c.title,
    agent_reports.c.display_hint,
    agent_reports.c.schema_version,
    agent_reports.c.period_start,
    agent_reports.c.period_end,
    agent_reports.c.created_at,
    # ent#365, appended in review: operators need to answer "who was this
    # produced for" — the support question a deliverable creates. APPEND-only,
    # because `_row_to_summary` reads this tuple POSITIONALLY.
    agent_reports.c.addressed_to_email,
)


def _agent_reports_prune_predicate(cutoff: str):
    """WHERE clause for the #918 agent_reports retention sweep.

    #1644: shared with `count_agent_reports_candidates` so the guard's count and
    the prune's delete can never describe different row sets.
    """
    return agent_reports.c.created_at < cutoff


class ReportOperations:
    """Agent report database operations (#918)."""

    @staticmethod
    def _row_to_summary(row) -> Dict:
        """Metadata-only dict (no payload) from a `_SUMMARY_COLUMNS` row."""
        return {
            "id": row[0],
            "agent_name": row[1],
            "report_type": row[2],
            "title": row[3],
            "display_hint": row[4],
            "schema_version": row[5],
            "period_start": row[6],
            "period_end": row[7],
            "created_at": row[8],
            "addressed_to": row[9],
        }

    @staticmethod
    def _mapping_to_report(row) -> Dict:
        """Full dict (incl. decoded payload) from a name-accessible mapping row.

        ent#365 note: `addressed_to` is exposed here for the operator surfaces,
        but the AUDIENCE GATE must never read it — `get_report_for_client` runs
        its own SELECT for exactly that reason. A gate written against this
        whitelist would compare `None` to an email and deny everything: silently
        wrong, and fail-closed enough to look fine.
        """
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "user_id": row["user_id"],
            "report_type": row["report_type"],
            "title": row["title"],
            "payload": json.loads(row["payload"]) if row["payload"] else {},
            "display_hint": row["display_hint"],
            "schema_version": row["schema_version"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "created_at": row["created_at"],
            "addressed_to": row["addressed_to_email"],
        }

    def create_report(
        self,
        agent_name: str,
        user_id: Optional[int],
        report_type: str,
        title: str,
        payload: Dict,
        display_hint: Optional[str] = None,
        schema_version: int = 1,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        addressed_to_email: Optional[str] = None,
        portal_session_id: Optional[str] = None,
    ) -> Dict:
        """Insert a report row and return the full report dict.

        ent#365: `addressed_to_email` is the Workspace audience and
        `portal_session_id` the chat it was produced in. Both arrive already
        decided by the caller — the router validates the addressee against the
        agent's roster and resolves the session from the publishing turn — so
        this layer stores them without re-deciding, in keeping with the db
        layer holding no policy.
        """
        report_id = str(uuid.uuid4())
        now = utc_now_iso()
        stmt = insert(agent_reports).values(
            id=report_id,
            agent_name=agent_name,
            user_id=user_id,
            report_type=report_type,
            title=title,
            payload=json.dumps(payload),
            display_hint=display_hint,
            schema_version=schema_version,
            period_start=period_start,
            period_end=period_end,
            created_at=now,
            addressed_to_email=addressed_to_email,
            portal_session_id=portal_session_id,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return {
            "id": report_id,
            "agent_name": agent_name,
            "user_id": user_id,
            "report_type": report_type,
            "title": title,
            "payload": payload,
            "display_hint": display_hint,
            "schema_version": schema_version,
            "period_start": period_start,
            "period_end": period_end,
            "created_at": now,
            "addressed_to_email": addressed_to_email,
            "portal_session_id": portal_session_id,
        }

    def get_report(self, report_id: str) -> Optional[Dict]:
        """Full report (incl. payload) by id, or None."""
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_reports).where(agent_reports.c.id == report_id)
            ).mappings().first()
        return self._mapping_to_report(row) if row else None

    def get_reports_for_agent(
        self,
        agent_name: str,
        report_type: Optional[str] = None,
        hours: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """Metadata list for one agent, newest first.

        `hours`/`search` (#1539) are built by `_fleet_conditions` rather than
        re-derived here: two independently-written predicates for "the same
        filter" drift the moment one side gains a column, and the fleet view is
        where an operator learns what `search` matches. Passing a single-agent
        list keeps the agent scope an ordinary `IN (...)` term.
        """
        conditions = self._fleet_conditions(
            [agent_name], report_type, hours, search, search_agent_name=False
        )
        if conditions is None:  # unreachable: a single-agent list is never empty
            return []
        stmt = (
            select(*_SUMMARY_COLUMNS)
            .where(and_(*conditions))
            .order_by(agent_reports.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with get_engine().connect() as conn:
            return [self._row_to_summary(r) for r in conn.execute(stmt).all()]

    def get_reports_for_client(
        self,
        agent_name: str,
        client_email: str,
        portal_session_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """Reports **addressed to this person** by this agent, newest first.

        ent#365. Deliberately NOT `get_reports_for_agent` with a filter bolted
        on: that method answers the operator question ("everything this agent
        published") and the Workspace asks a different one ("what was produced
        for me"). Before this, the Workspace listed the agent's entire report
        history to every rostered client, so one client saw reports produced for
        another — free-form agent JSON rendered on a client-facing surface.

        An empty/None `client_email` matches nothing rather than everything: the
        rows with a NULL audience are operator-only, and a caller who cannot be
        identified must not inherit them.
        """
        if not client_email:
            return []
        conditions = [
            agent_reports.c.agent_name == agent_name,
            agent_reports.c.addressed_to_email == client_email,
        ]
        if portal_session_id:
            conditions.append(agent_reports.c.portal_session_id == portal_session_id)
        stmt = (
            select(*_SUMMARY_COLUMNS)
            .where(and_(*conditions))
            .order_by(agent_reports.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with get_engine().connect() as conn:
            return [self._row_to_summary(r) for r in conn.execute(stmt).all()]

    def get_report_for_client(self, report_id: str, client_email: str) -> Optional[Dict]:
        """One report, only if it is addressed to this person (ent#365).

        The single gate the portal detail route needs: `get_report` answers by
        id alone, and an id is guessable enough to matter on a surface served to
        people outside the fleet.

        The audience is read with its own SELECT rather than off the returned
        dict: `_mapping_to_report` whitelists the fields it exposes and does not
        carry `addressed_to_email`, so a gate written against its output would
        compare `None` to an email and deny every report — fail-closed, and
        silently wrong. It also keeps the audience out of the operator response
        shape, which no caller asked to change.
        """
        if not client_email:
            return None
        with get_engine().connect() as conn:
            owner = conn.execute(
                select(agent_reports.c.addressed_to_email)
                .where(agent_reports.c.id == report_id)
            ).scalar()
        if owner != client_email:
            return None
        return self.get_report(report_id)

    def _fleet_conditions(
        self,
        agent_names: Optional[List[str]],
        report_type: Optional[str],
        hours: Optional[int],
        search: Optional[str],
        search_agent_name: bool = True,
    ) -> Optional[list]:
        """Shared WHERE conditions for fleet list/stats.

        Returns None to signal "no rows" (non-admin with empty access set).
        """
        conditions = []
        if agent_names is not None:
            if not agent_names:
                return None
            conditions.append(agent_reports.c.agent_name.in_(agent_names))
        if report_type:
            conditions.append(agent_reports.c.report_type == report_type)
        if hours:
            conditions.append(agent_reports.c.created_at >= iso_cutoff(hours=hours))
        if search:
            like = f"%{search}%"
            # `agent_name` is a useful search arm on the FLEET view (that is how
            # you find "everything scout published") but actively wrong on a
            # single-agent list: every row already carries that name, so a term
            # matching it — "recon" inside agent `recon-bot` — would return the
            # agent's whole history regardless of title, looking like search was
            # ignored (#1539).
            arms = [
                agent_reports.c.title.like(like),
                agent_reports.c.report_type.like(like),
            ]
            if search_agent_name:
                arms.append(agent_reports.c.agent_name.like(like))
            conditions.append(or_(*arms))
        return conditions

    def get_fleet_reports(
        self,
        agent_names: Optional[List[str]],
        report_type: Optional[str] = None,
        hours: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """Metadata list across accessible agents (admin: agent_names=None)."""
        conditions = self._fleet_conditions(agent_names, report_type, hours, search)
        if conditions is None:
            return []
        stmt = select(*_SUMMARY_COLUMNS)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(agent_reports.c.created_at.desc()).limit(limit).offset(offset)
        with get_engine().connect() as conn:
            return [self._row_to_summary(r) for r in conn.execute(stmt).all()]

    def get_fleet_report_stats(
        self,
        agent_names: Optional[List[str]],
        report_type: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> Dict:
        """Aggregate counts for the fleet KPI tiles: total, by_type, distinct agents."""
        conditions = self._fleet_conditions(agent_names, report_type, hours, None)
        if conditions is None:
            return {"total": 0, "by_type": {}, "agents": 0}
        where = and_(*conditions) if conditions else None

        by_type_stmt = select(
            agent_reports.c.report_type, func.count().label("n")
        ).group_by(agent_reports.c.report_type)
        agents_stmt = select(func.count(func.distinct(agent_reports.c.agent_name)))
        if where is not None:
            by_type_stmt = by_type_stmt.where(where)
            agents_stmt = agents_stmt.where(where)

        with get_engine().connect() as conn:
            by_type = {r["report_type"]: r["n"] for r in conn.execute(by_type_stmt).mappings()}
            agents = conn.execute(agents_stmt).scalar() or 0
        return {"total": sum(by_type.values()), "by_type": by_type, "agents": agents}

    def delete_report(self, agent_name: str, report_id: str) -> bool:
        """Delete a report scoped by BOTH agent_name and id (#918 Codex #2).

        Returns True if a row was deleted — the router 404s a mismatch, so an
        owner of agent A can never delete agent B's report by guessing its id.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_reports).where(
                    and_(
                        agent_reports.c.id == report_id,
                        agent_reports.c.agent_name == agent_name,
                    )
                )
            )
        return result.rowcount > 0

    def count_agent_reports_candidates(self, retention_days: int, limit: int) -> int:
        """#1644: how many rows `prune_agent_reports(retention_days)` would DELETE."""
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        inner = (
            select(agent_reports.c.id)
            .where(_agent_reports_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(inner)).scalar() or 0
            )

    def prune_agent_reports(self, retention_days: int = 90, chunk_size: int = 1000) -> int:
        """Delete reports older than ``retention_days`` (#918 retention sweep).

        Chunked DELETE (mirrors ``db/monitoring.cleanup_old_records``) so a large
        table doesn't hold the write lock for the full purge. Uses
        ``iso_cutoff()`` for Invariant #16 lex-safe comparison and the
        ``idx_agent_reports_created`` index. ``0`` disables the sweep.
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
                        select(agent_reports.c.id)
                        .where(_agent_reports_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(agent_reports).where(agent_reports.c.id.in_(ids))
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total
