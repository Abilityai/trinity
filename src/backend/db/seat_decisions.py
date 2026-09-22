"""The seat-level decision record (trinity-enterprise#638, ruling R25).

One row per decision a seat — an agent × the person it serves, the ent#637
memory scope — recorded: why a thing was approved, deferred or killed. The
ROWS are the history: a correction is a new row that `supersedes_id` the old
one (whose status becomes `superseded`), a close/reverse is a status flip on
the row, and expiry is never written — the service computes it from
`review_by` on read.

Two JSON documents ride in TEXT (`alternatives`, `cites`) — the tables.py
convention, no JSON type on either engine — and every stat is computed in
Python over one seat's rows by the service. SQLAlchemy Core so it runs
unchanged on SQLite and PostgreSQL. Emails are lower-cased here, at the
boundary, so two spellings never make two seats.
"""
import json
import secrets
from typing import Optional, List

from sqlalchemy import select, insert, update, func

from .engine import get_engine
from .tables import seat_decisions
from utils.helpers import utc_now_iso

DECISION_STATUSES = ("active", "superseded", "closed", "reversed", "routed")
_JSON_COLUMNS = ("alternatives", "cites")


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _decode(row) -> dict:
    d = dict(row)
    for col in _JSON_COLUMNS:
        raw = d.get(col)
        try:
            val = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            val = []
        d[col] = val if isinstance(val, list) else []
    return d


def _encode(values: dict) -> dict:
    out = dict(values)
    for col in _JSON_COLUMNS:
        if col in out and not isinstance(out[col], str):
            out[col] = json.dumps(list(out[col] or []))
    return out


class SeatDecisionOperations:
    """Seat decision record operations — rows only; policy lives in
    services/seat_decision_service.py."""

    def insert_seat_decision(self, values: dict) -> dict:
        """Insert one decision row. The caller has validated the grammar; the
        status vocabulary is the one thing checked here, at the sink."""
        if values.get("status", "active") not in DECISION_STATUSES:
            raise ValueError(f"unknown decision status: {values.get('status')!r}")
        now = utc_now_iso()
        row = _encode({
            "id": values.get("id") or secrets.token_urlsafe(12),
            "scope": "seat",
            "status": "active",
            "cites": [],
            **values,
            "seat_email": _norm_email(values["seat_email"]),
            "decided_by_person": _norm_email(values["decided_by_person"]),
            "created_at": now,
            "updated_at": now,
        })
        with get_engine().begin() as conn:
            conn.execute(insert(seat_decisions).values(**row))
        return _decode(row)

    def get_seat_decision(self, agent_name: str, decision_id: str) -> Optional[dict]:
        stmt = select(seat_decisions).where(
            seat_decisions.c.agent_name == agent_name,
            seat_decisions.c.id == decision_id,
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _decode(row) if row else None

    def list_seat_decisions(
        self, agent_name: str, seat_email: Optional[str] = None, *, limit: int = 500
    ) -> List[dict]:
        """Every row for the agent (or one seat), newest first, bounded."""
        stmt = select(seat_decisions).where(seat_decisions.c.agent_name == agent_name)
        if seat_email is not None:
            stmt = stmt.where(seat_decisions.c.seat_email == _norm_email(seat_email))
        stmt = stmt.order_by(seat_decisions.c.decided_at.desc(), seat_decisions.c.id).limit(limit)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_decode(r) for r in rows]

    def list_seat_decision_seats(self, agent_name: str, *, limit: int = 50) -> List[str]:
        """The distinct seats that hold records on this agent, bounded — the
        reader filter calls the assignment provider once per seat."""
        stmt = (
            select(seat_decisions.c.seat_email)
            .where(seat_decisions.c.agent_name == agent_name)
            .group_by(seat_decisions.c.seat_email)
            .order_by(func.max(seat_decisions.c.decided_at).desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(stmt).all()]

    def supersede_seat_decision(self, agent_name: str, old_id: str, values: dict) -> Optional[dict]:
        """Correct a decision: insert the new row and flip the old one to
        `superseded` in ONE transaction, gated on the old row still being
        `active` (CAS) — a double submit cannot produce two children. Returns
        the new row, or None when the old one was not active."""
        now = utc_now_iso()
        new_row = _encode({
            "id": values.get("id") or secrets.token_urlsafe(12),
            "scope": "seat",
            "status": "active",
            "cites": [],
            **values,
            "seat_email": _norm_email(values["seat_email"]),
            "decided_by_person": _norm_email(values["decided_by_person"]),
            "supersedes_id": old_id,
            "created_at": now,
            "updated_at": now,
        })
        with get_engine().begin() as conn:
            flipped = conn.execute(
                update(seat_decisions)
                .where(
                    seat_decisions.c.agent_name == agent_name,
                    seat_decisions.c.id == old_id,
                    seat_decisions.c.status == "active",
                )
                .values(status="superseded", updated_at=now)
            ).rowcount
            if not flipped:
                return None
            conn.execute(insert(seat_decisions).values(**new_row))
        return _decode(new_row)

    def set_seat_decision_status(
        self, agent_name: str, decision_id: str, status: str, *,
        reason: Optional[str], by: str,
    ) -> bool:
        """Close / reverse an ACTIVE decision (CAS). False when it was not active."""
        if status not in ("closed", "reversed"):
            raise ValueError(f"not a terminal decision status: {status!r}")
        now = utc_now_iso()
        with get_engine().begin() as conn:
            return bool(conn.execute(
                update(seat_decisions)
                .where(
                    seat_decisions.c.agent_name == agent_name,
                    seat_decisions.c.id == decision_id,
                    seat_decisions.c.status == "active",
                )
                .values(status=status, close_reason=reason, closed_at=now,
                        closed_by=_norm_email(by), updated_at=now)
            ).rowcount)

    def reconfirm_seat_decision(self, agent_name: str, decision_id: str, review_by: str) -> bool:
        """Move `review_by` forward on an ACTIVE decision (CAS)."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            return bool(conn.execute(
                update(seat_decisions)
                .where(
                    seat_decisions.c.agent_name == agent_name,
                    seat_decisions.c.id == decision_id,
                    seat_decisions.c.status == "active",
                )
                .values(review_by=review_by, reconfirmed_at=now, updated_at=now)
            ).rowcount)
