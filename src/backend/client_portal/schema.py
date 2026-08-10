"""Test/bootstrap helper for the workspace tables (ent#356).

The DDL itself lives in ``db/schema.py`` with every other OSS table, and is
versioned on the two-track runner (``db/migrations.py`` + Alembic
``0036_client_portal_oss``) — Invariant #3. This module owns no DDL; it only
applies the canonical statements to the connected database.

It exists because callers (tests, and any bootstrap that wants the tables
present without running the full migration chain) previously imported
``init_client_portal_schema`` from the enterprise module. Keeping the entry
point means the move did not force every caller to learn a new one, while the
single source of truth is now the OSS schema.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from db.engine import get_engine
from db.schema import TABLES, INDEXES

logger = logging.getLogger(__name__)

# The tables this module owns, by their canonical (historical) names. The
# `enterprise_` prefix predates the OSS move and is kept deliberately — see
# `client_portal/__init__.py`.
TABLE_NAMES = (
    "enterprise_portal_sessions",
    "enterprise_portal_messages",
    "enterprise_client_blocks",
)


def init_client_portal_schema() -> None:
    """Create the workspace tables + their indexes if absent. Idempotent."""
    with get_engine().begin() as conn:
        for name in TABLE_NAMES:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if "enterprise_portal" in stmt or "enterprise_client_blocks" in stmt:
                conn.execute(text(stmt))
    logger.info("[client_portal] workspace schema ensured")
