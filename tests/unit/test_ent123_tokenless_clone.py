"""PAT-free clone of public github: templates — trinity-enterprise#123.

Covers the tokenless-create seams end to end at the unit level:

  * `_gate_tokenless_request` — ""→None normalization, the source-mode-only
    400 (explicit False AND None — `source_mode` is Optional[bool], the falsy
    check must catch both), fork-to-own passthrough.
  * `_parse_github_ref` — the owner/repo charset guard that keeps garbage out
    of startup.sh's eval-built clone command now that tokenless creates can
    bypass the REST validation.
  * `_validate_github_access` — tokenless ls-remote probe outcomes (ok /
    unavailable→400 / transient→502 fail-closed), anonymous branch check,
    and the PAT-ful regression (REST path unchanged, GitHubError → 502).
  * `_apply_github_env` — tokenless env has repo+sync flags but NO token
    vars and never GIT_SYNC_AUTO; PAT env byte-compatible with before.
  * `lifecycle._apply_persisted_auth_env` — the rebuild-recovery seam gates
    on REPO, not PAT (a tokenless rebuild must still clone, #843/#1439
    silent-empty class), and re-derives source-mode/branch.
  * `git_service.probe_anonymous_repo_access` — stderr classification.
  * `git_service._agent_has_write_credentials` + the sync/reset guards —
    baked env OR per-agent PAT row (the #1264 live-injection window),
    fail-open, named `no_write_credentials` conflict.
  * startup.sh static guards — repo-only gate, credential-less CLONE_URL,
    GIT_TERMINAL_PROMPT=0, push blackhole, .env PAT fallback.

Harness: purge-and-mock shape copied from
test_1484_create_agent_characterization.py::_load_crud (deliberately NOT
shared cross-file — see its D6 note on sys.modules leaks).

Issue: abilityai/trinity-enterprise#123 (Epic ent#122)
Target: src/backend/services/agent_service/crud.py,
        src/backend/services/agent_service/lifecycle.py,
        src/backend/services/git_service.py, docker/base-image/startup.sh
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent123_tokenless.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_STARTUP_SH = _PROJECT_ROOT / "docker" / "base-image" / "startup.sh"

from fastapi import HTTPException  # noqa: E402

from models import AgentConfig  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _NotFound(Exception):
    pass


class _GitHubError(Exception):
    """Real exception class for the mocked github_service module — an
    `except <MagicMock>` clause raises TypeError at match time."""


class _FakeRepoInfo:
    def __init__(self, exists=True, default_branch="main", private=False):
        self.exists = exists
        self.default_branch = default_branch
        self.private = private


class _GitSyncResult:
    """Attr-compatible stand-in for database.GitSyncResult."""

    def __init__(self, success, message="", commit_sha=None, files_changed=0,
                 branch=None, sync_time=None, conflict_type=None,
                 conflict_class=None):
        self.success = success
        self.message = message
        self.commit_sha = commit_sha
        self.files_changed = files_changed
        self.branch = branch
        self.conflict_type = conflict_type
        self.conflict_class = conflict_class


def _purge_real_services(monkeypatch, mocks):
    """Drop every real `services*` module not explicitly mocked so a fresh
    `from services import X` resolves the sys.modules mock, not a stale
    attribute of a previously-imported real package (1484 harness shape)."""
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)


# ---------------------------------------------------------------------------
# crud harness
# ---------------------------------------------------------------------------
def _load_crud(monkeypatch):
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    settings_service = MagicMock()
    settings_service.resolve_github_pat = MagicMock(return_value=("", "none"))
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant")
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"})
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)
    settings_service.get_ephemeral_agent_quota = MagicMock(return_value=5)
    settings_service.get_ephemeral_ttl_ceiling_seconds = MagicMock(return_value=86400)

    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    git_service.probe_anonymous_repo_access = AsyncMock(return_value="ok")
    git_service.check_remote_branch_exists = AsyncMock(return_value=True)
    git_service.reserve_and_generate_instance_id = AsyncMock(
        return_value=("iid-1", "main"))
    # #2069: `_apply_github_env` now gates GIT_SYNC_AUTO on the real
    # `_git_auto_sync_baked` predicate (the single owner). A bare MagicMock is
    # TRUTHY, which would set GIT_SYNC_AUTO for every case; give the mock a
    # faithful copy so these tests exercise the real gating. The REAL predicate's
    # matrix is guard-tested in `test_2069_gitignore_at_creation::TestBakePredicate`.
    git_service._git_auto_sync_baked = (
        lambda config, github_repo, github_pat, fork_upstream: bool(github_repo)
        and bool(github_pat)
        and (not config.source_mode or bool(fork_upstream))
    )

    github_service_mod = MagicMock()
    github_service_mod.GitHubError = _GitHubError
    github_service_mod.GitHubService = MagicMock()

    template_service = MagicMock()
    template_service.get_github_template = MagicMock(return_value=None)

    pkg = "services.agent_service"
    sibling_mocks = {
        f"{pkg}.{sib}": MagicMock()
        for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                    "files", "folders", "helpers", "lifecycle", "mcp_tool_names",
                    "metrics", "permissions", "queue", "read_only", "stats",
                    "terminal", "capabilities", "ephemeral", "pull_mode"]
    }

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": MagicMock(),
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": template_service,
        "services.git_service": git_service,
        "services.settings_service": settings_service,
        "services.github_service": github_service_mod,
        "services.entitlement_service": MagicMock(),
        "services.rate_limiter": MagicMock(),
        "services.agent_runtime_state": MagicMock(),
        "services.agent_auth": MagicMock(),
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_real_services(monkeypatch, mocks)
    import services.agent_service.crud as crud_mod
    return crud_mod, patcher, git_service


@pytest.fixture()
def crud_env(monkeypatch):
    crud_mod, patcher, git_service = _load_crud(monkeypatch)
    try:
        yield crud_mod, git_service
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# _gate_tokenless_request
# ---------------------------------------------------------------------------
class TestGateTokenlessRequest:
    def test_pat_passes_through(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")
        assert crud._gate_tokenless_request(cfg, "ghp_x") == "ghp_x"

    def test_empty_string_normalized_to_none(self, crud_env):
        # resolve_github_pat returns ("", "none") — NOT None (eng F4a).
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=True)
        assert crud._gate_tokenless_request(cfg, "") is None

    def test_tokenless_source_mode_default_allowed(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")  # default True
        assert crud._gate_tokenless_request(cfg, "") is None

    @pytest.mark.parametrize("mode", [False, None])
    def test_tokenless_non_source_mode_rejected(self, crud_env, mode):
        # source_mode is Optional[bool]: explicit None must hit the same 400
        # (falsy check), or it silently flows into working-branch mode (F4b).
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=mode)
        with pytest.raises(HTTPException) as exc:
            crud._gate_tokenless_request(cfg, "")
        assert exc.value.status_code == 400
        assert "write credentials" in exc.value.detail

    def test_fork_to_own_passthrough_never_raises(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=False)
        cfg.fork_to_own = MagicMock()  # any truthy fork spec
        assert crud._gate_tokenless_request(cfg, "") is None


# ---------------------------------------------------------------------------
# _parse_github_ref charset guard
# ---------------------------------------------------------------------------
class TestParseGithubRefGuard:
    def test_valid_repo_path_passes(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:My-Org/repo.name_1")
        _lookup, path, _branch = crud._parse_github_ref(cfg)
        assert path == "My-Org/repo.name_1"

    @pytest.mark.parametrize("bad", [
        "github:owner/repo;rm -rf /",
        "github:owner/repo$(id)",
        "github:owner/re po",
        "github:owner/repo/extra",
        "github:owner/`x`",
    ])
    def test_garbage_repo_path_rejected(self, crud_env, bad):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template=bad)
        with pytest.raises(HTTPException) as exc:
            crud._parse_github_ref(cfg)
        assert exc.value.status_code == 400
        assert "Invalid GitHub repository reference" in exc.value.detail

    def test_predefined_template_name_without_slash_skips_guard(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:business-assistant")
        _lookup, path, _branch = crud._parse_github_ref(cfg)
        assert path == "business-assistant"


# ---------------------------------------------------------------------------
# _validate_github_access — tokenless probe
# ---------------------------------------------------------------------------
class TestValidateGithubAccessTokenless:
    def test_public_reachable_proceeds(self, crud_env):
        crud, gs = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")
        _run(crud._validate_github_access(cfg, "o/r", None))  # no raise
        gs.probe_anonymous_repo_access.assert_awaited_once_with("o/r")

    def test_unavailable_is_named_400(self, crud_env):
        crud, gs = crud_env
        gs.probe_anonymous_repo_access = AsyncMock(return_value="unavailable")
        cfg = AgentConfig(name="a1", template="github:o/r")
        with pytest.raises(HTTPException) as exc:
            _run(crud._validate_github_access(cfg, "o/r", None))
        assert exc.value.status_code == 400
        assert "not found or is private" in exc.value.detail
        assert "GitHub token" in exc.value.detail

    def test_transient_is_fail_closed_502(self, crud_env):
        crud, gs = crud_env
        gs.probe_anonymous_repo_access = AsyncMock(return_value="transient")
        cfg = AgentConfig(name="a1", template="github:o/r")
        with pytest.raises(HTTPException) as exc:
            _run(crud._validate_github_access(cfg, "o/r", None))
        assert exc.value.status_code == 502

    def test_missing_branch_is_named_400(self, crud_env):
        crud, gs = crud_env
        gs.check_remote_branch_exists = AsyncMock(return_value=False)
        cfg = AgentConfig(name="a1", template="github:o/r", source_branch="main")
        with pytest.raises(HTTPException) as exc:
            _run(crud._validate_github_access(cfg, "o/r", None))
        assert exc.value.status_code == 400
        assert "Branch 'main' not found" in exc.value.detail

    def test_tokenless_never_touches_rest_api(self, crud_env):
        crud, _gs = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")
        with patch.object(crud, "GitHubService") as gh_cls:
            _run(crud._validate_github_access(cfg, "o/r", None))
            gh_cls.assert_not_called()

    def test_patful_uses_rest_and_github_error_still_502(self, crud_env):
        # AC#5 regression: the PAT path is unchanged, incl. GitHubError → 502.
        crud, _gs = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")
        gh_instance = MagicMock()
        gh_instance.check_repo_exists = AsyncMock(side_effect=_GitHubError("boom"))
        with patch.object(crud, "GitHubService", return_value=gh_instance) as gh_cls:
            with pytest.raises(HTTPException) as exc:
                _run(crud._validate_github_access(cfg, "o/r", "ghp_x"))
            gh_cls.assert_called_once_with("ghp_x")
        assert exc.value.status_code == 502

    def test_patful_happy_path_unchanged(self, crud_env):
        crud, gs = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r")
        gh_instance = MagicMock()
        gh_instance.check_repo_exists = AsyncMock(return_value=_FakeRepoInfo())
        with patch.object(crud, "GitHubService", return_value=gh_instance):
            _run(crud._validate_github_access(cfg, "o/r", "ghp_x"))
        gh_instance.check_repo_exists.assert_awaited_once()
        gs.probe_anonymous_repo_access.assert_not_awaited()


# ---------------------------------------------------------------------------
# _apply_github_env
# ---------------------------------------------------------------------------
class TestApplyGithubEnv:
    def test_tokenless_env_has_no_token_vars(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=True)
        env = {}
        crud._apply_github_env(cfg, env, "o/r", None, None, None)
        assert env["GITHUB_REPO"] == "o/r"
        assert env["GIT_SYNC_ENABLED"] == "true"
        assert env["GIT_SOURCE_MODE"] == "true"
        assert env["GIT_SOURCE_BRANCH"] == "main"
        for var in ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN", "GIT_SYNC_AUTO"):
            assert var not in env

    def test_patful_source_mode_env_unchanged(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=True)
        env = {}
        crud._apply_github_env(cfg, env, "o/r", "ghp_x", None, None)
        assert env["GITHUB_PAT"] == "ghp_x"
        assert env["GH_TOKEN"] == "ghp_x"
        assert env["GITHUB_TOKEN"] == "ghp_x"
        assert env["GIT_SYNC_ENABLED"] == "true"
        assert env["GIT_SOURCE_MODE"] == "true"
        assert "GIT_SYNC_AUTO" not in env  # source-mode: no auto-push

    def test_patful_working_branch_gets_auto_sync(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=False)
        env = {}
        crud._apply_github_env(cfg, env, "o/r", "ghp_x", None, "trinity/a1/i1")
        assert env["GIT_SYNC_AUTO"] == "true"
        assert env["GIT_WORKING_BRANCH"] == "trinity/a1/i1"

    def test_tokenless_never_auto_sync_even_non_source_mode(self, crud_env):
        # Belt (F7): unreachable combination today (the 400 gate blocks it),
        # but auto-push must never engage without credentials.
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="github:o/r", source_mode=False)
        env = {}
        crud._apply_github_env(cfg, env, "o/r", None, None, "wb")
        assert "GIT_SYNC_AUTO" not in env

    def test_no_repo_is_noop(self, crud_env):
        crud, _ = crud_env
        cfg = AgentConfig(name="a1", template="local:x")
        env = {}
        crud._apply_github_env(cfg, env, None, None, None, None)
        assert env == {}


# ---------------------------------------------------------------------------
# lifecycle._apply_persisted_auth_env — the rebuild seam (eng F2)
# ---------------------------------------------------------------------------
def _load_lifecycle(monkeypatch, pat=None):
    helpers = MagicMock()
    helpers.is_claude_runtime = MagicMock(return_value=False)

    routers_git = MagicMock()
    routers_git.get_github_pat_for_agent = MagicMock(return_value=pat)

    agent_auth = MagicMock()
    agent_auth.derive_agent_token = MagicMock(return_value="tok")

    database_mod = MagicMock()
    database_mod.db.get_guardrails_config.return_value = None

    pkg = "services.agent_service"
    sibling_mocks = {
        f"{pkg}.{sib}": MagicMock()
        for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                    "files", "folders", "mcp_tool_names", "metrics",
                    "permissions", "queue", "read_only", "stats", "terminal",
                    "capabilities", "ephemeral", "pull_mode", "crud"]
    }
    sibling_mocks[f"{pkg}.helpers"] = helpers

    mocks = {
        "docker": MagicMock(),
        "docker.errors": MagicMock(),
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": database_mod,
        "routers.git": routers_git,
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": MagicMock(),
        "services.git_service": MagicMock(),
        "services.settings_service": MagicMock(),
        "services.github_service": MagicMock(),
        "services.entitlement_service": MagicMock(),
        "services.rate_limiter": MagicMock(),
        "services.agent_runtime_state": MagicMock(),
        "services.agent_auth": agent_auth,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_real_services(monkeypatch, mocks)
    import services.agent_service.lifecycle as lc
    return lc, patcher, database_mod.db, routers_git


@pytest.fixture()
def lifecycle_env(monkeypatch):
    lc, patcher, db, routers_git = _load_lifecycle(monkeypatch)
    try:
        yield lc, db, routers_git
    finally:
        patcher.stop()


class TestApplyPersistedAuthEnvRebuildSeam:
    def test_tokenless_rebuild_still_gets_repo_and_sync(self, lifecycle_env):
        lc, db, _rg = lifecycle_env
        db.get_git_config.return_value = {
            "github_repo": "o/r", "source_mode": True, "source_branch": "main",
        }
        env = {}
        lc._apply_persisted_auth_env("a1", env, "codex")
        assert env["GITHUB_REPO"] == "o/r"
        assert env["GIT_SYNC_ENABLED"] == "true"
        assert env["GIT_SOURCE_MODE"] == "true"
        assert env["GIT_SOURCE_BRANCH"] == "main"
        assert "GITHUB_PAT" not in env

    def test_patful_rebuild_unchanged(self, lifecycle_env):
        lc, db, rg = lifecycle_env
        rg.get_github_pat_for_agent.return_value = "ghp_x"
        db.get_git_config.return_value = {
            "github_repo": "o/r", "source_mode": False, "source_branch": "main",
        }
        env = {}
        lc._apply_persisted_auth_env("a1", env, "codex")
        assert env["GITHUB_REPO"] == "o/r"
        assert env["GITHUB_PAT"] == "ghp_x"
        assert env["GIT_SYNC_ENABLED"] == "true"
        assert "GIT_SOURCE_MODE" not in env

    def test_no_git_config_is_noop(self, lifecycle_env):
        lc, db, _rg = lifecycle_env
        db.get_git_config.return_value = None
        env = {}
        lc._apply_persisted_auth_env("a1", env, "codex")
        assert "GITHUB_REPO" not in env


# ---------------------------------------------------------------------------
# git_service — real module with heavy imports mocked
# ---------------------------------------------------------------------------
def _load_git_service(monkeypatch):
    database_mod = MagicMock()
    database_mod.GitSyncResult = _GitSyncResult

    mocks = {
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": database_mod,
        "services.agent_auth": MagicMock(),
        "services.docker_service": MagicMock(),
    }
    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_real_services(monkeypatch, mocks)
    # #1028: git_service is a package; `gs` is the package and each patch in
    # this file lands on the module whose function is being driven —
    # provisioning for the anonymous probe, sync for the sync/reset verbs,
    # gitignore for the migrate hook sync reads through it.
    import services.git_service as gs
    return gs, patcher, database_mod.db


@pytest.fixture()
def git_service_env(monkeypatch):
    gs, patcher, db = _load_git_service(monkeypatch)
    try:
        yield gs, db
    finally:
        patcher.stop()


class _FakeProc:
    def __init__(self, returncode, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):
        pass

    async def wait(self):
        pass


class TestProbeAnonymousRepoAccess:
    def _probe(self, gs, proc):
        with patch.object(gs.provisioning.asyncio, "create_subprocess_exec",
                          AsyncMock(return_value=proc)):
            return _run(gs.probe_anonymous_repo_access("o/r"))

    def test_exit_zero_is_ok(self, git_service_env):
        gs, _ = git_service_env
        assert self._probe(gs, _FakeProc(0)) == "ok"

    @pytest.mark.parametrize("stderr", [
        b"fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        b"fatal: Authentication failed for 'https://github.com/o/r.git/'",
        b"remote: Repository not found.",
    ])
    def test_auth_challenge_is_unavailable(self, git_service_env, stderr):
        gs, _ = git_service_env
        assert self._probe(gs, _FakeProc(128, stderr)) == "unavailable"

    def test_network_error_is_transient(self, git_service_env):
        gs, _ = git_service_env
        proc = _FakeProc(
            128,
            b"fatal: unable to access '...': Could not resolve host: github.com",
        )
        assert self._probe(gs, proc) == "transient"

    def test_spawn_failure_is_transient(self, git_service_env):
        gs, _ = git_service_env
        with patch.object(gs.provisioning.asyncio, "create_subprocess_exec",
                          AsyncMock(side_effect=FileNotFoundError("no git"))):
            assert _run(gs.probe_anonymous_repo_access("o/r")) == "transient"


class _FakeContainer:
    def __init__(self, env_pairs, status="running"):
        self.attrs = {"Config": {"Env": list(env_pairs)}}
        self.status = status


class TestWriteCredentialGuard:
    def test_baked_env_pat_is_credentialed(self, git_service_env):
        gs, db = git_service_env
        c = _FakeContainer(["GITHUB_PAT=ghp_x", "OTHER=1"])
        assert gs._agent_has_write_credentials("a1", c) is True
        db.get_agent_github_pat.assert_not_called()

    def test_empty_env_pat_var_does_not_count(self, git_service_env):
        gs, db = git_service_env
        db.get_agent_github_pat.return_value = None
        c = _FakeContainer(["GITHUB_PAT=", "OTHER=1"])
        assert gs._agent_has_write_credentials("a1", c) is False

    def test_per_agent_db_pat_counts_without_env(self, git_service_env):
        # The #1264 window: PAT live-injected + origin rewritten, baked env
        # still tokenless until recreate — must NOT be blocked (eng F1).
        gs, db = git_service_env
        db.get_agent_github_pat.return_value = "ghp_live"
        c = _FakeContainer(["OTHER=1"])
        assert gs._agent_has_write_credentials("a1", c) is True

    def test_no_creds_anywhere_is_tokenless(self, git_service_env):
        gs, db = git_service_env
        db.get_agent_github_pat.return_value = None
        c = _FakeContainer(["OTHER=1"])
        assert gs._agent_has_write_credentials("a1", c) is False

    def test_fail_open_on_db_error(self, git_service_env):
        gs, db = git_service_env
        db.get_agent_github_pat.side_effect = RuntimeError("db down")
        c = _FakeContainer(["OTHER=1"])
        assert gs._agent_has_write_credentials("a1", c) is True

    def test_sync_to_github_blocks_tokenless_with_named_conflict(self, git_service_env):
        gs, db = git_service_env
        db.get_agent_github_pat.return_value = None
        c = _FakeContainer(["OTHER=1"])
        with patch.object(gs.sync, "get_agent_container", MagicMock(return_value=c)):
            result = _run(gs.sync_to_github("a1"))
        assert result.success is False
        assert result.conflict_type == "no_write_credentials"
        # Anchored on the CONSTANT, not on its current wording: this test used
        # to assert the literal "fork-to-own"/"create a new agent" phrasing and
        # broke when ent#109 retired that workaround. What ent#123 actually
        # requires is that the refusal carries the platform's named message and
        # stays actionable — the exact copy is owned by
        # test_ent109_no_write_credentials_message.py.
        assert result.message == gs.NO_WRITE_CREDENTIALS_MESSAGE
        low = result.message.lower()
        assert "no write credentials" in low
        assert "token" in low, "the refusal must still name a remedy"

    def test_sync_to_github_passes_credentialed_agent(self, git_service_env):
        gs, _db = git_service_env
        c = _FakeContainer(["GITHUB_PAT=ghp_x"])
        migrate = AsyncMock()
        with patch.object(gs.sync, "get_agent_container", MagicMock(return_value=c)), \
             patch.object(gs.gitignore, "_migrate_workspace_gitignore", migrate), \
             patch.object(gs.sync, "agent_httpx_client",
                          MagicMock(side_effect=RuntimeError("stop here"))):
            result = _run(gs.sync_to_github("a1"))
        # Reached past the guard (the sentinel error came from the HTTP layer).
        migrate.assert_awaited_once()
        assert result.conflict_type != "no_write_credentials"

    def test_reset_to_main_blocks_tokenless(self, git_service_env):
        gs, db = git_service_env
        db.get_agent_github_pat.return_value = None
        c = _FakeContainer(["OTHER=1"])
        activity_mod = MagicMock()
        activity_mod.activity_service.get_current_activities = AsyncMock(
            return_value=[])
        with patch.dict(sys.modules, {"services.activity_service": activity_mod}), \
             patch.object(gs.sync, "get_agent_container", MagicMock(return_value=c)):
            result = _run(gs.reset_to_main_preserve_state("a1"))
        assert result["error"] == "no_write_credentials"


# ---------------------------------------------------------------------------
# startup.sh static guards
# ---------------------------------------------------------------------------
class TestStartupShTokenless:
    @pytest.fixture(scope="class")
    def sh(self):
        return _STARTUP_SH.read_text()

    def test_clone_gate_is_repo_only(self, sh):
        assert '[ -n "${GITHUB_REPO}" ] && [ -n "${GITHUB_PAT}" ]' not in sh
        assert 'if [ -n "${GITHUB_REPO}" ]; then' in sh

    def test_credential_less_clone_url_branch_exists(self, sh):
        assert 'CLONE_URL="${GIT_SCHEME}://${GIT_HOST_PATH}/${GITHUB_REPO}.git"' in sh

    def test_pat_clone_url_still_authenticated(self, sh):
        assert 'CLONE_URL="${GIT_SCHEME}://oauth2:${GITHUB_PAT}@${GIT_HOST_PATH}/${GITHUB_REPO}.git"' in sh

    def test_terminal_prompt_disabled(self, sh):
        assert "export GIT_TERMINAL_PROMPT=0" in sh

    def test_push_remote_blackhole_present(self, sh):
        assert sh.count("configure_push_remote()") == 1
        assert sh.count("configure_push_remote") >= 3  # def + 2 call sites
        assert "no-write-credentials" in sh

    def test_env_pat_fallback_present(self, sh):
        assert "grep -m1 '^GITHUB_PAT=' /home/developer/.env" in sh

    def test_private_repo_cause_in_failure_text(self, sh):
        assert sh.count("Repository is private and no GitHub token is configured") == 2

    def test_gh_token_export_still_pat_gated(self, sh):
        # #1574 regression: GH_TOKEN/GITHUB_TOKEN must stay gated on a
        # resolved PAT — never exported empty for a tokenless agent.
        assert 'if [ -n "${GITHUB_PAT}" ]; then\n    export GH_TOKEN="${GITHUB_PAT}"' in sh
