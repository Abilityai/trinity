"""Owner tier for reading credential files on the Files routes
(abilityai/trinity-enterprise#819).

`GET /files/download` and `GET /files/preview` serve every other path at the
accessor tier; an owner-tier path (credential files, their other spellings, the
Trinity-managed copies, the secret file classes) is served only to a person who
passes the owner tier (the agent's owner, or an admin).

Everything here drives the shipped logic functions in
`src/backend/services/agent_service/files.py`, or the real FastAPI router over
them. The agent call is mocked; what the backend decides is the subject.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from services.agent_service import files

AGENT = "shared-agent"
OWNER = "owner"
ADMIN = "admin-user"
SHARED = "teammate"

LOGIC = ("download_agent_file_logic", "preview_agent_file_logic")

OWNER_TIER_MESSAGE = (
    "Credential files can be opened only by the agent's owner or an admin. "
    "You can still chat with this agent, and it keeps using its credentials. "
    "To view or change them, ask the agent's owner or an admin."
)


def _user(username, role="user", mcp_scope=None, agent_name=None, connector_agent=None):
    # Non-ambient on purpose: role="user" passes no tier by role, so a 200 for
    # the shared user could only come from a missing gate.
    return SimpleNamespace(
        id=hash(username) % 1000,
        username=username,
        email=f"{username}@example.com",
        role=role,
        mcp_scope=mcp_scope,
        agent_name=agent_name,
        connector_agent=connector_agent,
        mcp_key_id=None,
        mcp_key_name=None,
    )


def shared_user():
    return _user(SHARED)


def owner_jwt():
    return _user(OWNER)


def owner_user_key():
    return _user(OWNER, mcp_scope="user")


def admin_jwt():
    return _user(ADMIN, role="admin")


def _request(method="GET", path=f"/api/agents/{AGENT}/files/download"):
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        method=method,
        scope={"path": path},
        state=SimpleNamespace(request_id="req-1"),
    )


def _response(status=200, text="FILE-BODY", json_body=None):
    def _json():
        if json_body is None:
            raise ValueError("not json")
        return json_body

    return SimpleNamespace(
        status_code=status,
        text=text,
        content=text.encode(),
        headers={"content-type": "text/plain"},
        json=_json,
    )


@pytest.fixture
def agent(monkeypatch):
    """Every gate's inputs stubbed; the agent answers 200 with FILE-BODY."""
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    # role-aware: the admin bypass lives inside can_user_share_agent
    monkeypatch.setattr(
        files.db, "can_user_share_agent", lambda u, a: u in (OWNER, ADMIN)
    )
    monkeypatch.setattr(
        files.db, "get_agent_owner", lambda a: {"owner_username": OWNER}
    )
    container = SimpleNamespace(
        status="running", id="c-1", name=f"agent-{AGENT}", attrs={"Image": "sha256:img"}
    )
    monkeypatch.setattr(files, "get_agent_container", lambda name: container)
    monkeypatch.setattr(files, "container_reload", AsyncMock())
    sent = AsyncMock(return_value=_response())
    monkeypatch.setattr(files, "agent_http_request", sent)
    return SimpleNamespace(sent=sent, container=container)


def _call(fn, path, user, request=None):
    return asyncio.run(getattr(files, fn)(AGENT, path, user, request or _request()))


def _body(result):
    """The served bytes, for either a PlainTextResponse or a StreamingResponse."""
    if hasattr(result, "body_iterator"):

        async def _drain():
            return b"".join([c async for c in result.body_iterator])

        return asyncio.run(_drain())
    return result.body


# One literal per owner-tier pattern. The guard below fails when a pattern is
# added without a row here.
PATTERN_INSTANCES = {
    ".env": ".env",
    ".env.*": "app/.env.production",
    ".mcp.json": ".mcp.json",
    ".mcp.json.template": ".mcp.json.template",
    ".credentials.enc": ".credentials.enc",
    ".ssh/*": ".ssh/id_rsa",
    ".aws/*": ".aws/credentials",
    ".gcp/*": ".gcp/service-account.json",
    ".claude/settings.json": ".claude/settings.json",
    ".claude/settings.local.json": ".claude/settings.local.json",
    ".git/config": ".git/config",
    "/proc/*": "/proc/self/cwd/notes.md",
    "/dev/*": "/dev/fd/7",
    ".trinity/git-credential": ".trinity/git-credential",
    ".trinity/backup/*": ".trinity/backup/2026-10-06T000000Z/snapshot.tar",
    ".kube/config": ".kube/config",
    ".config/gcloud/*": ".config/gcloud/application_default_credentials.json",
    "*.key": "certs/tls.key",
    "*.pem": "server.pem",
    "*.p12": "keystore.p12",
    "*.pfx": "bundle.pfx",
}


def test_every_owner_tier_pattern_has_a_row_here():
    assert set(PATTERN_INSTANCES) == set(files._OWNER_TIER_READ_PATTERNS)


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("pattern,path", sorted(PATTERN_INSTANCES.items()))
def test_a_shared_user_is_refused_every_owner_tier_pattern(agent, fn, pattern, path):
    with pytest.raises(HTTPException) as exc:
        _call(fn, path, shared_user())
    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "code": "owner_tier_path",
        "message": OWNER_TIER_MESSAGE,
        "path": files._normalize_user_path(path),
    }
    assert agent.sent.await_count == 0


def test_the_refusal_tells_a_teammate_whom_to_ask(agent):
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", ".env", shared_user())
    assert "ask the agent's owner or an admin" in exc.value.detail["message"]
    assert "You can still chat with this agent" in exc.value.detail["message"]


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize(
    "path",
    [
        "//home/developer/.env",
        "///home/developer/.mcp.json",
        "content/../.env",
        "/home/developer/./.ssh/id_rsa",
        "/proc/self/cwd/.ssh/id_rsa",
        "/proc/self/root/home/developer/.env",
        "/dev/fd/7",
        "/dev/stdin",
        ".env/",
    ],
)
def test_every_spelling_of_a_credential_path_is_refused(agent, fn, path):
    with pytest.raises(HTTPException) as exc:
        _call(fn, path, shared_user())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "owner_tier_path"
    assert agent.sent.await_count == 0


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("user", [shared_user, owner_jwt], ids=["shared", "owner"])
def test_a_nul_byte_is_refused_before_the_agent_is_called(agent, fn, user):
    with pytest.raises(HTTPException) as exc:
        _call(fn, ".env\x00", user())
    assert exc.value.status_code == 400
    assert exc.value.detail == {"code": "invalid_path", "message": "Invalid path"}
    assert agent.sent.await_count == 0


@pytest.mark.parametrize("fn", LOGIC)
def test_an_empty_path_is_owner_tier(agent, fn):
    with pytest.raises(HTTPException) as exc:
        _call(fn, "", shared_user())
    assert exc.value.status_code == 403
    assert agent.sent.await_count == 0


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize(
    "user",
    [owner_jwt, owner_user_key, admin_jwt],
    ids=["owner-jwt", "owner-user-key", "admin-jwt"],
)
@pytest.mark.parametrize("path", [".env", ".mcp.json", "server.pem"])
def test_the_owner_and_an_admin_still_read(agent, fn, user, path):
    result = _call(fn, path, user())
    assert _body(result) == b"FILE-BODY"
    assert agent.sent.await_args.kwargs["params"] == {
        "path": files._normalize_user_path(path)
    }


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize(
    "path",
    [
        ".trinity/pipelines/x.yaml",
        ".trinity/pipeline-state/p/i.json",
        "CLAUDE.md",
        "template.yaml",
        "notes.md",
        ".git/HEAD",
        "certs/ca.crt",
        "project/.kube/config",
    ],
)
def test_other_paths_stay_at_the_accessor_tier(agent, fn, path):
    result = _call(fn, path, shared_user())
    assert _body(result) == b"FILE-BODY"


@pytest.mark.parametrize(
    "path",
    [
        ".trinity/git-credential",
        ".trinity/backup/2026-10-06T000000Z/snapshot.tar",
        ".trinity/backup/2026-10-06T000000Z/files.txt",
    ],
)
def test_trinity_managed_copies_are_owner_tier(agent, path):
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", path, shared_user())
    assert exc.value.status_code == 403
    assert _body(_call("download_agent_file_logic", path, owner_jwt())) == b"FILE-BODY"


@pytest.mark.parametrize(
    "path",
    [
        "server.pem",
        "certs/tls.key",
        ".claude/private.pem",
        "node_modules/x/private.key",
        ".kube/config",
        ".config/gcloud/adc.json",
    ],
)
def test_the_secret_file_classes_are_owner_tier(agent, path):
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", path, shared_user())
    assert exc.value.detail["code"] == "owner_tier_path"


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize(
    "principal",
    [
        lambda: _user(OWNER, mcp_scope="agent", agent_name="sibling"),
        lambda: _user(OWNER, mcp_scope="system", agent_name=None),
        lambda: _user(OWNER, mcp_scope="ops"),
        lambda: _user(OWNER, mcp_scope="user", connector_agent=AGENT),
    ],
    ids=["agent-key", "system-key", "ops", "connector-key"],
)
def test_a_non_person_key_is_refused_credential_reads(agent, fn, principal):
    with pytest.raises(HTTPException) as exc:
        _call(fn, ".env", principal())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "person_required"
    assert agent.sent.await_count == 0


def test_an_agent_key_still_reads_a_pipeline_file(agent):
    key = _user(OWNER, mcp_scope="agent", agent_name="sibling")
    assert (
        _body(_call("download_agent_file_logic", ".trinity/pipelines/x.yaml", key))
        == b"FILE-BODY"
    )


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize(
    "path,forwarded",
    [
        ("//home/developer/notes.md", "/home/developer/notes.md"),
        ("docs/../notes.md", "/home/developer/notes.md"),
    ],
)
def test_the_agent_is_sent_the_path_that_was_checked(agent, fn, path, forwarded):
    _call(fn, path, shared_user())
    assert agent.sent.await_args.kwargs["params"] == {"path": forwarded}


@pytest.mark.parametrize("route", ["download", "preview"])
def test_the_real_routes_refuse_a_shared_user(agent, route):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import agent_files

    app = FastAPI()
    app.include_router(agent_files.router)
    app.dependency_overrides[agent_files.get_current_user] = shared_user
    r = TestClient(app).get(
        f"/api/agents/{AGENT}/files/{route}", params={"path": "//home/developer/.env"}
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "owner_tier_path",
        "message": OWNER_TIER_MESSAGE,
        "path": "/home/developer/.env",
    }
    assert agent.sent.await_count == 0
