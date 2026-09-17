"""trinity-enterprise#413 — per-user, server-persisted UI preferences (OSS-core).

The Dashboard Grid's layout, tile prefs and org toggles used to live in three
browser-global localStorage keys: shared by every user of one browser, lost on
every other device. They now live in `user_ui_preferences (user_id, key)`,
behind `GET|PUT|DELETE /api/users/me/preferences[/{key}]`.

What is pinned here, and why each one is load-bearing:

  * **isolation** — the record is keyed by the caller's id and nothing else;
    two users' rows never mix (the whole point of the issue);
  * **the write is never unconditional** — `base_updated_at=None` is
    insert-only and a string is compare-and-set, each decided by a single
    atomic statement (PK refusal / rowcount), so two tabs are safe writers
    where `db/canvas.py`'s read-then-write would not be;
  * **the key allowlist and the byte cap are named refusals** (404 / 422 /
    413), measured on the canonical stored bytes;
  * **the routes are interactive-only** — an agent-scoped key resolves to its
    owner on REST, and without the gate any agent could rewrite the
    operator's board (AST-pinned, so a fourth route cannot forget it);
  * **both migration tracks exist** (Rule of Engagement #9).

Exercises the REAL code against an ephemeral migrated SQLite (tables.py
MetaData → `create_all`) and the REAL DatabaseManager facade, re-owned per
test via `monkeypatch.setitem` (learnings 2026-07-06 / 2026-07-12).
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_OWNED = [
    "db.engine", "db.connection", "db.tables", "db.users", "db.user_preferences",
    "database", "models",
    "services.user_preferences_service",
    "routers.users",
]
_REAL = {name: importlib.import_module(name) for name in _OWNED}


@pytest.fixture()
def env_db(tmp_path, monkeypatch):
    """Fresh sqlite with `users` + `user_ui_preferences`, two seeded users."""
    for name, mod in _REAL.items():
        monkeypatch.setitem(sys.modules, name, mod)

    db_file = tmp_path / "ent413.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(_REAL["db.connection"], "DB_PATH", str(db_file), raising=False)

    from db.engine import get_engine, dispose_engines
    from db.tables import metadata, users, user_ui_preferences
    from sqlalchemy import insert

    metadata.create_all(get_engine(), tables=[users, user_ui_preferences])
    with get_engine().begin() as conn:
        for uid, name in ((1, "alice"), (2, "bob")):
            conn.execute(insert(users).values(
                id=uid, username=name, role="user", email=f"{name}@example.com",
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
            ))
    try:
        yield str(db_file)
    finally:
        dispose_engines()


def _svc():
    return _REAL["services.user_preferences_service"]


def _db():
    from database import db
    return db


# ---------------------------------------------------------------------------
# Isolation — the reason the issue exists
# ---------------------------------------------------------------------------

def test_two_users_records_never_mix(env_db):
    svc = _svc()
    svc.put(1, "grid_layout", {"a": {"c": 0, "r": 0}}, None)
    svc.put(2, "grid_layout", {"a": {"c": 5, "r": 5}}, None)
    assert svc.get_all(1)["grid_layout"]["value"] == {"a": {"c": 0, "r": 0}}
    assert svc.get_all(2)["grid_layout"]["value"] == {"a": {"c": 5, "r": 5}}
    # Reset is per user: Bob's delete leaves Alice's row alone.
    assert svc.delete(2, "grid_layout") is True
    assert "grid_layout" not in svc.get_all(2)
    assert "grid_layout" in svc.get_all(1)


def test_get_all_is_empty_for_a_user_with_nothing_stored(env_db):
    """The "sensible default" AC: no dead state — the client falls through to
    its default layout when the map is empty."""
    assert _svc().get_all(1) == {}


# ---------------------------------------------------------------------------
# The write is never unconditional
# ---------------------------------------------------------------------------

def test_insert_only_write_refuses_when_a_row_exists(env_db):
    svc = _svc()
    first = svc.put(1, "grid_widgets", {"x": True}, None)
    with pytest.raises(svc.PreferenceConflict) as exc:
        svc.put(1, "grid_widgets", {"x": False}, None)
    assert exc.value.status_code == 409
    # The 409 carries the LIVE record so the client can adopt without a GET.
    assert exc.value.current["value"] == {"x": True}
    assert exc.value.current["updated_at"] == first["updated_at"]
    assert svc.get_all(1)["grid_widgets"]["value"] == {"x": True}


def test_compare_and_set_succeeds_on_the_current_base_and_moves_updated_at(env_db):
    svc = _svc()
    first = svc.put(1, "grid_org", {"zones": True}, None)
    second = svc.put(1, "grid_org", {"zones": False}, first["updated_at"])
    assert second["value"] == {"zones": False}
    assert second["updated_at"] != first["updated_at"]


def test_stale_base_is_refused_and_the_newer_save_survives(env_db):
    """Two tabs: the older tab's write lands after the newer tab's."""
    svc = _svc()
    first = svc.put(1, "grid_layout", {"a": {"c": 0, "r": 0}}, None)
    newer = svc.put(1, "grid_layout", {"a": {"c": 1, "r": 1}}, first["updated_at"])
    with pytest.raises(svc.PreferenceConflict) as exc:
        svc.put(1, "grid_layout", {"a": {"c": 9, "r": 9}}, first["updated_at"])
    assert exc.value.current["updated_at"] == newer["updated_at"]
    assert svc.get_all(1)["grid_layout"]["value"] == {"a": {"c": 1, "r": 1}}


def test_compare_and_set_against_a_deleted_row_conflicts_with_null_current(env_db):
    svc = _svc()
    first = svc.put(1, "grid_layout", {"a": {"c": 0, "r": 0}}, None)
    svc.delete(1, "grid_layout")
    with pytest.raises(svc.PreferenceConflict) as exc:
        svc.put(1, "grid_layout", {}, first["updated_at"])
    assert exc.value.current is None


def test_the_db_layer_never_reads_then_writes(env_db):
    """The single-statement contract, pinned at the source: the conditional
    write must not contain a SELECT — `upsert_canvas`'s read-then-write is
    documented as single-writer-only and two tabs are two writers."""
    src = (_BACKEND / "db" / "user_preferences.py").read_text()
    body = src[src.index("def set_user_preference"):src.index("def delete_user_preference")]
    assert "select(" not in body
    assert "rowcount" in body


# ---------------------------------------------------------------------------
# Named refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["", "grid", "GRID_LAYOUT", "grid_layout ", "../x", "password"])
def test_unknown_key_is_a_404(env_db, key):
    with pytest.raises(_svc().PreferenceError) as exc:
        _svc().put(1, key, {}, None)
    assert exc.value.status_code == 404
    with pytest.raises(_svc().PreferenceError) as exc:
        _svc().delete(1, key)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("value", [[], "x", 1, None, [{"a": 1}]])
def test_non_object_value_is_a_422(env_db, value):
    with pytest.raises(_svc().PreferenceError) as exc:
        _svc().put(1, "grid_layout", value, None)
    assert exc.value.status_code == 422


def test_oversize_value_is_a_413_measured_on_stored_bytes(env_db):
    svc = _svc()
    cap = svc.MAX_VALUE_BYTES
    # Exactly-at-cap passes; one byte over fails. Compact encoding is what is
    # measured, so the boundary is computed from the same serializer.
    overhead = len(json.dumps({"k": ""}, separators=(",", ":")).encode())
    at_cap = {"k": "x" * (cap - overhead)}
    assert len(svc.serialize_value(at_cap).encode()) == cap
    svc.put(1, "grid_layout", at_cap, None)
    with pytest.raises(svc.PreferenceError) as exc:
        svc.put(1, "grid_widgets", {"k": "x" * (cap - overhead + 1)}, None)
    assert exc.value.status_code == 413


def test_an_undecodable_stored_value_reads_as_empty_not_500(env_db):
    from db.engine import get_engine
    from db.tables import user_ui_preferences
    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(user_ui_preferences).values(
            user_id=1, key="grid_org", value_json="{not json", updated_at="2026-01-01T00:00:00Z",
        ))
    rec = _svc().get_all(1)["grid_org"]
    assert rec["value"] == {}
    assert rec["updated_at"] == "2026-01-01T00:00:00Z"


def test_cascade_hook_removes_every_row_of_one_user(env_db):
    svc = _svc()
    for k in ("grid_layout", "grid_widgets", "grid_org"):
        svc.put(1, k, {}, None)
        svc.put(2, k, {}, None)
    assert _db().delete_user_preferences(1) == 3
    assert svc.get_all(1) == {}
    assert len(svc.get_all(2)) == 3


# ---------------------------------------------------------------------------
# Router wiring — interactive humans only, own record only (AST)
# ---------------------------------------------------------------------------

def _preference_handlers():
    tree = ast.parse((_BACKEND / "routers" / "users.py").read_text())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                path = dec.args[0].value
                if "/me/preferences" in path:
                    found[(dec.func.attr, path)] = node
    return found


def test_the_three_routes_exist():
    routes = set(_preference_handlers())
    assert routes == {
        ("get", "/me/preferences"),
        ("put", "/me/preferences/{key}"),
        ("delete", "/me/preferences/{key}"),
    }


def test_every_preference_route_rejects_non_interactive_principals():
    """An ALLOWlist gate (JWT humans only), on every route — an agent-scoped
    key resolves to its owner and must not read or rewrite the owner's
    board. The FIRST statement, so nothing runs before the refusal."""
    for (method, path), fn in _preference_handlers().items():
        first = fn.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first = fn.body[1]  # skip the docstring
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), (method, path)
        assert getattr(first.value.func, "id", None) == "reject_non_interactive_principal", (method, path)
        assert first.value.args[0].id == "current_user"


def test_every_preference_route_scopes_on_current_user_id_only():
    """No path parameter names another user; the only id that reaches the
    service is `current_user.id`."""
    for (method, path), fn in _preference_handlers().items():
        src = ast.unparse(fn)
        assert "current_user.id" in src, (method, path)
        assert "{username}" not in path


def test_non_interactive_principals_are_refused_at_runtime():
    from fastapi import HTTPException
    from dependencies import reject_non_interactive_principal
    from models import User
    human = User(id=1, username="alice", role="user")
    reject_non_interactive_principal(human)  # no raise
    for scope, extra in (("agent", {"agent_name": "a"}), ("connector", {"connector_agent": "a"}),
                         ("system", {}), ("user", {}), ("ops", {})):
        machine = User(id=1, username="alice", role="admin", mcp_scope=scope, **extra)
        with pytest.raises(HTTPException) as exc:
            reject_non_interactive_principal(machine)
        assert exc.value.status_code == 403, scope


def test_write_model_requires_an_explicit_base():
    """`base_updated_at` omitted is a 422, not "unconditional": every client
    write states what it believes the server holds."""
    from pydantic import ValidationError
    from models import UserPreferenceWrite
    with pytest.raises(ValidationError):
        UserPreferenceWrite(value={})
    assert UserPreferenceWrite(value={}, base_updated_at=None).base_updated_at is None
    assert UserPreferenceWrite(value={}, base_updated_at="t").base_updated_at == "t"
    with pytest.raises(ValidationError):
        UserPreferenceWrite(value=[], base_updated_at=None)


# ---------------------------------------------------------------------------
# Both migration tracks (Rule of Engagement #9)
# ---------------------------------------------------------------------------

def test_sqlite_migration_is_registered_and_alembic_revision_exists():
    migrations = (_BACKEND / "db" / "migrations.py").read_text()
    assert '("user_ui_preferences_table", _migrate_user_ui_preferences_table)' in migrations
    versions = _BACKEND / "migrations" / "versions"
    files = [p for p in versions.glob("*.py") if "user_ui_preferences" in p.name]
    assert len(files) == 1, files
    rev = files[0].read_text()
    assert re.search(r'^revision = "0053_user_ui_preferences"$', rev, re.M)
    assert re.search(r'^down_revision = "0052_portal_session_title_source"$', rev, re.M)
    assert "has_table" in rev  # fresh PG builds from schema.py first
    schema = (_BACKEND / "db" / "schema.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS user_ui_preferences" in schema
    assert 'user_ui_preferences = Table(' in (_BACKEND / "db" / "tables.py").read_text()


# ---------------------------------------------------------------------------
# HTTP contract — the real router mounted on a bare app, real DB underneath
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(env_db):
    """The users router on a bare FastAPI app, `get_current_user` overridden
    per test via `client.as_user(...)`. Everything below the router is real."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from models import User

    users_router = _REAL["routers.users"]
    app = FastAPI()
    app.include_router(users_router.router)
    principal = {"user": User(id=1, username="alice", role="user")}
    # Override the EXACT object the router bound at import (learnings
    # 2026-07-12: patch by object, never by a string/re-import that resolves
    # through a possibly-leaked `sys.modules` entry).
    app.dependency_overrides[users_router.get_current_user] = lambda: principal["user"]
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


def test_http_round_trip_insert_cas_conflict_and_delete(client):
    r = client.get("/api/users/me/preferences")
    assert r.status_code == 200 and r.json() == {"preferences": {}}

    r = client.put("/api/users/me/preferences/grid_layout",
                   json={"value": {"a": {"c": 1, "r": 2}}, "base_updated_at": None})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["key"] == "grid_layout" and rec["value"] == {"a": {"c": 1, "r": 2}}

    # Insert-only against an existing row → 409 with the live record.
    r = client.put("/api/users/me/preferences/grid_layout",
                   json={"value": {}, "base_updated_at": None})
    assert r.status_code == 409
    assert r.json()["detail"]["current"]["updated_at"] == rec["updated_at"]

    # CAS on the right base → 200; on the stale base → 409.
    r = client.put("/api/users/me/preferences/grid_layout",
                   json={"value": {"a": {"c": 3, "r": 3}}, "base_updated_at": rec["updated_at"]})
    assert r.status_code == 200
    r2 = r.json()
    r = client.put("/api/users/me/preferences/grid_layout",
                   json={"value": {}, "base_updated_at": rec["updated_at"]})
    assert r.status_code == 409
    assert r.json()["detail"]["current"]["value"] == {"a": {"c": 3, "r": 3}}

    r = client.get("/api/users/me/preferences")
    assert r.json()["preferences"]["grid_layout"]["updated_at"] == r2["updated_at"]

    r = client.delete("/api/users/me/preferences/grid_layout")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    r = client.delete("/api/users/me/preferences/grid_layout")
    assert r.json() == {"deleted": False}


def test_http_named_refusals(client):
    assert client.put("/api/users/me/preferences/nope",
                      json={"value": {}, "base_updated_at": None}).status_code == 404
    assert client.delete("/api/users/me/preferences/nope").status_code == 404
    # base omitted → 422 (never unconditional); non-object value → 422.
    assert client.put("/api/users/me/preferences/grid_layout", json={"value": {}}).status_code == 422
    assert client.put("/api/users/me/preferences/grid_layout",
                      json={"value": [], "base_updated_at": None}).status_code == 422
    # Over the cap → 413, from the exact byte check.
    big = {"k": "x" * (_svc().MAX_VALUE_BYTES + 1)}
    r = client.put("/api/users/me/preferences/grid_layout", json={"value": big, "base_updated_at": None})
    assert r.status_code == 413


def test_http_scopes_on_the_caller_and_refuses_machine_principals(client):
    from models import User
    client.put("/api/users/me/preferences/grid_org", json={"value": {"zones": False}, "base_updated_at": None})
    client.as_user(User(id=2, username="bob", role="admin"))
    assert client.get("/api/users/me/preferences").json() == {"preferences": {}}
    # An agent-scoped key that resolves to Alice (owner role carried) is refused
    # on every route — including the read.
    client.as_user(User(id=1, username="alice", role="admin", mcp_scope="agent", agent_name="a"))
    assert client.get("/api/users/me/preferences").status_code == 403
    assert client.put("/api/users/me/preferences/grid_org",
                      json={"value": {}, "base_updated_at": None}).status_code == 403
    assert client.delete("/api/users/me/preferences/grid_org").status_code == 403
    client.as_user(User(id=1, username="alice", role="user"))
    assert client.get("/api/users/me/preferences").json()["preferences"]["grid_org"]["value"] == {"zones": False}


def test_frontend_pref_keys_match_the_backend_allowlist():
    """The keys the store writes must be the keys the service admits — a key
    added on one side only is a silent 404 on every save (the #2199 hand-copy
    class, across the tree boundary)."""
    js = (_REPO / "src" / "frontend" / "src" / "utils" / "gridStorageKeys.js").read_text()
    block = js[js.index("export const PREF_KEYS"):]
    block = block[:block.index("})")]
    frontend_keys = set(re.findall(r"'([a-z_]+)'", block))
    assert frontend_keys == set(_svc().PREFERENCE_KEYS)
