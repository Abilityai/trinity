"""Workspace / client portal — OSS core (epic #78, moved from the entitled
enterprise seam by ent#356).

The client-facing surface: an external client signs in with a verified email and
sees every agent shared with them, or a signed-in platform user reaches the same
surface in one click (ent#357). Chat, documents and voice over that roster.

**Why this is OSS.** It was an entitled module (`client_portal`) and returned
404 in community builds. The workspace is the main surface a non-operator uses
to work with agents, so gating it capped adoption at exactly the population we
most want using it (Intelligence Design Weekly, 2026-08-07).

**Table names keep their `enterprise_` prefix** — `enterprise_portal_sessions`,
`enterprise_portal_messages`, `enterprise_client_blocks`. Renaming them would
require a data migration on every existing entitled install, and "no data
migration, no re-auth for signed-in clients" is an acceptance criterion of the
move. The prefix is now historical, not a statement about licensing.

The DDL lives in `db/schema.py` and is versioned on the OSS two-track runner
(`db/migrations.py` for SQLite, an Alembic revision for PostgreSQL) — NOT the
enterprise `enterprise_schema_migrations` track it used to use. Both tracks are
`CREATE TABLE IF NOT EXISTS`, so an install that already has these tables from
the enterprise runner is a no-op.
"""
from __future__ import annotations

# Deliberately NO `from .router import router` here. That would bind the
# APIRouter object to the name `client_portal.router`, shadowing the `router`
# SUBMODULE — so `client_portal.router.block_agent_client` (how the tests and
# any introspection reach the handlers) would resolve to an attribute lookup on
# the APIRouter and raise AttributeError. Import the object explicitly instead:
#
#     from client_portal.router import router
