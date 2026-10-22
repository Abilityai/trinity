"""Read side of the evaluation referee surface (trinity-enterprise#937).

The rows every aggregate, failed list and chain view folds over. SQL only
(Invariant #1): scoping is decided by the caller (`services/
evaluation_read_service.py`) and arrives here as plain name sets; folding,
projection and redaction are the service's.

Three rules are carried by the SQL itself:

* **Ages out with the run.** The window is an INNER JOIN from the Tier-0 row
  to its `schedule_executions` row, so once execution retention removes a run
  its grade stops counting — no separate prune of `agent_evaluations`.
* **Bucketed on `completed_at`.** A run is in the window it finished in; a
  late re-grade restates that bucket, not today's.
* **A Tier-1 verdict is its own row** (`platform:tier1:<model>`, keyed like the
  Tier-0 row on `(target_kind='execution', target_id)`). It is LEFT JOINed one
  per execution, newest wins, so a run judged twice is still one run.
* **A pair row rides along** (`platform:tier0:pair`, ent#940): LEFT JOINed on
  the same unique key as `pair_checks`, so a delegated run is still one run.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from sqlalchemy import and_, exists, or_, select

from db.engine import get_engine
from db.evaluations import (
    TIER0_EVALUATOR, TIER0_PAIR_EVALUATOR, TIER0_TARGET_KIND, TIER1_EVALUATOR_PREFIX,
)
from db.tables import agent_evaluations, schedule_executions

# The person-label kinds (ent#366). Anything else is not a human label.
HUMAN_EVALUATOR_PREFIXES = ("workspace:", "operator:")
AGG_ROW_CAP = 5000
CHAIN_NODE_CAP = 200
_IN_CHUNK = 500

_NODE_COLUMNS = (
    schedule_executions.c.id,
    schedule_executions.c.agent_name,
    schedule_executions.c.source_agent_name,
    schedule_executions.c.parent_execution_id,
    schedule_executions.c.status,
    schedule_executions.c.triggered_by,
    schedule_executions.c.chain_depth,
    schedule_executions.c.started_at,
    schedule_executions.c.completed_at,
)


def _json(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _chunks(ids: list, size: int = _IN_CHUNK):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


class EvaluationAggregateOperations:
    """Bounded read queries behind the evaluation read API."""

    def window_rows(
        self,
        *,
        since: str,
        until: str,
        agents: Optional[Iterable[str]] = None,
        callers: Optional[Iterable[str]] = None,
        either: Optional[str] = None,
        caller: Optional[str] = None,
        callee: Optional[str] = None,
        schedule_id: Optional[str] = None,
        delegated_only: bool = False,
        cap: int = AGG_ROW_CAP,
    ) -> tuple[list[dict], bool]:
        """Tier-0-graded runs that completed in `[since, until)`, newest first.

        Returns `(rows, truncated)`; `truncated` is True when more than `cap`
        rows matched. Scope arrives as name sets, each with the accessible-set
        tri-state (None = unrestricted, empty = nothing):

        * `agents` — the graded agent (`agent_name`, the callee of a pair);
        * `callers` — the delegating agent; with `agents` this is the person
          pair rule, both sides visible;
        * `either` — an agent key's own name: rows where it is the graded
          agent OR the caller.

        `delegated_only` keeps rows with a `caller_agent` (the `by=pair` set).
        """
        se = schedule_executions.c
        ev = agent_evaluations.alias("ev")
        t1 = agent_evaluations.alias("t1")
        t2 = agent_evaluations.alias("t2")
        pr = agent_evaluations.alias("pr")

        def _tier1(alias):
            return and_(
                alias.c.target_kind == TIER0_TARGET_KIND,
                alias.c.evaluator.like(f"{TIER1_EVALUATOR_PREFIX}%"),
            )

        newer = exists().where(and_(
            _tier1(t2),
            t2.c.target_id == t1.c.target_id,
            or_(t2.c.created_at > t1.c.created_at,
                and_(t2.c.created_at == t1.c.created_at, t2.c.id > t1.c.id)),
        ))
        joined = (
            schedule_executions
            .join(ev, and_(
                ev.c.target_id == se.id,
                ev.c.evaluator == TIER0_EVALUATOR,
                ev.c.target_kind == TIER0_TARGET_KIND,
            ))
            .outerjoin(t1, and_(_tier1(t1), t1.c.target_id == se.id, ~newer))
            # One pair row per run (the ent#366 unique index), so no newest-wins.
            .outerjoin(pr, and_(
                pr.c.evaluator == TIER0_PAIR_EVALUATOR,
                pr.c.target_kind == TIER0_TARGET_KIND,
                pr.c.target_id == se.id,
            ))
        )
        conds = [se.completed_at >= since, se.completed_at < until]
        for names, col in ((agents, ev.c.agent_name), (callers, ev.c.caller_agent)):
            if names is not None:
                names = list(names)
                if not names:
                    return [], False
                conds.append(col.in_(names))
        if either is not None:
            conds.append(or_(ev.c.agent_name == either, ev.c.caller_agent == either))
        if caller is not None:
            conds.append(ev.c.caller_agent == caller)
        if callee is not None:
            conds.append(ev.c.agent_name == callee)
        if schedule_id is not None:
            conds.append(se.schedule_id == schedule_id)
        if delegated_only:
            conds.append(ev.c.caller_agent.isnot(None))

        cap = max(0, int(cap))
        stmt = (
            select(
                ev.c.id.label("evaluation_id"),
                ev.c.agent_name,
                ev.c.caller_agent,
                ev.c.parent_execution_id,
                ev.c.delegation_mode,
                ev.c.completion,
                ev.c.checks_json,
                se.id.label("execution_id"),
                se.schedule_id,
                se.status,
                se.triggered_by,
                se.started_at,
                se.completed_at,
                se.cost,
                t1.c.quality.label("judge_quality"),
                t1.c.judge_json,
                t1.c.evaluator.label("judge_evaluator"),
                pr.c.checks_json.label("pair_checks_json"),
            )
            .select_from(joined)
            .where(and_(*conds))
            .order_by(se.completed_at.desc(), se.id.desc())
            .limit(cap + 1)
        )
        with get_engine().connect() as conn:
            raw = conn.execute(stmt).mappings().all()
        rows = []
        for r in raw[:cap]:
            d = dict(r)
            d["checks"] = _json(d.pop("checks_json"))
            d["judge"] = _json(d.pop("judge_json"))
            d["pair_checks"] = _json(d.pop("pair_checks_json"))
            if d["completion"] is not None:
                d["completion"] = bool(d["completion"])
            rows.append(d)
        return rows, len(raw) > cap

    def window_agents(self, *, since: str, until: str) -> list[str]:
        """The distinct graded agents with a Tier-0 row completed in
        `[since, until)` — the retry list when the fleet window is over the
        cap (a capped page names only its newest agents)."""
        se = schedule_executions.c
        ev = agent_evaluations.alias("ev")
        stmt = (
            select(ev.c.agent_name).distinct()
            .select_from(schedule_executions.join(ev, and_(
                ev.c.target_id == se.id,
                ev.c.evaluator == TIER0_EVALUATOR,
                ev.c.target_kind == TIER0_TARGET_KIND,
            )))
            .where(and_(se.completed_at >= since, se.completed_at < until,
                        ev.c.agent_name.isnot(None)))
            .order_by(ev.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    def agent_costs(self, agent_name: str, *, since: str, until: str,
                    cap: int = AGG_ROW_CAP) -> list[float]:
        """The recorded cost of each of `agent_name`'s successful runs that
        completed in `[since, until)` — the cost baseline for ent#939's
        cost-vs-baseline indicator. Graded or not: the baseline is what the
        agent spends, not what was evaluated."""
        se = schedule_executions.c
        stmt = (
            select(se.cost)
            .where(and_(se.agent_name == agent_name, se.status == "success",
                        se.completed_at >= since, se.completed_at < until,
                        se.cost.isnot(None)))
            .order_by(se.completed_at.desc())
            .limit(max(0, int(cap)))
        )
        with get_engine().connect() as conn:
            return [float(r[0]) for r in conn.execute(stmt).all()]

    def human_labels(self, execution_ids: Iterable[str]) -> dict[str, list[float]]:
        """`{execution_id: [quality, ...]}` from person ratings (ent#366) that
        name the execution. Machine rows never answer."""
        ids = [i for i in dict.fromkeys(execution_ids) if i]
        out: dict[str, list[float]] = {}
        if not ids:
            return out
        ev = agent_evaluations.c
        person = or_(*[ev.evaluator.like(f"{p}%") for p in HUMAN_EVALUATOR_PREFIXES])
        with get_engine().connect() as conn:
            for chunk in _chunks(ids):
                stmt = select(ev.execution_id, ev.quality).where(and_(
                    ev.execution_id.in_(chunk), person, ev.quality.isnot(None),
                ))
                for eid, q in conn.execute(stmt).all():
                    out.setdefault(eid, []).append(float(q))
        return out

    # ------------------------------------------------------------------
    # the delegation chain (schedule_executions.parent_execution_id)
    # ------------------------------------------------------------------

    @staticmethod
    def _nodes(conn, where) -> list[dict]:
        return [dict(r) for r in conn.execute(select(*_NODE_COLUMNS).where(where)).mappings()]

    def chain_node(self, execution_id: str) -> Optional[dict]:
        with get_engine().connect() as conn:
            rows = self._nodes(conn, schedule_executions.c.id == execution_id)
        return rows[0] if rows else None

    def chain_ancestors(self, execution_id: str, *, max_depth: int,
                        visible: Optional[set] = None) -> tuple[list[dict], Optional[str]]:
        """Parents of `execution_id`, nearest first, and why the walk stopped
        early (`depth` | `access` | `missing`; None = reached the root).

        A parent that is gone or whose agent is not in `visible` is NOT
        returned — the walk stops before it, and the reason names a kind,
        never the id. A cycle ends at the visited set."""
        out: list[dict] = []
        seen = {execution_id}
        with get_engine().connect() as conn:
            start = self._nodes(conn, schedule_executions.c.id == execution_id)
            parent = start[0]["parent_execution_id"] if start else None
            while parent:
                if parent in seen:
                    return out, None
                if len(out) >= max_depth:
                    return out, "depth"
                rows = self._nodes(conn, schedule_executions.c.id == parent)
                if not rows:
                    return out, "missing"
                node = rows[0]
                if visible is not None and node["agent_name"] not in visible:
                    return out, "access"
                seen.add(parent)
                out.append(node)
                parent = node["parent_execution_id"]
        return out, None

    def chain_descendants(self, execution_id: str, *, max_depth: int,
                          node_cap: int = CHAIN_NODE_CAP,
                          visible: Optional[set] = None) -> tuple[list[dict], Optional[str]]:
        """Runs delegated from `execution_id`, breadth first, bounded by
        `max_depth` levels and `node_cap` nodes. A child whose agent is not in
        `visible` is dropped with its subtree (reason `access`); the stop
        reason is the first bound hit (`depth` | `nodes` | `access`)."""
        out: list[dict] = []
        seen = {execution_id}
        stop: Optional[str] = None
        level = [execution_id]
        depth = 0
        with get_engine().connect() as conn:
            while level:
                children = []
                for chunk in _chunks(level):
                    children.extend(self._nodes(
                        conn, schedule_executions.c.parent_execution_id.in_(chunk)))
                children = [c for c in sorted(children, key=lambda n: (n["started_at"] or "", n["id"]))
                            if c["id"] not in seen]
                if not children:
                    break
                if depth >= max_depth:
                    return out, "depth"
                nxt = []
                for c in children:
                    seen.add(c["id"])
                    if visible is not None and c["agent_name"] not in visible:
                        stop = stop or "access"
                        continue
                    if len(out) >= node_cap:
                        return out, "nodes"
                    out.append(c)
                    nxt.append(c["id"])
                level = nxt
                depth += 1
        return out, stop
