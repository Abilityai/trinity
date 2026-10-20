"""
Gated skills, the in-container hook — the marker that decides an outage
(trinity-enterprise#752).

When the platform gives the hook no verdict, ONE fact decides: does this agent
have gates? The backend keeps that fact in a root-owned file inside the
container, ``/opt/trinity/skill-gates-active``, written by a root ``docker
exec`` with a constant argv. The agent cannot remove it without ``sudo``
(stated limit), and an agent with no gates never has one, so it keeps every
skill through a backend restart.

Targets: ``skill_gate_service.sync_gate_marker`` / ``spawn_gate_marker_sync`` /
``marker_command``, and the two lifecycle tails that call the spawn —
``start_agent_internal`` and ``recreate_container_with_updated_config``
(a recreate drops the writable layer, and with it the marker), each driven
through its real body with Docker mocked out.

No test here may reach the local Docker daemon: the exec is replaced in every
test that could get there.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.docker_service as _DS  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402

pytestmark = pytest.mark.unit

MARKER = "/opt/trinity/skill-gates-active"


@pytest.fixture
def gates(monkeypatch):
    state = {"map": {"fin": {"pay-invoice": _GATE.SkillGate()}}, "fail": None}

    def _list(agent):
        if state["fail"] == "raise":
            raise RuntimeError("gate store down")
        if state["fail"] == "none":
            return None
        return state["map"].get(agent, {})

    monkeypatch.setattr(_GATE, "list_skill_gates", _list)
    return state


@pytest.fixture
def execs(monkeypatch):
    calls = []
    box = {"result": {"exit_code": 0, "output": "", "timed_out": False}, "raise": None}

    async def _exec(container_name, command, timeout=60, *, environment=None, user="developer"):
        calls.append(SimpleNamespace(container=container_name, command=list(command), timeout=timeout,
                                     environment=environment, user=user))
        if box["raise"]:
            raise box["raise"]
        return box["result"]

    monkeypatch.setattr(_DS, "execute_command_in_container", _exec)
    return SimpleNamespace(calls=calls, box=box)


# ---------------------------------------------------------------------------
# sync_gate_marker — what read_gates derives, nothing else
# ---------------------------------------------------------------------------

def test_a_gated_agent_gets_the_marker_by_a_root_exec(gates, execs):
    assert asyncio.run(_GATE.sync_gate_marker("fin")) is True
    [call] = execs.calls
    assert (call.container, call.user) == ("agent-fin", "root")
    assert call.command == _GATE.marker_command(True)
    assert 0 < call.timeout <= 15
    assert call.environment is None


def test_an_agent_without_gates_has_it_removed(gates, execs):
    assert asyncio.run(_GATE.sync_gate_marker("nobody")) is False
    [call] = execs.calls
    assert call.command == _GATE.marker_command(False)
    assert call.user == "root"


@pytest.mark.parametrize("fail", ["raise", "none"])
def test_an_unreadable_gate_map_never_touches_the_marker(gates, execs, fail):
    """Removing it on a failed read would open every gated skill during an
    outage; writing it would refuse every skill on an agent with none."""
    gates["fail"] = fail
    assert asyncio.run(_GATE.sync_gate_marker("fin")) is None
    assert execs.calls == []


@pytest.mark.parametrize("outcome", [
    pytest.param({"result": {"exit_code": 124, "output": "", "timed_out": True}}, id="timed-out"),
    pytest.param({"result": {"exit_code": 1, "output": "denied", "timed_out": False}}, id="failed"),
    pytest.param({"raise": RuntimeError("no such container")}, id="raised"),
])
def test_a_failed_exec_reports_no_change(gates, execs, outcome, caplog):
    execs.box.update(outcome)
    assert asyncio.run(_GATE.sync_gate_marker("fin")) is None
    assert "marker" in caplog.text.lower()


def test_the_command_is_constant_whatever_the_agent_and_its_gates(gates, execs):
    gates["map"]["other"] = {"weird; rm -rf /": _GATE.SkillGate()}
    asyncio.run(_GATE.sync_gate_marker("fin"))
    asyncio.run(_GATE.sync_gate_marker("other"))
    assert execs.calls[0].command == execs.calls[1].command == _GATE.marker_command(True)
    assert not any("fin" in part or "rm" in part.split() for part in execs.calls[1].command)
    assert MARKER in " ".join(_GATE.marker_command(True))
    assert MARKER in " ".join(_GATE.marker_command(False))


def test_the_commands_do_what_they_say(tmp_path):
    """The real argv, run against a temp path: created read-only, rewritten in
    place without a moment of absence, removed, removed again harmlessly."""
    marker = tmp_path / "opt" / "skill-gates-active"
    marker.parent.mkdir()
    subprocess.run(_GATE.marker_command(True, path=str(marker)), check=True)
    st = os.lstat(marker)
    assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o444
    subprocess.run(_GATE.marker_command(True, path=str(marker)), check=True)
    assert sorted(p.name for p in marker.parent.iterdir()) == ["skill-gates-active"]
    subprocess.run(_GATE.marker_command(False, path=str(marker)), check=True)
    assert not marker.exists()
    subprocess.run(_GATE.marker_command(False, path=str(marker)), check=True)


def test_two_syncs_racing_a_gate_change_end_in_the_latest_map(gates, monkeypatch):
    """A slow sync that read "no gates" must not land its removal after a
    later sync wrote the marker for a gate added meanwhile (the ent#753
    ordering contract, within one worker)."""
    effects = []

    async def main():
        release = asyncio.Event()

        async def _exec(container_name, command, timeout=60, *, environment=None, user="developer"):
            kind = "create" if command == _GATE.marker_command(True) else "remove"
            if kind == "remove":
                await release.wait()           # the stale sync is slow
            effects.append(kind)
            return {"exit_code": 0, "output": "", "timed_out": False}

        monkeypatch.setattr(_DS, "execute_command_in_container", _exec)
        gates["map"]["fin"] = {}
        first = asyncio.create_task(_GATE.sync_gate_marker("fin"))
        await asyncio.sleep(0)
        gates["map"]["fin"] = {"pay-invoice": _GATE.SkillGate()}
        second = asyncio.create_task(_GATE.sync_gate_marker("fin"))
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()
        return await first, await second

    assert asyncio.run(main()) == (False, True)
    assert effects == ["remove", "create"]


# ---------------------------------------------------------------------------
# spawn_gate_marker_sync — fire-and-forget, gated agents only by default
# ---------------------------------------------------------------------------

@pytest.fixture
def synced(monkeypatch):
    seen = []

    async def _sync(agent):
        seen.append(agent)
        return True

    monkeypatch.setattr(_GATE, "sync_gate_marker", _sync)
    return seen


def _settle():
    async def main(calls):
        results = [call() for call in calls]
        for _ in range(5):
            await asyncio.sleep(0)
        return results
    return main


def test_a_start_syncs_only_a_gated_agent(gates, synced):
    results = asyncio.run(_settle()([
        lambda: _GATE.spawn_gate_marker_sync("fin"),
        lambda: _GATE.spawn_gate_marker_sync("nobody"),
    ]))
    assert results == [True, False]
    assert synced == ["fin"]


def test_a_heal_syncs_whatever_the_map_says(gates, synced):
    results = asyncio.run(_settle()([
        lambda: _GATE.spawn_gate_marker_sync("nobody", only_if_gated=False),
    ]))
    assert results == [True]
    assert synced == ["nobody"]


def test_an_unreadable_map_spawns_nothing(gates, synced):
    gates["fail"] = "raise"
    assert asyncio.run(_settle()([lambda: _GATE.spawn_gate_marker_sync("fin")])) == [False]
    assert synced == []


def test_outside_a_running_loop_it_does_nothing(gates, synced):
    assert _GATE.spawn_gate_marker_sync("fin") is False
    assert synced == []


def test_the_spawned_sync_is_held_until_it_finishes(gates, monkeypatch):
    """asyncio keeps only a weak reference to a task; an unreferenced
    fire-and-forget task can be collected mid-flight."""
    async def main():
        gate = asyncio.Event()

        async def _sync(agent):
            await gate.wait()
            return True

        monkeypatch.setattr(_GATE, "sync_gate_marker", _sync)
        assert _GATE.spawn_gate_marker_sync("fin") is True
        await asyncio.sleep(0)
        held = len(_GATE._inflight)
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        return held, len(_GATE._inflight)

    held, after = asyncio.run(main())
    assert held >= 1 and after == held - 1


# ---------------------------------------------------------------------------
# The lifecycle tails — a start, and a recreate (which drops the marker)
# ---------------------------------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(_GATE, "spawn_gate_marker_sync",
                        lambda agent, **kw: calls.append((agent, kw)) or True)
    return calls


class _Container:
    def __init__(self, status="running"):
        self.status = status
        self.attrs = {"Config": {"Image": "trinity-agent-base:latest", "Env": ["AGENT_NAME=fin"],
                                 "Labels": {"trinity.ssh-port": "2299"}},
                      "HostConfig": {}, "Mounts": []}
        self.id = "c0ffee"


def _start(monkeypatch, *, running=True):
    from services.agent_service import lifecycle as lc
    container = _Container("running" if running else "exited")
    for name in ("check_shared_folder_mounts_match", "check_base_image_matches"):
        monkeypatch.setattr(lc, name, AsyncMock(return_value=True))
    for name in ("check_public_folder_mount_matches", "check_api_key_env_matches",
                 "check_github_pat_env_matches", "check_resource_limits_match",
                 "check_full_capabilities_match", "check_guardrails_env_matches",
                 "check_agent_auth_token_env_matches", "check_agent_mcp_key_matches"):
        monkeypatch.setattr(lc, name, MagicMock(return_value=True))
    monkeypatch.setattr(lc, "get_agent_container", MagicMock(return_value=container))
    monkeypatch.setattr(lc, "container_reload", AsyncMock())
    monkeypatch.setattr(lc, "container_start", AsyncMock())
    monkeypatch.setattr(lc, "clear_agent_breakers", MagicMock(), raising=False)
    db_mock = MagicMock()
    db_mock.get_agent_owner.return_value = "owner"
    db_mock.get_agent_ephemeral_info.return_value = None
    db_mock.get_read_only_mode.return_value = {"enabled": False}
    db_mock.get_git_auto_sync_enabled.return_value = False
    monkeypatch.setattr(lc, "db", db_mock)
    for name in ("wait_for_agent_ready", "inject_assigned_credentials", "inject_assigned_skills",
                 "inject_read_only_hooks", "remove_read_only_hooks"):
        monkeypatch.setattr(lc, name, AsyncMock(return_value={"status": "stubbed", "success": True}))
    import services.git_service as gs
    monkeypatch.setattr(gs, "spawn_git_remote_token_scrub", MagicMock())
    import services.metric_registry as mr
    monkeypatch.setattr(mr, "spawn_refresh_from_running_agent", MagicMock())
    return asyncio.run(lc.start_agent_internal("fin"))


@pytest.mark.parametrize("running", [True, False], ids=["already-running", "cold-start"])
def test_every_start_syncs_the_marker(monkeypatch, spawned, running):
    result = _start(monkeypatch, running=running)
    assert result["message"] == "Agent fin started"
    assert spawned == [("fin", {})]


def test_a_failing_sync_spawn_never_fails_the_start(monkeypatch):
    def _boom(agent, **kw):
        raise RuntimeError("marker spawn exploded")

    monkeypatch.setattr(_GATE, "spawn_gate_marker_sync", _boom)
    assert _start(monkeypatch)["message"] == "Agent fin started"


def _recreate(monkeypatch, *, provision_raises=None):
    from services.agent_service import lifecycle as lc
    import services.agent_service.pull_mode as pm
    db_mock = MagicMock()
    db_mock.get_agent_subscription_id.return_value = None
    db_mock.get_use_platform_api_key.return_value = False
    db_mock.get_guardrails_config.return_value = None
    db_mock.get_resource_limits.return_value = None
    db_mock.get_public_mount_path.return_value = "/home/developer/public"
    monkeypatch.setattr(lc, "db", db_mock)
    monkeypatch.setattr(lc, "validate_base_image", MagicMock())
    monkeypatch.setattr(lc, "_apply_git_env_from_db", MagicMock())
    monkeypatch.setattr(lc, "derive_agent_token", MagicMock(return_value="tok"))
    monkeypatch.setattr(lc, "get_agent_default_resources", MagicMock(return_value={"cpu": "2", "memory": "4g"}))
    monkeypatch.setattr(lc, "get_agent_full_capabilities", MagicMock(return_value=False))
    monkeypatch.setattr(lc, "image_get", AsyncMock(return_value=SimpleNamespace(labels={})))
    monkeypatch.setattr(lc, "container_stop", AsyncMock())
    monkeypatch.setattr(lc, "container_remove", AsyncMock())
    monkeypatch.setattr(lc, "reserve_port_for_recreate", MagicMock())
    monkeypatch.setattr(pm, "pull_mode_env_vars", lambda agent: {})
    new = _Container("running")
    provision = AsyncMock(return_value=new, side_effect=provision_raises)
    monkeypatch.setattr(lc, "_provision_folders_and_run_agent_container", provision)
    result = asyncio.run(lc.recreate_container_with_updated_config("fin", _Container(), "owner"))
    return result, new, provision


def test_a_recreate_syncs_the_marker_on_the_new_container(monkeypatch, spawned):
    result, new, provision = _recreate(monkeypatch)
    assert result is new
    provision.assert_awaited_once()
    assert spawned == [("fin", {})]


def test_a_recreate_that_did_not_produce_a_container_syncs_nothing(monkeypatch, spawned):
    import docker
    with pytest.raises(docker.errors.APIError):
        _recreate(monkeypatch, provision_raises=docker.errors.APIError("boom"))
    assert spawned == []
