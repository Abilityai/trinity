"""Data-access for behavioral evaluations (ent#206). The referee surface.

An `agent_evaluations` row is a run's *quality* grade, kept apart from the
clean-exit `completion` signal (schedule_executions.status). The load-bearing
rule: **the graded agent must never write its own grade.** This layer has no
concept of "the caller" — the write-fence lives at the router
(`routers/evaluations.py`: human-admin-only, `reject_agent_principal`) and, in
child 3, at the server-side evaluator that calls `create_evaluation` directly.
So the only writers are the platform and a dedicated evaluator; no agent-scoped
key reaches here.

Three-layer (Invariant #1): SQL only, no HTTP. Dual-track schema (#1183) —
db/schema.py + db/migrations.py (SQLite) and Alembic 0029 (PostgreSQL).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select, insert, update, and_, func

from db.engine import get_engine
from db.tables import agent_evaluations
from utils.helpers import utc_now_iso


class EvaluationOperations:
    """CRUD for the agent_evaluations referee surface."""

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        for k in ("checks_json", "judge_json"):
            raw = d.pop(k, None)
            key = k[:-5]  # checks_json -> checks
            try:
                d[key] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                d[key] = None
        # `completion` is stored 1/0 (INTEGER); surface it as a bool (None-safe)
        # so the db-layer shape matches the API and every consumer.
        if d.get("completion") is not None:
            d["completion"] = bool(d["completion"])
        return d

    def create_evaluation(
        self,
        agent_name: str,
        *,
        evaluator: str,
        execution_id: Optional[str] = None,
        archetype: Optional[str] = None,
        completion: Optional[bool] = None,
        quality: Optional[float] = None,
        checks: Optional[Any] = None,
        judge: Optional[Any] = None,
    ) -> dict:
        """Write one evaluation. `evaluator` records WHO graded (a platform pass
        name or an evaluator-agent id) — never the graded agent. Returns the row."""
        eval_id = f"eval_{uuid.uuid4().hex[:16]}"
        now = utc_now_iso()
        stmt = insert(agent_evaluations).values(
            id=eval_id,
            agent_name=agent_name,
            execution_id=execution_id,
            archetype=archetype,
            completion=(None if completion is None else (1 if completion else 0)),
            quality=quality,
            checks_json=json.dumps(checks) if checks is not None else None,
            judge_json=json.dumps(judge) if judge is not None else None,
            evaluator=evaluator,
            created_at=now,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return self.get_evaluation(eval_id)

    # ------------------------------------------------------------------
    # ent#366 — one-click Workspace ratings
    # ------------------------------------------------------------------

    RATING_UP = 1.0
    RATING_DOWN = 0.0

    def upsert_workspace_rating(
        self,
        agent_name: str,
        *,
        evaluator: str,
        target_kind: str,
        target_id: str,
        quality: float,
        comment: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> dict:
        """Record (or change) one person's rating of one thing.

        Idempotent per (evaluator, target) by construction — the unique index
        carries the rule, and this method's UPDATE-then-INSERT reflects it. A
        second thumb on the same message is a correction, not a second vote,
        which is what makes a raw tally mean "how many people", not "how many
        clicks".

        `comment` is stored as sent (it is the point of the box) and is
        withheld from the rated agent at the READ boundary — never here: a db
        layer that redacted by caller would be a policy in the wrong place, and
        the operator surfaces legitimately need the text.

        Clearing the comment is explicit: passing None on a re-rate leaves the
        previous text, because a person changing thumbs-down to thumbs-up has
        not necessarily retracted what they wrote.
        """
        now = utc_now_iso()
        values = {
            "quality": quality,
            "updated_at": now,
        }
        if comment is not None:
            values["comment"] = comment
        if execution_id is not None:
            values["execution_id"] = execution_id
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_evaluations)
                .where(and_(
                    agent_evaluations.c.evaluator == evaluator,
                    agent_evaluations.c.target_kind == target_kind,
                    agent_evaluations.c.target_id == target_id,
                ))
                .values(**values)
            )
            if result.rowcount:
                row = conn.execute(
                    select(agent_evaluations).where(and_(
                        agent_evaluations.c.evaluator == evaluator,
                        agent_evaluations.c.target_kind == target_kind,
                        agent_evaluations.c.target_id == target_id,
                    ))
                ).mappings().first()
                return self._row_to_dict(row)
            eval_id = f"eval_{uuid.uuid4().hex[:16]}"
            conn.execute(insert(agent_evaluations).values(
                id=eval_id,
                agent_name=agent_name,
                execution_id=execution_id,
                archetype=None,
                completion=None,
                quality=quality,
                checks_json=None,
                judge_json=None,
                evaluator=evaluator,
                created_at=now,
                target_kind=target_kind,
                target_id=target_id,
                comment=comment,
                updated_at=now,
            ))
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row)

    def get_workspace_rating(self, evaluator: str, target_kind: str,
                             target_id: str) -> Optional[dict]:
        """This person's current rating of this thing, if any."""
        stmt = select(agent_evaluations).where(and_(
            agent_evaluations.c.evaluator == evaluator,
            agent_evaluations.c.target_kind == target_kind,
            agent_evaluations.c.target_id == target_id,
        ))
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def list_workspace_ratings_for_targets(self, evaluator: str, target_kind: str,
                                           target_ids: list) -> dict:
        """`{target_id: quality}` for ONE evaluator over many targets (ent#366).

        One query per thread rather than one per message. Scoped to a single
        evaluator by construction — this is how a client sees their own thumb
        and never anyone else's.
        """
        if not evaluator or not target_ids:
            return {}
        stmt = select(
            agent_evaluations.c.target_id, agent_evaluations.c.quality
        ).where(and_(
            agent_evaluations.c.evaluator == evaluator,
            agent_evaluations.c.target_kind == target_kind,
            agent_evaluations.c.target_id.in_(list(target_ids)),
        ))
        with get_engine().connect() as conn:
            return {tid: q for tid, q in conn.execute(stmt).all()}

    def workspace_rating_tally(self, agent_name: str) -> dict:
        """Raw up/down counts for an agent — never a percentage (ent#366 AC #4).

        At the volumes this surface sees, a percentage is the dishonest form: one
        thumbs-down out of one rating is "100% negative", which is a number that
        looks like evidence and is not. The denominator is the honest part, so
        both numbers are returned and the caller renders both.
        """
        stmt = (
            select(agent_evaluations.c.quality, func.count().label("n"))
            .where(and_(
                agent_evaluations.c.agent_name == agent_name,
                agent_evaluations.c.target_id.isnot(None),
            ))
            .group_by(agent_evaluations.c.quality)
        )
        up = down = 0
        with get_engine().connect() as conn:
            for quality, n in conn.execute(stmt).all():
                if quality is None:
                    continue
                if quality >= 0.5:
                    up += int(n)
                else:
                    down += int(n)
        return {"up": up, "down": down, "total": up + down}

    def get_evaluation(self, eval_id: str) -> Optional[dict]:
        stmt = select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None

    def list_evaluations_for_agent(self, agent_name: str, limit: int = 50) -> list[dict]:
        stmt = (
            select(agent_evaluations)
            .where(agent_evaluations.c.agent_name == agent_name)
            .order_by(agent_evaluations.c.created_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def list_evaluations_for_agents(self, agent_names: Optional[list[str]],
                                    limit: int = 100) -> list[dict]:
        """Fleet read. `agent_names=None` (admin) → all; a list → that set;
        empty list → none (the accessible-set tri-state)."""
        stmt = select(agent_evaluations).order_by(agent_evaluations.c.created_at.desc())
        if agent_names is not None:
            if not agent_names:
                return []
            stmt = stmt.where(agent_evaluations.c.agent_name.in_(agent_names))
        stmt = stmt.limit(limit)
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def latest_for_execution(self, execution_id: str) -> Optional[dict]:
        stmt = (
            select(agent_evaluations)
            .where(agent_evaluations.c.execution_id == execution_id)
            .order_by(agent_evaluations.c.created_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None
