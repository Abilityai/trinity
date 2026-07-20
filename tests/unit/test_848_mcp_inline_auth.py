"""Unit tests for MCP inline email auth (#848) — the backend half.

Four internal endpoints the MCP server calls for a keyless session. The tests
below are organised around the two properties the feature must not lose:

  * ``/request`` is not an open email relay AND its unknown-address branch is
    indistinguishable from the known one (byte-identical body, no code row, no
    audit row, silent rate-limit skip).
  * The internal secret authenticates the CALLER, never the action — every data
    call re-gates on the asserted email's own access, and nothing ever returns a
    credential.

True unit tests: no Docker, no running backend, no real Redis. The DB is a
throwaway sqlite seeded with just the tables these paths touch.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SECRET = "unit-internal-secret"
_HDR = {"X-Internal-Secret": _SECRET}

_OWNER = "owner@example.com"
_SHARED = "shared@example.com"
_UNKNOWN = "nobody@example.com"


@pytest.fixture()
def inline_db(tmp_path, monkeypatch):
    """Fresh sqlite with the tables inline auth reads/writes."""
    db_file = tmp_path / "trinity-848.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        users,
        agent_ownership,
        agent_sharing,
        email_login_codes,
        email_whitelist,
        enterprise_connectors,
        mcp_api_keys,
        idempotency_keys,
    )
    oss_metadata.create_all(
        get_engine(),
        tables=[
            users, agent_ownership, agent_sharing, email_login_codes,
            email_whitelist, enterprise_connectors, mcp_api_keys, idempotency_keys,
        ],
    )

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username=_OWNER, role="creator", email=_OWNER,
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        ))
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-a", owner_id=1, created_at="2026-01-01T00:00:00Z",
        ))
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-b", owner_id=1, created_at="2026-01-01T00:00:00Z",
        ))
        # agent-a is shared with an address that has NO user account yet — the
        # exact user inline auth exists to onboard.
        conn.execute(insert(agent_sharing).values(
            agent_name="agent-a", shared_with_email=_SHARED, shared_by_id=1,
            created_at="2026-01-01T00:00:00Z",
        ))
        conn.execute(insert(enterprise_connectors).values(
            agent_name="agent-a", enabled=1, created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
        # agent-b is shared too, but its connector is DISABLED.
        conn.execute(insert(agent_sharing).values(
            agent_name="agent-b", shared_with_email=_SHARED, shared_by_id=1,
            created_at="2026-01-01T00:00:00Z",
        ))
        conn.execute(insert(enterprise_connectors).values(
            agent_name="agent-b", enabled=0, created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
    yield str(db_file)


@pytest.fixture()
def client(inline_db, monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.mcp_auth import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _no_redis_limiters(client, monkeypatch):
    """The auth limiters fail open when Redis is absent; pin that explicitly so
    a developer machine with a live Redis can't make these tests flaky (several
    tests here deliberately burn OTP attempts). Resolved after ``client`` for
    the same module-eviction reason as ``audit_rows``."""
    import routers.auth as auth_mod
    monkeypatch.setattr(auth_mod, "get_redis_client", lambda: None)


@pytest.fixture(autouse=True)
def audit_rows(client, monkeypatch):
    """Capture audit writes instead of hitting the DB.

    Patches the singleton *as the router module holds it*, and only after the
    client is built. The unit conftest evicts backend modules from
    ``sys.modules`` before every test, so a singleton resolved by an earlier
    import is not necessarily the one the router will call — patch the live
    reference, not a freshly-imported one.
    """
    import routers.mcp_auth as router_mod
    rows = []

    async def _log(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(router_mod.platform_audit_service, "log", _log)
    return rows


@pytest.fixture()
def sent_emails(client, monkeypatch):
    """Intercept the fire-and-forget code dispatch (no real mail, ever).

    Depends on ``client`` so it patches the same service module the router
    imported — see ``audit_rows``.
    """
    sent = []

    async def _dispatch(email, code):
        sent.append((email, code))

    import services.mcp_auth_service as svc
    monkeypatch.setattr(svc, "_dispatch_code_email", _dispatch)
    return sent


def _code_rows():
    from db.engine import get_engine
    from db.tables import email_login_codes
    from sqlalchemy import select
    with get_engine().connect() as conn:
        return conn.execute(select(email_login_codes)).mappings().all()


# ---------------------------------------------------------------------------
# /request — not an email relay, and enumeration-safe
# ---------------------------------------------------------------------------

class TestRequestEnumerationSafety:
    def test_known_and_unknown_return_byte_identical_responses(self, client, sent_emails):
        """The whole point. Same status, same bytes — a caller cannot tell
        whether the address is registered here."""
        known = client.post("/api/internal/mcp-auth/request",
                            json={"email": _SHARED}, headers=_HDR)
        unknown = client.post("/api/internal/mcp-auth/request",
                              json={"email": _UNKNOWN}, headers=_HDR)

        assert known.status_code == unknown.status_code == 202
        assert known.content == unknown.content
        assert known.json() == {"status": "ok"}
        # No expires_in_seconds / message differential leaking back in.
        assert set(known.json().keys()) == {"status"}

    def test_unknown_email_sends_nothing_and_creates_no_code(self, client, sent_emails):
        """An unauthenticated caller must not be able to make Trinity email an
        arbitrary address — the open-relay property."""
        r = client.post("/api/internal/mcp-auth/request",
                        json={"email": _UNKNOWN}, headers=_HDR)
        assert r.status_code == 202
        assert sent_emails == []
        assert _code_rows() == []

    def test_known_shared_email_gets_a_code(self, client, sent_emails):
        """An address on an agent's sharing allow-list is 'known' even with no
        user account — that is the onboarding case the feature exists for."""
        r = client.post("/api/internal/mcp-auth/request",
                        json={"email": _SHARED}, headers=_HDR)
        assert r.status_code == 202
        assert [e for e, _c in sent_emails] == [_SHARED]
        rows = _code_rows()
        assert len(rows) == 1 and rows[0]["email"] == _SHARED

    def test_existing_user_email_gets_a_code(self, client, sent_emails):
        client.post("/api/internal/mcp-auth/request",
                    json={"email": _OWNER}, headers=_HDR)
        assert [e for e, _c in sent_emails] == [_OWNER]

    def test_email_is_normalized(self, client, sent_emails):
        client.post("/api/internal/mcp-auth/request",
                    json={"email": f"  {_SHARED.upper()}  "}, headers=_HDR)
        assert [e for e, _c in sent_emails] == [_SHARED]

    def test_emits_no_audit_row(self, client, sent_emails, audit_rows):
        """An audit row is itself an enumeration oracle — the known branch must
        not write one the unknown branch doesn't."""
        client.post("/api/internal/mcp-auth/request",
                    json={"email": _SHARED}, headers=_HDR)
        client.post("/api/internal/mcp-auth/request",
                    json={"email": _UNKNOWN}, headers=_HDR)
        assert audit_rows == []

    def test_rate_limit_caps_at_3_per_10_min_with_same_response(self, client, sent_emails):
        """Over-limit is a silent skip, never a 429 — a status differential on
        the 4th request would tell the caller the first 3 were 'real'."""
        bodies = []
        for _ in range(5):
            r = client.post("/api/internal/mcp-auth/request",
                            json={"email": _SHARED}, headers=_HDR)
            assert r.status_code == 202
            bodies.append(r.content)

        assert len(sent_emails) == 3, "cap is 3 sends per 10-minute window"
        assert len(set(bodies)) == 1, "every response identical, limited or not"

    def test_known_email_lookup_failure_fails_closed(self, client, sent_emails, monkeypatch):
        """If we cannot answer 'do we know this address', we must not email it."""
        import services.mcp_auth_service as svc
        monkeypatch.setattr(
            svc.db, "get_user_by_email",
            lambda _e: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        r = client.post("/api/internal/mcp-auth/request",
                        json={"email": _SHARED}, headers=_HDR)
        assert r.status_code == 202
        assert sent_emails == []


# ---------------------------------------------------------------------------
# /verify
# ---------------------------------------------------------------------------

def _issue_code(email: str) -> str:
    from database import db
    return db.create_login_code(email, expiry_minutes=10)["code"]


class TestVerify:
    def test_bad_code_returns_401_and_audits_login_failed(self, client, audit_rows):
        r = client.post("/api/internal/mcp-auth/verify",
                        json={"email": _SHARED, "code": "000000"}, headers=_HDR)
        assert r.status_code == 401

        failures = [a for a in audit_rows if a["event_action"] == "login_failed"]
        assert len(failures) == 1
        assert failures[0]["source"] == "mcp"
        assert failures[0]["details"] == {"method": "mcp_inline", "email": _SHARED}

    def test_success_returns_accessible_agents(self, client):
        code = _issue_code(_SHARED)
        r = client.post("/api/internal/mcp-auth/verify",
                        json={"email": _SHARED, "code": code}, headers=_HDR)
        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is True
        assert body["username"] == _SHARED
        # agent-a only: agent-b is shared but its connector is disabled.
        assert [a["name"] for a in body["agents"]] == ["agent-a"]

    def test_success_returns_no_credential_of_any_kind(self, client):
        """§7.6: session, not a minted key. Scan the WHOLE payload — a nested
        token would pass a top-level key check."""
        code = _issue_code(_SHARED)
        r = client.post("/api/internal/mcp-auth/verify",
                        json={"email": _SHARED, "code": code}, headers=_HDR)
        assert r.status_code == 200

        raw = json.dumps(r.json()).lower()
        for forbidden in ("token", "api_key", "apikey", "secret",
                          "trinity_mcp_", "password", "bearer", "access_token"):
            assert forbidden not in raw, f"credential-ish field {forbidden!r} in verify payload"

        def _keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from _keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from _keys(item)

        assert set(_keys(r.json())) == {"verified", "username", "agents", "name", "description"}

    def test_first_login_creates_user_with_role_user_never_creator(self, client):
        """#314 regression: an access grant must not silently promote."""
        code = _issue_code(_SHARED)
        r = client.post("/api/internal/mcp-auth/verify",
                        json={"email": _SHARED, "code": code}, headers=_HDR)
        assert r.status_code == 200

        from database import db
        user = db.get_user_by_email(_SHARED)
        assert user is not None
        assert user["role"] == "user", "inline login must never mint a creator"

    def test_success_audits_login_success(self, client, audit_rows):
        code = _issue_code(_SHARED)
        client.post("/api/internal/mcp-auth/verify",
                    json={"email": _SHARED, "code": code}, headers=_HDR)
        ok = [a for a in audit_rows if a["event_action"] == "login_success"]
        assert len(ok) == 1
        assert ok[0]["source"] == "mcp"
        assert ok[0]["target_id"] == _SHARED

    def test_code_is_single_use(self, client):
        code = _issue_code(_SHARED)
        first = client.post("/api/internal/mcp-auth/verify",
                            json={"email": _SHARED, "code": code}, headers=_HDR)
        second = client.post("/api/internal/mcp-auth/verify",
                             json={"email": _SHARED, "code": code}, headers=_HDR)
        assert first.status_code == 200
        assert second.status_code == 401

    def test_verified_unknown_email_gets_no_agents(self, client):
        """A code issued for an address nothing is shared with verifies fine but
        reaches nothing — access is the sharing gate, not the code."""
        code = _issue_code(_UNKNOWN)
        r = client.post("/api/internal/mcp-auth/verify",
                        json={"email": _UNKNOWN, "code": code}, headers=_HDR)
        assert r.status_code == 200
        assert r.json()["agents"] == []


# ---------------------------------------------------------------------------
# /playbooks + /chat — the per-call authorization gate
# ---------------------------------------------------------------------------

_PLAYBOOKS = "/api/internal/mcp-auth/playbooks"
_CHAT = "/api/internal/mcp-auth/chat"


class TestAccessGate:
    def test_playbooks_403_when_email_lacks_access(self, client):
        r = client.post(_PLAYBOOKS, json={"email": _UNKNOWN, "agent": "agent-a"},
                        headers=_HDR)
        assert r.status_code == 403

    def test_chat_403_when_email_lacks_access(self, client):
        r = client.post(_CHAT, json={"email": _UNKNOWN, "agent": "agent-a",
                                     "message": "hi"}, headers=_HDR)
        assert r.status_code == 403

    def test_playbooks_403_when_connector_disabled(self, client):
        """agent-b IS shared with this email — the connector being off is what
        denies it."""
        r = client.post(_PLAYBOOKS, json={"email": _SHARED, "agent": "agent-b"},
                        headers=_HDR)
        assert r.status_code == 403

    def test_chat_403_when_connector_disabled(self, client):
        r = client.post(_CHAT, json={"email": _SHARED, "agent": "agent-b",
                                     "message": "hi"}, headers=_HDR)
        assert r.status_code == 403

    def test_playbooks_403_for_unknown_agent(self, client):
        r = client.post(_PLAYBOOKS, json={"email": _SHARED, "agent": "no-such-agent"},
                        headers=_HDR)
        assert r.status_code == 403

    def test_denied_bodies_are_uniform(self, client):
        """No-access vs connector-disabled vs no-such-agent must not be
        distinguishable — that split enumerates the fleet."""
        a = client.post(_PLAYBOOKS, json={"email": _UNKNOWN, "agent": "agent-a"}, headers=_HDR)
        b = client.post(_PLAYBOOKS, json={"email": _SHARED, "agent": "agent-b"}, headers=_HDR)
        c = client.post(_PLAYBOOKS, json={"email": _SHARED, "agent": "nope"}, headers=_HDR)
        assert a.status_code == b.status_code == c.status_code == 403
        assert a.content == b.content == c.content

    def test_playbooks_allowed_returns_exposed_set(self, client):
        live = [
            {"name": "cso", "description": "audit", "user_invocable": True},
            {"name": "internal-only", "user_invocable": False},
        ]
        with patch("services.mcp_auth_service.fetch_live_playbooks",
                   new=AsyncMock(return_value=live)):
            r = client.post(_PLAYBOOKS, json={"email": _SHARED, "agent": "agent-a"},
                            headers=_HDR)
        assert r.status_code == 200
        assert [p["name"] for p in r.json()] == ["cso"]

    def test_chat_allowed_dispatches_and_returns_response(self, client):
        from services.task_execution_service import TaskExecutionResult
        result = TaskExecutionResult(
            execution_id="exec-1", status="success", response="hello there",
        )
        svc = AsyncMock()
        svc.execute_task = AsyncMock(return_value=result)
        with patch("services.task_execution_service.get_task_execution_service",
                   return_value=svc):
            r = client.post(_CHAT, json={"email": _SHARED, "agent": "agent-a",
                                         "message": "hi"}, headers=_HDR)
        assert r.status_code == 200
        assert r.json()["response"] == "hello there"
        assert r.json()["execution_id"] == "exec-1"

        kwargs = svc.execute_task.await_args.kwargs
        assert kwargs["agent_name"] == "agent-a"
        assert kwargs["source_user_email"] == _SHARED
        assert kwargs["triggered_by"] == "mcp"

    def test_chat_gate_runs_before_dispatch(self, client):
        """A denied caller must never reach the execution path at all."""
        svc = AsyncMock()
        with patch("services.task_execution_service.get_task_execution_service",
                   return_value=svc):
            r = client.post(_CHAT, json={"email": _UNKNOWN, "agent": "agent-a",
                                         "message": "hi"}, headers=_HDR)
        assert r.status_code == 403
        svc.execute_task.assert_not_awaited()


# ---------------------------------------------------------------------------
# Invariant #18 — idempotency on the chat trigger boundary
# ---------------------------------------------------------------------------

class TestChatIdempotency:
    def test_same_key_replays_without_a_second_dispatch(self, client):
        from services.task_execution_service import TaskExecutionResult
        result = TaskExecutionResult(
            execution_id="exec-1", status="success", response="once",
        )
        svc = AsyncMock()
        svc.execute_task = AsyncMock(return_value=result)
        body = {"email": _SHARED, "agent": "agent-a", "message": "hi",
                "idempotency_key": "k-1"}

        with patch("services.task_execution_service.get_task_execution_service",
                   return_value=svc):
            first = client.post(_CHAT, json=body, headers=_HDR)
            second = client.post(_CHAT, json=body, headers=_HDR)

        assert first.status_code == 200 and second.status_code == 200
        assert second.json()["response"] == "once"
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert svc.execute_task.await_count == 1


# ---------------------------------------------------------------------------
# C-003 — the internal-secret gate on all four endpoints
# ---------------------------------------------------------------------------

class TestInternalSecretGate:
    _CALLS = [
        ("/api/internal/mcp-auth/request", {"email": _SHARED}),
        ("/api/internal/mcp-auth/verify", {"email": _SHARED, "code": "123456"}),
        (_PLAYBOOKS, {"email": _SHARED, "agent": "agent-a"}),
        (_CHAT, {"email": _SHARED, "agent": "agent-a", "message": "hi"}),
    ]

    @pytest.mark.parametrize("path,body", _CALLS)
    def test_missing_secret_rejected(self, client, path, body, sent_emails):
        r = client.post(path, json=body)
        assert r.status_code in (401, 403)
        assert sent_emails == [], "a rejected call must not have any side effect"

    @pytest.mark.parametrize("path,body", _CALLS)
    def test_wrong_secret_rejected(self, client, path, body, sent_emails):
        r = client.post(path, json=body, headers={"X-Internal-Secret": "wrong"})
        assert r.status_code in (401, 403)
        assert sent_emails == []
