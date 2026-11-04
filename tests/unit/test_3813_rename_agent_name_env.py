"""#3813 — a renamed agent must stop identifying itself under the dead name.

Rename moves the Docker container name only. The next start recreates the
container (the #1159 token predicate fires), but the recreate replays the OLD
container's ``Config.Env`` and ``Config.Labels`` and re-derived only
``TRINITY_AGENT_AUTH_TOKEN``. ``AGENT_NAME`` — read once by the agent server and
used for ``/health``, the heartbeat path, the pull worker ids (``<name>#wN``),
the #1083 result callback and git auto-sync — kept the old name, so every one of
those calls 403'd. After that recreate the token matched, so no predicate fired
again and the agent stayed stuck.

Two halves, both pinned here:
  * the recreate bakes ``AGENT_NAME`` and the ``trinity.agent-name`` label from
    the live name, beside the token;
  * ``check_agent_name_env_matches`` repairs an agent already in the stuck state
    on its next COLD start, and never fires for a never-renamed agent.

Related flow: docs/memory/feature-flows/agent-lifecycle.md
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

OLD = "oracle-5-geopolitics"
NEW = "oracle-5-energy"


def _container(env: dict | None = None, labels: dict | None = None, status: str = "exited"):
    config: dict = {}
    if env is not None:
        config["Env"] = [f"{k}={v}" for k, v in env.items()]
    if labels is not None:
        config["Labels"] = dict(labels)
    return SimpleNamespace(status=status, id="c0ffee", attrs={"Config": config, "HostConfig": {}})


# ---------------------------------------------------------------------------
# The replay path (the bug)
# ---------------------------------------------------------------------------

def _recreate(monkeypatch, agent_name: str, old_env: list[str], old_labels: dict):
    """Drive the real `recreate_container_with_updated_config` (the #3106
    harness shape) and return the env + labels handed to the run tail."""
    from services.agent_service import lifecycle

    captured: dict = {}

    async def _fake_provision(name, **kw):
        captured.update(kw)
        return SimpleNamespace(name=f"agent-{name}")

    old = SimpleNamespace(
        attrs={
            "Config": {"Env": list(old_env), "Image": "trinity-agent-base:latest",
                       "Labels": {"trinity.ssh-port": "2222", **old_labels}},
            "HostConfig": {"RestartPolicy": {}},
            "Mounts": [],
        },
        status="running",
    )
    monkeypatch.setattr(lifecycle, "_provision_folders_and_run_agent_container", _fake_provision)
    monkeypatch.setattr(lifecycle, "validate_base_image", lambda image: None)
    monkeypatch.setattr(lifecycle, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(lifecycle, "get_agent_default_resources", lambda: {"cpu": "2", "memory": "4g"})

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(lifecycle, "container_stop", _noop)
    monkeypatch.setattr(lifecycle, "container_remove", _noop)

    async def _img(_i):
        return SimpleNamespace(labels={})
    monkeypatch.setattr(lifecycle, "image_get", _img)
    for name, val in (
        ("get_agent_subscription_id", None), ("get_resource_limits", None),
        ("get_guardrails_config", None), ("get_agent_github_pat", None),
        ("get_git_config", None), ("get_public_mount_path", "/home/developer/public"),
    ):
        monkeypatch.setattr(lifecycle.db, name, (lambda v: (lambda *a, **k: v))(val))

    asyncio.run(lifecycle.recreate_container_with_updated_config(agent_name, old, "owner"))
    return captured["env_vars"], captured["labels"]


def test_recreate_after_rename_bakes_the_live_name(monkeypatch):
    from services.agent_auth import derive_agent_token

    env, labels = _recreate(
        monkeypatch, NEW,
        [f"AGENT_NAME={OLD}", f"TRINITY_AGENT_AUTH_TOKEN={derive_agent_token(OLD)}", "KEEP=me"],
        {"trinity.agent-name": OLD, "trinity.template": "t"},
    )
    assert env["AGENT_NAME"] == NEW
    assert labels["trinity.agent-name"] == NEW
    # One recreate derives every identity value from the same name.
    assert env["TRINITY_AGENT_AUTH_TOKEN"] == derive_agent_token(NEW)
    # Nothing else replayed is touched.
    assert env["KEEP"] == "me"
    assert labels["trinity.template"] == "t"


def test_recreate_of_a_never_renamed_agent_is_unchanged(monkeypatch):
    env, labels = _recreate(monkeypatch, NEW, [f"AGENT_NAME={NEW}"], {"trinity.agent-name": NEW})
    assert env["AGENT_NAME"] == NEW
    assert labels["trinity.agent-name"] == NEW


# ---------------------------------------------------------------------------
# The drift predicate
# ---------------------------------------------------------------------------

class TestCheckAgentNameEnvMatches:
    def test_stale_env_and_label_drift(self):
        from services.agent_service.helpers import check_agent_name_env_matches

        c = _container({"AGENT_NAME": OLD}, {"trinity.agent-name": OLD})
        assert check_agent_name_env_matches(c, NEW) is False

    def test_stale_env_alone_drifts(self):
        from services.agent_service.helpers import check_agent_name_env_matches

        c = _container({"AGENT_NAME": OLD}, {"trinity.agent-name": NEW})
        assert check_agent_name_env_matches(c, NEW) is False

    def test_stale_label_alone_drifts(self):
        from services.agent_service.helpers import check_agent_name_env_matches

        c = _container({"AGENT_NAME": NEW}, {"trinity.agent-name": OLD})
        assert check_agent_name_env_matches(c, NEW) is False

    def test_never_renamed_agent_matches(self):
        from services.agent_service.helpers import check_agent_name_env_matches

        c = _container({"AGENT_NAME": NEW}, {"trinity.agent-name": NEW})
        assert check_agent_name_env_matches(c, NEW) is True

    def test_absent_values_match(self):
        """Absent is never a drift verdict: every create site bakes both, and
        start-path fakes across the suite carry neither."""
        from services.agent_service.helpers import check_agent_name_env_matches

        assert check_agent_name_env_matches(_container(), NEW) is True
        assert check_agent_name_env_matches(_container({}, {}), NEW) is True

    def test_unreadable_attrs_match(self):
        """A predicate that raises would turn every start into a 500."""
        from services.agent_service.helpers import check_agent_name_env_matches

        assert check_agent_name_env_matches(SimpleNamespace(attrs=None), NEW) is True
        assert check_agent_name_env_matches(SimpleNamespace(), NEW) is True


# ---------------------------------------------------------------------------
# Wiring into the start path (the test_3590 harness shape)
# ---------------------------------------------------------------------------

def _run_start(monkeypatch, container):
    """Real `start_agent_internal`, every OTHER predicate satisfied, the new
    one left real — the recreate decision is its alone."""
    from services.agent_service import lifecycle as lc

    recreate_calls = []

    async def _fake_recreate(agent_name, old_container, owner, **kwargs):
        recreate_calls.append(agent_name)
        return None

    async def _fake_start(c):
        c.status = "running"

    for name in (
        "check_shared_folder_mounts_match", "check_public_folder_mount_matches",
        "check_api_key_env_matches", "check_github_pat_env_matches",
        "check_resource_limits_match", "check_full_capabilities_match",
        "check_guardrails_env_matches", "check_agent_auth_token_env_matches",
        "check_pull_mode_env_matches", "check_agent_mcp_key_matches",
        "check_base_image_matches",
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

    asyncio.run(lc.start_agent_internal(NEW))
    return recreate_calls


def test_cold_start_recreates_an_agent_stuck_under_its_old_name(monkeypatch):
    """The already-renamed agent (token already re-derived, name still stale)
    is repaired by a stop -> start."""
    c = _container({"AGENT_NAME": OLD}, {"trinity.agent-name": OLD}, status="exited")
    assert _run_start(monkeypatch, c) == [NEW]


def test_cold_start_leaves_a_never_renamed_agent_alone(monkeypatch):
    c = _container({"AGENT_NAME": NEW}, {"trinity.agent-name": NEW}, status="exited")
    assert _run_start(monkeypatch, c) == []


def test_start_on_a_running_agent_does_not_recreate_for_name_drift(monkeypatch):
    """Start-on-running is the idempotent ensure-running path (MCP chat): a
    name-drift recreate there would kill a live execution. Cold start only."""
    c = _container({"AGENT_NAME": OLD}, {"trinity.agent-name": OLD}, status="running")
    assert _run_start(monkeypatch, c) == []
