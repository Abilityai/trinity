"""The autonomy dial's earned half (trinity-enterprise#641, P12).

One row per (agent, seat, ask class). The row records what was EARNED — the
state, the evidence it rests on, its hash, and when that evidence expires —
and nothing that can change underneath it: the instance level, the agent's
autonomy switch and the clock are read-time conjuncts in
``services/autonomy_dial_service.live_verdict``, never columns here.

The write is a compare-and-set on ``evidence_hash``: an unchanged
re-evaluation writes nothing, and two workers racing the same event converge
on one row rather than trading writes. ``held`` is the operator's refusal and
survives every re-evaluation — it is the one field the rule never sets.

SQLAlchemy Core, so it runs unchanged on SQLite and PostgreSQL; the JSON
documents ride in TEXT (the tables.py convention). Emails are lower-cased at
the boundary, the ent#638 rule, so two spellings never make two seats.
"""
import json
import secrets
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, insert, select, update

from .engine import get_engine
from .tables import seat_ask_class_state
from utils.helpers import utc_now_iso

_JSON_COLUMNS = ("blocked_by", "evidence")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _decode(row) -> Dict[str, Any]:
    d = dict(row)
    for col, empty in (("blocked_by", []), ("evidence", {})):
        raw = d.get(col)
        try:
            d[col] = json.loads(raw) if raw else empty
        except (TypeError, ValueError):
            d[col] = empty
    d["held"] = bool(d.get("held"))
    return d


class SeatAskClassStateOperations:
    """Rows only — the rule lives in services/autonomy_dial_service.py."""

    def list_seat_ask_class_states(self, agent_name: str, seat_email: str) -> List[Dict[str, Any]]:
        stmt = select(seat_ask_class_state).where(and_(
            seat_ask_class_state.c.agent_name == agent_name,
            seat_ask_class_state.c.seat_email == _norm(seat_email),
        )).order_by(seat_ask_class_state.c.ask_class)
        with get_engine().connect() as conn:
            return [_decode(r) for r in conn.execute(stmt).mappings().all()]

    def get_seat_ask_class_state(self, agent_name: str, seat_email: str,
                                 ask_class: str) -> Optional[Dict[str, Any]]:
        stmt = select(seat_ask_class_state).where(and_(
            seat_ask_class_state.c.agent_name == agent_name,
            seat_ask_class_state.c.seat_email == _norm(seat_email),
            seat_ask_class_state.c.ask_class == ask_class,
        ))
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _decode(row) if row else None

    def upsert_seat_ask_class_state(
        self, *, agent_name: str, seat_email: str, ask_class: str, state: str,
        blocked_by: List[str], evidence: Dict[str, Any], evidence_hash: str,
        evidence_expires_at: Optional[str], previous_hash: Optional[str] = None,
    ) -> bool:
        """CAS on ``evidence_hash``. False when another writer got there first
        (the row no longer carries ``previous_hash``) — the caller re-reads
        rather than overwriting a verdict it did not compute."""
        now = utc_now_iso()
        values = {
            "state": state,
            "blocked_by": json.dumps(list(blocked_by or [])),
            "evidence": json.dumps(evidence or {}),
            "evidence_hash": evidence_hash,
            "evidence_expires_at": evidence_expires_at,
            "updated_at": now,
        }
        if state == "graduated":
            values["promoted_at"] = now
        else:
            values["demoted_at"] = now
        with get_engine().begin() as conn:
            where = and_(
                seat_ask_class_state.c.agent_name == agent_name,
                seat_ask_class_state.c.seat_email == _norm(seat_email),
                seat_ask_class_state.c.ask_class == ask_class,
            )
            if previous_hash is None:
                # No row yet in the caller's read — insert, and fall back to a
                # guarded update when a concurrent writer created it first.
                existing = conn.execute(select(seat_ask_class_state.c.id).where(where)).first()
                if existing is None:
                    conn.execute(insert(seat_ask_class_state).values(
                        id=secrets.token_urlsafe(12), agent_name=agent_name,
                        seat_email=_norm(seat_email), ask_class=ask_class,
                        guard_metric="not_assessed", held=0,
                        created_at=now, **values,
                    ))
                    return True
                return bool(conn.execute(update(seat_ask_class_state).where(and_(
                    where, seat_ask_class_state.c.evidence_hash.is_(None)
                )).values(**values)).rowcount)
            return bool(conn.execute(update(seat_ask_class_state).where(and_(
                where, seat_ask_class_state.c.evidence_hash == previous_hash
            )).values(**values)).rowcount)

    def set_seat_ask_class_hold(self, *, agent_name: str, seat_email: str, ask_class: str,
                                held: bool, by: str) -> Dict[str, Any]:
        """The operator's refusal (or its release). Upserts, because a class can
        be held before it has ever been evaluated — the hold must not depend on
        the rule having run first."""
        now = utc_now_iso()
        seat = _norm(seat_email)
        values = {"held": 1 if held else 0, "held_by": _norm(by) if held else None,
                  "held_at": now if held else None, "updated_at": now}
        where = and_(
            seat_ask_class_state.c.agent_name == agent_name,
            seat_ask_class_state.c.seat_email == seat,
            seat_ask_class_state.c.ask_class == ask_class,
        )
        with get_engine().begin() as conn:
            if not conn.execute(update(seat_ask_class_state).where(where).values(**values)).rowcount:
                conn.execute(insert(seat_ask_class_state).values(
                    id=secrets.token_urlsafe(12), agent_name=agent_name, seat_email=seat,
                    ask_class=ask_class, state="on_request", blocked_by="[]", evidence="{}",
                    guard_metric="not_assessed", created_at=now, **values,
                ))
            row = conn.execute(select(seat_ask_class_state).where(where)).mappings().first()
        return _decode(row)

    def set_seat_ask_class_guard(self, *, agent_name: str, seat_email: str, ask_class: str,
                                 guard_metric: str) -> bool:
        """The operator's guard-metric verdict for a class (canon's hard cap).
        Default is `not_assessed`, so "reviewed and clear" reads differently
        from "nobody looked"."""
        now = utc_now_iso()
        seat = _norm(seat_email)
        where = and_(
            seat_ask_class_state.c.agent_name == agent_name,
            seat_ask_class_state.c.seat_email == seat,
            seat_ask_class_state.c.ask_class == ask_class,
        )
        with get_engine().begin() as conn:
            if conn.execute(update(seat_ask_class_state).where(where).values(
                guard_metric=guard_metric, updated_at=now
            )).rowcount:
                return True
            conn.execute(insert(seat_ask_class_state).values(
                id=secrets.token_urlsafe(12), agent_name=agent_name, seat_email=seat,
                ask_class=ask_class, state="on_request", blocked_by="[]", evidence="{}",
                guard_metric=guard_metric, held=0, created_at=now, updated_at=now,
            ))
            return True
