"""
Fork-to-own agent creation (trinity-enterprise#93).

Covers:
- ForkToOwnRequest model validation (destination regex, SecretStr hygiene)
- fork_to_own.fork_template_to_own_repo orchestrator: PAT validation,
  destination collision semantics (empty reuse / SHA-match reuse / 409),
  owner mismatch, copy failure paths, secret scrubbing
- crud.create_agent_internal gates: FORK_REQUIRES_GITHUB_TEMPLATE,
  FORK_TO_OWN_REQUIRED, FORK_BRANCH_UNSUPPORTED, FORK_DESTINATION_IN_USE
- crud deep slice: the SecretStr is unwrapped exactly once — the container
  env carries the PLAIN token (str(SecretStr) == '**********' would mean a
  silent empty agent, #1439 class), GIT_UPSTREAM_REPO + GIT_SYNC_AUTO are
  baked, the per-agent PAT (#347) is persisted, and a PAT-persist failure
  rolls back the reserved git-config row + MCP key
- template_service._build_template surfaces fork_to_own + tagline
- startup.sh carries the idempotent upstream-remote block in both paths

Modules: src/backend/services/agent_service/fork_to_own.py
         src/backend/services/agent_service/crud.py
         src/backend/services/github_service.py
         src/backend/services/template_service.py
Issue:   abilityai/trinity-enterprise#93
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Env prerequisites before any backend import (repo test convention)
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_fork_to_own.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pydantic import ValidationError  # noqa: E402

from models import AgentConfig, ForkToOwnRequest  # noqa: E402


# ---------------------------------------------------------------------------
# ForkToOwnRequest model
# ---------------------------------------------------------------------------


class TestForkToOwnRequestModel:
    def test_valid_destination_and_defaults(self):
        req = ForkToOwnRequest(destination_repo="alice/my-brain", github_pat="ghp_x")
        assert req.destination_repo == "alice/my-brain"
        assert req.private is True

    @pytest.mark.parametrize("bad", [
        "no-slash", "owner/", "/repo", "own er/repo", "owner/re po",
        "owner/../etc", "owner/repo/extra", "-owner/repo",
        "owner/repo;rm -rf", "https://github.com/owner/repo",
    ])
    def test_invalid_destinations_rejected(self, bad):
        with pytest.raises(ValidationError):
            ForkToOwnRequest(destination_repo=bad, github_pat="ghp_x")

    def test_empty_pat_rejected(self):
        with pytest.raises(ValidationError):
            ForkToOwnRequest(destination_repo="a/b", github_pat="   ")

    def test_pat_masked_in_repr(self):
        req = ForkToOwnRequest(destination_repo="a/b", github_pat="ghp_supersecret")
        assert "ghp_supersecret" not in repr(req)
        assert "ghp_supersecret" not in str(req)
        # But the secret is recoverable exactly once at the boundary
        assert req.github_pat.get_secret_value() == "ghp_supersecret"

    def test_agent_config_accepts_and_defaults_none(self):
        cfg = AgentConfig(name="x")
        assert cfg.fork_to_own is None
        cfg2 = AgentConfig(
            name="x",
            fork_to_own={"destination_repo": "a/b", "github_pat": "ghp_x"},
        )
        assert cfg2.fork_to_own.destination_repo == "a/b"


# ---------------------------------------------------------------------------
# Orchestrator: fork_template_to_own_repo
# ---------------------------------------------------------------------------


def _load_fork_module():
    # Importing the agent_service package runs its __init__, which eagerly pulls
    # in helpers.py -> `from services.docker_service import list_all_agents_fast`.
    # Under full-suite ordering another test can leave a bare `services.docker_service`
    # stub in sys.modules that only exposes `list_all_agents` (not `_fast`); after a
    # crud_env teardown deletes the cached submodules, the fresh package re-import then
    # fails with ImportError. Backfill the attribute defensively so this fixture never
    # depends on cross-file stub completeness. (`list_all_agents_fast` is a real symbol
    # in services.docker_service; this only guards against a leaked stub.)
    ds = sys.modules.get("services.docker_service")
    if ds is not None and not hasattr(ds, "list_all_agents_fast"):
        ds.list_all_agents_fast = lambda *a, **kw: []
    import services.agent_service.fork_to_own as f2o
    return f2o


class _FakeRepoInfo:
    def __init__(self, exists, default_branch="main", private=True):
        self.exists = exists
        self.default_branch = default_branch
        self.private = private


class _FakeGH:
    """Scriptable stand-in for GitHubService, keyed by constructor PAT."""

    instances: dict = {}

    def __init__(self, pat):
        self.pat = pat
        self.script = _FakeGH.instances.setdefault(pat, {})

    async def validate_token(self):
        return self.script.get("validate_token", (True, "alice"))

    async def check_repo_exists(self, owner, name):
        return self.script["repos"][f"{owner}/{name}"]

    async def list_branches(self, repo, limit=10):
        return self.script["branches"][repo]

    async def get_branch_sha(self, repo, branch):
        for b in self.script["branches"].get(repo, []):
            if b["name"] == branch:
                return b["sha"]
        return None

    async def get_owner_type(self, owner):
        from services.github_service import OwnerType
        return self.script.get("owner_type", OwnerType.USER)

    async def create_repository(self, owner, name, private=True, description=None,
                                auto_init=False):
        from services.github_service import GitHubCreateResult
        self.script.setdefault("created", []).append((f"{owner}/{name}", private))
        result = self.script.get("create_result")
        if result is not None:
            return result
        # After creation the repo becomes visible
        self.script["repos"][f"{owner}/{name}"] = _FakeRepoInfo(True)
        return GitHubCreateResult(success=True, repo_url=f"https://github.com/{owner}/{name}")


@pytest.fixture
def fork_env(monkeypatch):
    """Patched orchestrator: fake GitHubService + recorded _run_git."""
    f2o = _load_fork_module()
    _FakeGH.instances = {}
    git_calls = []

    async def fake_run_git(args, timeout, auth_pat=""):
        git_calls.append({"args": list(args), "auth_pat": auth_pat})
        outcome = fork_env_state.get(args[0] if args[0] != "-C" else "push", (0, "ok"))
        if args[0] == "ls-remote":
            return fork_env_state.get("ls-remote", (0, "abc123\trefs/heads/main"))
        return outcome

    fork_env_state = {}
    monkeypatch.setattr(f2o, "GitHubService", _FakeGH)
    monkeypatch.setattr(f2o, "_run_git", fake_run_git)
    monkeypatch.setattr(f2o, "_POLL_INTERVAL_S", 0.01)
    return f2o, _FakeGH, git_calls, fork_env_state


def _template_scripts(read_pat=""):
    """Standard template side: exists, default main, tip sha 'tipsha'."""
    _FakeGH.instances[read_pat] = {
        "repos": {"Abilityai/cornelius": _FakeRepoInfo(True, "main")},
        "branches": {"Abilityai/cornelius": [{"name": "main", "sha": "tipsha"}]},
    }


@pytest.mark.asyncio
async def test_invalid_pat_rejected_before_any_write(fork_env):
    f2o, gh, git_calls, _ = fork_env
    gh.instances["badpat"] = {"validate_token": (False, None), "repos": {}, "branches": {}}
    _template_scripts()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "badpat", "", True)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_PAT_INVALID"
    assert git_calls == []


@pytest.mark.asyncio
async def test_nonempty_destination_409(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "repos": {"alice/brain": _FakeRepoInfo(True)},
        "branches": {"alice/brain": [{"name": "main", "sha": "OTHERSHA"}]},
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "ghp_u", "", True)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FORK_DESTINATION_EXISTS"
    assert git_calls == []


@pytest.mark.asyncio
async def test_empty_destination_reused_and_pushed(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts(read_pat="readpat")
    gh.instances["ghp_u"] = {
        "repos": {"alice/brain": _FakeRepoInfo(True)},
        "branches": {"alice/brain": []},  # exists but no branches → empty
    }
    result = await f2o.fork_template_to_own_repo(
        "Abilityai/cornelius", "alice/brain", "ghp_u", "readpat", True)
    assert result.destination_repo == "alice/brain"
    assert result.default_branch == "main"
    assert result.reused_existing is True
    ops = [c["args"][0] for c in git_calls]
    assert "clone" in ops
    # push happens via `git -C <dir> push …`
    assert any("push" in c["args"] for c in git_calls)


@pytest.mark.asyncio
async def test_sha_match_reuse_skips_push(fork_env):
    """A prior attempt that completed the copy retries idempotently."""
    f2o, gh, git_calls, _ = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "repos": {"alice/brain": _FakeRepoInfo(True)},
        "branches": {"alice/brain": [{"name": "main", "sha": "tipsha"}]},
    }
    result = await f2o.fork_template_to_own_repo(
        "Abilityai/cornelius", "alice/brain", "ghp_u", "", True)
    assert result.reused_existing is True
    assert git_calls == []  # no clone, no push — nothing to do


@pytest.mark.asyncio
async def test_owner_mismatch_forbidden(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "validate_token": (True, "alice"),
        "repos": {"bob/brain": _FakeRepoInfo(False)},
        "branches": {},
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "bob/brain", "ghp_u", "", True)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_DESTINATION_FORBIDDEN"
    assert "alice" in exc.value.detail["error"]


@pytest.mark.asyncio
async def test_create_and_copy_happy_path(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts(read_pat="platform-pat")
    gh.instances["ghp_u"] = {
        "validate_token": (True, "alice"),
        "repos": {"alice/brain": _FakeRepoInfo(False)},
        "branches": {},
    }
    result = await f2o.fork_template_to_own_repo(
        "Abilityai/cornelius", "alice/brain", "ghp_u", "platform-pat", True)
    assert result.destination_repo == "alice/brain"
    assert result.reused_existing is False
    # Repo created private
    assert gh.instances["ghp_u"]["created"] == [("alice/brain", True)]
    # Clone authenticated with the READ pat; push + ls-remote with the USER pat
    clone = next(c for c in git_calls if c["args"][0] == "clone")
    assert clone["auth_pat"] == "platform-pat"
    assert "--bare" in clone["args"] and "--single-branch" in clone["args"]
    push = next(c for c in git_calls if "push" in c["args"])
    assert push["auth_pat"] == "ghp_u"
    # URLs are credential-less — the token never rides the argv
    for c in git_calls:
        joined = " ".join(str(a) for a in c["args"])
        assert "ghp_u" not in joined
        assert "platform-pat" not in joined


@pytest.mark.asyncio
async def test_public_visibility_honored(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "validate_token": (True, "alice"),
        "repos": {"alice/brain": _FakeRepoInfo(False)},
        "branches": {},
    }
    await f2o.fork_template_to_own_repo(
        "Abilityai/cornelius", "alice/brain", "ghp_u", "", private=False)
    assert gh.instances["ghp_u"]["created"] == [("alice/brain", False)]


@pytest.mark.asyncio
async def test_clone_failure_maps_to_502(fork_env):
    f2o, gh, git_calls, state = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "repos": {"alice/brain": _FakeRepoInfo(True)},
        "branches": {"alice/brain": []},
    }
    state["clone"] = (128, "fatal: could not read from remote")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "ghp_u", "", True)
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_TEMPLATE_CLONE_FAILED"


@pytest.mark.asyncio
async def test_push_failure_maps_to_502_without_secret(fork_env):
    f2o, gh, git_calls, state = fork_env
    _template_scripts()
    gh.instances["ghp_u"] = {
        "repos": {"alice/brain": _FakeRepoInfo(True)},
        "branches": {"alice/brain": []},
    }
    state["push"] = (1, "error: failed to push some refs")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "ghp_u", "", True)
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_PUSH_FAILED"
    assert "ghp_u" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_failure_error_is_scrubbed(fork_env):
    f2o, gh, git_calls, _ = fork_env
    _template_scripts()
    from services.github_service import GitHubCreateResult
    gh.instances["ghp_secret"] = {
        "validate_token": (True, "alice"),
        "repos": {"alice/brain": _FakeRepoInfo(False)},
        "branches": {},
        "create_result": GitHubCreateResult(
            success=False, error="denied for token ghp_secret (scopes)"),
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "ghp_secret", "", True)
    assert exc.value.detail["code"] == "FORK_REPO_CREATE_FAILED"
    assert "ghp_secret" not in str(exc.value.detail)


def test_scrub_secret_covers_plain_and_b64():
    f2o = _load_fork_module()
    import base64
    secret = "ghp_abc123"
    b64 = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
    text = f"url https://x:{secret}@host and header basic {b64} end"
    out = f2o.scrub_secret(text, secret)
    assert secret not in out and b64 not in out


def test_git_auth_env_shape():
    f2o = _load_fork_module()
    assert f2o._git_auth_env("") == {}
    env = f2o._git_auth_env("tok")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: basic ")
    assert "tok" not in env["GIT_CONFIG_VALUE_0"]  # b64-encoded, not raw


# ---------------------------------------------------------------------------
# crud.create_agent_internal wiring
# ---------------------------------------------------------------------------


class _NotFound(Exception):
    pass


def _load_crud(docker_available=True):
    """Import services.agent_service.crud with heavy deps mocked.

    All sibling submodules of the package are mocked (the package __init__
    imports them eagerly); crud + fork_to_own stay real. Every mock goes in
    via patch.dict (auto-restored) — never sys.modules.setdefault (#1446).
    """
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    docker_service = MagicMock()
    docker_service.docker_client = MagicMock() if docker_available else None
    docker_service.get_agent_by_name = MagicMock(return_value=None)
    docker_service.get_next_available_port = MagicMock(return_value=2222)
    docker_service.get_agent_status_from_container = MagicMock(return_value=MagicMock())

    docker_utils = MagicMock()
    docker_utils.volume_get = AsyncMock()
    docker_utils.volume_create = AsyncMock()
    docker_utils.containers_run = AsyncMock(return_value=MagicMock())

    template_service = MagicMock()
    template_service.generate_credential_files = MagicMock(return_value={})

    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    git_service.DEFAULT_DATA_PATHS = []
    git_service.reserve_and_generate_instance_id = AsyncMock(
        return_value=("iid-1", "main"))
    git_service.materialize_persistent_state = AsyncMock()
    git_service.materialize_data_paths = AsyncMock()

    settings_service = MagicMock()
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant-key")
    settings_service.get_github_pat = MagicMock(return_value="platform-pat")
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"})
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)

    helpers_mod = MagicMock()
    helpers_mod.validate_base_image = MagicMock()
    helpers_mod.is_claude_runtime = MagicMock(return_value=False)  # skip sub assign
    helpers_mod.validate_runtime = MagicMock()

    lifecycle_mod = MagicMock()
    lifecycle_mod.RESTRICTED_CAPABILITIES = []
    lifecycle_mod.FULL_CAPABILITIES = []

    capabilities_mod = MagicMock()
    capabilities_mod.AGENT_TMPFS_MOUNT = {"/tmp": "size=512m"}
    capabilities_mod.AGENT_DEFAULT_TMPDIR = "/home/developer/.tmp"
    capabilities_mod.normalize_cpu = MagicMock(side_effect=lambda v, d: v or d)
    capabilities_mod.normalize_memory = MagicMock(side_effect=lambda v, d: v or d)

    agent_auth = MagicMock()
    agent_auth.derive_agent_token = MagicMock(return_value="agent-auth-token")

    database_mod = MagicMock()
    db = database_mod.db
    db.get_agent_owner.return_value = None
    db.is_agent_name_reserved.return_value = False
    db.get_agents_by_owner.return_value = []
    db.get_guardrails_config.return_value = None
    db.create_agent_mcp_api_key.return_value = MagicMock(
        api_key="trinity_mcp_test", key_prefix="trinity_mcp_te")
    db.get_git_config_agent_names_for_repo.return_value = []
    db.set_agent_github_pat.return_value = True
    db.get_shared_folder_config.return_value = None
    db.get_file_sharing_enabled.return_value = False
    db.grant_default_permissions.return_value = 0
    db.register_agent_owner.return_value = None

    pkg = "services.agent_service"
    sibling_mocks = {}
    for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                "files", "folders", "helpers", "lifecycle", "mcp_tool_names",
                "metrics", "permissions", "queue", "read_only", "stats",
                "terminal", "capabilities"]:
        mod = {
            "helpers": helpers_mod, "lifecycle": lifecycle_mod,
            "capabilities": capabilities_mod,
        }.get(sib, MagicMock())
        sibling_mocks[f"{pkg}.{sib}"] = mod

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": database_mod,
        "services.docker_service": docker_service,
        "services.docker_utils": docker_utils,
        "services.template_service": template_service,
        "services.git_service": git_service,
        "services.settings_service": settings_service,
        "services.agent_auth": agent_auth,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    # Purge every real `services*` module not explicitly mocked — an earlier
    # plain import (the orchestrator tests) leaves the real package bound with
    # the REAL git_service/template_service as attributes, and crud's
    # `from services import git_service` would resolve the stale attribute
    # instead of the sys.modules mock. patch.dict restores the full original
    # dict on stop(), so the deletions are test-scoped.
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            del sys.modules[key]
    import services.agent_service.crud as crud
    return crud, {
        "patcher": patcher,
        "db": db,
        "docker_utils": docker_utils,
        "template_service": template_service,
        "git_service": git_service,
        "settings_service": settings_service,
    }


@pytest.fixture
def crud_env():
    crud, ctx = _load_crud()
    try:
        yield crud, ctx
    finally:
        # patch.dict.stop() clears sys.modules and re-populates it from the
        # pre-start snapshot, so it already UNDOES the in-context purge at
        # _load_crud (lines above) and restores the real `services.agent_service.*`
        # modules other test files bound at collection time. Do NOT manually delete
        # them afterward: that leaves e.g. `services.agent_service.stats` absent, so
        # a later file's module-level `from services.agent_service import stats`
        # reference goes stale (a fresh re-import returns a different object) and its
        # monkeypatch silently misses — the #73 stats-cache tests failed exactly this
        # way under full-suite ordering. Let patch.dict own the restoration.
        ctx["patcher"].stop()


def _user():
    u = MagicMock()
    u.username = "eugene"
    u.role = "creator"
    return u


def _fork_config(name, template="github:Abilityai/cornelius", **fork_kwargs):
    fork = {
        "destination_repo": "alice/brain",
        "github_pat": "ghp_userpat123",
        "private": True,
    }
    fork.update(fork_kwargs)
    return AgentConfig(name=name, template=template, fork_to_own=fork)


def _script_github_template(ctx, fork_to_own_meta=None):
    ctx["template_service"].get_github_template.return_value = {
        "github_repo": "Abilityai/cornelius",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "fork_to_own": fork_to_own_meta,
    }


def _patch_repo_validation(monkeypatch, crud, default_branch="main"):
    """crud re-validates the (destination) repo with GitHubService."""
    class _CrudFakeGH:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            return _FakeRepoInfo(True, default_branch)

    monkeypatch.setattr(crud, "GitHubService", _CrudFakeGH)


@pytest.mark.asyncio
async def test_fork_with_local_template_rejected(crud_env):
    crud, ctx = crud_env
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _fork_config("f2o-local", template="local:scout"), _user(), MagicMock())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_REQUIRES_GITHUB_TEMPLATE"


@pytest.mark.asyncio
async def test_fork_with_at_branch_rejected(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _fork_config("f2o-branch", template="github:Abilityai/cornelius@dev"),
            _user(), MagicMock())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_BRANCH_UNSUPPORTED"


@pytest.mark.asyncio
async def test_required_template_without_fork_rejected(crud_env):
    crud, ctx = crud_env
    _script_github_template(ctx, fork_to_own_meta="required")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="f2o-req", template="github:Abilityai/cornelius"),
            _user(), MagicMock())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_TO_OWN_REQUIRED"


@pytest.mark.asyncio
async def test_destination_bound_to_live_agent_rejected(crud_env):
    crud, ctx = crud_env
    _script_github_template(ctx)
    ctx["db"].get_git_config_agent_names_for_repo.return_value = ["existing-agent"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _fork_config("f2o-bound"), _user(), MagicMock())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FORK_DESTINATION_IN_USE"
    assert "existing-agent" in exc.value.detail["error"]


@pytest.mark.asyncio
async def test_fork_deep_slice_env_and_pat_persist(crud_env, monkeypatch):
    """The E1 killer test: plain token in container env, never '**********'."""
    crud, ctx = crud_env
    _script_github_template(ctx, fork_to_own_meta="required")
    _patch_repo_validation(monkeypatch, crud)

    from services.agent_service.fork_to_own import ForkToOwnResult
    fork_mock = AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False))
    monkeypatch.setattr(crud, "fork_template_to_own_repo", fork_mock)

    await crud.create_agent_internal(
        _fork_config("f2o-deep"), _user(), MagicMock())

    # Orchestrator received the UNWRAPPED token and the platform read PAT
    call = fork_mock.call_args.kwargs
    assert call["user_pat"] == "ghp_userpat123"
    assert isinstance(call["user_pat"], str)
    assert call["read_pat"] == "platform-pat"
    assert call["template_repo"] == "Abilityai/cornelius"
    assert call["private"] is True

    # Container env: plain token (a masked '**********' here = silent empty
    # agent), origin = destination, upstream = template, auto-sync on
    env = ctx["docker_utils"].containers_run.call_args.kwargs["environment"]
    assert env["GITHUB_PAT"] == "ghp_userpat123"
    assert env["GITHUB_REPO"] == "alice/brain"
    assert env["GIT_UPSTREAM_REPO"] == "Abilityai/cornelius"
    assert env["GIT_SYNC_AUTO"] == "true"
    assert env["GIT_SOURCE_MODE"] == "true"
    assert env["GIT_SOURCE_BRANCH"] == "main"

    # Per-agent PAT (#347) persisted with the plain token
    pat_call = ctx["db"].set_agent_github_pat.call_args
    assert pat_call.args == ("f2o-deep", "ghp_userpat123")

    # Reservation ran against the DESTINATION in source mode
    reserve = ctx["git_service"].reserve_and_generate_instance_id.call_args.kwargs
    assert reserve["github_repo"] == "alice/brain"
    assert reserve["source_mode"] is True

    # Fork-to-own source-mode agents get the auto-sync heartbeat flag
    ctx["db"].set_git_auto_sync_enabled.assert_called_once_with("f2o-deep", True)


@pytest.mark.asyncio
async def test_fork_pat_persist_failure_rolls_back(crud_env, monkeypatch):
    """set_agent_github_pat=False → 500 + reserved row and MCP key rolled back."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)

    from services.agent_service.fork_to_own import ForkToOwnResult
    monkeypatch.setattr(
        crud, "fork_template_to_own_repo",
        AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False)))
    ctx["db"].set_agent_github_pat.return_value = False

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _fork_config("f2o-rollback"), _user(), MagicMock())
    assert exc.value.status_code == 500
    ctx["db"].delete_git_config.assert_called_once_with("f2o-rollback")
    ctx["db"].delete_agent_mcp_api_key.assert_called_once_with("f2o-rollback")
    # The container was never created
    ctx["docker_utils"].containers_run.assert_not_called()


@pytest.mark.asyncio
async def test_non_fork_github_creation_unchanged(crud_env, monkeypatch):
    """Regression: plain github-template creation keeps platform-PAT behavior."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)

    await crud.create_agent_internal(
        AgentConfig(name="plain-gh", template="github:Abilityai/cornelius"),
        _user(), MagicMock())

    env = ctx["docker_utils"].containers_run.call_args.kwargs["environment"]
    assert env["GITHUB_PAT"] == "platform-pat"
    assert "GIT_UPSTREAM_REPO" not in env
    # source_mode default True → no auto-sync for non-fork agents
    assert "GIT_SYNC_AUTO" not in env
    ctx["db"].set_agent_github_pat.assert_not_called()
    ctx["db"].set_git_auto_sync_enabled.assert_not_called()


@pytest.mark.asyncio
async def test_repo_visibility_timeout_maps_to_502(fork_env, monkeypatch):
    f2o, gh, git_calls, _ = fork_env
    monkeypatch.setattr(f2o, "CREATE_VISIBILITY_TIMEOUT_S", 0.05)

    class _NeverVisible:
        exists = False
    gh.instances["ghp_u"] = {"repos": {"alice/brain": _NeverVisible()}, "branches": {}}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o._wait_for_repo_visible(gh("ghp_u"), "alice/brain")
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_REPO_NOT_VISIBLE"


@pytest.mark.asyncio
async def test_push_visibility_timeout_maps_to_502(fork_env, monkeypatch):
    f2o, gh, git_calls, state = fork_env
    monkeypatch.setattr(f2o, "PUSH_VISIBILITY_TIMEOUT_S", 0.05)
    state["ls-remote"] = (2, "")  # git plane never sees the ref
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await f2o._wait_for_branch_on_git_plane("alice/brain", "main", "ghp_u")
    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "FORK_PUSH_NOT_VISIBLE"


def test_database_facade_delegates_repo_binding_lookup():
    """C1 regression: the facade must delegate the new accessor — a missing
    delegation is an AttributeError on the first fork-to-own create, invisible
    to the crud tests (they mock the whole database module)."""
    import importlib
    database = importlib.import_module("database")
    mgr = database.DatabaseManager.__new__(database.DatabaseManager)  # skip __init__
    mgr._schedule_ops = MagicMock()
    mgr._schedule_ops.get_git_config_agent_names_for_repo.return_value = ["a1"]
    assert mgr.get_git_config_agent_names_for_repo("alice/brain") == ["a1"]
    mgr._schedule_ops.get_git_config_agent_names_for_repo.assert_called_once_with(
        "alice/brain")


@pytest.mark.asyncio
async def test_fork_destination_race_loser_rolls_back(crud_env, monkeypatch):
    """C2: two concurrent creates to one destination — the reservation-time
    re-check makes everyone but the lexicographically-first name roll back."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    from services.agent_service.fork_to_own import ForkToOwnResult
    monkeypatch.setattr(
        crud, "fork_template_to_own_repo",
        AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False)))
    # Pre-check sees an empty binding; post-reserve check sees the rival too
    ctx["db"].get_git_config_agent_names_for_repo.side_effect = [
        [], ["a-rival", "z-loser"],
    ]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _fork_config("z-loser"), _user(), MagicMock())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FORK_DESTINATION_IN_USE"
    ctx["db"].delete_git_config.assert_called_once_with("z-loser")
    ctx["docker_utils"].containers_run.assert_not_called()


@pytest.mark.asyncio
async def test_fork_destination_race_winner_proceeds(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    from services.agent_service.fork_to_own import ForkToOwnResult
    monkeypatch.setattr(
        crud, "fork_template_to_own_repo",
        AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False)))
    ctx["db"].get_git_config_agent_names_for_repo.side_effect = [
        [], ["a-winner", "z-rival"],
    ]
    await crud.create_agent_internal(_fork_config("a-winner"), _user(), MagicMock())
    ctx["db"].delete_git_config.assert_not_called()
    ctx["docker_utils"].containers_run.assert_called_once()


# ---------------------------------------------------------------------------
# template_service._build_template surfaces the new metadata
# ---------------------------------------------------------------------------


def test_build_template_surfaces_fork_to_own_and_tagline():
    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    with patch.dict("sys.modules", {
        "services.git_service": git_service,
        "database": MagicMock(),
    }):
        for key in list(sys.modules.keys()):
            if key.startswith("services.template_service"):
                del sys.modules[key]
        import services.template_service as ts
        built = ts._build_template(
            "Abilityai/cornelius",
            {"display_name": "Cornelius", "fork_to_own": "required",
             "tagline": "Your second brain"},
        )
        assert built["fork_to_own"] == "required"
        assert built["tagline"] == "Your second brain"
        # Templates without the keys default to falsy (no featured card)
        plain = ts._build_template("a/b", {})
        assert plain["fork_to_own"] is None
        assert plain["tagline"] == ""


# ---------------------------------------------------------------------------
# startup.sh upstream-remote seam (static — e2e covers behavior)
# ---------------------------------------------------------------------------


def test_startup_sh_adds_upstream_remote_in_both_paths():
    content = (_PROJECT_ROOT / "docker" / "base-image" / "startup.sh").read_text()
    assert content.count('if [ -n "${GIT_UPSTREAM_REPO}" ]') == 2, (
        "upstream-remote block must exist in BOTH the fresh-clone and the "
        "restart path of startup.sh"
    )
    # Credential-less URL: composed from scheme+host, never embeds GITHUB_PAT
    for line in content.splitlines():
        if "UPSTREAM_URL=" in line:
            assert "GITHUB_PAT" not in line
            assert "${GIT_SCHEME}://${GIT_HOST_PATH}/${GIT_UPSTREAM_REPO}.git" in line
