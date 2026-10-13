"""
abilityai/trinity-enterprise#885 — the keyless trusted-network A2A path is gone.

Before 1.0 the operator ruled (ent#862, 2026-10-10; ent#885 body) that an
inbound A2A call with no credential must never run a task, whatever network
it arrives from. #3361 (ent#838) had added exactly that door: an agent whose
row said ``a2a_scope='internal', a2a_keyless_internal=1`` ran tasks for any
peer inside an admin-declared CIDR, keyed on the peer's address.

These tests drive the REAL router over the REAL unit-island SQLite database
(``tests/unit/conftest.py`` pins a per-process tmp DB built from the full
``schema.py`` DDL, so the 0096 columns exist) with a row seeded by raw SQL —
the shape a database that ran rc1–rc3 still carries. The columns stay (binding:
no drop-column migration in freeze week); they must be UNREAD:

* a caller with no credential gets the v0.9.5 401 bytes and nothing is
  dispatched, from a CGNAT peer, through ``X-Real-IP``, with cloudflared's
  headers, or from the public internet;
* a keyed caller is never 404'd for its source address again (Invariant #8's
  lattice is exposure ∧ access ∧ reach ∧ allow-list);
* the card is served at the public origin, never an "internal" one.

The outbound half of the trusted-networks list survives (operator ruling
2026-10-13: it is an outbound endpoint allowlist, ``test_ent885_outbound_allowlist``).
The ``a2a_trusted_networks`` row is seeded here on purpose: a surviving inbound
reader would find a trusted CIDR and go green on the wrong side.

Related flow: docs/memory/feature-flows/a2a-inbound-server.md
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import types
from pathlib import Path

import pytest

import dependencies as deps
from routers import a2a
from services import a2a_gate

pytestmark = pytest.mark.unit

LEGACY = "ent885-legacy"
TAILNET_PEER = "100.70.1.2"
PUBLIC_PEER = "8.8.8.8"
BACKEND_SRC = Path(__file__).resolve().parents[2] / "src" / "backend"


def _seed_legacy_row() -> None:
    """A row exactly as rc1–rc3's migration 0096 + the enterprise setter that
    never shipped (ent#845) would have left it, plus a trusted CIDR."""
    conn = sqlite3.connect(os.environ["TRINITY_DB_PATH"])
    try:
        # The island seeds no admin without ADMIN_PASSWORD; a plain admin row is
        # enough for the real `can_user_access_agent` to admit the keyed caller.
        conn.execute(
            "INSERT OR IGNORE INTO users (username, role, created_at, updated_at) "
            "VALUES ('ent885-admin', 'admin', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')"
        )
        admin = conn.execute("SELECT id FROM users WHERE username = 'ent885-admin'").fetchone()
        conn.execute("DELETE FROM agent_ownership WHERE agent_name = ?", (LEGACY,))
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, a2a_exposed, "
            "a2a_scope, a2a_keyless_internal) VALUES (?, ?, '2026-10-01T00:00:00Z', 1, 'internal', 1)",
            (LEGACY, admin[0]),
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, '2026-10-01T00:00:00Z')",
            ("a2a_trusted_networks", json.dumps(["100.64.0.0/10"])),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def door(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _seed_legacy_row()
    state = {"dispatched": [], "executions": {}}

    monkeypatch.setattr(a2a.a2a_payment_gate, "is_priced", lambda n, db=None: None)
    monkeypatch.setattr(a2a, "get_agent_container",
                        lambda n: types.SimpleNamespace(status="running", labels={}))

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": []}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    async def _dispatch(**kw):
        state["dispatched"].append(kw)
        state["executions"]["exec-1"] = {"agent_name": kw["agent_name"], "status": "success",
                                         "triggered_by": "a2a", "response": "ok"}
        return types.SimpleNamespace(execution_id="exec-1", status="success", response="ok", error=None)
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _dispatch)
    # tasks/get + tasks/cancel read the execution row; the dispatch stub above
    # never wrote one to the real DB, so answer from the recorder.
    monkeypatch.setattr(a2a.db, "get_execution", lambda eid: state["executions"].get(eid))

    class _Audit:
        async def log(self, **kw):
            return None
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    class _Idem:
        def begin(self, scope, key):
            state["idem_scope"] = scope
            return types.SimpleNamespace(replay=False, in_flight=False, snapshot=None, key=None)

        def complete(self, *a):
            pass

        def fail(self, *a):
            pass
    monkeypatch.setattr(a2a, "idempotency_service", _Idem())
    # Redis-backed; unreachable in the unit island.
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a, **k: None)
    a2a_gate.clear_provider()

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    principal = {"user": None}
    # Mandatory: without the override every bearer-less POST is a dependency
    # 401 before the handler runs, and the test passes on any tree.
    app.dependency_overrides[deps.get_user_or_anonymous] = lambda: principal["user"]
    peer = {"ip": PUBLIC_PEER}

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer["ip"], 40000))
        await app(scope, receive, send)

    http = TestClient(asgi)

    def call(method="message/send", params=None, *, src=PUBLIC_PEER, key=False, headers=None):
        peer["ip"] = src
        # The seeded admin: the REAL `can_user_access_agent` admits it, so a
        # 404 here can only come from a source-address fence.
        principal["user"] = (types.SimpleNamespace(id=1, username="ent885-admin", email=None,
                                                   role="admin", agent_name=None, mcp_key_id="k1")
                             if key else None)
        if params is None:
            params = {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}],
                                  "messageId": "m1"}}
        return http.post(f"/a2a/{LEGACY}", headers=headers or {},
                         content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))

    def card(*, src=PUBLIC_PEER, headers=None):
        peer["ip"] = src
        return http.get(f"/a2a/{LEGACY}/.well-known/agent-card.json", headers=headers or {})

    return types.SimpleNamespace(call=call, card=card, state=state)


# --------------------------------------------------------------------------- #
# The keyless door is gone: no credential → 401, nothing dispatched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method,params", [
    ("message/send", None),
    ("message/stream", None),
    ("tasks/get", {"id": "exec-1"}),
    ("tasks/cancel", {"id": "exec-1"}),
])
def test_a_trusted_peer_with_no_credential_is_refused(door, method, params):
    r = door.call(method, params, src=TAILNET_PEER)
    assert r.status_code == 401, r.text
    assert r.headers.get("www-authenticate") == "Bearer"
    assert door.state["dispatched"] == []


@pytest.mark.parametrize("src,headers", [
    (PUBLIC_PEER, {"X-Real-IP": TAILNET_PEER}),       # a proxy-shaped claim
    (TAILNET_PEER, {"CF-Connecting-IP": "1.2.3.4"}),  # through the public tunnel
    (PUBLIC_PEER, {}),                                # the public internet
])
def test_no_claimed_or_real_address_buys_a_task(door, src, headers):
    r = door.call(src=src, headers=headers)
    assert r.status_code == 401, r.text
    assert door.state["dispatched"] == []


# --------------------------------------------------------------------------- #
# The legacy row is unread: a keyed caller runs, the card is public
# --------------------------------------------------------------------------- #
def test_a_keyed_caller_from_the_public_internet_runs_the_task(door):
    r = door.call(src=PUBLIC_PEER, key=True)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"]["state"] == "completed"
    (kw,) = door.state["dispatched"]
    assert kw["triggered_by"] == "a2a"
    assert "source_host" not in kw
    assert door.state["idem_scope"] == f"a2a:{LEGACY}:k1"


def test_the_card_is_served_at_the_public_origin(door):
    r = door.card(src=PUBLIC_PEER)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == f"http://testserver/a2a/{LEGACY}"


# --------------------------------------------------------------------------- #
# Static: nothing reads the inert columns, the db object has no scope reader
# --------------------------------------------------------------------------- #
_INERT = re.compile(r"\ba2a_scope\b|\ba2a_keyless_internal\b|\bsource_host\b")
_DDL_HOMES = {
    BACKEND_SRC / "db" / "schema.py",
    BACKEND_SRC / "db" / "tables.py",
    BACKEND_SRC / "db" / "migrations.py",
    BACKEND_SRC / "migrations" / "versions" / "0096_a2a_internal_scope.py",
}


def test_nothing_reads_the_inert_columns():
    assert not hasattr(a2a.db, "get_a2a_scope")
    assert not hasattr(a2a.db, "set_a2a_scope")
    hits = []
    for path in BACKEND_SRC.rglob("*.py"):
        if path in _DDL_HOMES or "enterprise" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _INERT.search(line):
                hits.append(f"{path.relative_to(BACKEND_SRC)}:{n}: {line.strip()}")
    assert hits == [], "\n".join(hits)
