"""Person tags on the queue-item ledger (trinity-enterprise#631).

A tag is ONE `operator_queue` row — the store behind the Inbox's four doors
(ent#610) — addressed to the person who was named. No table of its own: the
ledger already carries an addressee (`addressed_to_email`, ent#364), a
platform-written `context`, an idempotency key (`(agent_name, request_id)`)
and a read time (`acknowledged_at`, unused on a platform-minted row because no
agent ever acknowledges one).

What makes a tag different from an ask is its STATUS, decided once here:

* `delivered` — the person has not opened it (the Inbox's *Unread*);
* `read`      — they have.

Never `pending`. `pending` is the ask lifecycle (*Action*): it is what every
depth cap, expiry sweep, delivery flag, ending observer and operator badge
reads. A tag that entered that lifecycle would spend an agent's ask budget,
light the operator's queue, and could be "answered" or "cancelled" by anyone
with access to the agent. Starting outside it keeps all of that machinery blind
to the row by construction, rather than by a filter in each of them.

SQLAlchemy Core over `db/tables.py`, so it runs unchanged on SQLite and
PostgreSQL (#300). No schema change.
"""
from __future__ import annotations

import json
import uuid
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select, update

from .engine import get_engine, make_insert
from .tables import operator_queue

MENTION_TYPE = "mention"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"


def _row(row) -> Dict:
    item = dict(row)
    ctx = item.get("context")
    try:
        item["context"] = json.loads(ctx) if ctx else None
    except (TypeError, ValueError):
        item["context"] = None
    return item


class QueueMentionOperations:
    """The four reads and two writes a person tag needs."""

    _COLS = (
        operator_queue.c.id,
        operator_queue.c.agent_name,
        operator_queue.c.request_id,
        operator_queue.c.type,
        operator_queue.c.status,
        operator_queue.c.title,
        operator_queue.c.question,
        operator_queue.c.context,
        operator_queue.c.created_at,
        operator_queue.c.acknowledged_at,
        operator_queue.c.addressed_to_email,
    )

    def create(self, *, agent_name: str, request_id: str, addressed_to_email: str,
               title: str, question: str, context: Dict, now: str) -> Tuple[Dict, bool]:
        """Insert one tag; a repeat of the same `(agent_name, request_id)` is a
        no-op that returns the row that already exists. Returns `(row, inserted)`."""
        values = dict(
            id=uuid.uuid4().hex,
            agent_name=agent_name,
            request_id=request_id,
            type=MENTION_TYPE,
            status=STATUS_DELIVERED,
            priority="low",
            title=title,
            question=question,
            context=json.dumps(context),
            created_at=now,
            addressed_to_email=(addressed_to_email or "").strip().lower(),
            raised_by="person",
            channel=MENTION_TYPE,
        )
        stmt = make_insert(operator_queue).values(**values).on_conflict_do_nothing(
            index_elements=["agent_name", "request_id"])
        with get_engine().begin() as conn:
            inserted = bool(conn.execute(stmt).rowcount)
            row = conn.execute(select(*self._COLS).where(and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.request_id == request_id,
            ))).mappings().first()
        return _row(row), inserted

    def list_for_addressee(self, email: str, limit: int = 200) -> List[Dict]:
        """The tags addressed to `email`, newest first. A falsy email matches
        nothing — it is the authorization boundary, not a narrowing filter."""
        email = (email or "").strip().lower()
        if not email:
            return []
        stmt = (select(*self._COLS)
                .where(and_(operator_queue.c.type == MENTION_TYPE,
                            func.lower(operator_queue.c.addressed_to_email) == email))
                .order_by(operator_queue.c.created_at.desc(), operator_queue.c.id.desc())
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt).mappings()]

    def get(self, item_id: str) -> Optional[Dict]:
        stmt = select(*self._COLS).where(and_(
            operator_queue.c.id == item_id, operator_queue.c.type == MENTION_TYPE))
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row(row) if row else None

    def mark_read(self, item_id: str, email: str, now: str) -> Optional[Dict]:
        """`delivered → read` for the addressee only (compare-and-set). A second
        read changes nothing and returns the row as it stands; None when the
        row is not this person's."""
        email = (email or "").strip().lower()
        if not email:
            return None
        mine = and_(operator_queue.c.id == item_id,
                    operator_queue.c.type == MENTION_TYPE,
                    func.lower(operator_queue.c.addressed_to_email) == email)
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue)
                         .where(and_(mine, operator_queue.c.status == STATUS_DELIVERED))
                         .values(status=STATUS_READ, acknowledged_at=now))
            row = conn.execute(select(*self._COLS).where(mine)).mappings().first()
        return _row(row) if row else None

    def list_by_request_prefix(self, prefix: str, limit: int = 2000,
                               agent_names: Optional[List[str]] = None) -> List[Dict]:
        """Every tag whose idempotency key starts with `prefix` — one
        conversation's tags, for the tagger's delivered/read marks. With
        `agent_names` (the conversation's agents) the read is served by the
        `(agent_name, request_id)` unique index; without, by
        `idx_operator_queue_type` (only tag rows are scanned)."""
        if not prefix:
            return []
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds = [operator_queue.c.type == MENTION_TYPE,
                 operator_queue.c.request_id.like(f"{esc}%", escape="\\")]
        if agent_names is not None:
            names = sorted({a for a in agent_names if a})
            if not names:
                return []
            conds.append(operator_queue.c.agent_name.in_(names))
        stmt = (select(*self._COLS)
                .where(and_(*conds))
                .order_by(operator_queue.c.created_at.asc(), operator_queue.c.id.asc())
                .limit(limit))
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt).mappings()]
