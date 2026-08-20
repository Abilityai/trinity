"""Multi-agent rooms / shared sessions — OSS core (engine ent#169, epic ent#8;
moved out of the entitled enterprise seam by ent#443).

A room is a shared persistent RECORD, never a shared CONTEXT: each agent keeps
its own isolated session and is handed only the transcript it has not seen
(``last_read_seq``). Turn-taking is mechanical — you are woken iff you were
@mentioned — so no LLM has to decide who speaks next, and a room does not cost
N x tokens. Every agent turn is an ordinary execution; everything is bounded.

**Why this is OSS.** It was an entitled module (``shared_sessions``) and its
routes 404'd in community builds, while the frontend that drives them
(``components/rooms/``, ``stores/rooms.js``, the composer typeahead) and the MCP
tools (``tools/rooms.ts``) shipped in *every* build and self-disabled. Three of
the four surfaces were already public; gating only the backend left an OSS
install rendering an affordance it then refused. Workspace itself moved to OSS
for the same adoption reason (ent#356), and rooms are the half that makes it the
place people work with agents rather than a second 1:1 chat.

**Table names keep their ``enterprise_`` prefix** — ``enterprise_rooms``,
``enterprise_room_participants``, ``enterprise_room_messages``. Renaming them
would require a data migration on every existing entitled install, and "no data
migration, no lost rooms" is an acceptance criterion of the move. The prefix is
historical provenance, not a statement about licensing.

The DDL lives in ``db/schema.py`` and is versioned on the OSS two-track runner
(``db/migrations.py`` for SQLite, Alembic ``0039_shared_sessions_oss`` for
PostgreSQL) — NOT the ``enterprise_schema_migrations`` track it used to use.
Both tracks are ``CREATE TABLE IF NOT EXISTS``, so an install that already has
these tables from the enterprise runner is a no-op.

Two routers, mounted in ``main.py``: ``/api/rooms`` (membership-scoped, reachable
by a workspace client) and ``/api/enterprise/room-budget-defaults`` (admin-only
operator surface, ent#387). The second keeps its path for the same reason the
tables keep their names — the OSS frontend already calls it.
"""
from __future__ import annotations

# Deliberately NO `from .router import router` here. That would bind the
# APIRouter object to the name `shared_sessions.router`, shadowing the `router`
# SUBMODULE — so `shared_sessions.router.post_message` (how the tests and any
# introspection reach the handlers) would resolve to an attribute lookup on the
# APIRouter and raise AttributeError. Import the object explicitly instead:
#
#     from shared_sessions.router import budget_router, router
