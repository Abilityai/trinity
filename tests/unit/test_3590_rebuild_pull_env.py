"""#3590 — the rebuild-from-nothing path bakes the pull-worker env like the other two.

Three sites assemble an agent container's env: creation (`crud._build_env_vars`),
the config-drift recreate (`lifecycle.recreate_container_with_updated_config`)
and the #1559 recovery rebuild (`lifecycle.recreate_missing_container`). The
first two applied the pull opt-in; the third never did, so `docker rm -f` +
`POST /start` rebuilt a pilot with no `TRINITY_PULL_MODE` while the backend kept
routing its work to the durable queue — every task sat `queued` with no warning.

The idiom now has one owner, `pull_mode.apply_pull_mode_env` (set-or-clear), and
`check_pull_mode_env_matches` joins the start-path drift chain so a container
whose pull flag disagrees with the allowlist is recreated on the next start
(the "allowlist edited after create" case B-08 used to send to a manual recreate).

Related flow: docs/memory/feature-flows/agent-lifecycle.md
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-3590.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-3590-logs"))

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_CRUD = _BACKEND / "services" / "agent_service" / "crud.py"
_LIFECYCLE = _BACKEND / "services" / "agent_service" / "lifecycle.py"

PILOT = "b9-pull"


# ---------------------------------------------------------------------------
# The regression: rebuild from nothing
# ---------------------------------------------------------------------------

def _drive_rebuild(monkeypatch, agent_name: str, *, mint_key: bool = True) -> dict:
    """Run the real `recreate_missing_container` with Docker, the DB and the
    sibling env helpers patched out; return the env handed to the shared
    provision tail. `_apply_persisted_auth_env` is stubbed on purpose: the pull
    call must sit in the rebuild function itself, next to it, not inside it."""
    import services.agent_service.lifecycle as mod

    captured: dict = {}

    async def _provision(name, **kw):
        captured.update(kw)
        return SimpleNamespace(name=f"agent-{name}", status="created")

    monkeypatch.setattr(mod, "_provision_folders_and_run_agent_container", _provision)
    monkeypatch.setattr(mod, "_read_template_yaml_from_volume", AsyncMock(return_value={}))
    monkeypatch.setattr(mod, "_apply_persisted_auth_env", Mock())
    monkeypatch.setattr(mod, "validate_base_image", Mock())
    monkeypatch.setattr(mod, "get_next_available_port", Mock(return_value=2245))
    monkeypatch.setattr(mod, "get_agent_full_capabilities", Mock(return_value=False))
    monkeypatch.setattr(mod, "get_agent_default_resources", Mock(return_value={"cpu": "2", "memory": "4g"}))

    fake_db = MagicMock()
    fake_db.get_agent_owner.return_value = {"owner_username": "bob"}
    fake_db.get_resource_limits.return_value = {}
    fake_db.get_git_config.return_value = None
    fake_db.deactivate_agent_mcp_keys.return_value = 0
    fake_db.create_agent_mcp_api_key.return_value = Mock(api_key="trinity_mcp_x") if mint_key else None
    fake_db.get_volume_base_name.side_effect = lambda n: n
    monkeypatch.setattr(mod, "db", fake_db)

    asyncio.run(mod.recreate_missing_container(agent_name))
    assert "env_vars" in captured, "the rebuild never reached the provision tail"
    return captured["env_vars"]


def _pilot_settings(monkeypatch, pool: int):
    """A pool size that is NOT the silent fallback (3), so the assertion proves
    the settings read ran rather than the except-arm."""
    import services.settings_service as ss

    monkeypatch.setattr(ss, "get_effective_max_parallel_tasks", lambda name: pool)


def test_rebuild_from_nothing_bakes_pull_env_for_a_pilot(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    _pilot_settings(monkeypatch, 7)

    env = _drive_rebuild(monkeypatch, PILOT)

    assert env["TRINITY_PULL_MODE"] == "true"
    assert env["TRINITY_MAX_PARALLEL_TASKS"] == "7"
    # The worker authenticates with the agent's own key — the rebuild minted one.
    assert env["TRINITY_MCP_API_KEY"] == "trinity_mcp_x"


def test_rebuild_with_a_failed_key_mint_names_the_stall(monkeypatch, caplog):
    """A pilot whose key mint failed on rebuild gets the pull env but no
    credential to poll with — the same stall as #3590 with a different cause.
    Lives on the rebuild path, where the env is final: the drift recreate may
    still receive the key through `env_overrides` (#1854 self-heal), so a
    warning inside the helper would fire falsely there."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    _pilot_settings(monkeypatch, 2)

    with caplog.at_level(logging.WARNING, logger="services.agent_service.lifecycle"):
        env = _drive_rebuild(monkeypatch, PILOT, mint_key=False)

    assert env["TRINITY_PULL_MODE"] == "true"
    assert "TRINITY_MCP_API_KEY" not in env
    assert any("TRINITY_MCP_API_KEY" in r.getMessage() for r in caplog.records)


def test_rebuild_with_a_minted_key_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    _pilot_settings(monkeypatch, 2)

    with caplog.at_level(logging.WARNING, logger="services.agent_service.lifecycle"):
        _drive_rebuild(monkeypatch, PILOT)

    assert not any("TRINITY_MCP_API_KEY" in r.getMessage() for r in caplog.records)


def test_rebuild_from_nothing_leaves_a_non_pilot_without_pull_env(monkeypatch):
    """Contrast case (green before the fix too): the opt-in is exact, never
    applied to an agent outside the allowlist."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "someone-else")
    _pilot_settings(monkeypatch, 7)

    env = _drive_rebuild(monkeypatch, PILOT)

    assert "TRINITY_PULL_MODE" not in env
    assert "TRINITY_MAX_PARALLEL_TASKS" not in env


# ---------------------------------------------------------------------------
# The one owner of the set-or-clear idiom
# ---------------------------------------------------------------------------

class TestApplyPullModeEnv:
    def test_pilot_on_an_empty_dict_sets_both_knobs(self, monkeypatch):
        """The rebuild shape: nothing baked yet."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        _pilot_settings(monkeypatch, 5)
        from services.agent_service.pull_mode import apply_pull_mode_env

        env = {"AGENT_NAME": PILOT, "TRINITY_MCP_API_KEY": "k"}
        assert apply_pull_mode_env(PILOT, env) is None  # in place, no return value

        assert env["TRINITY_PULL_MODE"] == "true"
        assert env["TRINITY_MAX_PARALLEL_TASKS"] == "5"
        assert env["AGENT_NAME"] == PILOT

    def test_depilot_clears_stale_knobs_and_touches_nothing_else(self, monkeypatch):
        """The #1081 B1 property, now owned by the helper."""
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        from services.agent_service.pull_mode import apply_pull_mode_env

        env = {"TRINITY_PULL_MODE": "true", "TRINITY_MAX_PARALLEL_TASKS": "5", "OTHER": "x"}
        apply_pull_mode_env(PILOT, env)

        assert "TRINITY_PULL_MODE" not in env
        assert "TRINITY_MAX_PARALLEL_TASKS" not in env
        assert env == {"OTHER": "x"}

    def test_depilot_then_rebuild_from_nothing_bakes_nothing(self, monkeypatch):
        """Pilot removed from the allowlist between create and rebuild."""
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        from services.agent_service.pull_mode import apply_pull_mode_env

        env: dict = {}
        apply_pull_mode_env(PILOT, env)
        assert env == {}

    def test_two_passes_produce_an_identical_dict(self, monkeypatch):
        """The drift path feeds config matchers; a non-idempotent writer is a
        recreate loop (the ent#109 property)."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        _pilot_settings(monkeypatch, 4)
        from services.agent_service.pull_mode import apply_pull_mode_env

        env = {"A": "1"}
        apply_pull_mode_env(PILOT, env)
        first = dict(env)
        apply_pull_mode_env(PILOT, env)
        assert env == first

    def test_honours_a_monkeypatched_pull_mode_env_vars(self, monkeypatch):
        """Three existing suites (1484, 1759, ent752) stub `pull_mode_env_vars`;
        the helper must resolve it through the module global at call time or
        those stubs silently detach (Invariant #1a)."""
        import services.agent_service.pull_mode as pm

        monkeypatch.setattr(pm, "pull_mode_env_vars", lambda name: {"TRINITY_PULL_MODE": "stubbed"})
        env: dict = {}
        pm.apply_pull_mode_env(PILOT, env)
        assert env == {"TRINITY_PULL_MODE": "stubbed"}

    def test_settings_fallback_is_logged(self, monkeypatch, caplog):
        """The size-3 fallback was silent; a pilot sized 3 when the owner set 8
        was invisible."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        import services.settings_service as ss

        def _boom(name):
            raise RuntimeError("db down")

        monkeypatch.setattr(ss, "get_effective_max_parallel_tasks", _boom)
        from services.agent_service.pull_mode import pull_mode_env_vars

        with caplog.at_level(logging.WARNING, logger="services.agent_service.pull_mode"):
            out = pull_mode_env_vars(PILOT)
        assert out["TRINITY_MAX_PARALLEL_TASKS"] == "3"
        assert any("db down" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The drift predicate and its wiring into the start chain
# ---------------------------------------------------------------------------

def _container(env: dict | None, status: str = "running"):
    c = SimpleNamespace(status=status, id="c0ffee", attrs={"Config": {}, "HostConfig": {}})
    if env is not None:
        c.attrs["Config"]["Env"] = [f"{k}={v}" for k, v in env.items()]
    return c


class TestCheckPullModeEnvMatches:
    def test_pilot_without_the_flag_drifts(self, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        from services.agent_service.helpers import check_pull_mode_env_matches

        assert check_pull_mode_env_matches(_container({"AGENT_NAME": PILOT}), PILOT) is False

    def test_pilot_with_the_flag_matches(self, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        from services.agent_service.helpers import check_pull_mode_env_matches

        assert check_pull_mode_env_matches(_container({"TRINITY_PULL_MODE": "true"}), PILOT) is True

    def test_non_pilot_without_the_flag_matches(self, monkeypatch):
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        from services.agent_service.helpers import check_pull_mode_env_matches

        assert check_pull_mode_env_matches(_container({}), PILOT) is True
        # A container Docker reports with no Env list at all (the mocks in the
        # sibling suites) is a non-pilot match, never a drift.
        assert check_pull_mode_env_matches(_container(None), PILOT) is True

    def test_depiloted_container_still_carrying_the_flag_drifts(self, monkeypatch):
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        from services.agent_service.helpers import check_pull_mode_env_matches

        assert check_pull_mode_env_matches(_container({"TRINITY_PULL_MODE": "true"}), PILOT) is False

    def test_pool_size_is_not_compared(self, monkeypatch):
        """Flag only — a changed parallel limit is #3039's live resize, not a
        recreate on every start."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        _pilot_settings(monkeypatch, 9)
        from services.agent_service.helpers import check_pull_mode_env_matches

        c = _container({"TRINITY_PULL_MODE": "true", "TRINITY_MAX_PARALLEL_TASKS": "2"})
        assert check_pull_mode_env_matches(c, PILOT) is True

    def test_unreadable_attrs_fail_safe_to_match(self, monkeypatch):
        """A predicate that raises would turn every start into a 500."""
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
        from services.agent_service.helpers import check_pull_mode_env_matches

        assert check_pull_mode_env_matches(SimpleNamespace(attrs=None), PILOT) is True


def _run_start(monkeypatch, container):
    """Drive the real `start_agent_internal` with every OTHER predicate satisfied
    and `check_pull_mode_env_matches` left REAL, so the recreate decision is
    the predicate's alone (the test_2186 harness shape)."""
    from services.agent_service import lifecycle as lc

    recreate_calls = []

    async def _fake_recreate(agent_name, old_container, owner, **kwargs):
        recreate_calls.append(kwargs)
        return None

    async def _fake_start(c):
        c.status = "running"

    for name in (
        "check_shared_folder_mounts_match", "check_public_folder_mount_matches",
        "check_api_key_env_matches", "check_github_pat_env_matches",
        "check_resource_limits_match", "check_full_capabilities_match",
        "check_guardrails_env_matches", "check_agent_auth_token_env_matches",
        "check_agent_mcp_key_matches", "check_base_image_matches",
    ):
        target = getattr(lc, name)
        monkeypatch.setattr(
            lc, name,
            AsyncMock(return_value=True) if asyncio.iscoroutinefunction(target)
            else MagicMock(return_value=True),
        )

    monkeypatch.setattr(lc, "get_agent_container", MagicMock(return_value=container))
    monkeypatch.setattr(lc, "container_reload", AsyncMock())
    monkeypatch.setattr(lc, "container_start", _fake_start)
    monkeypatch.setattr(lc, "recreate_container_with_updated_config", _fake_recreate)
    monkeypatch.setattr(lc, "clear_agent_runtime_state", MagicMock(), raising=False)
    monkeypatch.setattr(lc, "clear_agent_breakers", MagicMock(), raising=False)

    db_mock = MagicMock()
    db_mock.get_agent_owner.return_value = "owner"
    db_mock.get_agent_ephemeral_info.return_value = None
    monkeypatch.setattr(lc, "db", db_mock)

    for name in ("wait_for_agent_ready", "inject_assigned_credentials",
                 "inject_assigned_skills", "inject_read_only_hooks",
                 "remove_read_only_hooks"):
        if hasattr(lc, name):
            target = getattr(lc, name)
            monkeypatch.setattr(
                lc, name,
                AsyncMock(return_value={"status": "stubbed"})
                if asyncio.iscoroutinefunction(target)
                else MagicMock(return_value={"status": "stubbed"}),
            )

    asyncio.run(lc.start_agent_internal(PILOT))
    return recreate_calls


def test_start_recreates_a_pilot_whose_container_lacks_the_flag(monkeypatch):
    """The wiring: an allowlisted pilot running a pre-flag container is
    recreated on `POST /start`, not left to a manual recreate."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    calls = _run_start(monkeypatch, _container({"AGENT_NAME": PILOT}, status="exited"))
    assert len(calls) == 1


def test_start_leaves_a_matching_pilot_alone(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    calls = _run_start(monkeypatch, _container({"TRINITY_PULL_MODE": "true"}, status="exited"))
    assert calls == []


# ---------------------------------------------------------------------------
# Writer-set guard — source-text pin on top of the behavioural proofs above.
# Its live consumer is CI: a fourth env-assembling site must opt in.
# ---------------------------------------------------------------------------

_WRITERS = {
    _CRUD: {"_build_env_vars"},
    _LIFECYCLE: {"recreate_container_with_updated_config", "recreate_missing_container"},
}


def _calls_in(fn: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def test_every_env_assembler_applies_pull_env_through_the_one_helper():
    for path, expected in _WRITERS.items():
        tree = ast.parse(path.read_text())
        seen: dict[str, set[str]] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen[fn.name] = _calls_in(fn)
        for name in expected:
            assert name in seen, f"{name} not found in {path.name}"
            assert "apply_pull_mode_env" in seen[name], (
                f"{path.name}::{name} assembles a container env but does not call "
                "apply_pull_mode_env — a pilot rebuilt here runs no pull worker (#3590)"
            )
        # The idiom has ONE owner: nobody in these modules reaches past it.
        for name, calls in seen.items():
            assert "pull_mode_env_vars" not in calls and "PULL_MODE_ENV_KEYS" not in calls, (
                f"{path.name}::{name} calls pull_mode_env_vars directly — route it "
                "through apply_pull_mode_env so the set-or-clear idiom stays in one place"
            )


# ---------------------------------------------------------------------------
# Structural half of the guard. `_WRITERS` above pins the KNOWN assemblers; this
# half FINDS an unknown fourth. Two shapes reach Docker with an agent env — a
# caller of the shared provision tail, or a direct `containers_run(...,
# environment=...)` site — and both are discovered by AST over services/ (rglob,
# the Invariant #1 note: a one-level glob went blind on routers/settings/).
# ---------------------------------------------------------------------------

_SERVICES = _BACKEND / "services"
_TAIL = "_provision_folders_and_run_agent_container"

# Every function that hands Docker an `environment=` directly, with the assembler
# that feeds it. A set difference here is a new container-creating site.
_KNOWN_RUNNERS = {
    "agent_service/crud.py::_create_agent_container": "env from _build_env_vars (pinned in _WRITERS)",
    f"agent_service/lifecycle.py::{_TAIL}": "env from its callers (each must apply the helper, below)",
    "system_agent_service.py::_create_system_agent": (
        "trinity-system assembles its own env and is not a pull pilot; if an operator "
        "ever lists it, check_pull_mode_env_matches recreates it on its next cold start "
        "through the drift path, which applies the helper"
    ),
}


def _functions(path: Path):
    for fn in ast.walk(ast.parse(path.read_text())):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield fn


def _runs_container_with_env(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name in ("containers_run", "run") and any(k.arg == "environment" for k in node.keywords):
            return True
    return False


def test_every_docker_run_site_with_an_env_is_a_registered_runner():
    found = set()
    for path in sorted(_SERVICES.rglob("*.py")):
        rel = path.relative_to(_SERVICES).as_posix()
        for fn in _functions(path):
            if _runs_container_with_env(fn):
                found.add(f"{rel}::{fn.name}")
    assert found == set(_KNOWN_RUNNERS), (
        f"container-creating sites changed — new: {sorted(found - set(_KNOWN_RUNNERS))}, "
        f"gone: {sorted(set(_KNOWN_RUNNERS) - found)}. A new site assembles an agent env: "
        "route it through apply_pull_mode_env and register it in _KNOWN_RUNNERS with the "
        "assembler that feeds it (#3590)"
    )


def test_every_caller_of_the_provision_tail_applies_pull_env():
    callers: dict[str, set[str]] = {}
    for path in sorted(_SERVICES.rglob("*.py")):
        rel = path.relative_to(_SERVICES).as_posix()
        for fn in _functions(path):
            if fn.name == _TAIL:
                continue
            calls = _calls_in(fn)
            if _TAIL in calls:
                callers[f"{rel}::{fn.name}"] = calls
    assert len(callers) >= 2, f"the provision-tail scan found {sorted(callers)} — the scan is broken"
    missing = sorted(name for name, calls in callers.items() if "apply_pull_mode_env" not in calls)
    assert not missing, (
        f"{missing} hand an env to the provision tail without calling apply_pull_mode_env — "
        "a pilot built there runs no pull worker (#3590)"
    )
