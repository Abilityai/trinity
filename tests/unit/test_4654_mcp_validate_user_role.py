"""#4654 — key validation hands the MCP server the key OWNER's role.

The MCP chat ACL's admin bypass reads `userRole` from the auth context, and the
only source of that value is `user_role` on the `/api/mcp/validate` response.
Two lines carry it: `McpKeyOperations.validate_mcp_api_key` (db) reads
`users.role` into the result, and the router copies it onto the response. Lose
either one and every admin silently loses the bypass while nothing else fails,
so both are pinned here against the real schema (`tests/db_harness.py`) and the
real router.

`user_role` is the OWNER's role on every scope: an agent-scoped key owned by an
admin validates with `user_role == "admin"`. That is the stored fact, pinned so
a reader of the field knows to check the scope first.

The MCP server's half (the response field reaching the auth context) is pinned
in `src/mcp-server/src/validate-user-role.test.ts`.

Related: docs/memory/feature-flows/mcp-orchestration.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import POSTGRES, db_backend, run as _hrun  # noqa: E402,F401

import routers.mcp_keys as _KEYS  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

VALIDATE = "/api/mcp/validate"

# username -> role. The admin is deliberately not named "admin" and the plain
# user deliberately is: the role is what must come back, never the name.
OWNERS = {"role-root": "admin", "admin": "user"}


@pytest.fixture
def client(db_backend):
    for username, role in OWNERS.items():
        db.create_user(
            UserCreate(username=username, role=role, email=f"{username}@example.com")
        )
        db.register_agent_owner(f"agent-of-{username}", username)
    app = FastAPI()
    app.include_router(_KEYS.router)
    return TestClient(app)


def _user_key(username: str) -> str:
    return db.create_mcp_api_key(username, McpApiKeyCreate(name="laptop")).api_key


def _agent_key(username: str) -> str:
    return db.create_agent_mcp_api_key(f"agent-of-{username}", username).api_key


KEY_KINDS = {"user": _user_key, "agent": _agent_key}

CASES = [(owner, scope) for owner in sorted(OWNERS) for scope in sorted(KEY_KINDS)]


def _null_the_role(backend: str, username: str) -> None:
    """Store a NULL role, which the column's NOT NULL forbids on both tracks.

    The fallback under test exists for a row the constraint does not cover, so
    the constraint is lifted for this one test: a column alter on PostgreSQL, a
    constraint-free copy of the table on SQLite (which cannot alter one away).
    """
    if backend == POSTGRES:
        _hrun("ALTER TABLE users ALTER COLUMN role DROP NOT NULL")
    else:
        _hrun("CREATE TABLE users_nullable AS SELECT * FROM users")
        _hrun("DROP TABLE users")
        _hrun("ALTER TABLE users_nullable RENAME TO users")
    _hrun("UPDATE users SET role = NULL WHERE username = :u", u=username)


class TestDbLayer:
    """`McpKeyOperations.validate_mcp_api_key`, reached through the facade."""

    @pytest.mark.parametrize("owner,scope", CASES)
    def test_the_result_carries_the_owner_role(self, client, owner, scope):
        result = db.validate_mcp_api_key(KEY_KINDS[scope](owner))
        assert result is not None
        assert result["scope"] == scope
        assert result["user_id"] == owner
        assert result["user_role"] == OWNERS[owner]

    @pytest.mark.parametrize("scope", sorted(KEY_KINDS))
    def test_a_null_role_reads_as_user(self, client, db_backend, scope):
        key = KEY_KINDS[scope]("role-root")
        _null_the_role(db_backend, "role-root")
        result = db.validate_mcp_api_key(key)
        assert result is not None
        assert result["user_role"] == "user"


class TestValidateEndpoint:
    """`POST /api/mcp/validate` — the body the MCP server reads."""

    @pytest.mark.parametrize("owner,scope", CASES)
    def test_the_response_carries_the_owner_role(self, client, owner, scope):
        res = client.post(
            VALIDATE, headers={"Authorization": f"Bearer {KEY_KINDS[scope](owner)}"}
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["valid"] is True
        assert body["scope"] == scope
        assert body["user_id"] == owner
        # Required key, not `.get`: an absent field is the regression.
        assert body["user_role"] == OWNERS[owner]

    @pytest.mark.parametrize("scope", sorted(KEY_KINDS))
    def test_a_null_role_reads_as_user(self, client, db_backend, scope):
        key = KEY_KINDS[scope]("role-root")
        _null_the_role(db_backend, "role-root")
        res = client.post(VALIDATE, headers={"Authorization": f"Bearer {key}"})
        assert res.status_code == 200, res.text
        assert res.json()["user_role"] == "user"
