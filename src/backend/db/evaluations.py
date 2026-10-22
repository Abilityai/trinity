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

from sqlalchemy import select, insert, update, delete, and_, or_, func, case
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
# ent#940 TD-2 (ent#935 addendum A): delegation triggers, graded only when
# another agent sent the run (`source_agent_name` set) — a fan-out, loop or
# reminder an agent set up for itself or a person started is not an edge.
TIER0_DELEGATED_TRIGGERS = ("fan_out", "loop", "reminder")
TIER0_TERMINAL_STATUSES = ("success", "failed", "cancelled")
# ent#940 — the pair (caller→callee edge) evaluator. Its row hangs off the
# callee run under the same one-row key, so it needs no migration either. The
# id shares the `platform:tier0` stem but every Tier-0 reader matches the bare
# string exactly, so a pair row is never counted as a Tier-0 grade.
TIER0_PAIR_EVALUATOR = "platform:tier0:pair"
# The closed set the platform writer may file under. Anything else is refused:
# the writer is fenced (test_935_write_fence.py), and a free-form evaluator
# would let a caller of it mint a new platform identity.
PLATFORM_EVALUATORS = (TIER0_EVALUATOR, TIER0_PAIR_EVALUATOR)


def _platform_evaluator(evaluator: str) -> str:
    if evaluator not in PLATFORM_EVALUATORS:
        raise ValueError(f"not a platform evaluator: {evaluator!r}")
    return evaluator


def tier0_eligible(triggered_by: Optional[str], source_agent_name: Optional[str]) -> bool:
    """Whether Tier-0 grades a run with this trigger and sender (ent#940 TD-2).
    The one rule the candidate query and `evaluate_execution` both apply."""
    if triggered_by in TIER0_TRIGGERS:
        return True
    return triggered_by in TIER0_DELEGATED_TRIGGERS and bool(source_agent_name)


# ent#936 — the sampled judge (Tier-1) writes a SECOND platform row per run,
# `evaluator='platform:tier1:<model>'`, keyed on the same `(evaluator,
# target_kind='execution', target_id=<id>)` index. Two rows, not one: a Tier-0
# re-grade emits `quality=NULL` on ITS row on every write, so a verdict written
# in place would be clobbered by the next FAILED→SUCCESS relabel (TD-5). The
# judge itself (sampling, prompt, provider call) is an enterprise module; this
# layer is the seam it writes through — a claim, a finish, a candidate read and
# a schema guard — and takes execution ids and column values only. Nothing here
# knows a caller; no route reaches these writers (test_935_write_fence.py).
TIER1_EVALUATOR_PREFIX = "platform:tier1:"
TIER1_CLAIM_STALE_SECONDS = 600

JUDGE_SCHEMA = 1
JUDGE_STATES = ("claimed", "judged", "skipped")
JUDGE_FINAL_STATES = ("judged", "skipped")
JUDGE_VERDICTS = ("graded", "indeterminate")
JUDGE_DIMENSIONS = ("scope_fidelity", "escalation_correctness", "intent_alignment",
                    "helpfulness")
JUDGE_DIMENSION_VERDICTS = ("pass", "fail", "n/a")
JUDGE_REASON_MAX_CHARS = 160
# A reason is structural, never a quotation: the no-quote guard proper (a
# window of the transcript) lives beside the transcript, in the judge;
# here the structural proxy is that a reason carries no quotation mark at all.
_QUOTE_CHARS = frozenset('"\'`\u201c\u201d\u2018\u2019\u00ab\u00bb')
JUDGE_SKIP_REASONS = frozenset({
    "not_sampled", "excluded_trigger", "excluded_channel", "excluded_archetype",
    "run_too_cheap", "cost_unknown", "model_unknown", "same_family",
    "credential_echo", "no_response", "judge_unavailable", "judge_budget",
    "provider_error", "unparseable", "agent_opted_out", "flag_off",
})
# The claim marker as `json.dumps` serialises it (default separators). The
# finish writer's compare-and-set keys on it in SQL, so it is pinned here, once.
_CLAIMED_MARKER = '"state": "claimed"'


def tier1_evaluator(model: str) -> str:
    """`platform:tier1:<model>` — the row key of a judge verdict. The model id
    is part of the key, so it must be a bare id (no whitespace, non-empty)."""
    if not isinstance(model, str) or not model or any(c.isspace() for c in model):
        raise ValueError("judge model id must be a non-empty bare id")
    return f"{TIER1_EVALUATOR_PREFIX}{model}"


class JudgeJsonError(ValueError):
    """A `judge_json` document that fails the structural guard. `code` names
    the defect (`judge_<field>_<problem>`); the row is never written."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def validate_judge_json(judge: Any) -> dict:
    """The structural guard on a finished judge document. Structural fields
    ONLY: schema, state, the dimension/verdict enums, the reason cap and the
    no-quotation-mark rule, the quality range, the skip enum. Whether
    `quality` agrees with the dimensions is the rubric's job, not this
    layer's. An omitted dimension is `n/a`. Returns the normalised document;
    raises `JudgeJsonError` with a named code otherwise."""
    if not isinstance(judge, dict):
        raise JudgeJsonError("judge_json_not_object")
    if judge.get("schema") != JUDGE_SCHEMA:
        raise JudgeJsonError("judge_schema_unsupported", str(judge.get("schema")))
    state = judge.get("state")
    if state not in JUDGE_FINAL_STATES:
        raise JudgeJsonError("judge_state_invalid", str(state))
    out = dict(judge)
    if state == "skipped":
        if judge.get("skip") not in JUDGE_SKIP_REASONS:
            raise JudgeJsonError("judge_skip_unknown", str(judge.get("skip")))
        return out
    # judged
    if not isinstance(judge.get("model"), str) or not judge["model"]:
        raise JudgeJsonError("judge_model_missing")
    dims = judge.get("dimensions")
    if not isinstance(dims, dict):
        raise JudgeJsonError("judge_dimensions_invalid")
    unknown = set(dims) - set(JUDGE_DIMENSIONS)
    if unknown:
        raise JudgeJsonError("judge_dimension_unknown", ",".join(sorted(unknown)))
    norm = {}
    for name in JUDGE_DIMENSIONS:
        dim = dims.get(name)
        if dim is None:
            norm[name] = {"verdict": "n/a"}
            continue
        if not isinstance(dim, dict) or dim.get("verdict") not in JUDGE_DIMENSION_VERDICTS:
            raise JudgeJsonError("judge_dimension_verdict_invalid", name)
        entry = {"verdict": dim["verdict"]}
        if "reason" in dim and dim["reason"] is not None:
            reason = dim["reason"]
            if not isinstance(reason, str):
                raise JudgeJsonError("judge_reason_invalid", name)
            if len(reason) > JUDGE_REASON_MAX_CHARS:
                raise JudgeJsonError("judge_reason_too_long", name)
            if any(c in _QUOTE_CHARS for c in reason):
                raise JudgeJsonError("judge_reason_quoted", name)
            entry["reason"] = reason
        norm[name] = entry
    out["dimensions"] = norm
    quality = judge.get("quality")
    if quality is not None:
        if isinstance(quality, bool) or not isinstance(quality, (int, float)) \
                or not (0.0 <= float(quality) <= 1.0):
            raise JudgeJsonError("judge_quality_out_of_range", str(quality))
        out["quality"] = float(quality)
    if judge.get("verdict") not in JUDGE_VERDICTS:
        raise JudgeJsonError("judge_verdict_invalid", str(judge.get("verdict")))
    return out


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


def tier0_gate_outcome(checks: Any) -> Optional[bool]:
    """This Tier-0 row's gate outcome (ent#935, FR-10a): True passed, False
    failed, None when no gate check was graded. Shared by the analytics
    rollup and the evaluation read API (ent#937), so "graded" and "passed"
    have one definition.

    Takes the stored `checks_json` text or the parsed dict. Graded = at least
    one gate passed or failed. A skip-only row (every gate skipped — an
    evaluator timeout, nothing to check) is None: the execution page reads it
    as "No gate check applied", so a rollup must not count it as a failure
    either. A row whose headline cannot be read — malformed or wrong-typed —
    is None too: it shows no gate was graded. A graded row passed when no
    gate failed.
    """
    head = checks
    if isinstance(head, (str, bytes)) or head is None:
        try:
            head = json.loads(head or "null")
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    head = head.get("headline") if isinstance(head, dict) else None
    if not isinstance(head, dict):
        return None
    try:
        gate_pass = int(head.get("gate_pass") or 0)
        gate_fail = int(head.get("gate_fail") or 0)
    except (TypeError, ValueError):
        return None
    if gate_pass + gate_fail == 0:
        return None
    return gate_fail == 0 and head.get("passed") is True


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

    def _tier0_update(self, conn, execution_id: str, values: dict,
                      evaluator: str = TIER0_EVALUATOR) -> Optional[dict]:
        """UPDATE the run's platform row (under `evaluator`) if it exists; None
        if it does not."""
        where = and_(
            agent_evaluations.c.evaluator == evaluator,
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
        evaluator: str = TIER0_EVALUATOR,
    ) -> dict:
        """Write (or re-grade) the Tier-0 row for one execution.

        `evaluator` (ent#940) picks which platform row: the Tier-0 grade by
        default, or the pair row (`TIER0_PAIR_EVALUATOR`). Any other value
        raises `ValueError` before anything is read or written.

        One row per run: a second evaluation of the same execution — a FAILED
        terminal later overwritten by SUCCESS, two sweep workers — updates it in
        place. The delegation columns are re-derived from the execution row on
        EVERY write (addendum A), so a row never carries a stale link. A racing
        first INSERT is the ent#366 case: SAVEPOINT, then the loser's write is
        applied as the UPDATE on the same connection (see
        `upsert_workspace_rating` for why both halves are load-bearing).
        """
        evaluator = _platform_evaluator(evaluator)
        now = utc_now_iso()
        with get_engine().begin() as conn:
            values = self._tier0_values(conn, agent_name, execution_id, completion,
                                        checks, archetype, quality, now)
            existing = self._tier0_update(conn, execution_id, values, evaluator)
            if existing is not None:
                return existing
            eval_id = f"eval_{uuid.uuid4().hex[:16]}"
            try:
                with conn.begin_nested():
                    self._tier0_insert(conn, eval_id, values, now, evaluator)
            except IntegrityError:
                applied = self._tier0_update(conn, execution_id, values, evaluator)
                if applied is not None:
                    return applied
                raise
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row)

    def insert_platform_evaluation_if_absent(
        self,
        agent_name: str,
        *,
        execution_id: str,
        completion: Optional[bool],
        checks: Any,
        archetype: Optional[str] = None,
        evaluator: str = TIER0_EVALUATOR,
    ) -> Optional[dict]:
        """INSERT the run's Tier-0 row only if it has none; None if it has one.
        `evaluator` as in `upsert_platform_evaluation` (ent#940).

        The writer for a bookkeeping row (the sweep's `evaluator_timeout`) that
        must never replace a real grade. There is deliberately NO update
        fallback: an `IntegrityError` on the one-row index means a grade landed
        first (possibly after the caller's own existence check), and it is kept.
        A later real grade still re-grades this row in place through
        `upsert_platform_evaluation`.
        """
        evaluator = _platform_evaluator(evaluator)
        now = utc_now_iso()
        eval_id = f"eval_{uuid.uuid4().hex[:16]}"
        with get_engine().begin() as conn:
            values = self._tier0_values(conn, agent_name, execution_id, completion,
                                        checks, archetype, None, now)
            try:
                with conn.begin_nested():
                    self._tier0_insert(conn, eval_id, values, now, evaluator)
            except IntegrityError:
                return None
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row)

    def _tier0_insert(self, conn, eval_id: str, values: dict, now: str,
                      evaluator: str = TIER0_EVALUATOR) -> None:
        conn.execute(insert(agent_evaluations).values(
            id=eval_id, evaluator=evaluator, created_at=now,
            target_kind=TIER0_TARGET_KIND, target_id=values["execution_id"],
            judge_json=None, comment=None, **values,
        ))

    def _tier0_values(self, conn, agent_name, execution_id, completion, checks,
                      archetype, quality, now) -> dict:
        """The column values of a Tier-0 write; delegation re-derived each time."""
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
        return {
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

    def latest_tier0_for_execution(self, execution_id: str, *,
                                   evaluator: str = TIER0_EVALUATOR) -> Optional[dict]:
        """The run's Tier-0 row, or None ("not evaluated"). Other evaluators'
        rows for the same execution are a different axis and never answer.
        `evaluator=TIER0_PAIR_EVALUATOR` reads the pair row instead (ent#940)."""
        evaluator = _platform_evaluator(evaluator)
        stmt = select(agent_evaluations).where(and_(
            agent_evaluations.c.evaluator == evaluator,
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == execution_id,
        )).limit(1)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_dict(row) if row else None

    def unevaluated_execution_ids(self, *, limit: int = 25, settle_seconds: int = 30,
                                  lookback_hours: int = 24) -> list[str]:
        """Terminal runs Tier-0 grades (`tier0_eligible`) with no Tier-0 row
        yet, oldest first.

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
                or_(schedule_executions.c.triggered_by.in_(TIER0_TRIGGERS),
                    and_(schedule_executions.c.triggered_by.in_(TIER0_DELEGATED_TRIGGERS),
                         schedule_executions.c.source_agent_name.isnot(None),
                         schedule_executions.c.source_agent_name != "")),
                schedule_executions.c.completed_at >= iso_cutoff(lookback_hours),
                schedule_executions.c.completed_at <= iso_cutoff(seconds=settle_seconds),
                ~evaluated.exists(),
            ))
            .order_by(schedule_executions.c.completed_at.asc())
            .limit(max(0, int(limit)))
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    # ------------------------------------------------------------------
    # ent#936 — the Tier-1 (judge) row: claim / finish / candidates / reclaim
    # ------------------------------------------------------------------

    def _tier1_row_for(self, conn, execution_id: str) -> Optional[dict]:
        """The run's Tier-1 row under ANY model, or None."""
        row = conn.execute(select(agent_evaluations).where(and_(
            agent_evaluations.c.evaluator.like(f"{TIER1_EVALUATOR_PREFIX}%"),
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == execution_id,
        )).limit(1)).mappings().first()
        return self._row_to_dict(row) if row else None

    def claim_tier1_row(self, execution_id: str, *, model: str) -> Optional[dict]:
        """INSERT the run's Tier-1 row in state `claimed`; None if it cannot.

        The compare-and-set that makes the judge's provider call happen at most
        once per run: the row is keyed `platform:tier1:<model>` on the one-row
        index, so two claims on one execution — two sweep leaders, the lease
        failing open — race to one INSERT and the loser's `IntegrityError` is
        its answer (`None`: no call, no spend). The existence check before it
        covers a claim under a DIFFERENT model id (a changed primary), which the
        index alone would admit; both run in one write transaction. A run
        without a Tier-0 row is not claimable: the judge grades on top of Tier-0
        evidence, and the row copies the Tier-0 row's agent, archetype and
        delegation link — nothing is passed in. There is NO update fallback: a
        row that exists, in any state, is the answer to "has this run been
        judged or decided", and a skip is final per run (TD-10).
        """
        evaluator = tier1_evaluator(model)
        now = utc_now_iso()
        eval_id = f"eval_{uuid.uuid4().hex[:16]}"
        with get_engine().begin() as conn:
            tier0 = conn.execute(select(agent_evaluations).where(and_(
                agent_evaluations.c.evaluator == TIER0_EVALUATOR,
                agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
                agent_evaluations.c.target_id == execution_id,
            )).limit(1)).mappings().first()
            if tier0 is None:
                return None
            if self._tier1_row_for(conn, execution_id) is not None:
                return None
            judge = {"schema": JUDGE_SCHEMA, "state": "claimed", "evaluator": evaluator,
                     "claimed_at": now}
            try:
                with conn.begin_nested():
                    conn.execute(insert(agent_evaluations).values(
                        id=eval_id, evaluator=evaluator, created_at=now, updated_at=now,
                        target_kind=TIER0_TARGET_KIND, target_id=execution_id,
                        agent_name=tier0["agent_name"], execution_id=execution_id,
                        archetype=tier0["archetype"], completion=None, quality=None,
                        checks_json=None, judge_json=json.dumps(judge), comment=None,
                        caller_agent=tier0["caller_agent"],
                        parent_execution_id=tier0["parent_execution_id"],
                        delegation_mode=tier0["delegation_mode"],
                    ))
            except IntegrityError:
                return None
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row)

    def finish_tier1_row(self, eval_id: str, *, judge: Any) -> Optional[dict]:
        """Write the verdict (or the skip) onto a claimed Tier-1 row; None if
        no claimed Tier-1 row has that id.

        `judge` passes `validate_judge_json` first, so a malformed document
        never reaches the table. The UPDATE is keyed on the Tier-1 evaluator
        prefix AND the `claimed` marker: a Tier-0 row's id writes nothing, and a
        row already judged or skipped is final — a late second finish (the
        original worker returning after its claim was reclaimed, a retried
        tick) is dropped (`None`), never an overwrite. `quality` is the
        document's `quality` (NULL for a skip); `completion` stays NULL — the
        judge grades quality, not the exit.
        """
        doc = validate_judge_json(judge)
        now = utc_now_iso()
        doc.setdefault("finished_at", now)
        where = and_(
            agent_evaluations.c.id == eval_id,
            agent_evaluations.c.evaluator.like(f"{TIER1_EVALUATOR_PREFIX}%"),
            agent_evaluations.c.judge_json.like(f"%{_CLAIMED_MARKER}%"),
        )
        with get_engine().begin() as conn:
            rc = conn.execute(update(agent_evaluations).where(where).values(
                quality=doc.get("quality"), judge_json=json.dumps(doc), updated_at=now,
            )).rowcount
            if not rc:
                return None
            row = conn.execute(
                select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
            ).mappings().first()
        return self._row_to_dict(row) if row else None

    def latest_tier1_for_execution(self, execution_id: str) -> Optional[dict]:
        """The run's Tier-1 row (any model, any state), or None."""
        with get_engine().connect() as conn:
            return self._tier1_row_for(conn, execution_id)

    def tier1_candidates(self, *, limit: int = 25, lookback_hours: int = 24) -> list[dict]:
        """Tier-0 rows whose run has no Tier-1 row yet, oldest first.

        The candidate IS the Tier-0 row (the sampler needs its checks and
        headline). Bounded: Tier-0 rows created within `lookback_hours`, at
        most `limit`. A run with a Tier-1 row in ANY state is never offered
        again — claimed means in flight, judged/skipped means final — until a
        stale claim is released by `reclaim_stale_tier1`. Cutoff computed in
        Python (Invariant #16).
        """
        tier0 = agent_evaluations.alias("tier0")
        judged = select(agent_evaluations.c.id).where(and_(
            agent_evaluations.c.evaluator.like(f"{TIER1_EVALUATOR_PREFIX}%"),
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.target_id == tier0.c.target_id,
        ))
        stmt = (
            select(tier0)
            .where(and_(
                tier0.c.evaluator == TIER0_EVALUATOR,
                tier0.c.target_kind == TIER0_TARGET_KIND,
                tier0.c.target_id.isnot(None),
                tier0.c.created_at >= iso_cutoff(lookback_hours),
                ~judged.exists(),
            ))
            .order_by(tier0.c.created_at.asc(), tier0.c.id.asc())
            .limit(max(0, int(limit)))
        )
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def reclaim_stale_tier1(self, *, stale_seconds: int = TIER1_CLAIM_STALE_SECONDS) -> int:
        """DELETE `claimed` Tier-1 rows older than `stale_seconds` (a worker
        that died mid-call) so the run is a candidate again. Finished rows
        are never touched — a skip is final. Returns the count released."""
        stmt = delete(agent_evaluations).where(and_(
            agent_evaluations.c.evaluator.like(f"{TIER1_EVALUATOR_PREFIX}%"),
            agent_evaluations.c.target_kind == TIER0_TARGET_KIND,
            agent_evaluations.c.judge_json.like(f"%{_CLAIMED_MARKER}%"),
            agent_evaluations.c.updated_at < iso_cutoff(seconds=max(0, int(stale_seconds))),
        ))
        with get_engine().begin() as conn:
            return int(conn.execute(stmt).rowcount or 0)

    def unevaluated_pair_execution_ids(self, *, limit: int = 25, lookback_hours: int = 24
                                       ) -> list[str]:
        """Delegated runs (ent#940) that have a Tier-0 row naming a caller and
        no pair row yet, oldest Tier-0 row first.

        The pair row hangs off the Tier-0 row, so a run Tier-0 never graded is
        never a candidate, and the Tier-0 sweep's settle window already applies.
        Bounded by `lookback_hours` on the Tier-0 row's `created_at` (ISO-Z,
        cutoff computed in Python — Invariant #16) and by `limit`.

        A pair row written while the caller was still running carries
        `skip: caller_running` and is not final: once the caller run is
        terminal the run is a candidate again, so the re-grade (an in-place
        upsert) replaces the skips with real results. A row whose caller read
        raised (`skip: caller_unreadable`) is re-picked the same way. Matched
        on the quoted reason token, which no JSON separator choice can split.

        Fresh candidates (no pair row) sort before provisional re-grades, so
        rows whose re-grade keeps failing never starve a tick's batch (I1).
        """
        t0 = agent_evaluations.alias("t0")
        pr = agent_evaluations.alias("pr")
        se = schedule_executions
        paired = select(pr.c.id).where(and_(
            pr.c.evaluator == TIER0_PAIR_EVALUATOR,
            pr.c.target_kind == TIER0_TARGET_KIND,
            pr.c.target_id == t0.c.target_id,
        ))
        caller_ended = select(se.c.id).where(and_(
            se.c.id == pr.c.parent_execution_id,
            se.c.status.in_(TIER0_TERMINAL_STATUSES),
        ))
        provisional = paired.where(or_(
            and_(pr.c.checks_json.like('%"caller_running"%'), caller_ended.exists()),
            pr.c.checks_json.like('%"caller_unreadable"%'),
        ))
        stmt = (
            select(t0.c.target_id)
            .where(and_(
                t0.c.evaluator == TIER0_EVALUATOR,
                t0.c.target_kind == TIER0_TARGET_KIND,
                t0.c.caller_agent.isnot(None),
                t0.c.created_at >= iso_cutoff(lookback_hours),
                or_(~paired.exists(), provisional.exists()),
            ))
            .order_by(case((paired.exists(), 1), else_=0), t0.c.created_at.asc())
            .limit(max(0, int(limit)))
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    def get_evaluation(self, eval_id: str) -> Optional[dict]:
        stmt = select(agent_evaluations).where(agent_evaluations.c.id == eval_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None

    def list_evaluations_for_agent(self, agent_name: str, limit: int = 50, *,
                                   caller: Optional[str] = None) -> list[dict]:
        """`caller` keeps the runs that agent delegated to this one (ent#937)."""
        stmt = select(agent_evaluations).where(agent_evaluations.c.agent_name == agent_name)
        if caller is not None:
            stmt = stmt.where(agent_evaluations.c.caller_agent == caller)
        stmt = stmt.order_by(agent_evaluations.c.created_at.desc()).limit(limit)
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def list_evaluations_for_agents(self, agent_names: Optional[list[str]],
                                    limit: int = 100, *,
                                    callers: Optional[list[str]] = None,
                                    caller: Optional[str] = None) -> list[dict]:
        """Fleet read. `agent_names=None` (admin) → all; a list → that set;
        empty list → none (the accessible-set tri-state). `callers` (same
        tri-state) and `caller` filter on the delegating agent (ent#937)."""
        stmt = select(agent_evaluations).order_by(agent_evaluations.c.created_at.desc())
        for names, col in ((agent_names, agent_evaluations.c.agent_name),
                           (callers, agent_evaluations.c.caller_agent)):
            if names is not None:
                if not names:
                    return []
                stmt = stmt.where(col.in_(list(names)))
        if caller is not None:
            stmt = stmt.where(agent_evaluations.c.caller_agent == caller)
        stmt = stmt.limit(limit)
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]
