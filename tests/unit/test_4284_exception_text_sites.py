"""abilityai/trinity#4284: the 5xx sites the static guard found that still
rendered exception text or relayed an upstream body.

Each site is driven through its real function with a planted exception (or an
upstream answer) whose text carries a harmless marker and a credential: a
GitHub token in a remote URL's userinfo and in a ``key=value`` pair. The
answer must keep its status and prefix, name the exception class (or the
upstream status) and carry neither the marker nor the credential. Where the
site logs, the log must carry the marker, so the operator keeps the
diagnostic, and never the credential.

``test_4284_no_exception_text_in_5xx.py`` keeps new sites out; this file
proves the sites it found are fixed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"

# Built by concatenation so no source line, and so no traceback frame, holds it.
_TOKEN = "ghp_" + "PLANTEDTOKEN" + "a" * 24
_MARKER = "boom-marker-4284"
_PLANT = (
    f"{_MARKER} https://x-access-token:{_TOKEN}@github.com/acme/repo.git "
    f"token={_TOKEN}"
)


def _rendered(record: logging.LogRecord) -> str:
    return logging.Formatter("%(message)s").format(record)


def _assert_answer_clean(answer, expected: str) -> None:
    text = str(answer)
    assert _TOKEN not in text, f"credential reached the answer: {text!r}"
    assert _MARKER not in text, f"exception text reached the answer: {text!r}"
    assert expected in text, f"{expected!r} missing from the answer: {text!r}"


def _assert_log_keeps_text(records, needle: str) -> None:
    rendered = [_rendered(r) for r in records]
    for line in rendered:
        assert _TOKEN not in line, f"credential reached the log: {line!r}"
    hits = [line for line in rendered if needle in line and _MARKER in line]
    assert hits, f"no {needle!r} record carries the exception text: {rendered!r}"


# ---------------------------------------------------------------------------
# agent server: docker/base-image/agent_server/routers/git.py
# ---------------------------------------------------------------------------

# The router's imports are relative, so it is imported through the
# `agent_server` package, the way the agent server loads it (the test_2742
# loader and its snapshot/restore pair).
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _as_spec = importlib.util.spec_from_file_location(
        "agent_server", str(_BASE_IMAGE / "agent_server" / "__init__.py"),
        submodule_search_locations=[_real_path],
    )
    _as_mod = importlib.util.module_from_spec(_as_spec)
    sys.modules["agent_server"] = _as_mod
    _as_spec.loader.exec_module(_as_mod)

from agent_server.routers import git as agent_git_mod  # noqa: E402
from agent_server.services import process_registry as agent_registry  # noqa: E402

_STUBBED_MODULE_NAMES = [
    name for name in sys.modules
    if name == "agent_server" or name.startswith("agent_server.")
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def agent_git(tmp_path, monkeypatch):
    git_mod = agent_git_mod
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    real_path = Path
    monkeypatch.setattr(
        git_mod, "Path",
        lambda *a: real_path(home) if a == ("/home/developer",) else real_path(*a))

    def boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(git_mod, "subprocess", types.SimpleNamespace(
        run=boom, TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError, PIPE=subprocess.PIPE))
    monkeypatch.setattr(git_mod, "run_registered", boom)
    return git_mod, home


def _agent_git_call(git_mod, home, route: str):
    if route == "status":
        return lambda: git_mod._compute_git_status(home)
    if route == "sync":
        return lambda: asyncio.run(git_mod.sync_to_github(git_mod.GitSyncRequest()))
    if route == "log":
        return lambda: asyncio.run(git_mod.get_git_log(limit=5))
    return lambda: asyncio.run(git_mod.pull_from_github(git_mod.GitPullRequest()))


@pytest.mark.parametrize("route", ["status", "sync", "log", "pull"])
def test_agent_git_500_names_the_class_not_the_text(agent_git, caplog, route):
    git_mod, home = agent_git
    caplog.set_level(logging.ERROR)
    with pytest.raises(HTTPException) as exc:
        _agent_git_call(git_mod, home, route)()
    assert exc.value.status_code == 500
    _assert_answer_clean(exc.value.detail, f"Git {route} error: RuntimeError")
    assert isinstance(exc.value.__cause__, RuntimeError)
    _assert_log_keeps_text(caplog.records, f"Git {route} error")


# ---------------------------------------------------------------------------
# agent server: services/process_registry.py feeds the terminate route's 500
# ---------------------------------------------------------------------------

def test_registry_terminate_error_names_the_class_not_the_text(monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(agent_registry, "_signal_process_tree", boom)
    reg = agent_registry.ProcessRegistry()
    proc = SimpleNamespace(poll=lambda: None, pid=4242, returncode=None)
    reg._processes["exec-1"] = {"process": proc, "metadata": {"pgid": 4242}}
    caplog.set_level(logging.ERROR)
    result = reg.terminate("exec-1")
    assert result["success"] is False and result["reason"] == "error"
    _assert_answer_clean(result["error"], "Termination failed: RuntimeError")
    _assert_log_keeps_text(caplog.records, "exec-1")


# ---------------------------------------------------------------------------
# GitHub errors at 502: services/agent_service/crud.py and fork_to_own.py
#
# A GitHubError raised for a network failure embeds the httpx error text, and
# for a header-unsafe token httpx echoes the Authorization value there.
# ---------------------------------------------------------------------------

def _github_error(status=None):
    from services.github_service import GitHubError
    return GitHubError(f"Network error: {_PLANT}", status=status)


@pytest.fixture
def crud_gh(monkeypatch):
    import services.agent_service.crud as crud
    state = {"status": None, "probe": ("unavailable", "")}

    class _GH:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            raise _github_error(state["status"])

    async def probe(*_a, **_kw):
        return state["probe"]

    async def push(*_a, **_kw):
        return None

    monkeypatch.setattr(crud, "GitHubService", _GH)
    monkeypatch.setattr(crud.git_service, "probe_tokened_repo_access", probe)
    monkeypatch.setattr(crud, "_validate_push_access", push)

    def validate():
        config = types.SimpleNamespace(source_branch="dev")
        return asyncio.run(crud._validate_github_access(config, "acme/tpl", _TOKEN))

    return state, validate


@pytest.mark.parametrize("status, expected", [
    (None, "Failed to validate GitHub repository access (GitHubError"),
    (403, "Failed to validate GitHub repository access (GitHubError, HTTP 403"),
], ids=["network-error", "rest-403-and-git-cannot-read"])
def test_crud_github_502_names_the_class_not_the_text(crud_gh, caplog, status, expected):
    state, validate = crud_gh
    state["status"] = status
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        validate()
    assert exc.value.status_code == 502
    _assert_answer_clean(exc.value.detail, expected)
    _assert_log_keeps_text(caplog.records, "GitHub repository access")


class _ForkGH:
    def __init__(self, pat, *, fail_on):
        self.pat = pat
        self.fail_on = fail_on

    async def check_repo_exists(self, owner, name):
        if self.fail_on == "check":
            raise _github_error()
        return types.SimpleNamespace(exists=True, default_branch="main")

    async def get_branch_sha(self, repo, branch):
        raise _github_error(503)

    async def list_branches(self, repo, limit=10):
        raise _github_error(503)


@pytest.mark.parametrize("fail_on, code, expected", [
    ("check", "FORK_TEMPLATE_UNREACHABLE", "Could not read template 'acme/tpl' (GitHubError"),
    ("branch", "FORK_TEMPLATE_UNREACHABLE", "Could not read template branch (GitHubError, HTTP 503"),
])
def test_fork_template_502_names_the_class_not_the_text(monkeypatch, caplog, fail_on, code, expected):
    import services.agent_service.fork_to_own as fork
    monkeypatch.setattr(fork, "GitHubService", lambda pat: _ForkGH(pat, fail_on=fail_on))
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fork._resolve_template_tip("acme/tpl", _TOKEN))
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == code
    _assert_answer_clean(exc.value.detail["error"], expected)
    _assert_log_keeps_text(caplog.records, "fork-to-own")


@pytest.mark.parametrize("fail_on, expected", [
    ("check", "Could not check 'me/dest' (GitHubError"),
    ("branches", "Could not inspect 'me/dest' (GitHubError, HTTP 503"),
])
def test_fork_destination_502_names_the_class_not_the_text(caplog, fail_on, expected):
    import services.agent_service.fork_to_own as fork
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fork.inspect_or_create_destination_repo(
            _ForkGH(_TOKEN, fail_on=fail_on), "me/dest", "me",
            private=True, description="d", user_pat=_TOKEN))
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_DESTINATION_UNREACHABLE"
    _assert_answer_clean(exc.value.detail["error"], expected)
    _assert_log_keeps_text(caplog.records, "fork-to-own")


# ---------------------------------------------------------------------------
# services/agent_service/repo_binding.py: the destination pre-check
# (the recreate failure is pinned in test_ent109_repo_binding.py)
# ---------------------------------------------------------------------------

def test_repo_binding_destination_check_502_names_the_class(monkeypatch, caplog):
    import services.agent_service.repo_binding as rb

    def boom(_repo):
        raise RuntimeError(_PLANT)

    monkeypatch.setattr(rb.db, "get_git_config_agent_names_for_repo", boom)
    caplog.set_level(logging.WARNING)
    with pytest.raises(rb.BindError) as exc:
        rb._refuse_if_destination_bound_elsewhere("agent-a", "me/dest")
    assert exc.value.status_code == 502
    assert exc.value.code == "BIND_DESTINATION_UNREACHABLE"
    _assert_answer_clean(exc.value.message, "(RuntimeError — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "me/dest")


# ---------------------------------------------------------------------------
# Relays of an agent's own answer: an agent on an older base image still
# answers its 500s with its exception text, and the backend's relay is the only
# thing between that text and the caller. At an agent 5xx the backend answers
# a fixed sentence with the agent's status; a 4xx is the agent's refusal and is
# still relayed word for word.
# ---------------------------------------------------------------------------



def _agent_answer(status: int) -> httpx.Response:
    return httpx.Response(status, json={"detail": f"Failed: {_PLANT}"})


_FILES_CALLS = {
    "list": ("list_agent_files_logic", "Failed to list files",
             lambda f, u, r: f.list_agent_files_logic("a1", "/home/developer", u, r)),
    "download": ("download_agent_file_logic", "Failed to download file",
                 lambda f, u, r: f.download_agent_file_logic("a1", "notes.md", u, r)),
    "delete": ("delete_agent_file_logic", "Failed to delete",
               lambda f, u, r: f.delete_agent_file_logic("a1", "notes.md", u, r)),
    "preview": ("preview_agent_file_logic", "Failed to preview",
                lambda f, u, r: f.preview_agent_file_logic("a1", "notes.md", u, r)),
    "update": ("update_agent_file_logic", "Failed to update",
               lambda f, u, r: f.update_agent_file_logic("a1", "notes.md", "x", u, r)),
    "mkdir": ("create_agent_folder_logic", "Failed to create folder",
              lambda f, u, r: f.create_agent_folder_logic("a1", "newdir", u, r)),
}


@pytest.fixture
def files_relay(monkeypatch):
    import services.agent_service.files as files
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(files.db, "can_user_share_agent", lambda u, a: True)
    monkeypatch.setattr(files.db, "get_agent_owner", lambda a: {"owner_username": "owner"})
    monkeypatch.setattr(files, "agent_may_reach", lambda u, a, **kw: True)
    container = SimpleNamespace(status="running", id="c-1", name="agent-a1",
                                attrs={"Image": "sha256:i"})
    monkeypatch.setattr(files, "get_agent_container", lambda name: container)
    monkeypatch.setattr(files, "container_reload", AsyncMock())
    probe = AsyncMock(return_value={"exit_code": 0, "output": "", "timed_out": False})
    monkeypatch.setattr(files, "execute_command_in_container", probe, raising=False)
    monkeypatch.setattr(files, "_READ_POLICY_PROBE_CACHE", {}, raising=False)
    answer = {"response": None}
    monkeypatch.setattr(files, "agent_http_request",
                        AsyncMock(side_effect=lambda *a, **kw: answer["response"]))
    user = SimpleNamespace(
        id=1, username="owner", email="owner@example.com", role="user", mcp_scope=None,
        agent_name=None, connector_agent=None, mcp_key_id=None, mcp_key_name=None,
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), method="GET",
        scope={"path": "/api/agents/a1/files"}, state=SimpleNamespace(request_id="r1"),
    )
    return SimpleNamespace(files=files, user=user, request=request, answer=answer)


@pytest.mark.parametrize("route", sorted(_FILES_CALLS))
@pytest.mark.parametrize("status", [500, 503])
def test_files_relay_answers_a_fixed_sentence_at_an_agent_5xx(files_relay, caplog, route, status):
    _fn, prefix, call = _FILES_CALLS[route]
    files_relay.answer["response"] = _agent_answer(status)
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call(files_relay.files, files_relay.user, files_relay.request))
    assert exc.value.status_code == status
    _assert_answer_clean(
        exc.value.detail, f"{prefix} (the agent answered {status} — details in backend logs)")
    _assert_log_keeps_text(caplog.records, prefix)


@pytest.mark.parametrize("route", sorted(_FILES_CALLS))
def test_files_relay_still_relays_an_agent_4xx(files_relay, route):
    """Characterization: an agent's refusal (here a 404) is still the answer."""
    _fn, _prefix, call = _FILES_CALLS[route]
    files_relay.answer["response"] = httpx.Response(404, json={"detail": "File not found: notes.md"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call(files_relay.files, files_relay.user, files_relay.request))
    assert exc.value.status_code == 404
    assert "File not found: notes.md" in str(exc.value.detail)


def test_data_import_restore_failure_names_the_agent_status(tmp_path, monkeypatch, caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.agent_data as ad

    app = FastAPI()
    app.include_router(ad.router)
    # Override the callables the route itself bound, not a fresh import of
    # `dependencies`: another test file may have re-imported that module.
    route = next(r for r in ad.router.routes if getattr(r, "path", "").endswith("/data/import"))
    for dep in route.dependant.dependencies:
        if dep.name == "agent_name":
            app.dependency_overrides[dep.call] = lambda: "a1"
        elif dep.name == "current_user":
            app.dependency_overrides[dep.call] = lambda: SimpleNamespace(
                id=1, username="admin", email="admin@example.com")
    monkeypatch.setattr(ad, "_TMP_DIR", str(tmp_path / "agent-data-tmp"))
    monkeypatch.setattr(ad.platform_audit_service, "log", AsyncMock(return_value=None))
    monkeypatch.setattr(ad.git_service, "_data_paths_for", AsyncMock(return_value=[]))
    monkeypatch.setattr("routers.auth.get_redis_client", lambda: None)
    monkeypatch.setattr(ad, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(ad, "agent_http_request", AsyncMock(return_value=_agent_answer(500)))
    caplog.set_level(logging.WARNING)
    resp = TestClient(app).post(
        "/api/agents/a1/data/import",
        files={"tarball": ("data.tar", b"\0" * 1024, "application/x-tar")},
    )
    assert resp.status_code == 502, resp.text
    _assert_answer_clean(
        resp.json(), "Agent restore failed (the agent answered 500 — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "restore")


@pytest.fixture
def inject(monkeypatch):
    import routers.credentials as cr
    from models import CredentialInjectRequest

    answer = {"response": None}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return answer["response"]

    monkeypatch.setattr(cr, "agent_httpx_client", lambda name, **kw: _Client())
    monkeypatch.setattr(cr, "get_agent_container", lambda name: object())
    monkeypatch.setattr(cr, "get_agent_status_from_container",
                        lambda c: SimpleNamespace(status="running"))
    monkeypatch.setattr(cr.platform_audit_service, "log", AsyncMock(return_value=None))
    user = SimpleNamespace(
        id=1, username="owner", email="owner@example.com", role="user", mcp_scope=None,
        agent_name=None, connector_agent=None, mcp_key_id=None, mcp_key_name=None,
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                              scope={"path": "/api/agents/a1/credentials/inject"},
                              state=SimpleNamespace(request_id="r1"))

    def call():
        body = CredentialInjectRequest(files={".env": "A=1\n"})
        return asyncio.run(cr.inject_credentials(body, request, "a1", user))

    return answer, call


def test_credential_inject_answers_a_fixed_sentence_at_an_agent_5xx(inject, caplog):
    answer, call = inject
    answer["response"] = _agent_answer(500)
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 500
    _assert_answer_clean(
        exc.value.detail,
        "Agent rejected credential injection (the agent answered 500 — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "Credential injection")


def test_credential_inject_still_relays_an_agent_4xx(inject):
    """Characterization: the agent's own refusal reaches the person."""
    answer, call = inject
    answer["response"] = httpx.Response(400, json={"detail": "Path escapes the agent home"})
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 400
    assert exc.value.detail == "Agent rejected credential injection: Path escapes the agent home"


# ---------------------------------------------------------------------------
# services/system_service.py::_failure_reason: the deploy and apply reports
# are the body of a 500 when every member fails (routers/systems.py)
# ---------------------------------------------------------------------------

def test_failure_reason_names_an_unforeseen_exception_by_class(caplog):
    from services.system_service import _failure_reason
    caplog.set_level(logging.WARNING)
    reason, status = _failure_reason(RuntimeError(_PLANT))
    assert status is None
    _assert_answer_clean(reason, "RuntimeError — details in backend logs")
    _assert_log_keeps_text(caplog.records, "System manifest step failed")


@pytest.mark.parametrize("build, expected", [
    (lambda: ValueError("Invalid cpu '3': must be one of 1, 2, 4, 8, 16"), "Invalid cpu '3'"),
    (lambda: HTTPException(404, detail={"error": "Template not found: local:x"}),
     "Template not found: local:x"),
], ids=["validation-error", "http-exception"])
def test_failure_reason_keeps_platform_words(build, expected):
    """Characterization: a validation message and a platform detail still reach
    the manifest's author."""
    from services.system_service import _failure_reason
    reason, _ = _failure_reason(build())
    assert expected in reason


@pytest.mark.parametrize("module", ["gates", "grants", "seats"])
def test_failure_reason_keeps_an_apply_refusal(module):
    """The `<code>: <message>` an apply step refuses with is what the report's
    readers split on."""
    import importlib
    from services.system_service import _failure_reason
    refused = importlib.import_module(f"services.system_apply.{module}")._Refused
    reason, _ = _failure_reason(refused("calibrating_agent: the companion is calibrating"))
    assert reason.split(":")[0] == "calibrating_agent"


# ---------------------------------------------------------------------------
# A2A outbound and AAuth egress: a transport failure at 502
# ---------------------------------------------------------------------------

def _refusing_transport():
    def handler(request):
        raise httpx.ConnectError(f"{_PLANT} [Errno 111] Connection refused to 203.0.113.7:5432")
    return httpx.MockTransport(handler)


def test_a2a_unreachable_502_names_the_class_not_the_text(caplog):
    from services import a2a_client
    from routers.a2a import _map_call_error

    async def run():
        async with httpx.AsyncClient(transport=_refusing_transport()) as client:
            await a2a_client._read_capped(
                client, "POST", "https://203.0.113.5/rpc", sni="peer.example.com",
                host_header="peer.example.com", max_bytes=1000, headers={},
                error_prefix="call", secret=_TOKEN)

    caplog.set_level(logging.WARNING)
    with pytest.raises(a2a_client.A2ACallError) as exc:
        asyncio.run(run())
    answer = _map_call_error(exc.value)
    assert answer.status_code == 502
    assert answer.detail["reason"] == "call_unreachable"
    _assert_answer_clean(
        answer.detail, "The A2A endpoint could not be reached (ConnectError — details in backend logs).")
    _assert_log_keeps_text(caplog.records, "A2A endpoint could not be reached")


def test_aauth_egress_unreachable_names_the_class_not_the_text(monkeypatch, caplog):
    from services import a2a_client
    from services.aauth import egress

    async def validated(url):
        return SimpleNamespace(url=url, hostname="ps.example.com", addresses=["203.0.113.8"])

    monkeypatch.setattr(a2a_client, "validate_endpoint", validated)

    async def run():
        async with httpx.AsyncClient(transport=_refusing_transport()) as client:
            await egress.request_capped(client, "POST", "https://ps.example.com/token",
                                        max_bytes=1000, headers={}, secret=_TOKEN)

    caplog.set_level(logging.WARNING)
    with pytest.raises(egress.EgressError) as exc:
        asyncio.run(run())
    assert exc.value.code == "unreachable"
    _assert_answer_clean(
        exc.value.detail,
        "The person server could not be reached (ConnectError — details in backend logs).")
    _assert_log_keeps_text(caplog.records, "could not be reached")


# ---------------------------------------------------------------------------
# services/chat_execution_service.py: an agent's terminate 5xx
# ---------------------------------------------------------------------------

def _terminate_against(monkeypatch, status: int, detail: str):
    """The real proxy, against an agent that answers ``status`` / ``detail``."""
    import services.chat_execution_service as ces

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return httpx.Response(status, json={"detail": detail})

    monkeypatch.setattr(ces, "agent_httpx_client", lambda name, **kw: _Client())
    with pytest.raises(ces.ChatDispatchError) as exc:
        asyncio.run(ces._proxy_terminate_and_finalize("a1", "e-1", None, None))
    return exc.value


@pytest.mark.parametrize("status", [500, 503])
def test_terminate_answers_a_fixed_sentence_for_an_older_agents_5xx(monkeypatch, caplog, status):
    """An agent from before the agent-server fix answered the registry's raw
    exception text as its terminate 500."""
    caplog.set_level(logging.WARNING)
    err = _terminate_against(monkeypatch, status, f"Failed: {_PLANT}")
    assert err.status_code == status
    _assert_answer_clean(
        err.detail, f"Termination failed (the agent answered {status} — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "[Terminate]")


@pytest.mark.parametrize("status, detail", [
    (500, "termination_unconfirmed: execution e-1 did not exit after SIGINT and SIGKILL; "
          "it remains registered and termination can be retried"),
    (500, "Termination failed: PermissionError"),
    (500, "Termination failed"),
    (409, "Execution already finishing"),
], ids=["unconfirmed", "registry-class", "registry-default", "agent-4xx"])
def test_terminate_keeps_the_agents_own_sentences(monkeypatch, status, detail):
    """Characterization: the agent's own 5xx sentences and any 4xx go on."""
    err = _terminate_against(monkeypatch, status, detail)
    assert (err.status_code, err.detail) == (status, detail)


# ---------------------------------------------------------------------------
# services/task_execution_service.py: the execution's error, which the sync
# /task route answers at 503/504 and the session route at 502
# ---------------------------------------------------------------------------

def _execute_with(agent_error: BaseException, timeout_seconds: int = 3600):
    from unittest.mock import MagicMock, patch
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_execution_timeout.return_value = timeout_seconds
    mock_db.create_task_execution.return_value = MagicMock(id="exec-4284")
    mock_capacity = MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted")),
                              release=AsyncMock())
    mock_activity = MagicMock(track_activity=AsyncMock(return_value="act-4284"))

    async def boom(*args, **kwargs):
        raise agent_error

    p = "services.task_execution_service."
    with (
        patch(p + "db", mock_db),
        patch(p + "get_capacity_manager", return_value=mock_capacity),
        patch(p + "activity_service", mock_activity),
        patch(p + "dispatch_breaker_active", return_value=False),
        patch(p + "dispatch_async_eligible", return_value=False),
        patch(p + "note_unreachable_pull_trigger"),
        patch(p + "settings_service.get_platform_default_model", return_value="sonnet"),
        patch(p + "CircuitState", return_value=MagicMock(allow_request=lambda: True)),
        patch(p + "_resolve_agent_runtime", return_value="claude"),
        patch(p + "compose_system_prompt", return_value="prompt"),
        patch(p + "is_execution_context_enabled", return_value=False),
        patch(p + "agent_post_with_retry", side_effect=boom),
        patch(p + "terminate_execution_on_agent", new=AsyncMock()),
        patch(p + "_write_terminal_and_gate", new=AsyncMock()),
    ):
        return asyncio.run(TaskExecutionService().execute_task(
            agent_name="test-agent", message="do the thing", triggered_by="schedule",
            timeout_seconds=timeout_seconds))


def test_execute_task_unexpected_error_names_the_class(caplog):
    caplog.set_level(logging.ERROR)
    result = _execute_with(RuntimeError(_PLANT))
    assert result.status == "failed"
    _assert_answer_clean(result.error, "Task execution failed (RuntimeError — details in backend logs)")
    _assert_log_keeps_text(caplog.records, "Unexpected error executing task")


def test_execute_task_unexpected_timeout_keeps_timed_out():
    """The sync /task mapper answers 504 on "timed out": that word survives,
    the rest of the text does not."""
    result = _execute_with(RuntimeError(f"{_PLANT} Read timed out. (read timeout=60)"))
    _assert_answer_clean(
        result.error, "Task execution failed (RuntimeError: timed out — details in backend logs)")


def test_execute_task_short_timeout_names_the_class(caplog):
    caplog.set_level(logging.ERROR)
    result = _execute_with(httpx.ReadTimeout(f"{_PLANT} The read operation timed out"))
    assert result.error_code == "network"
    _assert_answer_clean(result.error, "(ReadTimeout: timed out)")
    _assert_log_keeps_text(caplog.records, "TIMEOUT on test-agent")


def test_slot_admission_error_names_the_conflict_class():
    """The admission give-up's text reaches the same execution error."""
    import redis
    from services import slot_service

    class _Pipe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def watch(self, *keys):
            raise redis.WatchError(_PLANT)

        def reset(self):
            pass

    svc = slot_service.SlotService.__new__(slot_service.SlotService)
    svc.redis = SimpleNamespace(pipeline=lambda: _Pipe())
    svc.slots_prefix, svc.metadata_prefix = "agent:slots:", "agent:slot:"
    svc._cleanup_stale_slots_for_agent = AsyncMock()
    with pytest.raises(slot_service.SlotAdmissionError) as exc:
        asyncio.run(svc.acquire_slot("a1", "e-1", 1, "msg", 60))
    _assert_answer_clean(str(exc.value), "(WatchError)")
