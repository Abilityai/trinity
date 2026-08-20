"""Test/bootstrap helper for the multi-agent room tables (ent#443).

The DDL itself lives in ``db/schema.py`` with every other OSS table, and is
versioned on the two-track runner (``db/migrations.py`` +
Alembic ``0044_shared_sessions_oss``) — Invariant #3. This module owns no DDL;
it only applies the canonical statements to the connected database.

It exists because callers (tests, and any bootstrap that wants the tables
present without running the full migration chain) previously imported
``init_shared_sessions_schema`` from the enterprise module. Keeping the entry
point means the move did not force every caller to learn a new one, while the
single source of truth is now the OSS schema — the ``client_portal/schema.py``
shape, deliberately.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from db.engine import get_engine
from db.schema import INDEXES, TABLES

logger = logging.getLogger(__name__)

# The tables this module owns, by their canonical (historical) names. The
# `enterprise_` prefix predates the OSS move and is kept deliberately — see
# `shared_sessions/__init__.py`.
TABLE_NAMES = (
    "enterprise_rooms",
    "enterprise_room_participants",
    "enterprise_room_messages",
)

# Index statements are matched by the table names above rather than by a
# hand-kept list, so an index added to `db/schema.py` cannot be silently missed
# here (the failure mode would be a test DB that is subtly faster or slower than
# production, and a UNIQUE that never gets enforced).
_INDEX_MARKERS = ("enterprise_rooms(", "enterprise_room_participants(",
                  "enterprise_room_messages(")


def init_shared_sessions_schema() -> None:
    """Create the room tables + their indexes if absent. Idempotent."""
    with get_engine().begin() as conn:
        for name in TABLE_NAMES:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if any(marker in stmt for marker in _INDEX_MARKERS):
                conn.execute(text(stmt))
    logger.info("[shared_sessions] room schema ensured")
