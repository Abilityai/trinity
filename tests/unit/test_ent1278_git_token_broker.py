"""trinity-enterprise#1278 — agent containers fetch a repo-scoped git token at use time.

Pins the open-core half:

* `wants_broker` is the one mode decision shared by creation, the recreate
  matcher and the recreate env writer;
* `coverage` is tri-state, and "unknown" keeps the mode a container has;
* the recreate matcher asks for a recreate only when the mode should change;
* `GET /api/agents/{name}/git/token` serves the calling agent's own repo, only
  to a broker-mode container, and never returns a PAT;
* the credential helper (the real script, run against a local HTTP server)
  uses the endpoint first, falls through on 204 only, and never falls back to
  a PAT when the mint failed;
* the `gh` wrapper hands the helper's answer to the real `gh`;
* a PAT rotation skips broker-mode containers.

No network beyond 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity_test_ent1278.db"))

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402

import routers.git as gitmod  # noqa: E402
import services.agent_service.helpers as helpers_mod  # noqa: E402
import services.github_pat_propagation_service as prop  # noqa: E402
from services import github_token_provider as gtp  # noqa: E402
from services import settings_service as ss  # noqa: E402

HELPER = _ROOT / "docker" / "base-image" / "git-credential-trinity.sh"
GH_WRAPPER = _ROOT / "docker" / "base-image" / "gh-trinity.sh"
FLAG = gtp.BROKER_ENV


class _Provider:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    async def token_for_repo(self, repo, permissions=None):
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture(autouse=True)
def _no_provider():
    gtp.clear_provider()
    yield
    gtp.clear_provider()


def _container(**env):
    return SimpleNamespace(attrs={"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}})


# --- the mode decision -------------------------------------------------------

@pytest.mark.parametrize("coverage,carried_flag,would_bake_pat,has_own_pat,expected", [
    (True, False, True, False, True),     # covered, platform PAT would be baked
    (True, False, False, False, False),   # covered, deliberately tokenless: stays tokenless
    (True, True, False, False, True),     # already in broker mode
    (False, True, False, False, False),   # repo no longer covered
    (False, False, True, False, False),   # no provider: PAT mode
    (None, True, False, False, True),     # unknown: keep broker mode
    (None, False, True, False, False),    # unknown: keep PAT mode
    (True, True, True, True, False),      # a per-agent PAT outranks the provider
    (None, True, False, True, False),
])
def test_wants_broker(coverage, carried_flag, would_bake_pat, has_own_pat, expected):
    assert gtp.wants_broker(
        coverage=coverage, carried_flag=carried_flag,
        would_bake_pat=would_bake_pat, has_own_pat=has_own_pat) is expected


def test_coverage_is_tri_state(monkeypatch):
    assert asyncio.run(gtp.coverage("acme/app")) is False  # no provider

    gtp.register_provider(_Provider(gtp.RepoToken("ghs_x", "2026-11-19T12:00:00Z")))
    assert asyncio.run(gtp.coverage("acme/app")) is True

    gtp.register_provider(_Provider(None))
    assert asyncio.run(gtp.coverage("acme/app")) is False

    gtp.register_provider(_Provider(error=gtp.GithubTokenUnavailable("down")))
    assert asyncio.run(gtp.coverage("acme/app")) is None


def test_a_slow_provider_is_unknown(monkeypatch):
    class _Slow:
        async def token_for_repo(self, repo, permissions=None):
            await asyncio.sleep(5)

    monkeypatch.setattr(gtp, "_COVERAGE_TIMEOUT", 0.05)
    gtp.register_provider(_Slow())
    assert asyncio.run(gtp.coverage("acme/app")) is None


# --- the recreate matcher ----------------------------------------------------

@pytest.fixture
def _platform_pat(monkeypatch):
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: None)
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "ghp_platform")


@pytest.mark.parametrize("env,coverage,matches", [
    ({"GITHUB_PAT": "ghp_platform"}, True, False),   # covered: convert to broker mode
    ({"GITHUB_PAT": "ghp_platform"}, None, True),    # GitHub unreachable: keep PAT mode
    ({"GITHUB_PAT": "ghp_platform"}, False, True),
    ({FLAG: "1"}, True, True),                       # nothing to compare, no recreate
    ({FLAG: "1"}, None, True),                       # GitHub unreachable: keep broker mode
    ({FLAG: "1"}, False, False),                     # uncovered: recreate, PAT comes back
    ({}, True, True),                                # tokenless stays tokenless
])
def test_matcher_recreates_only_when_the_mode_changes(_platform_pat, env, coverage, matches):
    assert helpers_mod.check_github_pat_env_matches(_container(**env), "a1", coverage) is matches


def test_matcher_leaves_broker_mode_when_a_per_agent_pat_is_set(_platform_pat, monkeypatch):
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: "ghp_own")
    assert helpers_mod.check_github_pat_env_matches(_container(**{FLAG: "1"}), "a1", True) is False


# --- the endpoint ------------------------------------------------------------

def _agent(name):
    return SimpleNamespace(agent_name=name, mcp_scope="agent", role="user", id=1, username="o")


@pytest.fixture
def _endpoint(monkeypatch):
    """A broker-mode container for agent `a1`, bound to acme/app."""
    import services.docker_service as docker_service

    state = {"env": {FLAG: "1"}, "resolved": ("ghs_scoped", "app", "2026-11-19T12:00:00Z")}
    monkeypatch.setattr(docker_service, "get_agent_container", lambda n: _container(**state["env"]))
    monkeypatch.setattr(
        gitmod.db, "get_git_config", lambda n: SimpleNamespace(github_repo="acme/app"))

    async def _resolve(repo, agent_name=None, owner_id=None, permissions=None):
        assert (repo, agent_name) == ("acme/app", "a1")
        if isinstance(state["resolved"], Exception):
            raise state["resolved"]
        return state["resolved"]

    monkeypatch.setattr(ss, "resolve_github_token", _resolve)
    return state


def _call(name="a1", caller=None):
    return asyncio.run(gitmod.get_git_token(name, current_user=caller or _agent("a1")))


def test_endpoint_returns_the_scoped_token_uncached(_endpoint):
    response = _call()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert b'"token":"ghs_scoped"' in response.body


def test_endpoint_refuses_another_agents_key(_endpoint):
    with pytest.raises(HTTPException) as raised:
        _call("a1", caller=_agent("a2"))
    assert raised.value.status_code == 403


def test_endpoint_refuses_a_person(_endpoint):
    person = SimpleNamespace(agent_name=None, mcp_scope=None, role="admin", id=1, username="o")
    with pytest.raises(HTTPException) as raised:
        _call("a1", caller=person)
    assert raised.value.status_code == 403


def test_endpoint_gives_nothing_to_a_container_without_the_flag(_endpoint):
    _endpoint["env"] = {"GITHUB_REPO": "acme/app"}  # deliberately tokenless
    assert _call().status_code == 204


@pytest.mark.parametrize("tier", ["global", "per_agent", "none"])
def test_endpoint_never_returns_a_pat(_endpoint, tier):
    _endpoint["resolved"] = ("ghp_platform", tier, None)
    response = _call()
    assert response.status_code == 204
    assert b"ghp_platform" not in response.body


def test_endpoint_does_not_read_an_unreadable_container_as_not_broker(_endpoint, monkeypatch):
    """204 sends the helper to its PAT sources. A Docker fault must not."""
    import services.docker_service as docker_service

    monkeypatch.setattr(docker_service, "get_agent_container", lambda n: None)
    with pytest.raises(HTTPException) as raised:
        _call()
    assert raised.value.status_code == 503


@pytest.mark.parametrize("ephemeral", [True, False])
def test_the_route_is_reachable_through_the_agent_key_fences(ephemeral):
    """An ephemeral agent's key and an agent owned by a Workspace-only person
    are held to an allowlist of own-runtime routes. A broker container that
    cannot reach this route has no git credential at all."""
    import dependencies as deps

    routes = deps._EPHEMERAL_ALLOWED_ROUTES if ephemeral else deps._AGENT_SELF_ROUTES
    request = SimpleNamespace(
        method="GET", scope={"path": "/api/agents/a1/git/token"},
        path_params={"agent_name": "a1"})
    assert deps._is_own_runtime_route(request, "a1", routes)
    assert not deps._is_own_runtime_route(request, "a2", routes)


def test_endpoint_reports_a_failed_mint_as_503(_endpoint):
    _endpoint["resolved"] = gtp.GithubTokenUnavailable("installation suspended")
    with pytest.raises(HTTPException) as raised:
        _call()
    assert raised.value.status_code == 503


# --- the credential helper, run for real -------------------------------------

@pytest.fixture
def backend():
    """A local stand-in for the backend's token route."""
    state = {"status": 200, "body": b'{"token":"ghs_fromendpoint","expires_at":"x"}', "seen": []}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["seen"].append((self.path, self.headers.get("Authorization")))
            self.send_response(state["status"])
            self.end_headers()
            if state["status"] == 200:
                self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def _helper_env(tmp_path, backend_url, **extra):
    env = {
        "PATH": os.environ["PATH"],
        "TRINITY_AGENT_HOME": str(tmp_path),
        FLAG: "1",
        "TRINITY_BACKEND_URL": backend_url,
        "TRINITY_MCP_API_KEY": "trinity_mcp_testkey",
        "AGENT_NAME": "a1",
        "TRINITY_GIT_TOKEN_TIMEOUT": "2",
    }
    env.update(extra)
    return env


def _run_helper(env):
    return subprocess.run(
        ["sh", str(HELPER), "get"], input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, env=env, timeout=20)


def test_helper_uses_the_endpoint_first(tmp_path, backend):
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_stale\n")
    result = _run_helper(_helper_env(tmp_path, backend["url"]))
    assert result.stdout == "username=oauth2\npassword=ghs_fromendpoint\n"
    assert backend["seen"] == [("/api/agents/a1/git/token", "Bearer trinity_mcp_testkey")]


def test_helper_falls_through_to_the_pat_sources_on_204(tmp_path, backend):
    backend["status"] = 204
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_own\n")
    result = _run_helper(_helper_env(tmp_path, backend["url"]))
    assert result.stdout == "username=oauth2\npassword=ghp_own\n"


@pytest.mark.parametrize("status", [503, 403, 500])
def test_helper_never_falls_back_to_a_pat_when_the_mint_failed(tmp_path, backend, status):
    backend["status"] = status
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_platform\n")
    result = _run_helper(_helper_env(tmp_path, backend["url"], GITHUB_PAT="ghp_baked"))
    assert result.stdout == ""
    assert f"TRINITY_GIT_NO_CREDENTIAL host=github.com broker={status}" in result.stderr


def test_helper_gives_no_credential_when_the_backend_is_unreachable(tmp_path):
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_platform\n")
    result = _run_helper(_helper_env(tmp_path, "http://127.0.0.1:1"))
    assert result.stdout == ""
    assert "TRINITY_GIT_NO_CREDENTIAL" in result.stderr


@pytest.mark.parametrize("missing", ["TRINITY_BACKEND_URL", "TRINITY_MCP_API_KEY", "AGENT_NAME"])
def test_helper_gives_no_credential_when_broker_mode_lacks_a_precondition(tmp_path, backend, missing):
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_platform\n")
    env = _helper_env(tmp_path, backend["url"])
    del env[missing]
    result = _run_helper(env)
    assert result.stdout == ""
    assert "TRINITY_GIT_NO_CREDENTIAL" in result.stderr


def test_helper_skips_the_endpoint_without_the_flag(tmp_path, backend):
    (tmp_path / ".env").write_text("GITHUB_PAT=ghp_own\n")
    env = _helper_env(tmp_path, backend["url"])
    del env[FLAG]
    assert _run_helper(env).stdout == "username=oauth2\npassword=ghp_own\n"
    assert backend["seen"] == []


def test_helper_keeps_the_agent_key_off_argv(tmp_path, backend):
    """The key reaches curl on stdin. A `ps` during the call must not show it."""
    script = HELPER.read_text()
    rung = script[script.index("TRINITY_GIT_TOKEN_BROKER"):script.index("pat=$(_read_env_value")]
    curl_line = next(line for line in rung.splitlines() if "curl " in line)
    assert "-K -" in curl_line and "TRINITY_MCP_API_KEY" not in curl_line


# --- the gh wrapper ----------------------------------------------------------

def _run_gh(tmp_path, backend_url, **extra):
    real_gh = tmp_path / "real-gh"
    real_gh.write_text('#!/bin/sh\necho "token=${GH_TOKEN:-none} args=$*"\n')
    real_gh.chmod(0o755)
    helper = tmp_path / "helper"
    helper.write_text(f'#!/bin/sh\nexec sh {HELPER} "$@"\n')
    helper.chmod(0o755)
    env = _helper_env(
        tmp_path, backend_url, TRINITY_REAL_GH=str(real_gh), TRINITY_GIT_HELPER=str(helper))
    env.update(extra)
    for key in [k for k, v in env.items() if v is None]:
        del env[key]
    return subprocess.run(
        ["sh", str(GH_WRAPPER), "api", "repos/acme/app"],
        capture_output=True, text=True, env=env, timeout=20).stdout.strip()


def test_gh_wrapper_fetches_a_token_per_call(tmp_path, backend):
    assert _run_gh(tmp_path, backend["url"]) == "token=ghs_fromendpoint args=api repos/acme/app"
    _run_gh(tmp_path, backend["url"])
    assert len(backend["seen"]) == 2


def test_gh_wrapper_runs_gh_unauthenticated_when_the_mint_failed(tmp_path, backend):
    backend["status"] = 503
    assert _run_gh(tmp_path, backend["url"]) == "token=none args=api repos/acme/app"


def test_gh_wrapper_is_inert_outside_broker_mode(tmp_path, backend):
    assert _run_gh(tmp_path, backend["url"], **{FLAG: None, "GH_TOKEN": "ghp_baked"}) == (
        "token=ghp_baked args=api repos/acme/app")
    assert backend["seen"] == []


# --- PAT rotation ------------------------------------------------------------

def test_rotation_skips_broker_containers(monkeypatch):
    agents = [SimpleNamespace(name="broker", status="running"),
              SimpleNamespace(name="pat", status="running")]
    envs = {"broker": {FLAG: "1"}, "pat": {"GITHUB_PAT": "ghp_old"}}
    applied = []

    monkeypatch.setattr(prop, "list_all_agents_fast", lambda: agents)
    monkeypatch.setattr(prop, "get_agent_container", lambda n: _container(**envs[n]))
    monkeypatch.setattr(prop.db, "has_agent_github_pat", lambda n: False)

    async def _propagate(agent_name, new_pat, client):
        applied.append(agent_name)
        return prop.AgentPropagationStatus(agent_name=agent_name, status="updated")

    monkeypatch.setattr(prop, "_propagate_to_agent", _propagate)
    asyncio.run(prop.propagate_github_pat("ghp_new"))
    assert applied == ["pat"]


# --- startup.sh --------------------------------------------------------------

STARTUP = _ROOT / "docker" / "base-image" / "startup.sh"


def _startup_function(name):
    """The body of one shell function, cut from the real startup.sh."""
    text = STARTUP.read_text()
    start = text.index(f"    {name}() {{")
    return text[start:text.index("\n    }\n", start) + len("\n    }\n")]


@pytest.mark.parametrize("flag,resolves", [("1", True), ("", False)])
def test_startup_treats_broker_mode_as_credentialed_without_a_network_call(flag, resolves):
    """A boot before the backend is reachable must not blackhole the push
    remote. No backend URL is set here, so a network call could not succeed."""
    script = _startup_function("credential_resolves") + "credential_resolves\n"
    result = subprocess.run(
        ["sh", "-c", script], env={"PATH": os.environ["PATH"], FLAG: flag,
                                   "GIT_SCHEME": "https", "GIT_HOSTPORT": "github.com"},
        capture_output=True, timeout=20)
    assert (result.returncode == 0) is resolves


def test_startup_removes_stale_token_lines_from_env_in_broker_mode(tmp_path):
    """A rotation wrote the platform PAT into `.env` before the switch. The
    agent server re-reads that file at every spawn, so the lines must go."""
    text = STARTUP.read_text()
    start = text.index('if [ "${TRINITY_GIT_TOKEN_BROKER:-}" = "1" ] && [ -f /home/developer/.env ]')
    block = text[start:text.index("\nfi\n", start) + 4]
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / ".env"
    block = block.replace("/home/developer", str(home))

    def _run(flag):
        env_file.write_text("FOO=1\nGITHUB_PAT=ghp_stale\nGH_TOKEN=ghp_stale\nGITHUB_TOKEN=ghp_stale\nBAR=2\n")
        subprocess.run(["sh", "-c", block], env={"PATH": os.environ["PATH"], FLAG: flag},
                       capture_output=True, timeout=20, check=True)
        return env_file.read_text()

    assert _run("1") == "FOO=1\nBAR=2\n"
    assert "ghp_stale" in _run("")


def test_startup_does_not_adopt_a_stale_env_pat_in_broker_mode(tmp_path):
    """The block that copies `.env`'s GITHUB_PAT into the environment (and from
    there into GH_TOKEN/GITHUB_TOKEN) is skipped for a broker container."""
    text = STARTUP.read_text()
    start = text.index('if [ -z "${GITHUB_PAT}" ] && [ "${TRINITY_GIT_TOKEN_BROKER:-}" != "1" ]')
    block = text[start:text.index("\nfi\n", text.index("export GITHUB_TOKEN", start)) + 4]
    env_file = tmp_path / ".env"
    env_file.write_text("GITHUB_PAT=ghp_stale\n")
    block = block.replace("/home/developer/.env", str(env_file))

    def _run(flag):
        return subprocess.run(
            ["sh", "-c", block + 'echo "pat=${GITHUB_PAT:-} gh=${GH_TOKEN:-}"'],
            env={"PATH": os.environ["PATH"], FLAG: flag, "GITHUB_PAT": ""},
            capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[-1]

    assert _run("1") == "pat= gh="
    assert _run("") == "pat=ghp_stale gh=ghp_stale"


# --- wiring ------------------------------------------------------------------

def test_every_git_env_writer_and_the_matcher_receive_coverage():
    """The mode decision only holds if the awaited coverage reaches all three
    consumers. A call that drops the argument falls back to PAT-only for that
    path while the others switch, which is a recreate loop."""
    import ast

    tree = ast.parse(
        (_ROOT / "src/backend/services/agent_service/lifecycle.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)]
    writers = [c for c in calls if c.func.id == "_apply_git_env_from_db"]
    assert len(writers) == 2
    assert all(any(kw.arg == "coverage" for kw in c.keywords) for c in writers)
    matchers = [c for c in calls if c.func.id == "check_github_pat_env_matches"]
    assert [len(c.args) for c in matchers] == [3]
    rebuilds = [c for c in calls if c.func.id == "_apply_persisted_auth_env"]
    assert [len(c.args) for c in rebuilds] == [4]
