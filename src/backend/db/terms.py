"""Terms acceptances (trinity-enterprise#1107).

One row per (person, terms version) in ``terms_acceptances`` — the record of
which text a person accepted and when. A platform account's row is keyed by
(user_id, version); a Workspace portal session's (no ``users`` row, ruling A(1)
on trinity-enterprise#1107) carries ``user_id`` NULL and is keyed by
(email, version) through a partial unique index. Emails are stored lower-cased. No HTTP concerns, no business rules
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
        self, user_id: Optional[int], email: Optional[str], terms_version: str, accepted_at: str,
        *, door: Optional[str] = None, actor_ip: Optional[str] = None,
    ) -> Optional[int]:
        """Record the acceptance; the row id, or ``None`` when (user, version) —
        or, for a session row (``user_id`` None), (email, version) — already
        has one: the table's two unique keys are what make a second accept a
        no-op."""
        if user_id is None and not (email or "").strip():
            raise ValueError("a terms acceptance needs an account or an email")
        stmt = make_insert(terms_acceptances).values(
            user_id=int(user_id) if user_id is not None else None,
            email=(email or "").strip().lower() or None, terms_version=terms_version,
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

    def get_terms_acceptance_by_email(self, email: str, terms_version: str) -> Optional[Dict]:
        """Any row for this address and version — a session row or an account's:
        the same person has accepted either way."""
        stmt = select(terms_acceptances).where(
            (func.lower(terms_acceptances.c.email) == (email or "").strip().lower())
            & (terms_acceptances.c.terms_version == terms_version)
        ).order_by(terms_acceptances.c.id.asc())
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
