"""#4284: exception text must not reach a 5xx error body (services + routers).

The #1917 pattern, applied to the backend service layer and the routers the
first census missed: a broad ``except`` that turns a failure into a 5xx answers
with fixed words plus the exception's CLASS NAME ("... (RuntimeError — details
in backend logs)"), and the full exception goes to the backend log. Where the
text can carry a credential (a git remote URL with a PAT in its userinfo, a
``KEY=value`` echo), the log copy is scrubbed first.

Every test drives the real service function or route with a planted sentinel in
the exception and asserts three things:

* the sentinel is absent from the response (the leak is closed);
* the class name is present (the failure stays diagnosable from the response);
* the full text reached the log, or, for the credential-bearing sites, reached
  it scrubbed (moving the text into Vector logs must not leak the secret there).

Sites deliberately left are pinned at the bottom by characterization tests that
state why they are safe: the exception classes they render only ever carry
platform-authored constant text.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
import logging
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# Imported at module level, as the sibling test files of these modules do: a
# router first imported inside a test binds its `Depends(...)` targets to the
# module objects present then, and a later test's dependency override would
# not match them.
import routers.agent_data as ad
import routers.internal as internal
import routers.messages as messages
import routers.reports as reports
import routers.ws_tickets as ws
import services.agent_shared_files_service as svc
import services.system_service as system_service
from dependencies import get_current_user, get_owned_agent_by_name
from models import InternalAuditRequest, InternalTaskExecutionRequest, SendMessageRequest
from services import report_export
from services.agent_service import deploy as deploy_mod
from services.agent_service import files, queue, stats

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"

# A string no platform-authored message contains. In a response body it means
# a raw exception message reached the caller.
SENTINEL = "s13-sekrit-internal-host-10.0.0.7:5432"

# Credential payloads for the scrubbed-log sites. The PAT sits in a git remote
# URL's userinfo (learnings 2026-07-14) and the second rides a KEY=value; both
# have a shape `sanitize_text` knows. The third is a plain `user:password@` in a
# remote URL, which no shape pattern recognises: only the URL-userinfo pass
# removes it (pinned by `test_url_only_password_has_no_known_shape`), so it is
# what proves that pass is applied.
PAT = "ghp_S13sentinelPATaaaaaaaaaaaaaaaaaaaaaaaaaa"
KV_SECRET = "s13-kv-secret-value-9f8e7d"
URL_ONLY_PW = "s13UrlOnlyPw0rd"
URL_ONLY_REMOTE = f"https://deploy:{URL_ONLY_PW}@git.example.com/acme/x.git"
CREDENTIAL_TEXT = (
    f"{SENTINEL} clone of https://x-access-token:{PAT}@github.com/acme/private.git "
    f"failed; API_KEY={KV_SECRET}; mirror {URL_ONLY_REMOTE} refused"
)
# A value staged for identity scrubbing (services/runtime_secret_scrub.py). It
# has no shape either; only the staged-value mask removes it.
STAGED_VALUE = "s13StagedQwertyValue77"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blob(obj) -> str:
    return json.dumps(obj, default=str)


def _log_text(caplog) -> str:
    """Every captured record as the handler would write it, traceback included."""
    fmt = logging.Formatter("%(message)s")
    return "\n".join(fmt.format(r) for r in caplog.records)


def _assert_class_not_text(detail, cls: str = "RuntimeError") -> None:
    body = _blob(detail)
    assert SENTINEL not in body, f"raw exception text reached the response: {body}"
    assert cls in body, f"the class name must survive for diagnosis: {body}"
    assert "details in backend logs" in body, body


def _assert_logged_raw(caplog) -> None:
    """The details the response points at are really in the backend log."""
    assert SENTINEL in _log_text(caplog), "the full exception must reach the backend log"


def _assert_logged_scrubbed(caplog, *, traceback: bool = False) -> None:
    text = _log_text(caplog)
    assert "RuntimeError" in text, "the failure must still be logged"
    assert SENTINEL in text, "the non-credential details must still be logged"
    assert PAT not in text, f"the PAT reached the log: {text}"
    assert KV_SECRET not in text, f"the KEY=value secret reached the log: {text}"
    assert URL_ONLY_PW not in text, f"the URL userinfo password reached the log: {text}"
    assert "git.example.com/acme/x.git" in text, "only the userinfo is redacted, not the URL"
    if traceback:
        assert "Traceback (most recent call last)" in text, (
            f"the scrubbed traceback must reach the log: {text}"
        )


def test_url_only_password_has_no_known_shape():
    """The test values prove what they are used for: the shape pass alone keeps
    the URL password and the staged value, so a log without them shows that the
    URL-userinfo pass and the staged-value mask ran."""
    from utils.credential_sanitizer import redact_url_userinfo, sanitize_text

    assert URL_ONLY_PW in sanitize_text(CREDENTIAL_TEXT)
    assert URL_ONLY_PW not in redact_url_userinfo(CREDENTIAL_TEXT)
    staged = f"agent said {STAGED_VALUE}"
    assert STAGED_VALUE in sanitize_text(redact_url_userinfo(staged))


# ---------------------------------------------------------------------------
# services/agent_service/files.py — six file-browser proxies to the agent
# ---------------------------------------------------------------------------

FILES_CALLS = {
    "list_agent_files_logic": lambda f, u, r: f.list_agent_files_logic("a1", "/home/developer", u, r),
    "download_agent_file_logic": lambda f, u, r: f.download_agent_file_logic("a1", "notes.md", u, r),
    "delete_agent_file_logic": lambda f, u, r: f.delete_agent_file_logic("a1", "notes.md", u, r),
    "preview_agent_file_logic": lambda f, u, r: f.preview_agent_file_logic("a1", "notes.md", u, r),
    "update_agent_file_logic": lambda f, u, r: f.update_agent_file_logic("a1", "notes.md", "x", u, r),
    "create_agent_folder_logic": lambda f, u, r: f.create_agent_folder_logic("a1", "newdir", u, r),
}


@pytest.fixture
def files_env(monkeypatch):

    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(files.db, "can_user_share_agent", lambda u, a: True)
    monkeypatch.setattr(files.db, "get_agent_owner", lambda a: {"owner_username": "owner"})
    monkeypatch.setattr(files, "agent_may_reach", lambda u, a, **kw: True)
    container = SimpleNamespace(status="running", id="c-1", name="agent-a1", attrs={"Image": "sha256:i"})
    monkeypatch.setattr(files, "get_agent_container", lambda name: container)
    monkeypatch.setattr(files, "container_reload", AsyncMock())
    probe = AsyncMock(return_value={"exit_code": 0, "output": "", "timed_out": False})
    monkeypatch.setattr(files, "execute_command_in_container", probe, raising=False)
    monkeypatch.setattr(files, "_READ_POLICY_PROBE_CACHE", {}, raising=False)
    # The agent call is where the failure is planted.
    monkeypatch.setattr(files, "agent_http_request", AsyncMock(side_effect=RuntimeError(SENTINEL)))
    user = SimpleNamespace(
        id=1, username="owner", email="owner@example.com", role="user", mcp_scope=None,
        agent_name=None, connector_agent=None, mcp_key_id=None, mcp_key_name=None,
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), method="GET",
        scope={"path": "/api/agents/a1/files"}, state=SimpleNamespace(request_id="r1"),
    )
    return SimpleNamespace(files=files, user=user, request=request)


@pytest.mark.parametrize("fn", sorted(FILES_CALLS))
def test_files_proxy_500_carries_class_not_text(files_env, fn, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as exc:
        _run(FILES_CALLS[fn](files_env.files, files_env.user, files_env.request))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail)
    _assert_logged_raw(caplog)


# ---------------------------------------------------------------------------
# services/agent_service/queue.py — capacity-manager reads and resets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,method", [
    ("get_agent_queue_status_logic", "get_status"),
    ("clear_agent_queue_logic", "clear_in_memory_queue"),
    ("force_release_agent_logic", "force_release"),
])
def test_queue_500_carries_class_not_text(monkeypatch, caplog, fn, method):

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(queue, "get_agent_container", lambda name: object())
    capacity = MagicMock()
    setattr(capacity, method, AsyncMock(side_effect=RuntimeError(SENTINEL)))
    monkeypatch.setattr(queue, "get_capacity_manager", lambda: capacity)

    with pytest.raises(HTTPException) as exc:
        _run(getattr(queue, fn)("a1", MagicMock()))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail)
    _assert_logged_raw(caplog)


# ---------------------------------------------------------------------------
# services/agent_service/stats.py — live container stats
# ---------------------------------------------------------------------------

def test_stats_500_carries_class_not_text(monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(stats, "_agent_stats", {})
    monkeypatch.setattr(stats, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(stats, "container_reload", AsyncMock())
    monkeypatch.setattr(stats, "container_stats", AsyncMock(side_effect=RuntimeError(SENTINEL)))

    with pytest.raises(HTTPException) as exc:
        _run(stats.get_agent_stats_logic("a1", MagicMock()))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail)
    _assert_logged_raw(caplog)


# ---------------------------------------------------------------------------
# services/agent_service/deploy.py — deploy-local catch-all + workspace pre-pop
# ---------------------------------------------------------------------------

def _deploy_archive(name: str) -> str:

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in {
            "template.yaml": (
                f"name: {name}\ntype: business-assistant\n"
                "resources:\n  cpu: '2'\n  memory: 4g\n"
            ),
            "CLAUDE.md": "# agent\n",
        }.items():
            info = tarfile.TarInfo(f"{name}/{rel}")
            data = content.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def deploy_env(tmp_path, monkeypatch):

    monkeypatch.setattr(deploy_mod, "DEPLOYED_TEMPLATES_DIR_IN_BACKEND", str(tmp_path / "dt"))
    monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda *a: [])
    monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: None)
    monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda n: n)
    monkeypatch.setattr(deploy_mod, "get_agent_quota_for_role", lambda r: 0)
    monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: [])
    monkeypatch.setattr(deploy_mod, "collect_mcp_credential_warnings", lambda p: [])
    monkeypatch.setattr(deploy_mod, "_prepopulate_workspace_from_template", lambda v, d: None)
    monkeypatch.setattr(deploy_mod, "_compatibility_hard_count", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(deploy_mod, "_deploy_lock_client", lambda: None, raising=False)
    # Never reach the real Docker socket: the failure path removes the deploy
    # volume by name, which on a machine with Docker running is a real volume.
    monkeypatch.setattr(deploy_mod, "_cleanup_deploy_volume", lambda volume_name: None)
    return deploy_mod


def test_deploy_local_catch_all_carries_class_and_scrubs_the_log(deploy_env, caplog):
    caplog.set_level(logging.DEBUG)

    async def _create(config, *a, **kw):
        raise RuntimeError(CREDENTIAL_TEXT)

    body = SimpleNamespace(archive=_deploy_archive("s13agent"), name="s13agent",
                           credentials=None, require_manifest=False)
    user = SimpleNamespace(username="creator", email="c@example.com", role="creator", id=7)
    with pytest.raises(HTTPException) as exc:
        _run(deploy_env.deploy_local_agent_logic(body, user, SimpleNamespace(), _create))
    assert exc.value.status_code == 500
    assert exc.value.detail["code"] == "DEPLOY_FAILED"
    _assert_class_not_text(exc.value.detail)
    assert PAT not in _blob(exc.value.detail)
    _assert_logged_scrubbed(caplog, traceback=True)


def test_workspace_prepop_failure_carries_class_and_scrubs_the_log(tmp_path, monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)
    template = tmp_path / "tpl"
    template.mkdir()
    (template / "CLAUDE.md").write_text("# agent\n")
    client = MagicMock()
    client.images.get.side_effect = RuntimeError(CREDENTIAL_TEXT)
    monkeypatch.setattr(deploy_mod.docker, "from_env", lambda: client)
    monkeypatch.setattr(deploy_mod, "_ensure_fresh_workspace_volume", lambda *a: None)

    with pytest.raises(HTTPException) as exc:
        deploy_mod._prepopulate_workspace_from_template("s13agent", template)
    assert exc.value.status_code == 500
    assert exc.value.detail["code"] == "WORKSPACE_PREPOP_FAILED"
    assert "s13agent" in exc.value.detail["error"]  # the platform's own name stays
    _assert_class_not_text(exc.value.detail)
    assert PAT not in _blob(exc.value.detail)
    _assert_logged_scrubbed(caplog, traceback=True)


# ---------------------------------------------------------------------------
# services/system_service.py — deploy and apply catch-alls (git/PAT-capable)
# ---------------------------------------------------------------------------

def _admin():
    return SimpleNamespace(id=1, username="admin", role="admin", email="admin@example.com",
                           agent_name=None, connector_agent=None, mcp_scope=None)


@pytest.mark.parametrize("entry", ["deploy_manifest", "apply_manifest"])
def test_system_catch_all_carries_class_and_scrubs_the_log(monkeypatch, caplog, entry):

    caplog.set_level(logging.DEBUG)

    def _boom(_yaml):
        raise RuntimeError(CREDENTIAL_TEXT)

    monkeypatch.setattr(system_service, "parse_manifest", _boom)
    kwargs = {"dry_run": True} if entry == "apply_manifest" else {}
    with pytest.raises(HTTPException) as exc:
        _run(getattr(system_service, entry)("name: x\nagents: {}\n", _admin(), **kwargs))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail)
    assert PAT not in _blob(exc.value.detail)
    _assert_logged_scrubbed(caplog, traceback=True)


# ---------------------------------------------------------------------------
# services/agent_shared_files_service.py — single-file pull from an agent
# ---------------------------------------------------------------------------

def test_shared_file_extraction_500_carries_class_not_text(monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(svc, "get_agent_container", lambda name: object())
    monkeypatch.setattr(svc, "container_get_archive", AsyncMock(return_value=(iter([b"x" * 600]), {})))

    def _bad_open(*a, **kw):
        raise tarfile.TarError(SENTINEL)

    monkeypatch.setattr(svc.tarfile, "open", _bad_open)
    with pytest.raises(HTTPException) as exc:
        _run(svc.extract_from_agent("a1", "/home/developer/out.txt"))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail, cls="TarError")
    _assert_logged_raw(caplog)


# ---------------------------------------------------------------------------
# routers/agent_data.py — data export / import
# ---------------------------------------------------------------------------

@pytest.fixture
def data_client(tmp_path, monkeypatch):


    app = FastAPI()
    app.include_router(ad.router)
    app.dependency_overrides[get_owned_agent_by_name] = lambda: "a1"
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, username="admin", email="admin@example.com")
    monkeypatch.setattr(ad, "_TMP_DIR", str(tmp_path / "agent-data-tmp"))
    monkeypatch.setattr(ad.platform_audit_service, "log", AsyncMock(return_value=None))
    monkeypatch.setattr(ad.git_service, "_data_paths_for", AsyncMock(return_value=[]))
    monkeypatch.setattr("routers.auth.get_redis_client", lambda: None)
    monkeypatch.setattr(ad, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    return SimpleNamespace(client=TestClient(app), ad=ad)


def test_data_export_500_carries_class_not_text(data_client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(data_client.ad, "container_get_archive",
                        AsyncMock(side_effect=RuntimeError(SENTINEL)))
    resp = data_client.client.post("/api/agents/a1/data/export?format=base64")
    assert resp.status_code == 500
    _assert_class_not_text(resp.json())
    _assert_logged_raw(caplog)


def test_data_import_502_carries_class_not_text(data_client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(data_client.ad, "agent_http_request",
                        AsyncMock(side_effect=RuntimeError(SENTINEL)))
    resp = data_client.client.post(
        "/api/agents/a1/data/import",
        files={"tarball": ("data.tar", b"\0" * 1024, "application/x-tar")},
    )
    assert resp.status_code == 502, resp.text
    _assert_class_not_text(resp.json())
    _assert_logged_raw(caplog)


# ---------------------------------------------------------------------------
# routers/internal.py — scheduler execute-task (sync) and MCP audit ingest
# ---------------------------------------------------------------------------

def _execute_task_failing_with(monkeypatch, text: str) -> MagicMock:
    """Route the sync execute-task branch into a failure carrying ``text``;
    returns the idempotency ``fail`` spy."""

    class _Svc:
        async def execute_task(self, **kw):
            raise RuntimeError(text)

    # The route imports the gate at call time, so patch the module sys.modules holds.
    monkeypatch.setattr("services.cleanup_service.is_startup_recovery_complete", lambda: True)
    monkeypatch.setattr(internal, "get_task_execution_service", lambda: _Svc())
    monkeypatch.setattr(internal.idempotency_service, "begin", lambda *a, **k: SimpleNamespace(
        replay=False, execution_id=None, in_flight=False, snapshot=None))
    monkeypatch.setattr(internal.idempotency_service, "attach_execution", lambda *a, **k: None)
    monkeypatch.setattr(internal.idempotency_service, "complete", lambda *a, **k: None)
    failed = MagicMock()
    monkeypatch.setattr(internal.idempotency_service, "fail", failed)
    monkeypatch.setattr(internal.platform_audit_service, "log", AsyncMock())
    # Hermetic: never read a live staged store; tests that need staged values set their own.
    monkeypatch.setattr(internal, "get_staged_values", lambda: [])
    return failed


def _call_execute_task():
    req = InternalTaskExecutionRequest(agent_name="analyst", message="go", triggered_by="manual",
                                       execution_id="e1")
    with pytest.raises(HTTPException) as exc:
        _run(internal.execute_task_internal(req, idempotency_key=None))
    return exc.value


def test_internal_execute_task_500_carries_class_and_scrubs_the_log(monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)
    failed = _execute_task_failing_with(monkeypatch, CREDENTIAL_TEXT)
    exc = _call_execute_task()
    assert exc.status_code == 500
    _assert_class_not_text(exc.detail)
    assert failed.called, "the idempotency claim must still be released"
    _assert_logged_scrubbed(caplog)


def test_internal_execute_task_log_masks_staged_values(monkeypatch, caplog):
    """An execution error can echo agent output, and agent output can carry a
    value the platform staged for scrubbing (one with no credential shape). The
    log copy masks it the way the persisted copies of that output are masked."""

    caplog.set_level(logging.DEBUG)
    failed = _execute_task_failing_with(monkeypatch, f"{SENTINEL} agent echoed {STAGED_VALUE}")
    monkeypatch.setattr(internal, "get_staged_values", lambda: [STAGED_VALUE])
    exc = _call_execute_task()
    assert exc.status_code == 500
    assert failed.called, "the idempotency claim must still be released"
    text = _log_text(caplog)
    assert SENTINEL in text, "the rest of the error must still be logged"
    assert STAGED_VALUE not in text, f"a staged value reached the log: {text}"


STAGED_URL_PATH = "s13-tenant-path-7f3a9c"
STAGED_URL_VALUE = f"https://svc:s13StagedPw0rd@stage.example.com/{STAGED_URL_PATH}"


def test_internal_execute_task_masks_a_staged_value_before_the_url_pass(monkeypatch, caplog):
    """The staged-value scrub runs before the URL-userinfo pass. Run after it,
    a staged connection string has already been rewritten, its identity match
    misses, and everything after the userinfo (host and path) reaches the log."""

    caplog.set_level(logging.DEBUG)
    _execute_task_failing_with(monkeypatch, f"{SENTINEL} agent echoed {STAGED_URL_VALUE}")
    monkeypatch.setattr(internal, "get_staged_values", lambda: [STAGED_URL_VALUE])
    exc = _call_execute_task()
    assert exc.status_code == 500
    text = _log_text(caplog)
    assert SENTINEL in text, "the rest of the error must still be logged"
    assert "s13StagedPw0rd" not in text, f"the staged URL's password reached the log: {text}"
    assert STAGED_URL_PATH not in text, f"the staged URL's path reached the log: {text}"


STAGED_SHAPED_PREFIX = "s13StagedPrefix7c1d-"
STAGED_SHAPED_VALUE = STAGED_SHAPED_PREFIX + "ghp_" + "S13shapedStagedTokenbbbbbbbbbbbbbbbbbbb"[:36]


def test_internal_execute_task_masks_a_staged_value_before_the_shape_pass(monkeypatch, caplog):
    """The staged-value scrub also runs before the shape pass. Run after it, a
    staged value that embeds a recognisable token has already had that token
    rewritten, its identity match misses, and the rest of the value is logged."""

    caplog.set_level(logging.DEBUG)
    _execute_task_failing_with(monkeypatch, f"{SENTINEL} agent echoed {STAGED_SHAPED_VALUE}")
    monkeypatch.setattr(internal, "get_staged_values", lambda: [STAGED_SHAPED_VALUE])
    exc = _call_execute_task()
    assert exc.status_code == 500
    text = _log_text(caplog)
    assert SENTINEL in text, "the rest of the error must still be logged"
    assert STAGED_SHAPED_PREFIX not in text, f"the staged value's unshaped part reached the log: {text}"


def test_internal_execute_task_releases_the_claim_when_the_staged_read_fails(monkeypatch):
    """The claim release does not depend on the log scrub: a staged-set read
    that raises still leaves the claim released, so a resend is not refused as
    in flight for the claim's lifetime."""

    failed = _execute_task_failing_with(monkeypatch, SENTINEL)

    def _boom():
        raise RuntimeError("staged store unreadable")

    monkeypatch.setattr(internal, "get_staged_values", _boom)
    req = InternalTaskExecutionRequest(agent_name="analyst", message="go", triggered_by="manual",
                                       execution_id="e1")
    with pytest.raises(Exception):
        _run(internal.execute_task_internal(req, idempotency_key=None))
    assert failed.called, "the idempotency claim must be released before the log scrub"


def test_internal_audit_500_carries_class_and_scrubs_the_log(monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(internal.platform_audit_service, "log",
                        AsyncMock(side_effect=RuntimeError(CREDENTIAL_TEXT)))
    req = InternalAuditRequest(event_type="mcp_operation", event_action="tool_call")
    with pytest.raises(HTTPException) as exc:
        _run(internal.log_audit_entry(req))
    assert exc.value.status_code == 500
    _assert_class_not_text(exc.value.detail)
    _assert_logged_scrubbed(caplog)


# ---------------------------------------------------------------------------
# routers/ws_tickets.py — WebSocket ticket mint
# ---------------------------------------------------------------------------

def test_ws_ticket_503_carries_fixed_text_not_the_exception(monkeypatch, caplog):

    caplog.set_level(logging.DEBUG)

    def _boom(*a, **kw):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ws, "mint_ticket", _boom)
    with pytest.raises(HTTPException) as exc:
        _run(ws.create_ws_ticket(current_user=SimpleNamespace(username="u1")))
    assert exc.value.status_code == 503
    assert SENTINEL not in _blob(exc.value.detail)
    # The text a client sees when the store is down is unchanged.
    assert exc.value.detail == "Redis unavailable — cannot mint WebSocket ticket"
    # The log names the class only, as the ticket service does: a store error's
    # text can carry the store URL, and nothing here scrubs it.
    text = _log_text(caplog)
    assert "RuntimeError" in text, "the failure must still be logged"
    assert SENTINEL not in text, f"the exception text reached the log: {text}"


# ---------------------------------------------------------------------------
# Deliberately left — characterization (green before and after the change)
# ---------------------------------------------------------------------------

def _constructions(class_name: str):
    """Every `ClassName(...)` call in the OSS backend tree, with its args."""
    out = []
    for path in BACKEND.rglob("*.py"):
        if "enterprise" in path.relative_to(BACKEND).parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name == class_name:
                    out.append((path.relative_to(REPO), node))
    return out


@pytest.mark.parametrize("class_name,expected_min", [
    # routers/messages.py renders str(ChannelDeliveryError) into its 502.
    ("ChannelDeliveryError", 2),
    # routers/reports.py renders str(ExportUnavailable) into its 503.
    ("ExportUnavailable", 2),
    # routers/ws_tickets.py used to render str(TicketStoreUnavailable).
    ("TicketStoreUnavailable", 2),
])
def test_rendered_domain_errors_only_carry_constant_text(class_name, expected_min):
    """These narrow handlers stay as they are because the exception they render
    is only ever raised with a platform-authored constant. If someone raises one
    with foreign text, this fails and the handler must move to the class-name
    pattern."""
    sites = _constructions(class_name)
    assert len(sites) >= expected_min, f"expected raise sites for {class_name}: {sites}"
    for path, call in sites:
        assert not call.keywords, f"{path}:{call.lineno}"
        assert len(call.args) == 1, f"{path}:{call.lineno}"
        arg = call.args[0]
        if (path, ast.unparse(arg)) in _PLATFORM_PARAM_TEXT:
            continue
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"{path}:{call.lineno}: {class_name} built from non-constant text: {ast.unparse(call)}"
        )


# A construction whose only interpolation is a parameter that every caller
# passes as a string literal. Each entry is (file, the exact argument text),
# and test_platform_param_text_is_only_given_literals checks the callers.
_PLATFORM_PARAM_TEXT = {
    (
        Path("src/backend/services/proactive_message_service.py"),
        "f\"recipient_blocked: this client is blocked on the agent's {channel} channel\"",
    ),
}
_PLATFORM_PARAM_CALLERS = {
    # file -> (function whose parameter is interpolated, that parameter's position)
    Path("src/backend/services/proactive_message_service.py"): ("_refuse_blocked", 1),
}


def test_platform_param_text_is_only_given_literals():
    """The channel name in `recipient_blocked` comes from the platform: every
    call to `_refuse_blocked` passes it as a string literal. If a caller passes
    anything else, the exemption above no longer holds."""
    for rel, (fn_name, pos) in _PLATFORM_PARAM_CALLERS.items():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name]
        assert len(defs) == 1, f"{rel}: expected one {fn_name}"
        assert defs[0].args.args[pos].arg == "channel", f"{rel}: {fn_name} parameter {pos} renamed"
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn_name
        ]
        assert calls, f"{rel}: no calls to {fn_name}"
        for call in calls:
            arg = call.args[pos]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"{rel}:{call.lineno}: {fn_name} given a non-literal channel: {ast.unparse(arg)}"
            )
    for rel, arg_text in _PLATFORM_PARAM_TEXT:
        assert arg_text in {ast.unparse(c.args[0]) for p, c in _constructions("ChannelDeliveryError") if p == rel}, (
            f"stale exemption: {rel} no longer builds {arg_text}"
        )


def test_messages_catch_all_is_already_fixed_text(monkeypatch):
    """routers/messages.py: the broad handler already answers with fixed words."""

    monkeypatch.setattr(messages, "assert_tool_allowed", lambda *a, **k: None)
    monkeypatch.setattr(messages, "_recipient", lambda *a, **k: "p@example.com")
    monkeypatch.setattr(messages.proactive_message_service, "send_message",
                        AsyncMock(side_effect=RuntimeError(SENTINEL)))
    caller = SimpleNamespace(agent_name=None, username="owner")
    with pytest.raises(HTTPException) as exc:
        _run(messages.send_proactive_message(SendMessageRequest(text="hi", recipient_email="p@example.com"), "a1", current_user=caller))
    assert exc.value.status_code == 500
    assert SENTINEL not in _blob(exc.value.detail)


def test_report_export_503_names_the_missing_library_not_the_cause(monkeypatch):
    """routers/reports.py: the 503 names the library (authored text) and never
    the underlying ImportError, which rides `__cause__` only."""

    monkeypatch.setattr(reports, "_report_or_404", lambda rid, user: {"title": "t", "payload": {}})

    def _missing(*a, **kw):
        raise report_export.ExportUnavailable("openpyxl is not installed") from ImportError(SENTINEL)

    monkeypatch.setattr(report_export, "build_xlsx", _missing)
    with pytest.raises(HTTPException) as exc:
        _run(reports.export_report("r1", format="xlsx", current_user=MagicMock()))
    assert exc.value.status_code == 503
    assert SENTINEL not in _blob(exc.value.detail)
    assert "openpyxl is not installed" in exc.value.detail
