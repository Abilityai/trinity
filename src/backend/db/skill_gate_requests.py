"""Gated-skill requests — the gate's durable record (trinity-enterprise#751).

A request to run a gated skill is frozen here while its approval ask is open.
The ask (``operator_queue``) records the DECISION; this row records the EFFECT.
``ask_service`` observers are in-process and at most once, so this row is also
what the reconcile sweep reads: an ask that ended while its record is still
``pending`` was never consumed.

Exactly once is two database facts, never an in-process flag:

* ``pending → dispatching`` is a compare-and-set on ``state`` (the claim), and
* ``dispatched_execution_id`` is UNIQUE, so two records can never point at one
  run and a racing claim that reuses an id fails instead of double-dispatching.

The lattice (``_ENTERED_FROM``) is written as "may be entered from", never as
"is not X", so a reflexive edge — ``dispatched → dispatched``, the double run —
is refused by construction:

    pending ──claim──▶ dispatching ──▶ dispatched | stale | not_run | unknown
       └──▶ denied | expired | cancelled | refused
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .engine import get_engine, make_insert
from .tables import operator_queue, schedule_executions, skill_gate_requests
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

PENDING = "pending"
DISPATCHING = "dispatching"
DISPATCHED = "dispatched"
STALE = "stale"
NOT_RUN = "not_run"
UNKNOWN = "unknown"
DENIED = "denied"
EXPIRED = "expired"
CANCELLED = "cancelled"
REFUSED = "refused"

# to_state → the states it may be entered from. `dispatching` is entered only
# through `claim_gate_request_for_dispatch`, which also writes the run's id.
_ENTERED_FROM = {
    DENIED: (PENDING,),
    EXPIRED: (PENDING,),
    CANCELLED: (PENDING,),
    REFUSED: (PENDING,),
    DISPATCHED: (DISPATCHING,),
    STALE: (DISPATCHING,),
    NOT_RUN: (DISPATCHING,),
    UNKNOWN: (DISPATCHING,),
}
TERMINAL_STATES = tuple(_ENTERED_FROM)
STATES = (PENDING, DISPATCHING) + TERMINAL_STATES

_JSON_COLUMNS = ("skills", "fingerprints", "dispatch")


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _row(r) -> Dict:
    row = dict(r._mapping)
    for col in _JSON_COLUMNS:
        row[col] = _decode(row.get(col))
    return row


class SkillGateRequestOperations:
    """The gate's record of each frozen request (trinity-enterprise#751)."""

    def create_gate_request(self, **fields) -> Tuple[Dict, bool]:
        """Insert a `pending` record; `(row, created)`.

        A second insert with the same `request_id` changes nothing and returns
        the FIRST row — the request id is one occurrence, so a retry of that
        occurrence must not overwrite what was frozen.
        """
        values = {c.name: fields.get(c.name) for c in skill_gate_requests.columns}
        for col in _JSON_COLUMNS:
            if values[col] is not None:
                values[col] = json.dumps(values[col], sort_keys=True)
        values["state"] = PENDING
        values["state_detail"] = None
        values["dispatched_execution_id"] = None
        values["created_at"] = fields.get("created_at") or utc_now_iso()
        values["decided_at"] = values["dispatched_at"] = values["notified_at"] = None
        stmt = make_insert(skill_gate_requests).values(**values).on_conflict_do_nothing(
            index_elements=["request_id"])
        with get_engine().begin() as conn:
            created = conn.execute(stmt).rowcount == 1
        return self.get_gate_request(values["request_id"]), created

    def get_gate_request(self, request_id: str) -> Optional[Dict]:
        stmt = select(skill_gate_requests).where(skill_gate_requests.c.request_id == request_id)
        with get_engine().connect() as conn:
            r = conn.execute(stmt).first()
        return _row(r) if r else None

    def get_gate_request_by_dispatched_execution(self, execution_id: str) -> Optional[Dict]:
        """The record whose approved run is `execution_id`, if any."""
        if not execution_id:
            return None
        stmt = select(skill_gate_requests).where(
            skill_gate_requests.c.dispatched_execution_id == execution_id)
        with get_engine().connect() as conn:
            r = conn.execute(stmt).first()
        return _row(r) if r else None

    def get_gate_requests_by_origin_executions(self, execution_ids: List[str]) -> Dict[str, Dict]:
        """`{origin execution id: record}` — which SKIPPED rows the gate closed
        (a gated scheduled / loop / fan-out run), read structurally rather than
        parsed out of the row's error text."""
        ids = [e for e in (execution_ids or []) if e]
        if not ids:
            return {}
        stmt = select(skill_gate_requests).where(
            skill_gate_requests.c.origin_execution_id.in_(ids))
        with get_engine().connect() as conn:
            return {r.origin_execution_id: _row(r) for r in conn.execute(stmt)}

    def attach_gate_ask(self, request_id: str, ask_item_id: str) -> bool:
        """Link the record to its ask row. Written once."""
        stmt = (update(skill_gate_requests)
                .where(and_(skill_gate_requests.c.request_id == request_id,
                            skill_gate_requests.c.ask_item_id.is_(None)))
                .values(ask_item_id=ask_item_id))
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def count_pending_gate_requests(self, agent_name: str,
                                    requester_key: Optional[str] = None) -> int:
        """Pending records on `agent_name`, optionally from one requester."""
        stmt = select(func.count()).select_from(skill_gate_requests).where(and_(
            skill_gate_requests.c.agent_name == agent_name,
            skill_gate_requests.c.state == PENDING))
        if requester_key is not None:
            stmt = stmt.where(skill_gate_requests.c.requester_key == requester_key)
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def claim_gate_request_for_dispatch(self, request_id: str, execution_id: str) -> bool:
        """`pending → dispatching`, writing the approved run's id in the same
        statement. True only for the one caller that won.

        The id is written here, before the run exists, so a crash between the
        claim and the run's row leaves a `dispatching` record whose run is
        missing — which the sweep turns into `unknown` and never re-runs.
        """
        stmt = (update(skill_gate_requests)
                .where(and_(skill_gate_requests.c.request_id == request_id,
                            skill_gate_requests.c.state == PENDING))
                .values(state=DISPATCHING, dispatched_execution_id=execution_id,
                        decided_at=utc_now_iso()))
        try:
            with get_engine().begin() as conn:
                return conn.execute(stmt).rowcount == 1
        except IntegrityError:
            logger.warning("[SkillGate] claim of %s refused: run id already taken", request_id)
            return False

    def transition_gate_request(self, request_id: str, to_state: str, *,
                                detail: Optional[str] = None) -> bool:
        """Move a record to a terminal state, only from the states that may
        enter it. True only for the caller whose write landed."""
        if to_state not in _ENTERED_FROM:
            raise ValueError(f"transition_gate_request: not a terminal state: {to_state!r}")
        now = utc_now_iso()
        values = {"state": to_state, "state_detail": detail}
        if to_state == DISPATCHED:
            values["dispatched_at"] = now
        elif _ENTERED_FROM[to_state] == (PENDING,):
            values["decided_at"] = now
        stmt = (update(skill_gate_requests)
                .where(and_(skill_gate_requests.c.request_id == request_id,
                            skill_gate_requests.c.state.in_(_ENTERED_FROM[to_state])))
                .values(**values))
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def mark_gate_request_notified(self, request_id: str) -> bool:
        """Stamp that the requester was told. True only the first time."""
        stmt = (update(skill_gate_requests)
                .where(and_(skill_gate_requests.c.request_id == request_id,
                            skill_gate_requests.c.notified_at.is_(None)))
                .values(notified_at=utc_now_iso()))
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def list_pending_gate_requests(self, agent_name: str) -> List[Dict]:
        stmt = (select(skill_gate_requests)
                .where(and_(skill_gate_requests.c.agent_name == agent_name,
                            skill_gate_requests.c.state == PENDING))
                .order_by(skill_gate_requests.c.created_at))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]

    def list_gate_requests_with_ended_asks(self, limit: int = 200) -> List[Dict]:
        """`pending` records whose ask is no longer pending — endings no
        observer consumed (a worker died, an observer raised)."""
        ended = exists().where(and_(
            operator_queue.c.id == skill_gate_requests.c.ask_item_id,
            operator_queue.c.status != "pending"))
        stmt = (select(skill_gate_requests)
                .where(and_(skill_gate_requests.c.state == PENDING,
                            skill_gate_requests.c.ask_item_id.isnot(None),
                            ended))
                .order_by(skill_gate_requests.c.created_at)
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]

    def list_gate_requests_lost_in_dispatch(self, claimed_before: str,
                                            limit: int = 200) -> List[Dict]:
        """`dispatching` records claimed before `claimed_before` whose run row
        never appeared — the process died between the claim and the run."""
        has_run = exists().where(
            schedule_executions.c.id == skill_gate_requests.c.dispatched_execution_id)
        stmt = (select(skill_gate_requests)
                .where(and_(skill_gate_requests.c.state == DISPATCHING,
                            skill_gate_requests.c.decided_at < claimed_before,
                            ~has_run))
                .order_by(skill_gate_requests.c.decided_at)
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]

    def list_gate_requests_dispatched_unrecorded(self, claimed_before: str,
                                                 limit: int = 200) -> List[Dict]:
        """`dispatching` records claimed before `claimed_before` whose run row
        DOES exist — the process died after starting the run and before
        recording it. The run is real; only the record and the notice lag."""
        has_run = exists().where(
            schedule_executions.c.id == skill_gate_requests.c.dispatched_execution_id)
        stmt = (select(skill_gate_requests)
                .where(and_(skill_gate_requests.c.state == DISPATCHING,
                            skill_gate_requests.c.decided_at < claimed_before,
                            has_run))
                .order_by(skill_gate_requests.c.decided_at)
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]

    def list_gate_requests_without_live_ask(self, created_before: str,
                                            limit: int = 200) -> List[Dict]:
        """`pending` records older than `created_before` with no ask behind
        them — never attached (the process died between raising and
        attaching), or the ask row is gone (cleared, retention). No ending will
        ever consume them, and they count against the caps."""
        live_ask = exists().where(operator_queue.c.id == skill_gate_requests.c.ask_item_id)
        stmt = (select(skill_gate_requests)
                .where(and_(skill_gate_requests.c.state == PENDING,
                            skill_gate_requests.c.created_at < created_before,
                            or_(skill_gate_requests.c.ask_item_id.is_(None), ~live_ask)))
                .order_by(skill_gate_requests.c.created_at)
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]
