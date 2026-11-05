"""Terms acceptances (trinity-enterprise#1107).

One row per (person, terms version) in ``terms_acceptances`` — the record of
which text a person accepted and when. No HTTP concerns, no business rules
(those live in ``services/terms_service.py``). SQLAlchemy Core over
``db/tables.py`` so it runs unchanged on SQLite and PostgreSQL.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .engine import get_engine, make_insert
from .tables import terms_acceptances


class TermsOperations:
    """Mixin composed into ``DatabaseManager``."""

    def insert_terms_acceptance(
        self, user_id: int, email: Optional[str], terms_version: str, accepted_at: str,
        *, door: Optional[str] = None, actor_ip: Optional[str] = None,
    ) -> Optional[int]:
        """Record the acceptance; the row id, or ``None`` when (user, version)
        already has one — the table's UNIQUE is what makes a second accept a
        no-op."""
        stmt = make_insert(terms_acceptances).values(
            user_id=int(user_id), email=email, terms_version=terms_version,
            accepted_at=accepted_at, door=door, actor_ip=actor_ip,
        )
        try:
            with get_engine().begin() as conn:
                result = conn.execute(stmt)
                pk = result.inserted_primary_key
                return int(pk[0]) if pk and pk[0] is not None else -1
        except IntegrityError:
            return None

    def get_terms_acceptance(self, user_id: int, terms_version: str) -> Optional[Dict]:
        stmt = select(terms_acceptances).where(
            (terms_acceptances.c.user_id == int(user_id))
            & (terms_acceptances.c.terms_version == terms_version)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row else None

    def terms_acceptances_for_user(self, user_id: int) -> List[Dict]:
        stmt = (
            select(terms_acceptances)
            .where(terms_acceptances.c.user_id == int(user_id))
            .order_by(terms_acceptances.c.accepted_at.desc())
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def terms_acceptances_for_email(self, email: str) -> List[Dict]:
        stmt = (
            select(terms_acceptances)
            .where(func.lower(terms_acceptances.c.email) == (email or "").lower())
            .order_by(terms_acceptances.c.accepted_at.desc())
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def list_terms_acceptances(self, limit: int = 200) -> List[Dict]:
        stmt = (
            select(terms_acceptances)
            .order_by(terms_acceptances.c.accepted_at.desc(), terms_acceptances.c.id.desc())
            .limit(int(limit))
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]
