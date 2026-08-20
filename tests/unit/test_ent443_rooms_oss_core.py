"""ent#443 — multi-agent rooms are OSS core, served on a build with no submodule.

The bug this move fixes is a *silence*: `shared_sessions` lived in the private
enterprise submodule behind `requires_entitlement`, while the frontend that
drives it (`components/rooms/`, `stores/rooms.js`, the ent#392 composer
typeahead) and the MCP tools (`tools/rooms.ts`) shipped in EVERY build and
self-disabled. An OSS install therefore rendered the affordance and refused it.

These assertions are deliberately structural rather than behavioural — the room
ENGINE is covered verbatim by the five ported suites (`test_ent169_*`,
`test_ent220_*`, `test_ent361_*`, `test_ent362_*`, `test_ent387_*`), and this
file exists to pin the properties those suites cannot see, because they import
the module directly and would keep passing if it were never mounted:

  1. both routers are mounted in the assembled app;
  2. nothing on them carries an entitlement dependency;
  3. the module registers no feature id, so `enterprise_features` stays clean;
  4. the DDL is on the OSS two-track (schema + SQLite migration + Alembic), not
     the enterprise runner;
  5. the table names keep their historical `enterprise_` prefix — renaming is
     the data migration the move forbids.

Self-sufficient env + `import main` harness copied from
`test_ent126_route_order.py`; see its header for the skip rationale.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent443-rooms.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent443-logs")
)

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

try:
    import main  # noqa: E402  (must follow the env/path setup above)
except ImportError as _exc:  # pragma: no cover — polluted sweep only
    pytest.skip(
        f"requires pristine sys.modules — `import main` failed ({_exc}). "
        "Run standalone: pytest tests/unit/test_ent443_rooms_oss_core.py",
        allow_module_level=True,
    )

ROOMS_FEATURE_ID = "shared_sessions"

# The historical names. Not a style choice: every entitled install already holds
# live transcripts under them, so a rename IS the data migration ent#443 forbids.
ROOM_TABLES = (
    "enterprise_rooms",
    "enterprise_room_participants",
    "enterprise_room_messages",
)


def _flat_routes(app):
    flat: list[APIRoute] = []
    for entry in app.routes:
        if type(entry).__name__ == "_IncludedRouter":
            flat.extend(
                r for r in entry.original_router.routes if isinstance(r, APIRoute)
            )
        elif isinstance(entry, APIRoute):
            flat.append(entry)
    return flat


# ---------------------------------------------------------------------------
# 1-2 — mounted, and mounted ungated
# ---------------------------------------------------------------------------

def test_room_routes_are_mounted_on_an_oss_build():
    """THE regression test. Before ent#443 this app had no `/api/rooms` at all."""
    paths = {r.path for r in _flat_routes(main.app)}

    assert "/api/rooms" in paths, "the room surface is not mounted"
    assert "/api/rooms/{room_id}" in paths
    assert "/api/rooms/{room_id}/messages" in paths
    # ent#387's operator surface, deliberately NOT under /api/rooms (a
    # `/budget-defaults` path there would sit beside `/{room_id}`, Invariant #4).
    assert "/api/enterprise/room-budget-defaults" in paths


def _first_serving(flat, method: str, path: str):
    """The route that actually handles `path` — first full match wins."""
    from starlette.routing import Match

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for route in flat:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/rooms"),
    ("GET", "/api/rooms"),
    ("POST", "/api/rooms/room_abc/messages"),
    ("GET", "/api/enterprise/room-budget-defaults"),
])
def test_the_serving_room_route_is_the_oss_one_and_is_ungated(method, path):
    """Two properties in one, because they are the same property in a transition
    build.

    On a machine with the enterprise submodule still mounted, BOTH routers are
    installed and the paths collide. That is not a bug as long as the OSS mount
    wins — `main.py` includes it well before `register_enterprise(app)`, so the
    first full match is the ungated one and an entitled install keeps serving
    rooms throughout the submodule-bump window. On CI (no submodule) there is
    only ever one.

    Asserting on the FIRST match rather than on every route with that path is
    the whole point: a duplicate that never serves cannot 403 anyone, and a
    test that failed on its mere presence would be red for the entire
    transition and tell nobody anything.
    """
    def _callables(dependant):
        for sub in dependant.dependencies:
            call = getattr(sub, "call", None)
            if call is not None:
                yield call
            yield from _callables(sub)

    route = _first_serving(_flat_routes(main.app), method, path)
    assert route is not None, f"nothing serves {method} {path}"

    handler_module = getattr(route.endpoint, "__module__", "")
    assert handler_module.startswith("shared_sessions"), (
        f"{method} {path} is served by {handler_module!r}, not the OSS module — "
        "the OSS routers must be mounted before register_enterprise(app)"
    )

    names = [
        f"{getattr(c, '__module__', '?')}.{getattr(c, '__qualname__', repr(c))}"
        for c in _callables(route.dependant)
    ]
    assert not any("entitl" in name.lower() for name in names), (
        f"{method} {path} still carries an entitlement dependency: {names}"
    )


def test_oss_rooms_are_mounted_before_the_enterprise_seam():
    """The ordering above, pinned at its source rather than inferred.

    If someone moves the room `include_router` calls below
    `register_enterprise(app)`, a stale submodule would silently take the paths
    back and 403 community builds again — with every other test in this file
    still green, because they read the OSS module directly.
    """
    source = (_BACKEND / "main.py").read_text()
    oss_mount = source.index("app.include_router(rooms_router)")
    seam = source.index("register_enterprise(app)")
    assert oss_mount < seam


def test_the_module_registers_no_feature_id():
    """`enterprise_features` is the public edition surface (`/api/version`,
    `/api/settings/feature-flags`). Rooms must not appear there any more, or an
    OSS build would advertise itself as carrying an enterprise module."""
    import ast
    import inspect

    import shared_sessions
    import shared_sessions.router as room_router
    import shared_sessions.service as room_service

    # Parsed, not grepped: a docstring is allowed to SAY the module used to
    # carry `requires_entitlement("shared_sessions")` — recording why the gate
    # went away is the point. What must not exist is a call.
    banned = {"register_module", "requires_entitlement"}
    for module in (shared_sessions, room_router, room_service):
        tree = ast.parse(inspect.getsource(module))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert not (called & banned), (
            f"{module.__name__} calls {sorted(called & banned)}"
        )
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "requires_entitlement" not in imported, (
            f"{module.__name__} still imports the entitlement gate"
        )

    # `FEATURE_ID` may survive as a label; what must not survive is a claim on
    # the registry. Asserted separately so the string alone is not a failure.
    assert not hasattr(shared_sessions, "register"), (
        "the enterprise `register(app)` entry point should be gone — main.py "
        "mounts the routers directly"
    )


# ---------------------------------------------------------------------------
# 3-5 — the schema is on the OSS two-track, under its historical names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table", ROOM_TABLES)
def test_ddl_lives_in_the_oss_schema(table):
    from db.schema import TABLES

    assert table in TABLES, f"{table} is not in the OSS schema (Invariant #3)"
    assert "CREATE TABLE IF NOT EXISTS" in TABLES[table], (
        "adoption, not creation: the statement must be a no-op on an install "
        "that already has the table from the enterprise runner"
    )


def test_sqlite_track_has_the_adoption_migration():
    from db.migrations import MIGRATIONS

    names = [name for name, _ in MIGRATIONS]
    assert "shared_sessions_tables_to_oss" in names


def test_postgres_track_has_the_adoption_revision():
    versions = _BACKEND / "migrations" / "versions"
    revision = versions / "0039_shared_sessions_oss.py"
    assert revision.exists(), "no OSS Alembic revision for the room tables"

    body = revision.read_text()
    for table in ROOM_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in body
    assert "def downgrade() -> None:" in body
    assert "DROP TABLE" not in body, (
        "downgrade must not drop tables this revision only ADOPTED — that is "
        "the one outcome the move promised could not happen"
    )


def test_the_bootstrap_helper_owns_no_ddl():
    """`shared_sessions/schema.py` is a test/bootstrap applier over the canonical
    OSS statements (the `client_portal/schema.py` shape). A second copy of the
    DDL here is how the two sources drift."""
    import inspect

    from shared_sessions import schema

    src = inspect.getsource(schema)
    assert "CREATE TABLE" not in src, "the helper re-declares DDL"
    assert "from db.schema import" in src


def test_schema_helper_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "rooms.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "rooms.db"))

    from sqlalchemy import inspect as sa_inspect

    from db.engine import get_engine
    from shared_sessions.schema import init_shared_sessions_schema

    init_shared_sessions_schema()
    init_shared_sessions_schema()  # second call must not raise

    present = set(sa_inspect(get_engine()).get_table_names())
    assert set(ROOM_TABLES) <= present


# ---------------------------------------------------------------------------
# 6 — the tables are under the OSS delete/rename contract, kind-scoped
# ---------------------------------------------------------------------------

def _rooms_registry():
    from db import agent_cleanup

    return {
        (ref.table, ref.column): ref
        for ref in agent_cleanup.AGENT_REFS
        if ref.table.startswith("enterprise_room")
    }


def test_room_tables_are_registered_with_a_kind_predicate():
    """Open question 1 from the issue, answered in code.

    Before ent#443 these tables sat outside `AGENT_REFS` (the registry only
    covers OSS tables), so a renamed agent silently stopped being woken — its
    participant row still named the old agent, and mention resolution matches
    against that row — and a purge orphaned participants and transcript.

    The `extra_filter` is the load-bearing half. `identity` and
    `sender_identity` are POLYMORPHIC: `kind`/`sender_kind` decides whether the
    value is an agent name, a platform user id, or a workspace client's verified
    email. An unscoped ref would rewrite — and on purge DELETE — a human
    participant whose id or email happened to equal the agent's name.
    """
    from db.agent_cleanup import Policy

    refs = _rooms_registry()
    participants = refs.get(("enterprise_room_participants", "identity"))
    messages = refs.get(("enterprise_room_messages", "sender_identity"))

    assert participants is not None, "room participants are not cascade-managed"
    assert messages is not None, "room messages are not cascade-managed"
    assert participants.policy is Policy.CASCADE
    assert messages.policy is Policy.CASCADE
    assert participants.extra_filter == "kind = 'agent'"
    assert messages.extra_filter == "sender_kind = 'agent'"


def test_rename_rekeys_the_agent_participant_and_leaves_humans_alone(
    tmp_path, monkeypatch
):
    """The behaviour behind the registration above.

    The human rows are the point of the test, not padding: a workspace client
    is identified by EMAIL and a platform user by id, in the same column, and
    the failure mode of an unscoped rename is that one of them silently becomes
    the new agent name.
    """
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "rename.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "rename.db"))

    from sqlalchemy import text

    from db.agent_cleanup import cascade_rename
    from db.engine import get_engine
    from shared_sessions.schema import init_shared_sessions_schema

    init_shared_sessions_schema()
    engine = get_engine()

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_rooms (id, name, status, max_messages, created_at) "
            "VALUES ('r1', 'Room', 'open', 60, 't')"
        ))
        for kind, identity in (
            ("agent", "scout"),
            ("user", "scout"),            # a platform user id that COLLIDES
            ("workspace_user", "scout"),  # and a client identity that collides
        ):
            conn.execute(text(
                "INSERT INTO enterprise_room_participants "
                "(room_id, kind, identity, role, joined_at, last_read_seq) "
                "VALUES ('r1', :k, :i, 'member', 't', 0)"
            ), {"k": kind, "i": identity})
        conn.execute(text(
            "INSERT INTO enterprise_room_messages "
            "(id, room_id, seq, sender_kind, sender_identity, kind, content, created_at) "
            "VALUES ('m1', 'r1', 1, 'agent', 'scout', 'message', 'hi', 't')"
        ))
        conn.execute(text(
            "INSERT INTO enterprise_room_messages "
            "(id, room_id, seq, sender_kind, sender_identity, kind, content, created_at) "
            "VALUES ('m2', 'r1', 2, 'user', 'scout', 'message', 'hello', 't')"
        ))

    with engine.begin() as conn:
        cascade_rename(conn, "scout", "ranger")

    with engine.begin() as conn:
        parts = dict(conn.execute(text(
            "SELECT kind, identity FROM enterprise_room_participants"
        )).all())
        msgs = dict(conn.execute(text(
            "SELECT id, sender_identity FROM enterprise_room_messages"
        )).all())

    assert parts["agent"] == "ranger", "the renamed agent was left stranded"
    assert parts["user"] == "scout", "a platform user id was rewritten"
    assert parts["workspace_user"] == "scout", "a client identity was rewritten"
    assert msgs["m1"] == "ranger"
    assert msgs["m2"] == "scout", "a human's message attribution was rewritten"
