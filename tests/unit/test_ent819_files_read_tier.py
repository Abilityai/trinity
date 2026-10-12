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
    # The agent runs the current image: the read-policy probe finds its target.
    probe = AsyncMock(return_value={"exit_code": 0, "output": "", "timed_out": False})
    monkeypatch.setattr(files, "execute_command_in_container", probe, raising=False)
    monkeypatch.setattr(files, "_READ_POLICY_PROBE_CACHE", {}, raising=False)
    return SimpleNamespace(sent=sent, container=container, probe=probe)


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
    ".claude.json": ".claude.json",
    ".claude/.credentials.json": ".claude/.credentials.json",
    ".gemini/settings.json": ".gemini/settings.json",
    ".tmp/codex/*": ".tmp/codex/auth.json",
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


# ---- audit ----------------------------------------------------------------------


@pytest.fixture
def audit(monkeypatch):
    from services.platform_audit_service import platform_audit_service

    log = AsyncMock(return_value="evt-1")
    monkeypatch.setattr(platform_audit_service, "log", log)
    return log


def _rows(log, action):
    return [c.kwargs for c in log.await_args_list if c.kwargs.get("event_action") == action]


@pytest.mark.parametrize("fn,endpoint", [
    ("download_agent_file_logic", f"/api/agents/{AGENT}/files/download"),
    ("preview_agent_file_logic", f"/api/agents/{AGENT}/files/preview"),
])
def test_a_refused_read_writes_one_row(agent, audit, fn, endpoint):
    from services.platform_audit_service import AuditEventType

    user = shared_user()
    with pytest.raises(HTTPException):
        _call(fn, "//home/developer/.env", user, _request(path=endpoint))
    rows = _rows(audit, "file_read_refused")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == AuditEventType.AUTHORIZATION
    assert row["source"] == "api"
    assert row["actor_user"] is user
    assert row["target_type"] == "agent" and row["target_id"] == AGENT
    assert row["endpoint"] == endpoint
    assert row["request_id"] == "req-1"
    assert row["details"] == {"path": "/home/developer/.env", "status": 403, "method": "GET",
                              "tier": "owner", "rule": "credential"}


@pytest.mark.parametrize("path,rule", [
    (".env", "credential"), ("/proc/self/cwd/x", "alias"), ("/dev/fd/7", "alias"),
    (".trinity/git-credential", "trinity_copy"), ("server.pem", "secret_class"), ("", "empty"),
    (".claude.json", "runtime"), (".tmp/codex/config.toml", "runtime"),
])
def test_the_row_names_the_rule(agent, audit, path, rule):
    with pytest.raises(HTTPException):
        _call("download_agent_file_logic", path, shared_user())
    assert _rows(audit, "file_read_refused")[0]["details"]["rule"] == rule


def test_an_agent_key_is_filed_as_the_agent_not_its_owner(agent, audit):
    key = _user(OWNER, mcp_scope="agent", agent_name="sibling")
    key.mcp_key_id, key.mcp_key_name = "k-1", "sibling-key"
    with pytest.raises(HTTPException):
        _call("download_agent_file_logic", ".env", key)
    row = _rows(audit, "file_read_refused")[0]
    assert "actor_user" not in row
    assert row["actor_agent_name"] == "sibling"
    assert row["actor_email"] == f"{OWNER}@example.com"
    assert (row["mcp_key_id"], row["mcp_key_name"], row["mcp_scope"]) == ("k-1", "sibling-key", "agent")
    assert row["details"]["status"] == 403


@pytest.mark.parametrize("scope,connector", [("system", None), ("ops", None), ("user", AGENT)],
                         ids=["system", "ops", "connector"])
def test_other_keys_are_filed_without_the_owner_as_actor(agent, audit, scope, connector):
    key = _user(OWNER, mcp_scope=scope, connector_agent=connector)
    with pytest.raises(HTTPException):
        _call("download_agent_file_logic", ".env", key)
    row = _rows(audit, "file_read_refused")[0]
    assert "actor_user" not in row
    assert row["mcp_scope"] == scope
    assert row["actor_email"] == f"{OWNER}@example.com"


@pytest.mark.parametrize("user,rows", [
    (admin_jwt, 1),        # an admin who is not the owner
    (owner_user_key, 1),   # the owner, through a key rather than a session
    (owner_jwt, 0),        # the owner's own session
], ids=["admin", "owner-key", "owner-jwt"])
def test_allowed_owner_tier_reads_are_audited_except_the_owners_own_session(agent, audit, user, rows):
    result = _call("download_agent_file_logic", ".env", user())
    assert _body(result) == b"FILE-BODY"
    allowed = _rows(audit, "file_read_allowed")
    assert len(allowed) == rows
    if rows:
        assert allowed[0]["details"]["status"] == 200
    assert _rows(audit, "file_read_refused") == []


def test_an_admin_who_owns_the_agent_is_not_audited(agent, audit, monkeypatch):
    monkeypatch.setattr(files.db, "get_agent_owner", lambda a: {"owner_username": ADMIN})
    _call("download_agent_file_logic", ".env", admin_jwt())
    assert _rows(audit, "file_read_allowed") == []


def test_an_owner_lookup_failure_audits_the_read(agent, audit, monkeypatch):
    def _boom(a):
        raise RuntimeError("db down")
    monkeypatch.setattr(files.db, "get_agent_owner", _boom)
    _call("download_agent_file_logic", ".env", owner_jwt())
    assert len(_rows(audit, "file_read_allowed")) == 1


def test_an_ordinary_read_writes_no_row(agent, audit):
    _call("download_agent_file_logic", "notes.md", shared_user())
    _call("download_agent_file_logic", "notes.md", admin_jwt())
    assert audit.await_count == 0


def test_an_audit_failure_never_changes_the_answer(agent, audit):
    audit.side_effect = RuntimeError("audit store down")
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", ".env", shared_user())
    assert exc.value.status_code == 403
    assert _body(_call("download_agent_file_logic", ".env", admin_jwt())) == b"FILE-BODY"


# ---- the agent's link refusal ---------------------------------------------------

LINK_MESSAGE = "This path is a link. Links are not opened by the file routes; open the file it points to."


def _link_refusal():
    return _response(status=403, text='{"detail": {"code": "resolved_path_mismatch"}}',
                     json_body={"detail": {"code": "resolved_path_mismatch", "message": LINK_MESSAGE}})


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("user", [shared_user, owner_jwt], ids=["shared", "owner"])
def test_a_link_refusal_from_the_agent_is_audited_and_structured(agent, audit, fn, user):
    agent.sent.return_value = _link_refusal()
    with pytest.raises(HTTPException) as exc:
        _call(fn, "//home/developer/notes-link", user())
    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "resolved_path_mismatch", "message": LINK_MESSAGE,
                                "path": "/home/developer/notes-link"}
    rows = _rows(audit, "file_read_refused")
    assert len(rows) == 1
    assert rows[0]["details"]["rule"] == "resolved_path_mismatch"
    assert rows[0]["details"]["status"] == 403
    assert rows[0]["details"]["path"] == "/home/developer/notes-link"


@pytest.mark.parametrize("fn", LOGIC)
def test_another_agent_403_passes_through_without_a_row(agent, audit, fn):
    agent.sent.return_value = _response(status=403, text='{"detail": "Access denied"}',
                                        json_body={"detail": "Access denied"})
    with pytest.raises(HTTPException) as exc:
        _call(fn, "notes.md", shared_user())
    assert exc.value.status_code == 403
    assert not isinstance(exc.value.detail, dict)
    assert audit.await_count == 0


def test_a_non_json_403_passes_through(agent, audit):
    # (preview's existing error path reads the body as JSON; unchanged here)
    agent.sent.return_value = _response(status=403, text="forbidden")
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", "notes.md", shared_user())
    assert exc.value.status_code == 403
    assert exc.value.detail == "Failed to download file: forbidden"
    assert audit.await_count == 0


def test_a_platform_read_of_a_link_logs_a_warning_naming_the_file(caplog):
    """`agent_client.read_file` feeds the platform's own reads (CLAUDE.md,
    template.yaml, ...). It still fails soft, and now says why."""
    import httpx
    from services.agent_client import AgentClient

    fake = SimpleNamespace(agent_name="shared-agent", get=AsyncMock(return_value=httpx.Response(
        403, json={"detail": {"code": "resolved_path_mismatch", "message": LINK_MESSAGE}})))
    with caplog.at_level("WARNING"):
        result = asyncio.run(AgentClient.read_file(fake, "CLAUDE.md"))
    assert result["success"] is False and result["status_code"] == 403
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("CLAUDE.md" in m and "link" in m and "shared-agent" in m for m in warned), warned


@pytest.mark.parametrize("detail,named", [
    ({"code": "resolved_path_mismatch", "message": "a link"}, True),
    ("Access denied", False),
], ids=["link", "string-detail"])
def test_an_objective_read_of_a_link_logs_a_warning_naming_the_file(caplog, detail, named):
    """The objective join reads template.yaml and objectives over the agent
    door directly, not `agent_client.read_file`. A linked file still fails
    soft there, and is named; any other 403 names nothing."""
    import httpx

    from services import objective_join_service as svc

    path = "canon/objectives/q4.yaml"
    fake = SimpleNamespace(agent_name="linked-agent", get=AsyncMock(
        return_value=httpx.Response(403, json={"detail": detail})))
    with caplog.at_level("WARNING"):
        assert asyncio.run(svc._read_yaml(fake, path)) == (None, "unreadable")
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    hits = [m for m in warned if path in m and "link" in m and "linked-agent" in m]
    assert bool(hits) is named, warned


def _pipeline_agent(monkeypatch, downloads):
    """The Work card's pipeline reader over a real httpx client: one listed
    state file, and `downloads` mapping a path to its (status, body)."""
    import json

    import httpx

    import services.agent_auth as auth
    from client_portal.work import pipeline_state as ps

    tree = [{"type": "directory", "name": "digest", "children": [
        {"type": "file", "name": "i1.json", "size": 200, "modified": "2026-09-06T10:05:00Z"}]}]

    def handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json={"tree": tree})
        status, body = downloads.get(request.url.params["path"], (404, b""))
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return httpx.Response(status, content=body)

    monkeypatch.setattr(auth, "agent_httpx_client", lambda name, **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    ps.clear_cache()
    return ps


@pytest.mark.parametrize("linked", ["state", "definition"])
def test_a_pipeline_read_of_a_link_logs_a_warning_naming_the_file(monkeypatch, caplog, linked):
    """The Work card reads pipeline files with its own httpx client, not
    `agent_client.read_file`. A linked file still fails soft there, and is named."""
    from client_portal.work import pipeline_state as ps

    state_path = f"{ps.STATE_DIR}/digest/i1.json"
    def_path = f"{ps.PIPELINES_DIR}/digest.yaml"
    refusal = (403, {"detail": {"code": "resolved_path_mismatch", "message": LINK_MESSAGE}})
    state = (200, {"current_stage": "draft", "updated_at": "2026-09-06T10:05:00Z"})
    definition = (200, b"stages:\n  - id: draft\n")
    linked_path = state_path if linked == "state" else def_path
    downloads = {state_path: refusal if linked == "state" else state,
                 def_path: refusal if linked == "definition" else definition}
    ps = _pipeline_agent(monkeypatch, downloads)
    try:
        with caplog.at_level("WARNING"):
            steps = asyncio.run(ps.read_pipeline_steps(AGENT))
    finally:
        ps.clear_cache()
    assert steps.state == ("none" if linked == "state" else "reported")
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(linked_path in m and "link" in m and AGENT in m for m in warned), warned


def test_a_pipeline_read_does_not_read_a_403_body_past_its_cap(monkeypatch, caplog):
    """The reader's byte budget covers a refusal body too: an oversized 403 is
    dropped unread past the cap, and names nothing."""
    from client_portal.work import pipeline_state as ps

    state_path = f"{ps.STATE_DIR}/digest/i1.json"
    huge = b'{"detail": {"code": "resolved_path_mismatch"}, "pad": "' + b"x" * (ps.MAX_FILE_BYTES + 5) + b'"}'
    ps = _pipeline_agent(monkeypatch, {state_path: (403, huge)})
    try:
        with caplog.at_level("WARNING"):
            steps = asyncio.run(ps.read_pipeline_steps(AGENT))
    finally:
        ps.clear_cache()
    assert steps.state == "none"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---- agents not yet on the current image -------------------------------------------

RESTART_MESSAGE = (
    "This agent needs a restart to apply an update. "
    "Ask the agent's owner or an admin to stop and start it in Trinity."
)


def _old_image(agent, code=1):
    agent.probe.return_value = {"exit_code": code, "output": "", "timed_out": False}


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("code", [1, 2], ids=["token-absent", "file-absent"])
def test_below_owner_reads_are_refused_on_an_unverified_image(agent, fn, code):
    _old_image(agent, code)
    with pytest.raises(HTTPException) as exc:
        _call(fn, "notes.md", shared_user())
    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "agent_restart_required", "message": RESTART_MESSAGE,
                                "path": "/home/developer/notes.md"}
    assert agent.sent.await_count == 0


def test_the_probe_greps_the_agent_servers_files_router(agent):
    _call("download_agent_file_logic", "notes.md", shared_user())
    call = agent.probe.await_args
    assert call.args[0] == f"agent-{AGENT}" or call.kwargs.get("container_name") == f"agent-{AGENT}"
    command = call.kwargs.get("command", call.args[1] if len(call.args) > 1 else None)
    assert command == ["grep", "-qsF", "--", "_open_for_read", "/app/agent_server/routers/files.py"]


def _without_comments(source):
    import io
    import tokenize

    lines = source.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:col] + " " * (end - col) + line[end:]
    return "".join(lines)


def test_the_probe_target_is_the_shipped_agent_servers_read_function():
    """The probe's path is where the base image copies the agent server's
    files router, and that router defines the probed function at module level.
    Renaming or moving either one must fail here, not on every agent."""
    import ast
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    base = repo / "docker/base-image"
    copies = re.findall(r"^COPY\s+(?:--\S+\s+)*\./agent_server\s+(/app/agent_server)\s*$",
                        (base / "Dockerfile").read_text(), flags=re.MULTILINE)
    assert copies == ["/app/agent_server"], copies
    probe_path, token = files._READ_POLICY_PROBE_PATH, files._READ_POLICY_PROBE_TOKEN
    assert probe_path.startswith("/app/agent_server/"), probe_path
    target = base / "agent_server" / probe_path[len("/app/agent_server/"):]
    assert target.is_file(), target
    source = target.read_text()
    assert re.search(rf"^def {re.escape(token)}\(", _without_comments(source), flags=re.MULTILINE), (
        f"{target.relative_to(repo)} defines no module-level def {token}(")
    assert token in {n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("user", [owner_jwt, owner_user_key, admin_jwt],
                         ids=["owner-jwt", "owner-user-key", "admin-jwt"])
def test_the_owner_and_an_admin_are_unaffected_and_never_probe(agent, fn, user):
    _old_image(agent)
    assert _body(_call(fn, "notes.md", user())) == b"FILE-BODY"
    assert agent.probe.await_count == 0


@pytest.mark.parametrize("fn", LOGIC)
@pytest.mark.parametrize("key", [
    lambda: _user(OWNER, mcp_scope="agent", agent_name=AGENT),
    lambda: _user(OWNER, mcp_scope="agent", agent_name="sibling"),
], ids=["own-agent-key", "sibling-agent-key"])
def test_an_agent_key_of_the_owner_is_refused_on_an_unverified_image(agent, fn, key):
    """Only a person who is the owner or an admin skips the image check; an
    agent key is below the owner tier, as on the credential paths."""
    _old_image(agent)
    with pytest.raises(HTTPException) as exc:
        _call(fn, "notes.md", key())
    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "agent_restart_required", "message": RESTART_MESSAGE,
                                "path": "/home/developer/notes.md"}
    assert agent.probe.await_count == 1
    assert agent.sent.await_count == 0


def test_an_agent_key_reads_a_pipeline_file_on_a_verified_image(agent):
    key = _user(OWNER, mcp_scope="agent", agent_name="sibling")
    assert (
        _body(_call("download_agent_file_logic", ".trinity/pipelines/x.yaml", key))
        == b"FILE-BODY"
    )
    assert agent.probe.await_count == 1


def test_a_verified_image_serves_a_shared_user(agent):
    assert _body(_call("download_agent_file_logic", "notes.md", shared_user())) == b"FILE-BODY"


def test_the_verdict_is_cached_per_container_and_image(agent):
    _call("download_agent_file_logic", "notes.md", shared_user())
    _call("preview_agent_file_logic", "a.png", shared_user())
    assert agent.probe.await_count == 1
    agent.container.attrs = {"Image": "sha256:newer"}          # recreated on a new image
    _call("download_agent_file_logic", "notes.md", shared_user())
    assert agent.probe.await_count == 2
    agent.container.id = "c-2"                                 # a new container
    _call("download_agent_file_logic", "notes.md", shared_user())
    assert agent.probe.await_count == 3


@pytest.mark.parametrize("failure", [
    {"exit_code": 124, "output": "", "timed_out": True},
    {"exit_code": 126, "output": "", "timed_out": False},
    {"exit_code": 1, "output": "Error executing command: boom", "timed_out": False},
    {"exit_code": 2, "output": "Container agent-x not found", "timed_out": False},
    RuntimeError("docker down"),
], ids=["timeout", "exec-error", "docker-error-text", "exit-2-with-text", "raises"])
def test_an_inconclusive_probe_fails_safe_and_is_not_cached(agent, failure):
    if isinstance(failure, Exception):
        agent.probe.side_effect = failure
    else:
        agent.probe.return_value = failure
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            _call("download_agent_file_logic", "notes.md", shared_user())
        assert exc.value.detail["code"] == "agent_restart_required"
    assert agent.probe.await_count == 2


def test_a_quiet_absent_token_is_cached_as_not_verified(agent):
    """grep -qs prints nothing: exit 1 with no output is the image's answer."""
    _old_image(agent, 1)
    for _ in range(2):
        with pytest.raises(HTTPException):
            _call("download_agent_file_logic", "notes.md", shared_user())
    assert agent.probe.await_count == 1
    assert files._READ_POLICY_PROBE_CACHE == {("c-1", "sha256:img"): False}


def test_the_real_exec_helper_without_docker_is_not_cached(agent, monkeypatch):
    """The shipped exec helper reports a Docker fault as exit 1 with text in
    `output`; that is not the image's answer, so nothing is cached."""
    from services import docker_service

    real = docker_service.execute_command_in_container
    assert real.__globals__ is vars(docker_service)
    monkeypatch.setattr(docker_service, "docker_client", None)
    monkeypatch.setattr(files, "execute_command_in_container", real)
    assert asyncio.run(files._agent_reads_without_links(agent.container, AGENT)) is False
    assert files._READ_POLICY_PROBE_CACHE == {}
    with pytest.raises(HTTPException) as exc:
        _call("download_agent_file_logic", "notes.md", shared_user())
    assert exc.value.detail["code"] == "agent_restart_required"
    assert files._READ_POLICY_PROBE_CACHE == {}


def test_each_refusal_is_counted_in_the_log(agent, caplog):
    _old_image(agent)
    before = files._UNVERIFIED_IMAGE_REFUSALS
    with caplog.at_level("WARNING"):
        for _ in range(2):
            with pytest.raises(HTTPException):
                _call("download_agent_file_logic", "notes.md", shared_user())
    assert files._UNVERIFIED_IMAGE_REFUSALS == before + 2
    lines = [r.getMessage() for r in caplog.records if "restart" in r.getMessage()]
    assert len(lines) == 2 and f"refusals={before + 2}" in lines[-1]


# ---- the real routes: the forwarded path, the audit and the image check ----------


def _route_client(user):
    """The shipped router over the shipped logic; only the signed-in user is
    supplied. Server errors come back as responses, so a 500 is asserted, not raised."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import agent_files

    app = FastAPI()
    app.include_router(agent_files.router)
    app.dependency_overrides[agent_files.get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("route", ["download", "preview"])
@pytest.mark.parametrize("path", ["//home/developer/notes.md", "docs/../notes.md"],
                         ids=["double-slash", "dot-dot"])
def test_the_real_routes_send_the_agent_the_path_that_was_checked(agent, route, path):
    """The agent is asked for the normalized path the checks approved, never the
    raw spelling the caller typed."""
    r = _route_client(shared_user()).get(f"/api/agents/{AGENT}/files/{route}", params={"path": path})
    assert r.status_code == 200, r.text
    assert r.content == b"FILE-BODY"
    assert agent.sent.await_count == 1
    assert agent.sent.await_args.args[2] == f"/api/files/{route}"
    assert agent.sent.await_args.kwargs["params"] == {"path": "/home/developer/notes.md"}


@pytest.mark.parametrize("route", ["download", "preview"])
def test_the_real_routes_refuse_a_shared_user_when_the_audit_store_fails(agent, audit, route):
    """Auditing is best-effort: with the audit store down, a refused read is
    still the structured 403, not a 500."""
    audit.side_effect = RuntimeError("audit store down")
    r = _route_client(shared_user()).get(f"/api/agents/{AGENT}/files/{route}", params={"path": ".env"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "owner_tier_path",
        "message": OWNER_TIER_MESSAGE,
        "path": "/home/developer/.env",
    }
    assert audit.await_count == 1
    assert agent.sent.await_count == 0


@pytest.mark.parametrize("route", ["download", "preview"])
def test_the_real_routes_serve_an_admin_when_the_audit_store_fails(agent, audit, route):
    """Auditing is best-effort: with the audit store down, an admin who is not
    the owner still reads the credential file."""
    audit.side_effect = RuntimeError("audit store down")
    r = _route_client(admin_jwt()).get(f"/api/agents/{AGENT}/files/{route}", params={"path": ".env"})
    assert r.status_code == 200, r.text
    assert r.content == b"FILE-BODY"
    assert audit.await_count == 1
    assert agent.sent.await_args.kwargs["params"] == {"path": "/home/developer/.env"}


@pytest.mark.parametrize("route", ["download", "preview"])
def test_the_real_routes_refuse_the_owners_agent_key_on_an_unverified_image(agent, route):
    """Only a person who is the owner or an admin skips the image check. The
    owner's agent-scoped key is below the owner tier, so on an image that is not
    verified it gets the restart answer and the agent is never called."""
    _old_image(agent)
    key = _user(OWNER, mcp_scope="agent", agent_name=AGENT)
    r = _route_client(key).get(f"/api/agents/{AGENT}/files/{route}", params={"path": "notes.md"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {"code": "agent_restart_required", "message": RESTART_MESSAGE,
                                  "path": "/home/developer/notes.md"}
    assert agent.probe.await_count == 1
    assert agent.sent.await_count == 0
