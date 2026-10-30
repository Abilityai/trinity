"""Retrieval memory facts (trinity-enterprise#1022, MEM-002).

One row per extracted fact. A scope is ``(agent_name, user_email)``; the agent
scope — shared with the agent's owner — is ``user_email = ''`` (never NULL, so
the partial unique index on the live ``text_hash`` holds on both engines).

Nothing here knows about embeddings beyond storing and returning the bytes;
ranking is the service's job (``services/memory_service.py``). Forget is a soft
delete that records who asked, so the owner view can show it.
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, select, insert, update
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_memory_facts
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

AGENT_SCOPE = ""   # the `user_email` value of the agent scope


def _live(agent_name: str, user_email: str):
    return and_(
        agent_memory_facts.c.agent_name == agent_name,
        agent_memory_facts.c.user_email == user_email,
        agent_memory_facts.c.deleted_at.is_(None),
    )


def _row_to_dict(row, *, with_embedding: bool) -> Dict:
    d = {
        "id": row.id,
        "agent_name": row.agent_name,
        "user_email": row.user_email,
        "text": row.text,
        "text_hash": row.text_hash,
        "embedding_model": row.embedding_model,
        "source": row.source,
        "execution_id": row.execution_id,
        "recall_count": row.recall_count or 0,
        "last_recalled_at": row.last_recalled_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "deleted_at": row.deleted_at,
        "deleted_by": row.deleted_by,
    }
    if with_embedding:
        d["embedding"] = row.embedding
    return d


class MemoryFactOperations:
    """CRUD for the agent_memory_facts table."""

    def insert_memory_fact(
        self, *, agent_name: str, user_email: str, text: str, text_hash: str,
        embedding: bytes, embedding_model: str, source: str,
        execution_id: Optional[str] = None,
    ) -> Optional[str]:
        """Insert one live fact; returns its id, or None when the scope already
        holds a live fact with the same hash (the unique index is the arbiter —
        a two-worker race ends here, not as a duplicate)."""
        now = utc_now_iso()
        fact_id = uuid.uuid4().hex
        stmt = insert(agent_memory_facts).values(
            id=fact_id, agent_name=agent_name, user_email=user_email or AGENT_SCOPE,
            text=text, text_hash=text_hash, embedding=embedding,
            embedding_model=embedding_model, source=source, execution_id=execution_id,
            recall_count=0, created_at=now, updated_at=now,
        )
        try:
            with get_engine().begin() as conn:
                conn.execute(stmt)
        except IntegrityError:
            return None
        return fact_id

    def list_memory_facts(
        self, agent_name: str, user_email: str, *, limit: int = 2000,
        with_embedding: bool = True, offset: int = 0,
    ) -> List[Dict]:
        """The scope's live facts, newest first."""
        cols = [c for c in agent_memory_facts.c if with_embedding or c.name != "embedding"]
        stmt = (
            select(*cols)
            .where(_live(agent_name, user_email or AGENT_SCOPE))
            .order_by(agent_memory_facts.c.created_at.desc(), agent_memory_facts.c.id.desc())
            .limit(limit).offset(offset)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()
        return [_row_to_dict(r, with_embedding=with_embedding) for r in rows]

    def memory_scope_fingerprint(self, agent_name: str, user_email: str) -> Tuple[int, Optional[str]]:
        """``(live count, max(updated_at))`` — what a per-process recall cache keys on."""
        stmt = select(
            func.count(agent_memory_facts.c.id), func.max(agent_memory_facts.c.updated_at)
        ).where(_live(agent_name, user_email or AGENT_SCOPE))
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return (int(row[0] or 0), row[1]) if row else (0, None)

    def update_memory_fact_text(
        self, fact_id: str, agent_name: str, *, text: str, text_hash: str, embedding: bytes,
    ) -> bool:
        """Rewrite a live fact in place (the reconcile step's UPDATE). False when
        the id is not a live fact of this agent, or the new hash collides with
        another live fact in the scope (then the caller treats it as NONE)."""
        stmt = (
            update(agent_memory_facts)
            .where(and_(
                agent_memory_facts.c.id == fact_id,
                agent_memory_facts.c.agent_name == agent_name,
                agent_memory_facts.c.deleted_at.is_(None),
            ))
            .values(text=text, text_hash=text_hash, embedding=embedding, updated_at=utc_now_iso())
        )
        try:
            with get_engine().begin() as conn:
                return conn.execute(stmt).rowcount > 0
        except IntegrityError:
            return False

    def soft_delete_memory_fact(self, fact_id: str, agent_name: str, *, deleted_by: str) -> bool:
        stmt = (
            update(agent_memory_facts)
            .where(and_(
                agent_memory_facts.c.id == fact_id,
                agent_memory_facts.c.agent_name == agent_name,
                agent_memory_facts.c.deleted_at.is_(None),
            ))
            .values(deleted_at=utc_now_iso(), deleted_by=deleted_by, updated_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def mark_memory_facts_recalled(self, fact_ids: Sequence[str], *, agent_name: str) -> None:
        """Usage counters — what makes production the readout corpus for ent#634.
        Bounded by ``agent_name`` like every other writer here."""
        if not fact_ids:
            return
        stmt = (
            update(agent_memory_facts)
            .where(and_(
                agent_memory_facts.c.id.in_(list(fact_ids)),
                agent_memory_facts.c.agent_name == agent_name,
            ))
            .values(
                recall_count=func.coalesce(agent_memory_facts.c.recall_count, 0) + 1,
                last_recalled_at=utc_now_iso(),
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def prune_memory_scope(self, agent_name: str, user_email: str, *, cap: int) -> int:
        """Keep the scope at ``cap`` live facts: the oldest never-recalled go first,
        then the oldest. Soft-deleted as ``deleted_by='cap'``. Returns how many."""
        live = _live(agent_name, user_email or AGENT_SCOPE)
        with get_engine().begin() as conn:
            count = conn.execute(select(func.count(agent_memory_facts.c.id)).where(live)).scalar() or 0
            excess = int(count) - cap
            if excess <= 0:
                return 0
            victims = conn.execute(
                select(agent_memory_facts.c.id).where(live)
                .order_by(
                    func.coalesce(agent_memory_facts.c.recall_count, 0).asc(),
                    agent_memory_facts.c.created_at.asc(),
                ).limit(excess)
            ).scalars().all()
            if not victims:
                return 0
            now = utc_now_iso()
            conn.execute(
                update(agent_memory_facts)
                .where(agent_memory_facts.c.id.in_(victims))
                .values(deleted_at=now, deleted_by="cap", updated_at=now)
            )
            return len(victims)

    def count_memory_retains_since(self, agent_name: str, since_iso: str) -> int:
        """Distinct executions that produced a fact for this agent since ``since_iso``
        (the daily retain budget's meter). Soft-deleted rows count: a retain
        that was later pruned still spent its calls."""
        stmt = select(func.count(func.distinct(agent_memory_facts.c.execution_id))).where(and_(
            agent_memory_facts.c.agent_name == agent_name,
            agent_memory_facts.c.created_at >= since_iso,
            agent_memory_facts.c.execution_id.isnot(None),
        ))
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def memory_fact_stats(self, agent_name: Optional[str] = None) -> Dict:
        """``{count, last_retained_at}`` over live facts — platform-wide or one agent."""
        where = agent_memory_facts.c.deleted_at.is_(None)
        if agent_name:
            where = and_(where, agent_memory_facts.c.agent_name == agent_name)
        stmt = select(
            func.count(agent_memory_facts.c.id), func.max(agent_memory_facts.c.created_at)
        ).where(where)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return {"count": int(row[0] or 0) if row else 0, "last_retained_at": row[1] if row else None}
