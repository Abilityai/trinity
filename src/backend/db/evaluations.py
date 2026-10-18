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
from sqlalchemy.exc import IntegrityError

from db.engine import get_engine
from db.tables import agent_evaluations, schedule_executions
from utils.helpers import iso_cutoff, utc_now_iso


# ent#935 — the deterministic (Tier-0) evaluator's identity and its one-row rule.
# A Tier-0 row is keyed `(evaluator, target_kind='execution', target_id=<id>)`,
# which `idx_agent_evaluations_rating_target` makes unique: one grade per run,
# with no migration (TD-2). Rating readers all key on a `workspace:`/`operator:`
# evaluator, so these rows never enter a tally.
TIER0_EVALUATOR = "platform:tier0"
TIER0_TARGET_KIND = "execution"
TIER0_TRIGGERS = ("schedule", "agent")
TIER0_TERMINAL_STATUSES = ("success", "failed", "cancelled")


def derive_delegation(row) -> tuple:
    """`(caller_agent, parent_execution_id, delegation_mode)` for one execution
    row (ent#935 addendum A). Read off the row, never passed by a caller:
    `fan_out_id` → fan_out, `loop_id` → loop, `triggered_by='reminder'` →
    reminder, in that order. Anything else — including an agent-sourced run —
    is NULL, because whether a `chat_with_agent` call was sync or async is not
    recorded on the row, and a guessed mode is worse than none."""
    if not row:
        return (None, None, None)
    if row.get("fan_out_id"):
        mode = "fan_out"
    elif row.get("loop_id"):
        mode = "loop"
    elif row.get("triggered_by") == "reminder":
        mode = "reminder"
    else:
        mode = None
    return (row.get("source_agent_name") or None, row.get("parent_execution_id") or None, mode)


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

        def _apply(conn) -> Optional[dict]:
            """UPDATE if the rating exists; return None if it does not."""
            where = and_(
                agent_evaluations.c.evaluator == evaluator,
                agent_evaluations.c.target_kind == target_kind,
                agent_evaluations.c.target_id == target_id,
            )
            if not conn.execute(update(agent_evaluations).where(where).values(**values)).rowcount:
                return None
            return self._row_to_dict(
                conn.execute(select(agent_evaluations).where(where)).mappings().first()
            )

        with get_engine().begin() as conn:
            existing = _apply(conn)
            if existing is not None:
                return existing
            # Two first-ratings of the same target by the same person race here:
            # both UPDATE zero rows, both INSERT, and the unique index refuses
            # the loser. That is a double-click, and it must read as "your
            # rating is recorded", not as a 500 — so the loser re-runs the
            # UPDATE it should have run, which is what the winner just made
            # possible. The index stays the authority; this is how a caller
            # experiences it.
            eval_id = f"eval_{uuid.uuid4().hex[:16]}"
            insert_values = dict(
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
            )
            try:
                # SAVEPOINT, so the failed INSERT can be undone without ending
                # the transaction. Required for dialect portability: SQLite
                # tolerates reusing the connection after a constraint violation
                # (verified on SQLAlchemy 2.0.51), but PostgreSQL aborts the
                # whole transaction — "current transaction is aborted, commands
                # ignored until end of transaction block" — so a bare retry
                # would fix the default backend and break the other one.
                with conn.begin_nested():
                    conn.execute(insert(agent_evaluations).values(**insert_values))
            except IntegrityError:
                # The loser of that race. Its rating is not lost: the row now
                # exists, so the UPDATE it originally skipped is the correct
                # action — retried on the SAME connection.
                #
                # Review finding: it used to open a SECOND connection here, and
                # on SQLite (the default backend) that deadlocks against itself.
                # pysqlite holds a RESERVED write lock on `conn` for the life of
                # the transaction, and a constraint violation rolls back only
                # the failed STATEMENT, not the transaction — so the retry's
                # UPDATE waits on a lock its own caller is holding, burns the
                # 30s `connect_args["timeout"]`, and raises
                # `OperationalError: database is locked`, which `submit_rating`
                # turns into the 503 this design exists to avoid. Deterministic,
                # not a flake, and reachable from two tabs (the client-side
                # `sending` guard covers one component instance). PostgreSQL
                # tolerated it, so the DEFAULT backend was the broken one.
                #
                # The SAVEPOINT above is what makes reusing `conn` safe on
                # both dialects; the same connection is then the only form that
                # cannot block on itself.
                applied = _apply(conn)
                if applied is not None:
                    return applied
                raise
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
                # ent#366 review — CLIENT ratings only. `include_owned` lets an
                # operator reach the Workspace and rate their own agent, which
                # is right; counting it here is not. This number is shown to
                # every client of the agent and answers "how did this land with
                # people" — an owner test-clicking is not that audience, and
                # once counted the click was unrecoverable (the agent-principal
                # redaction anonymises both kinds to the bare word `workspace`).
                # Prefix-matched rather than NOT-LIKE'd on the operator form, so
                # a future evaluator kind is excluded by default instead of
                # silently joining the client tally.
                agent_evaluations.c.evaluator.like("workspace:%"),
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

    # ------------------------------------------------------------------
    # ent#935 — the Tier-0 deterministic evaluator's writer
    # ------------------------------------------------------------------

    def _tier0_update(self, conn, execution_id: str, values: dict) -> Optional[dict]:
        """UPDATE the run's Tier-0 row if it exists; None if it does not."""
        where = and_(
            agent_evaluations.c.evaluator == TIER0_EVALUATOR,
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == execution_id,
        )
        if not conn.execute(update(agent_evaluations).where(where).values(**values)).rowcount:
            return None
        return self._row_to_dict(
            conn.execute(select(agent_evaluations).where(where)).mappings().first()
        )

    def upsert_platform_evaluation(
        self,
        agent_name: str,
        *,
        execution_id: str,
        completion: Optional[bool],
        checks: Any,
        archetype: Optional[str] = None,
        quality: Optional[float] = None,
    ) -> dict:
        """Write (or re-grade) the Tier-0 row for one execution.

        One row per run: a second evaluation of the same execution — a FAILED
        terminal later overwritten by SUCCESS, two sweep workers — updates it in
        place. The delegation columns are re-derived from the execution row on
        EVERY write (addendum A), so a row never carries a stale link. A racing
        first INSERT is the ent#366 case: SAVEPOINT, then the loser's write is
        applied as the UPDATE on the same connection (see
        `upsert_workspace_rating` for why both halves are load-bearing).
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            ex = conn.execute(
                select(
                    schedule_executions.c.source_agent_name,
                    schedule_executions.c.parent_execution_id,
                    schedule_executions.c.fan_out_id,
                    schedule_executions.c.loop_id,
                    schedule_executions.c.triggered_by,
                ).where(schedule_executions.c.id == execution_id)
            ).mappings().first()
            caller_agent, parent_execution_id, delegation_mode = derive_delegation(
                dict(ex) if ex else None
            )
            values = {
                "agent_name": agent_name,
                "execution_id": execution_id,
                "archetype": archetype,
                "completion": None if completion is None else (1 if completion else 0),
                "quality": quality,
                "checks_json": json.dumps(checks) if checks is not None else None,
                "caller_agent": caller_agent,
                "parent_execution_id": parent_execution_id,
                "delegation_mode": delegation_mode,
                "updated_at": now,
            }
            existing = self._tier0_update(conn, execution_id, values)
            if existing is not None:
                return existing
            eval_id = f"eval_{uuid.uuid4().hex[:16]}"
            try:
                with conn.begin_nested():
                    conn.execute(insert(agent_evaluations).values(
                        id=eval_id, evaluator=TIER0_EVALUATOR, created_at=now,
                        target_kind=TIER0_TARGET_KIND, target_id=execution_id,
                        judge_json=None, comment=None, **values,
                    ))
            except IntegrityError:
                applied = self._tier0_update(conn, execution_id, values)
                if applied is not None:
                    return applied
                raise
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row)

    def latest_tier0_for_execution(self, execution_id: str) -> Optional[dict]:
        """The run's Tier-0 row, or None ("not evaluated"). Other evaluators'
        rows for the same execution are a different axis and never answer."""
        stmt = select(agent_evaluations).where(and_(
            agent_evaluations.c.evaluator == TIER0_EVALUATOR,
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == execution_id,
        )).limit(1)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def unevaluated_execution_ids(self, *, limit: int = 25, settle_seconds: int = 30,
                                  lookback_hours: int = 24) -> list[str]:
        """Terminal schedule/agent runs with no Tier-0 row yet, oldest first.

        Bounded on every side: settled (completed at least `settle_seconds` ago,
        so evidence written just after the CAS has landed), recent (no backfill
        past `lookback_hours`), and at most `limit` ids. Cutoffs are computed in
        Python (Invariant #16 — `completed_at` is an ISO-Z string).
        """
        evaluated = select(agent_evaluations.c.id).where(and_(
            agent_evaluations.c.evaluator == TIER0_EVALUATOR,
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == schedule_executions.c.id,
        ))
        stmt = (
            select(schedule_executions.c.id)
            .where(and_(
                schedule_executions.c.status.in_(TIER0_TERMINAL_STATUSES),
                schedule_executions.c.triggered_by.in_(TIER0_TRIGGERS),
                schedule_executions.c.completed_at >= iso_cutoff(lookback_hours),
                schedule_executions.c.completed_at <= iso_cutoff(seconds=settle_seconds),
                ~evaluated.exists(),
            ))
            .order_by(schedule_executions.c.completed_at.asc())
            .limit(max(0, int(limit)))
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

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
