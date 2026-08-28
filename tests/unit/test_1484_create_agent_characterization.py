"""
Characterization tests for `create_agent_internal` (#1484).

This suite pins the CURRENT observable behavior of
`services.agent_service.crud.create_agent_internal` BEFORE it is decomposed
into named phase-helpers, so the pure-move refactor can be proven
byte-identical. Every case asserts a *real, observable* effect — the exact
kwargs handed to `containers_run`, which `db.*` / ephemeral-service mocks were
and were NOT called, the mutated `config.name` — never merely "did not raise"
(a mocked-unit test pins call-sequence, not runtime behavior).

Case map (→ plan §3b of .plan/issue-1484.md):
  1  local-template happy path (network/security kwargs + bind mounts)
  2  github-template happy path (predefined + dynamic; full env key-set)
  3  system-agent / Cornelius `request=None` (no NameError)
  4  no-template path (no github env, no git reservation)
  5  Claude-runtime subscription auto-assign (+ non-Claude skip)
  6  ephemeral SUCCESS end-to-end — cross-phase name mutation + ordering
  7  ephemeral gate ORDER (earliest-wins under overlap)
  8  per-agent PAT tier persistence (global / per_user / fork)
  9  volume-base guard #1664 ordering (precedes the #1667 probe)
  10 leftover-volume adopt #1667 (refuse vs adopt)
  11 existence guard (three truthy sources)
  12 role quota (429)
  13 runtime validation (propagates before container)
  14 non-fatal side-effect failures still return 200
  15-18 rollback matrix (fail-after-container; each guard exercised alone)
  19 MCP-key leak on ephemeral 429  — PINS A KNOWN LEAK
  20 docker-unavailable else leak    — PINS A KNOWN LEAK
  21 fork destination race (inline rollback)

Harness: copied + extended from `test_fork_to_own.py::_load_crud` (D6 — NOT
shared cross-file; that reintroduces a documented sys.modules/services.* leak).

Target: src/backend/services/agent_service/crud.py
Issue:  abilityai/trinity#1484
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_1484_create_agent.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models import AgentConfig  # noqa: E402


class _NotFound(Exception):
    """Stand-in for docker.errors.NotFound (the mocked docker module)."""


class _FakeRepoInfo:
    def __init__(self, exists=True, default_branch="main", private=True):
        self.exists = exists
        self.default_branch = default_branch
        self.private = private


# ---------------------------------------------------------------------------
# Harness — import crud with every heavy dep mocked.
#
# Extends test_fork_to_own.py::_load_crud to cover the paths that harness never
# touched (crud drifted past it): the tuple-returning `resolve_github_pat`
# (#162), the ephemeral pre-gates/reserve (ent#69), entitlement + rate-limiter,
# and pull-mode. All mocks go in via `patch.dict("sys.modules", ...)`
# (auto-restored, lint-clean #1446); the whole `db` object is swapped, never
# `setattr(database.db, ...)` (the #test_904 trap).
# ---------------------------------------------------------------------------


def _load_crud(monkeypatch, docker_available=True):
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    docker_service = MagicMock()
    docker_service.docker_client = MagicMock() if docker_available else None
    docker_service.get_agent_by_name = MagicMock(return_value=None)
    docker_service.get_next_available_port = MagicMock(return_value=2222)
    docker_service.get_agent_status_from_container = MagicMock(
        return_value=MagicMock())

    docker_utils = MagicMock()
    # NotFound is the truthful default for the fresh names these tests create
    # (a bare AsyncMock would answer "volume exists" → #1667 refuses every
    # create). A test that wants a pre-existing volume overrides this.
    docker_utils.volume_get = AsyncMock(side_effect=_NotFound("no such volume"))
    docker_utils.volume_create = AsyncMock()
    docker_utils.containers_run = AsyncMock(return_value=MagicMock())
    # ent#313: the failed-creation rollback now removes the container.
    docker_utils.container_remove = AsyncMock()

    template_service = MagicMock()
    template_service.generate_credential_files = MagicMock(return_value={})
    # Dynamic-vs-predefined dispatch keys on this: a truthy return picks the
    # predefined branch. Default None ⇒ dynamic github:owner/repo path.
    template_service.get_github_template = MagicMock(return_value=None)
    # ent#14 S2: the creation path reads `template.yaml` through the
    # reason-preserving form and UNPACKS the pair — the `fork_to_own` gate
    # decides on the reason, so it can no longer be dropped. An unstubbed
    # MagicMock iterates empty and fails the unpack, which is the loud failure
    # we want rather than a Mock silently standing in for a security decision.
    template_service.fetch_template_metadata_result_for_create = MagicMock(
        return_value=({}, None)
    )
    # ent#14 S2: the `fork_to_own` gate calls this classifier, which crud
    # imports FROM this module — an unstubbed MagicMock returns a truthy Mock,
    # so every github create would 503 TEMPLATE_METADATA_UNAVAILABLE. Mirrored
    # faithfully (real: services/template_service.py::metadata_reason_is_unreadable
    # — a clean 404 is absence, everything else is unreadable) rather than
    # pinned to False, so a case that DOES script an unreadable reason still
    # exercises the refusal.
    template_service.metadata_reason_is_unreadable = MagicMock(
        side_effect=lambda reason: bool(reason) and not reason.startswith("HTTP 404")
    )
    # ent#128: `_resolve_local_template` reads the template's declared MCP servers
    # through this tolerant accessor instead of reaching through `credentials:`
    # raw (a null/list/string block raised AttributeError, and that read sits
    # FIRST in a run of `config` mutations under one broad `except` — so it cost
    # the agent its `runtime:` and `shared_folders:` too). This harness MagicMocks
    # the whole module, so an unstubbed call returns a truthy Mock that lands in
    # `config.mcp_servers` and later blows up in a `yaml.dump`. Stubbed with a
    # faithful mirror rather than a fixed `[]` so a fixture that DOES declare
    # credentials cannot be silently masked.
    def _mcp_server_names(block):
        servers = block.get("mcp_servers") if isinstance(block, dict) else None
        return [str(n) for n in servers] if isinstance(servers, dict) else []

    template_service.credential_mcp_server_names = MagicMock(
        side_effect=_mcp_server_names
    )

    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    git_service.DEFAULT_DATA_PATHS = []
    git_service.reserve_and_generate_instance_id = AsyncMock(
        return_value=("iid-1", "main"))
    git_service.materialize_persistent_state = AsyncMock()
    git_service.materialize_data_paths = AsyncMock()

    settings_service = MagicMock()
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant-key")
    # #162 (17d0c8ef): crud now calls resolve_github_pat(owner_id=...) which
    # returns a (pat, tier) 2-tuple — the retired get_github_pat stub the fork
    # harness carried would ValueError on unpack. Default = global tier.
    settings_service.resolve_github_pat = MagicMock(
        return_value=("platform-pat", "global"))
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"})
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)
    settings_service.get_ephemeral_agent_quota = MagicMock(return_value=5)
    settings_service.get_ephemeral_ttl_ceiling_seconds = MagicMock(
        return_value=86400)

    entitlement_mod = MagicMock()
    # Real OSS entitlement_service also returns False on an empty registry; the
    # un-entitled 403 is the correct default. Entitled cases override per-test.
    entitlement_mod.entitlement_service.is_entitled = MagicMock(return_value=False)

    rate_limiter_mod = MagicMock()
    rate_limiter_mod.enforce = MagicMock()

    ephemeral_mod = MagicMock()
    ephemeral_mod.try_reserve_ephemeral_slot = MagicMock(return_value=True)
    ephemeral_mod.release_ephemeral_slot = MagicMock()

    pull_mode_mod = MagicMock()
    pull_mode_mod.pull_mode_env_vars = MagicMock(return_value={})

    runtime_state_mod = MagicMock()
    runtime_state_mod.clear_agent_breakers = MagicMock()
    # ent#313: cleared on the failed-creation path (async).
    runtime_state_mod.clear_agent_runtime_state = AsyncMock()

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
    db.is_volume_base_reserved.return_value = False
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
    # Ephemeral / registration / rollback stubs (crud drifted past the fork
    # harness — an unstubbed MagicMock attr is a truthy auto-child, which reads
    # wrong on the ephemeral-recursion + owner-resolution paths).
    db.get_agent_ephemeral_info.return_value = None
    db.get_user_by_username.return_value = {"id": 7}
    db.get_agent_mcp_api_key.return_value = None
    db.list_assignable_subscriptions.return_value = []  # skip auto-assign (#2409)
    db.get_subscription_token.return_value = None
    db.add_agent_permission.return_value = None
    db.assign_subscription_to_agent.return_value = None
    db.set_default_avatar.return_value = None
    db.set_git_auto_sync_enabled.return_value = None
    db.delete_git_config.return_value = None
    db.delete_agent_mcp_api_key.return_value = None
    db.upsert_shared_folder_config.return_value = None

    pkg = "services.agent_service"
    sibling_mocks = {}
    # `ephemeral` + `pull_mode` are NOT imported by the package __init__ (crud
    # imports them directly), but they must be mocked or the ephemeral paths hit
    # real Redis / the deferred pull_mode import resolves real.
    for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                "files", "folders", "helpers", "lifecycle", "mcp_tool_names",
                "metrics", "permissions", "queue", "read_only", "stats",
                "terminal", "capabilities", "ephemeral", "pull_mode"]:
        mod = {
            "helpers": helpers_mod, "lifecycle": lifecycle_mod,
            "capabilities": capabilities_mod, "ephemeral": ephemeral_mod,
            "pull_mode": pull_mode_mod,
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
        "services.entitlement_service": entitlement_mod,
        "services.rate_limiter": rate_limiter_mod,
        "services.agent_runtime_state": runtime_state_mod,
        "services.agent_auth": agent_auth,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    # Purge every real `services*` module not explicitly mocked so crud's
    # `from services import git_service` resolves the sys.modules mock, not a
    # stale attribute of a previously-imported real package. `monkeypatch.delitem`
    # (not a bare `del sys.modules[...]`, tests/lint_sys_modules.py) so the removal
    # is undone at teardown; `patch.dict.stop()` also restores the full snapshot,
    # so both finalizers converge on the pristine dict.
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.agent_service.crud as crud

    # #1793: an unresolvable `local:` template is now a 404 instead of a
    # silent templateless creation. These cases exercise the local-template
    # HAPPY path with `local:scout`, and passed previously only because the
    # unit env has no template roots on disk — the missing template.yaml was
    # tolerated. Point the roots at a tmp dir carrying a minimal `scout` so
    # the fixture supplies the template the cases always assumed. Deliberately
    # minimal (no `resources`, no `avatar_prompt`) so the pinned container
    # kwargs — mem_limit 4g / nano_cpus 2 — are unchanged, and so
    # set_default_avatar stays unreached for every case but the one that
    # builds its own root. Cases that monkeypatch `_LOCAL_TEMPLATE_ROOTS`
    # themselves still win: their setattr runs after this.
    # `.resolve()` is required, not cosmetic: on macOS `tempfile.mkdtemp()`
    # returns `/var/folders/...`, but `/var` is a symlink to `/private/var`, so
    # `_safe_local_template_path`'s `(root / name).resolve()` yields
    # `/private/var/...` and `is_relative_to(root)` fails — every local: case
    # 400s `INVALID_LOCAL_TEMPLATE_NAME`. Linux CI has no such symlink, which is
    # why #1793 shipped green while these 11 cases were red on every Mac.
    _tpl_root = Path(tempfile.mkdtemp(prefix="trinity_1484_templates_")).resolve()
    _scout = _tpl_root / "scout"
    _scout.mkdir()
    (_scout / "template.yaml").write_text("type: business-assistant\n")
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (_tpl_root, _tpl_root))

    return crud, {
        "patcher": patcher,
        "db": db,
        "docker_service": docker_service,
        "docker_utils": docker_utils,
        "template_service": template_service,
        "git_service": git_service,
        "settings_service": settings_service,
        "ephemeral": ephemeral_mod,
        "rate_limiter": rate_limiter_mod,
        "runtime_state": runtime_state_mod,
    }


@pytest.fixture
def crud_env(monkeypatch):
    crud, ctx = _load_crud(monkeypatch, docker_available=True)
    try:
        yield crud, ctx
    finally:
        # Let patch.dict own the restoration (see the fork harness comment on
        # why manual deletion strands sibling modules for later files).
        ctx["patcher"].stop()


@pytest.fixture
def crud_env_no_docker(monkeypatch):
    crud, ctx = _load_crud(monkeypatch, docker_available=False)
    try:
        yield crud, ctx
    finally:
        ctx["patcher"].stop()


# ---------------------------------------------------------------------------
# Config / user builders
# ---------------------------------------------------------------------------


def _user(agent_name=None, uid=7, username="eugene", role="creator"):
    u = MagicMock()
    u.id = uid
    u.username = username
    u.role = role
    u.agent_name = agent_name  # explicit None ⇒ spawn-provenance branches skip
    return u


def _local_config(name="loc-agent", **kw):
    return AgentConfig(name=name, template="local:scout", **kw)


def _github_config(name="gh-agent", template="github:Abilityai/cornelius", **kw):
    return AgentConfig(name=name, template=template, **kw)


def _script_github_template(ctx, fork_to_own_meta=None):
    """Make get_github_template return a predefined template (truthy)."""
    ctx["template_service"].get_github_template.return_value = {
        "github_repo": "Abilityai/cornelius",
        "resources": {"cpu": "2", "memory": "4g"},
        "mcp_servers": [],
        "fork_to_own": fork_to_own_meta,
    }


def _patch_repo_validation(monkeypatch, crud, default_branch="main"):
    """crud re-validates the repo via GitHubService(pat).check_repo_exists —
    services.github_service is NOT mocked, so without this every github case
    makes a real (silently-swallowed) network call and half-exercises the path.
    """
    class _CrudFakeGH:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            return _FakeRepoInfo(True, default_branch)

    monkeypatch.setattr(crud, "GitHubService", _CrudFakeGH)


def _agent_run_kwargs(ctx):
    """The kwargs of the containers_run call that created the AGENT container
    (detach=True) — never the throwaway alpine chown runs."""
    for call in ctx["docker_utils"].containers_run.call_args_list:
        if call.kwargs.get("detach"):
            return call.kwargs
    raise AssertionError("agent container was never created")


# ===========================================================================
# Case 1 — local template happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_case1_local_template_happy_path(crud_env):
    crud, ctx = crud_env
    await crud.create_agent_internal(_local_config("loc-happy"), _user(), None)

    kw = _agent_run_kwargs(ctx)
    # AC #5 + security kwargs are byte-identical
    assert kw["network"] == "trinity-agent-network"
    assert kw["cap_drop"] == ["ALL"]
    assert kw["tmpfs"] == {"/tmp": "size=512m"}
    assert kw["detach"] is True
    assert kw["name"] == "agent-loc-happy"
    assert kw["mem_limit"] == "4g"
    assert kw["nano_cpus"] == 2 * 1_000_000_000

    # S8: the config + credential binds AND the workspace mount survive
    binds = {v["bind"] for v in kw["volumes"].values()}
    assert {"/config/agent-config.yaml", "/config/credentials.json",
            "/home/developer"} <= binds
    assert "agent-loc-happy-workspace" in kw["volumes"]

    # Durable side effects fired
    ctx["docker_utils"].volume_create.assert_awaited()  # workspace volume
    ctx["db"].register_agent_owner.assert_called_once()
    ctx["git_service"].materialize_persistent_state.assert_awaited_once()
    ctx["git_service"].materialize_data_paths.assert_awaited_once()


# ===========================================================================
# Case 2 — github template happy path (predefined + dynamic), full env key-set
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("predefined", [True, False])
async def test_case2_github_template_full_env(crud_env, monkeypatch, predefined):
    crud, ctx = crud_env
    if predefined:
        _script_github_template(ctx)
        template = "github:Abilityai/cornelius"
    else:
        # dynamic: get_github_template default None ⇒ owner/repo path
        template = "github:someowner/somerepo"
    _patch_repo_validation(monkeypatch, crud)

    await crud.create_agent_internal(
        _github_config("gh-full", template=template), _user(uid=7), None)

    # PAT resolver keyed on ownership only; git branch reserved
    ctx["settings_service"].resolve_github_pat.assert_called_once_with(owner_id=7)
    ctx["git_service"].reserve_and_generate_instance_id.assert_awaited_once()

    # S8: assert base AND github env keys simultaneously (a subset assertion
    # would miss an env sub-phase ordering/aliasing bug).
    env = _agent_run_kwargs(ctx)["environment"]
    for key in ("AGENT_NAME", "AGENT_RUNTIME", "TMPDIR",
                "TRINITY_AGENT_AUTH_TOKEN", "TRINITY_MCP_API_KEY",
                "GITHUB_REPO", "GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN",
                "GIT_SYNC_ENABLED"):
        assert key in env, f"missing env key {key}"
    assert env["GITHUB_PAT"] == "platform-pat"
    assert env["GH_TOKEN"] == "platform-pat"
    assert env["GITHUB_TOKEN"] == "platform-pat"
    assert env["GIT_SYNC_ENABLED"] == "true"
    # source_mode default True ⇒ no working-branch autopush heartbeat
    assert env["GIT_SOURCE_MODE"] == "true"
    ctx["db"].set_git_auto_sync_enabled.assert_not_called()


@pytest.mark.asyncio
async def test_case2_github_non_source_mode_enables_autosync(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)

    await crud.create_agent_internal(
        _github_config("gh-legacy", source_mode=False), _user(), None)

    env = _agent_run_kwargs(ctx)["environment"]
    assert env["GIT_SYNC_AUTO"] == "true"
    assert env["GIT_WORKING_BRANCH"] == "main"
    assert "GIT_SOURCE_MODE" not in env
    ctx["db"].set_git_auto_sync_enabled.assert_called_once_with("gh-legacy", True)


# ===========================================================================
# Case 3 — system-agent / Cornelius seed (request=None)
# ===========================================================================


@pytest.mark.asyncio
async def test_case3_request_none_does_not_break(crud_env):
    crud, ctx = crud_env
    # Cornelius seeder + systems deploy call with request=None (no live request).
    await crud.create_agent_internal(_local_config("sys-seed"), _user(), request=None)
    assert _agent_run_kwargs(ctx)["name"] == "agent-sys-seed"


# ===========================================================================
# Case 4 — no template
# ===========================================================================


@pytest.mark.asyncio
async def test_case4_no_template_no_github_env(crud_env):
    crud, ctx = crud_env
    await crud.create_agent_internal(
        AgentConfig(name="bare"), _user(), None)

    env = _agent_run_kwargs(ctx)["environment"]
    assert "GITHUB_REPO" not in env
    assert "GITHUB_PAT" not in env
    assert "GIT_SYNC_ENABLED" not in env
    ctx["git_service"].reserve_and_generate_instance_id.assert_not_awaited()


# ===========================================================================
# Case 5 — Claude-runtime subscription auto-assign (+ non-Claude skip)
# ===========================================================================


@pytest.mark.asyncio
async def test_case5_claude_subscription_auto_assign(crud_env, monkeypatch):
    crud, ctx = crud_env
    monkeypatch.setattr(crud, "is_claude_runtime", lambda runtime: True)
    ctx["db"].list_assignable_subscriptions.return_value = [MagicMock(
        id="sub-1", name="sub-a")]
    ctx["db"].get_subscription_token.return_value = "oauth-tok"

    await crud.create_agent_internal(_local_config("claude-a"), _user(), None)

    env = _agent_run_kwargs(ctx)["environment"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert "ANTHROPIC_API_KEY" not in env  # popped when a subscription is assigned
    ctx["db"].assign_subscription_to_agent.assert_called_once_with(
        "claude-a", "sub-1")


@pytest.mark.asyncio
async def test_case5_non_claude_runtime_skips_subscription(crud_env):
    crud, ctx = crud_env  # is_claude_runtime defaults False
    await crud.create_agent_internal(_local_config("gem-a"), _user(), None)

    env = _agent_run_kwargs(ctx)["environment"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    ctx["db"].assign_subscription_to_agent.assert_not_called()


# ===========================================================================
# Case 6 — ephemeral SUCCESS end-to-end: cross-phase name mutation + ordering
# ===========================================================================


@pytest.mark.asyncio
async def test_case6_ephemeral_success_name_mutation_and_ordering(
        crud_env, monkeypatch):
    crud, ctx = crud_env
    monkeypatch.setattr(crud.entitlement_service, "is_entitled", lambda feat: True)
    monkeypatch.setattr(crud.secrets, "token_hex", lambda n: "deadbeef")

    # Record clear_agent_breakers-vs-containers_run ordering (#1560).
    order = []
    monkeypatch.setattr(
        crud, "clear_agent_breakers", lambda n: order.append(("clear", n)))

    async def _run(*a, **k):
        order.append("run")
        return MagicMock()
    ctx["docker_utils"].containers_run.side_effect = _run

    cfg = AgentConfig(name="ghost", template=None, ephemeral={"max_executions": 5})
    await crud.create_agent_internal(cfg, _user(), None)

    # The suffixed name threads to every downstream sink.
    assert cfg.name == "ghost-deadbeef"
    ctx["db"].create_agent_mcp_api_key.assert_called_once()
    assert ctx["db"].create_agent_mcp_api_key.call_args.kwargs[
        "agent_name"] == "ghost-deadbeef"
    reg = ctx["db"].register_agent_owner.call_args
    assert reg.args[0] == "ghost-deadbeef"
    assert reg.kwargs["is_ephemeral"] is True
    assert reg.kwargs["max_parallel_tasks"] == 1

    # Ghost is volume-less: the #1664 gate is never consulted and no workspace
    # volume is created.
    ctx["db"].is_volume_base_reserved.assert_not_called()
    ctx["docker_utils"].volume_create.assert_not_awaited()

    # Breakers cleared (under the suffixed name) BEFORE the container exists.
    assert order == [("clear", "ghost-deadbeef"), "run"]


# ===========================================================================
# Case 7 — ephemeral gate ORDER (earliest wins under overlap)
# ===========================================================================


def _entitle(monkeypatch, crud, value=True):
    monkeypatch.setattr(crud.entitlement_service, "is_entitled", lambda feat: value)


@pytest.mark.asyncio
async def test_case7_gate_unentitled_403(crud_env):
    crud, ctx = crud_env  # is_entitled default False
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="g", ephemeral={"ttl_seconds": 60}), _user(), None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ephemeral_not_entitled"


@pytest.mark.asyncio
async def test_case7_gate_fork_conflict_400(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    cfg = AgentConfig(
        name="g", template="github:x/y",
        fork_to_own={"destination_repo": "a/b", "github_pat": "ghp_x"},
        ephemeral={"ttl_seconds": 60})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(cfg, _user(), None)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "ephemeral_fork_to_own_conflict"


@pytest.mark.asyncio
async def test_case7_gate_spawn_recursion_403(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    ctx["db"].get_agent_ephemeral_info.return_value = {"is_ephemeral": True}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="g", ephemeral={"ttl_seconds": 60}),
            _user(agent_name="parent-ghost"), None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ephemeral_spawn_recursion"


@pytest.mark.asyncio
async def test_case7_gate_spawn_rate_limit_429(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    ctx["db"].get_agent_ephemeral_info.return_value = {"is_ephemeral": False}
    from fastapi import HTTPException
    ctx["rate_limiter"].enforce.side_effect = HTTPException(
        status_code=429, detail="rate")
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="g", ephemeral={"ttl_seconds": 60}),
            _user(agent_name="parent-durable"), None)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_case7_gate_ttl_exceeds_ceiling_400(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    ctx["settings_service"].get_ephemeral_ttl_ceiling_seconds.return_value = 100
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="g", ephemeral={"ttl_seconds": 200}), _user(), None)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "ephemeral_ttl_exceeds_ceiling"


@pytest.mark.asyncio
async def test_case7_gate_ordering_unentitled_beats_fork(crud_env):
    """Overlap: un-entitled AND fork_to_own scripted ⇒ the earlier gate wins."""
    crud, ctx = crud_env  # is_entitled default False (un-entitled)
    cfg = AgentConfig(
        name="g", template="github:x/y",
        fork_to_own={"destination_repo": "a/b", "github_pat": "ghp_x"},
        ephemeral={"ttl_seconds": 60})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(cfg, _user(), None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ephemeral_not_entitled"


# ===========================================================================
# Case 8 — per-agent PAT tier persistence (#162 Decision 2)
# ===========================================================================


@pytest.mark.asyncio
async def test_case8_pat_global_tier_not_persisted(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    # default resolve_github_pat ⇒ ("platform-pat", "global")
    await crud.create_agent_internal(
        _github_config("gh-global"), _user(), None)
    ctx["db"].set_agent_github_pat.assert_not_called()


@pytest.mark.asyncio
async def test_case8_pat_per_user_tier_persisted(crud_env, monkeypatch):
    """The case a fork test can't cover — fork forces tier='fork'."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    monkeypatch.setattr(
        crud, "resolve_github_pat", lambda owner_id: ("mypat", "per_user"))
    await crud.create_agent_internal(
        _github_config("gh-peruser"), _user(), None)
    ctx["db"].set_agent_github_pat.assert_called_once_with("gh-peruser", "mypat")


@pytest.mark.asyncio
async def test_case8_pat_fork_tier_persisted(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx, fork_to_own_meta="required")
    _patch_repo_validation(monkeypatch, crud)
    from services.agent_service.fork_to_own import ForkToOwnResult
    monkeypatch.setattr(
        crud, "fork_template_to_own_repo",
        AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False)))
    cfg = _github_config(
        "gh-fork",
        fork_to_own={"destination_repo": "alice/brain",
                     "github_pat": "ghp_userpat", "private": True})
    await crud.create_agent_internal(cfg, _user(), None)
    ctx["db"].set_agent_github_pat.assert_called_once_with("gh-fork", "ghp_userpat")


# ===========================================================================
# Case 9 — volume-base guard #1664 ordering (precedes the #1667 probe)
# ===========================================================================


@pytest.mark.asyncio
async def test_case9_volume_base_reserved_409_before_1667_probe(crud_env):
    crud, ctx = crud_env
    ctx["db"].is_volume_base_reserved.return_value = True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_local_config("renamed"), _user(), None)
    assert exc.value.status_code == 409
    assert "belong to another agent" in exc.value.detail
    # The #1664 gate (line 298) precedes the #1667 volume probe (line 336).
    ctx["docker_utils"].volume_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_case9_volume_base_reserved_409_even_with_adopt(crud_env):
    crud, ctx = crud_env
    ctx["db"].is_volume_base_reserved.return_value = True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _local_config("renamed2"), _user(), None,
            adopt_existing_workspace=True)
    assert exc.value.status_code == 409
    assert "belong to another agent" in exc.value.detail


# ===========================================================================
# Case 10 — leftover-volume adopt #1667
# ===========================================================================


@pytest.mark.asyncio
async def test_case10_leftover_volume_refused(crud_env):
    crud, ctx = crud_env
    # volume_get succeeds ⇒ a leftover exists and nothing claims it. Mutate the
    # existing mock in place — crud bound `volume_get` as a name at import, so
    # reassigning the module attribute would not reach crud's reference.
    ctx["docker_utils"].volume_get.side_effect = None
    ctx["docker_utils"].volume_get.return_value = MagicMock()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_local_config("orphan-vol"), _user(), None)
    assert exc.value.status_code == 409
    assert "already exists and no agent" in exc.value.detail
    ctx["docker_utils"].containers_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_case10_leftover_volume_adopted(crud_env):
    crud, ctx = crud_env
    ctx["docker_utils"].volume_get.side_effect = None
    ctx["docker_utils"].volume_get.return_value = MagicMock()
    await crud.create_agent_internal(
        _local_config("adopt-vol"), _user(), None,
        adopt_existing_workspace=True)
    # Adopt proceeds to create the container; the pre-existing workspace volume
    # is mounted, not re-created.
    ctx["docker_utils"].containers_run.assert_awaited()
    ctx["docker_utils"].volume_create.assert_not_awaited()


# ===========================================================================
# Case 11 — existence guard (three truthy sources)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["get_agent_by_name", "get_agent_owner",
                                    "is_agent_name_reserved"])
async def test_case11_existence_guard_409(crud_env, monkeypatch, source):
    crud, ctx = crud_env
    if source == "get_agent_by_name":
        monkeypatch.setattr(
            crud, "get_agent_by_name", MagicMock(return_value={"name": "x"}))
    elif source == "get_agent_owner":
        ctx["db"].get_agent_owner.return_value = {"owner": "someone"}
    else:
        ctx["db"].is_agent_name_reserved.return_value = True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_local_config("dupe"), _user(), None)
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


# ===========================================================================
# Case 12 — role quota (429)
# ===========================================================================


@pytest.mark.asyncio
async def test_case12_role_quota_exceeded_429(crud_env):
    crud, ctx = crud_env
    ctx["settings_service"].get_agent_quota_for_role.return_value = 1
    ctx["db"].get_agents_by_owner.return_value = ["existing-1"]
    # get_agent_owner default None ⇒ the owned agent counts as non-system.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_local_config("over-quota"), _user(), None)
    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "QUOTA_EXCEEDED"


# ===========================================================================
# Case 13 — runtime validation propagates before the container
# ===========================================================================


@pytest.mark.asyncio
async def test_case13_runtime_validation_propagates(crud_env, monkeypatch):
    crud, ctx = crud_env
    from fastapi import HTTPException
    monkeypatch.setattr(
        crud, "validate_runtime",
        MagicMock(side_effect=HTTPException(status_code=400, detail="bad runtime")))
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _local_config("bad-rt", runtime="nope"), _user(), None)
    assert exc.value.status_code == 400
    ctx["docker_utils"].containers_run.assert_not_awaited()


# ===========================================================================
# Case 14 — non-fatal side-effect failures still return 200
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", [
    "materialize_persistent_state", "materialize_data_paths",
    "grant_default_permissions",
])
async def test_case14_nonfatal_side_effects_still_succeed(crud_env, sink):
    crud, ctx = crud_env
    if sink in ("materialize_persistent_state", "materialize_data_paths"):
        getattr(ctx["git_service"], sink).side_effect = Exception("boom")
    else:
        getattr(ctx["db"], sink).side_effect = Exception("boom")
    # Container was created; a failed non-fatal step must not regress 200→500.
    await crud.create_agent_internal(_local_config("nf-agent"), _user(), None)
    ctx["docker_utils"].containers_run.assert_awaited()


@pytest.mark.asyncio
async def test_case14_nonfatal_subscription_persist_failure(crud_env, monkeypatch):
    crud, ctx = crud_env
    monkeypatch.setattr(crud, "is_claude_runtime", lambda runtime: True)
    ctx["db"].list_assignable_subscriptions.return_value = [MagicMock(
        id="sub-1", name="sub-a")]
    ctx["db"].get_subscription_token.return_value = "oauth-tok"
    ctx["db"].assign_subscription_to_agent.side_effect = Exception("boom")
    await crud.create_agent_internal(_local_config("nf-sub"), _user(), None)
    ctx["docker_utils"].containers_run.assert_awaited()


@pytest.mark.asyncio
async def test_case14_nonfatal_spawn_edge_failure(crud_env):
    crud, ctx = crud_env
    ctx["db"].add_agent_permission.side_effect = Exception("boom")
    # agent_name set ⇒ the spawn permission edge is attempted (and tolerated).
    await crud.create_agent_internal(
        _local_config("nf-spawn"), _user(agent_name="parent"), None)
    ctx["docker_utils"].containers_run.assert_awaited()


@pytest.mark.asyncio
async def test_case14_nonfatal_avatar_seed_failure(crud_env, monkeypatch, tmp_path):
    """set_default_avatar is only reached with a template.yaml carrying an
    avatar_prompt — build one on a monkeypatched local-template root."""
    crud, ctx = crud_env
    tpl = tmp_path / "avtpl"
    tpl.mkdir()
    (tpl / "template.yaml").write_text("type: business-assistant\navatar_prompt: a wizard\n")
    monkeypatch.setattr(crud, "_LOCAL_TEMPLATE_ROOTS", (tmp_path, tmp_path))
    ctx["db"].set_default_avatar.side_effect = Exception("boom")

    await crud.create_agent_internal(
        AgentConfig(name="nf-avatar", template="local:avtpl"), _user(), None)
    ctx["db"].set_default_avatar.assert_called_once()
    ctx["docker_utils"].containers_run.assert_awaited()


# ===========================================================================
# Cases 15-18 — rollback matrix (inject failure AFTER a successful container)
# ===========================================================================


def _fail_after_container(monkeypatch, crud):
    """Raise right after containers_run succeeds (inside the try-block), so the
    'container not removed' assertions aren't vacuous."""
    monkeypatch.setattr(
        crud, "get_agent_status_from_container",
        MagicMock(side_effect=RuntimeError("post-container boom")))


@pytest.mark.asyncio
async def test_case15_local_rollback_mcp_key_only(crud_env, monkeypatch):
    crud, ctx = crud_env
    container_mock = MagicMock()
    # Mutate the existing AsyncMock in place (crud bound `containers_run` as a
    # name at import) so crud actually returns THIS container mock.
    ctx["docker_utils"].containers_run.return_value = container_mock
    _fail_after_container(monkeypatch, crud)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_local_config("rb-local"), _user(), None)
    assert exc.value.status_code == 500
    ctx["db"].delete_agent_mcp_api_key.assert_called_once_with("rb-local")
    ctx["db"].delete_git_config.assert_not_called()      # no git handle (local)
    ctx["ephemeral"].release_ephemeral_slot.assert_not_called()
    # ent#313: the container is now removed inline. This assertion used to read
    # `remove.assert_not_called()` with the comment "left for the cleanup
    # watchdog (PRESERVED)" — characterizing the leak, because no watchdog
    # covers a non-ephemeral orphan. `container_remove(force=True)` reaches
    # `.remove(force=True)`; the graceful stop is deliberately skipped (nothing
    # is running yet to shut down cleanly).
    ctx["docker_utils"].container_remove.assert_awaited_once_with(
        container_mock, force=True)
    ctx["runtime_state"].clear_agent_runtime_state.assert_awaited_once_with("rb-local")
    container_mock.stop.assert_not_called()  # nothing running yet to stop gracefully


@pytest.mark.asyncio
async def test_case16_github_rollback_gitconfig_and_mcp_key(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    _fail_after_container(monkeypatch, crud)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_github_config("rb-gh"), _user(), None)
    assert exc.value.status_code == 500
    ctx["db"].delete_git_config.assert_called_once_with("rb-gh")
    ctx["db"].delete_agent_mcp_api_key.assert_called_once_with("rb-gh")
    ctx["ephemeral"].release_ephemeral_slot.assert_not_called()


@pytest.mark.asyncio
async def test_case17_ephemeral_rollback_slot_and_mcp_key(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    _fail_after_container(monkeypatch, crud)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="rb-ghost", ephemeral={"max_executions": 3}),
            _user(), None)
    assert exc.value.status_code == 500
    ctx["ephemeral"].release_ephemeral_slot.assert_called_once_with(7)
    ctx["db"].delete_agent_mcp_api_key.assert_called_once()
    ctx["db"].delete_git_config.assert_not_called()      # no git handle


@pytest.mark.asyncio
async def test_case18_gitconfig_not_rolled_back_without_instance_id(
        crud_env, monkeypatch):
    """Pins the `and git_instance_id` half of the except guard: repo set but
    the instance-id handle stayed None ⇒ delete_git_config is NOT called."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    ctx["git_service"].reserve_and_generate_instance_id = AsyncMock(
        return_value=(None, "main"))  # repo bound, but no instance id
    _fail_after_container(monkeypatch, crud)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_github_config("rb-noiid"), _user(), None)
    assert exc.value.status_code == 500
    ctx["db"].delete_git_config.assert_not_called()
    ctx["db"].delete_agent_mcp_api_key.assert_called_once_with("rb-noiid")


# ===========================================================================
# Case 19 — MCP-key leak on ephemeral 429
# PINS A KNOWN LEAK (not desired) — follow-up not yet filed (see report).
# The 429 raises BEFORE the docker try-block, so the MCP key minted at line
# 836 is never rolled back. This test asserts the leak PERSISTS so a future
# fixer knows the assertion is intentional-until-then, not wrong.
# ===========================================================================


@pytest.mark.asyncio
async def test_case19_ephemeral_429_leaks_mcp_key(crud_env, monkeypatch):
    crud, ctx = crud_env
    _entitle(monkeypatch, crud)
    monkeypatch.setattr(
        crud.ephemeral_service, "try_reserve_ephemeral_slot",
        MagicMock(return_value=False))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            AgentConfig(name="leak-ghost", ephemeral={"max_executions": 3}),
            _user(), None)
    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "ephemeral_quota_exceeded"
    ctx["db"].create_agent_mcp_api_key.assert_called_once()      # minted...
    ctx["db"].delete_agent_mcp_api_key.assert_not_called()        # ...never freed


# ===========================================================================
# Case 20 — docker-unavailable else-branch leak
# PINS A KNOWN LEAK (not desired) — follow-up not yet filed (see report).
# The else-branch (docker down) releases ONLY the ephemeral slot; the MCP key
# AND the git-config reservation (both minted before `if docker_client`) leak.
# ===========================================================================


@pytest.mark.asyncio
async def test_case20_no_docker_else_leaks_mcp_key_and_gitconfig(
        crud_env_no_docker, monkeypatch):
    crud, ctx = crud_env_no_docker
    _entitle(monkeypatch, crud)
    _script_github_template(ctx)            # ⇒ git_instance_id is reserved
    _patch_repo_validation(monkeypatch, crud)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(
            _github_config("leak-nodocker", ephemeral={"max_executions": 3}),
            _user(), None)
    assert exc.value.status_code == 503
    ctx["ephemeral"].release_ephemeral_slot.assert_called_once_with(7)
    ctx["db"].delete_agent_mcp_api_key.assert_not_called()   # leaked (preserved)
    ctx["db"].delete_git_config.assert_not_called()          # leaked (preserved)


# ===========================================================================
# Case 21 — fork destination race (inline rollback of the losing agent)
# ===========================================================================


@pytest.mark.asyncio
async def test_case21_fork_destination_race_loser_rolls_back(crud_env, monkeypatch):
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    from services.agent_service.fork_to_own import ForkToOwnResult
    monkeypatch.setattr(
        crud, "fork_template_to_own_repo",
        AsyncMock(return_value=ForkToOwnResult("alice/brain", "main", False)))
    # Pre-check sees an empty binding; the post-reservation re-check sees a rival
    # whose name sorts before "z-loser" ⇒ the loser rolls back inline.
    ctx["db"].get_git_config_agent_names_for_repo.side_effect = [
        [], ["a-rival", "z-loser"]]
    cfg = _github_config(
        "z-loser",
        fork_to_own={"destination_repo": "alice/brain",
                     "github_pat": "ghp_userpat", "private": True})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(cfg, _user(), None)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FORK_DESTINATION_IN_USE"
    ctx["db"].delete_git_config.assert_called_once_with("z-loser")
    ctx["docker_utils"].containers_run.assert_not_awaited()


# ===========================================================================
# Case 22 — #2215: a raising allocator lands INSIDE the rollback fence
# ===========================================================================


@pytest.mark.asyncio
async def test_case22_allocator_raise_rolls_back_gitconfig_and_mcp_key(
        crud_env, monkeypatch):
    """#2215 made `get_next_available_port` fail LOUD on a Docker listing fault
    (it used to degrade to the empty set and hand out 2222). A raise there must
    reach the same rollback as any other in-try failure: `_resolve_template`
    has already written the `agent_git_config` reservation, and a stranded row
    makes every later create of that name fail `agent_git_config already
    exists` (Cornelius's next-boot retry included — a permanent seed failure
    by a new route). Pinned so the allocation call cannot drift back out of
    the docker try-block."""
    crud, ctx = crud_env
    _script_github_template(ctx)
    _patch_repo_validation(monkeypatch, crud)
    monkeypatch.setattr(
        crud, "get_next_available_port",
        MagicMock(side_effect=RuntimeError(
            "cannot allocate a port: Docker listing failed (read timed out)")))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await crud.create_agent_internal(_github_config("rb-alloc"), _user(), None)
    assert exc.value.status_code == 500
    ctx["db"].delete_git_config.assert_called_once_with("rb-alloc")
    ctx["db"].delete_agent_mcp_api_key.assert_called_once_with("rb-alloc")
    ctx["docker_utils"].containers_run.assert_not_awaited()
