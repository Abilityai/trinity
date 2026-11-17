"""
`purge_agent_now` — remove an agent at once, audit first (abilityai/trinity-enterprise#1055).

Agent delete is a SOFT delete (#834): the container goes, but volumes, sharing,
permissions, the MCP key and every child row are kept for the retention window
(default 180 days) until the cleanup sweep runs `purge_agent_ownership`. Removing
an invited member's agent must leave nothing behind but the audit trail, so the
platform needs the sweep's purge as an immediate, edition-agnostic primitive.

What these tests pin, at the service layer on the real production schema
(SQLite, `db_harness`), with Docker and Redis stubbed:

* the audit row is written BEFORE any row goes, and it survives the purge;
* the ownership row and its children (sharing, permissions in both directions,
  the agent's MCP key) are gone; the container is removed; per-agent Redis state
  is cleared; the data volumes are removed under both identities of a renamed
  agent, never a base another agent still claims;
* a Docker fault does not stop the rows from going (#2196: Docker unreadable is
  not "nothing to do");
* if the audit row cannot be written, nothing is destroyed (503);
* a person caller must be a human admin — an agent key carrying an admin
  owner's role is refused, as is a non-admin; the platform itself (`actor=None`)
  is allowed; a system agent and an ephemeral agent are refused;
* an agent with no row at all is a no-op, not an error.

The module is loaded by file path so the test does not pull the whole
`services.agent_service` package (crud, lifecycle, Docker) in through its
`__init__`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from db_harness import run as _hrun, scalar as _hscalar, sqlite_only_backend  # noqa: E402,F401

from fastapi import HTTPException  # noqa: E402

from models import User  # noqa: E402

_PURGE_PATH = Path(_BACKEND) / "services" / "agent_service" / "purge.py"
_TS = "2026-01-01T00:00:00Z"
AGENT = "member-one"
OTHER = "helper-agent"


def _run(coro):
    return asyncio.run(coro)


def _load_purge(monkeypatch):
    spec = importlib.util.spec_from_file_location("_ent1055_purge", _PURGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    # a @dataclass resolves its module through sys.modules at class creation
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def _count(table: str, where: str, **binds) -> int:
    return _hscalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", **binds) or 0


def _seed(name: str = AGENT, *, deleted_at=None, volume_base=None, ephemeral=False):
    _hrun("INSERT INTO users (id, username, email, role, created_at, updated_at) "
          "VALUES (1, 'owner', 'owner@example.com', 'creator', :ts, :ts)", ts=_TS)
    _hrun("INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
          "VALUES (:n, 1, :ts, :d)", n=name, ts=_TS, d=deleted_at)
    _hrun("INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
          "VALUES (:n, 1, :ts)", n=OTHER, ts=_TS)
    if volume_base:
        _hrun("UPDATE agent_ownership SET volume_base_name = :b WHERE agent_name = :n",
              b=volume_base, n=name)
    if ephemeral:
        _hrun("UPDATE agent_ownership SET is_ephemeral = 1 WHERE agent_name = :n", n=name)
    _hrun("INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
          "VALUES (:n, 'member@example.com', 1, :ts)", n=name, ts=_TS)
    _hrun("INSERT INTO agent_permissions (source_agent, target_agent, created_by) "
          "VALUES (:n, :o, 'owner')", n=name, o=OTHER)
    _hrun("INSERT INTO agent_permissions (source_agent, target_agent, created_by) "
          "VALUES (:o, :n, 'owner')", n=name, o=OTHER)
    _hrun("INSERT INTO mcp_api_keys (id, name, key_prefix, key_hash, created_at, user_id, "
          "agent_name, scope) VALUES (:id, :n, 'pfx', :kh, :ts, 1, :n, 'agent')",
          id=f"key-{name}", n=name, kh=f"hash-{name}", ts=_TS)


def _rows_left(name: str = AGENT) -> dict:
    return {
        "agent_ownership": _count("agent_ownership", "agent_name = :n", n=name),
        "agent_sharing": _count("agent_sharing", "agent_name = :n", n=name),
        "agent_permissions": _count(
            "agent_permissions", "source_agent = :n OR target_agent = :n", n=name),
        "mcp_api_keys": _count("mcp_api_keys", "agent_name = :n", n=name),
    }


def _audit_rows(name: str = AGENT) -> int:
    return _count("audit_log",
                  "event_type = 'agent_lifecycle' AND event_action = 'purge' "
                  "AND target_id = :n", n=name)


ADMIN = User(id=1, username="admin", email="admin@example.com", role="admin")


@pytest.fixture
def world(sqlite_only_backend, monkeypatch):
    """Stub every Docker / Redis collaborator the purge reaches lazily."""
    import services

    container = MagicMock(name="container")
    docker_service = MagicMock()
    docker_service.get_agent_container = MagicMock(return_value=container)
    docker_utils = MagicMock()
    docker_utils.container_stop = AsyncMock()
    docker_utils.container_remove = AsyncMock()
    docker_utils.remove_agent_volumes = AsyncMock(return_value=3)
    capacity = MagicMock()
    capacity.cancel_all_overflow = AsyncMock()
    capacity_manager = MagicMock(get_capacity_manager=MagicMock(return_value=capacity))
    runtime_state = MagicMock(clear_agent_runtime_state=AsyncMock())
    skill_gate = MagicMock(cancel_pending_for_agent=AsyncMock())
    for name, mod in {
        "docker_service": docker_service,
        "docker_utils": docker_utils,
        "capacity_manager": capacity_manager,
        "agent_runtime_state": runtime_state,
        "skill_gate_service": skill_gate,
    }.items():
        monkeypatch.setitem(sys.modules, f"services.{name}", mod)
        monkeypatch.setattr(services, name, mod, raising=False)
    return {
        "purge": _load_purge(monkeypatch),
        "container": container,
        "docker_service": docker_service,
        "docker_utils": docker_utils,
        "capacity": capacity,
        "runtime_state": runtime_state,
    }


class TestPurgesEverything:
    def test_live_agent_rows_go_and_audit_row_stays(self, world):
        _seed()
        result = _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))

        assert result.purged is True
        assert _rows_left() == {
            "agent_ownership": 0, "agent_sharing": 0,
            "agent_permissions": 0, "mcp_api_keys": 0,
        }
        # the other agent is untouched
        assert _count("agent_ownership", "agent_name = :n", n=OTHER) == 1
        assert _audit_rows() == 1
        assert result.audit_id == _hscalar(
            "SELECT event_id FROM audit_log WHERE event_action = 'purge' "
            "AND target_id = :n", n=AGENT)

    def test_container_runtime_state_and_volumes_are_removed(self, world):
        _seed()
        _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))

        world["docker_utils"].container_remove.assert_awaited()
        world["capacity"].cancel_all_overflow.assert_awaited()
        world["runtime_state"].clear_agent_runtime_state.assert_awaited_with(AGENT)
        world["docker_utils"].remove_agent_volumes.assert_awaited_once_with(AGENT)

    def test_soft_deleted_agent_is_purged_too(self, world):
        _seed(deleted_at=_TS)
        result = _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        assert result.purged is True
        assert _rows_left()["agent_ownership"] == 0

    def test_renamed_agent_volumes_go_under_both_identities(self, world):
        _seed(volume_base="old-name")
        _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        bases = [c.args[0] for c in world["docker_utils"].remove_agent_volumes.await_args_list]
        assert bases == ["old-name", AGENT]

    def test_a_volume_base_another_agent_claims_is_left_alone(self, world):
        _seed(volume_base=OTHER)  # collision: the live agent OTHER owns that base
        _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        bases = [c.args[0] for c in world["docker_utils"].remove_agent_volumes.await_args_list]
        assert bases == [AGENT]

    def test_the_platform_itself_may_purge(self, world):
        _seed()
        result = _run(world["purge"].purge_agent_now(AGENT, actor=None))
        assert result.purged is True
        assert _hscalar("SELECT actor_type FROM audit_log WHERE event_action = 'purge'") == "system"


class TestAuditFirst:
    def test_audit_row_is_written_while_the_agent_still_exists(self, world):
        _seed()
        purge = world["purge"]
        seen = {}
        real_log = purge.platform_audit_service.log

        async def spy(*args, **kwargs):
            seen["ownership_rows_at_audit"] = _rows_left()["agent_ownership"]
            seen["container_removed_at_audit"] = world["docker_utils"].container_remove.await_count
            return await real_log(*args, **kwargs)

        with patch.object(purge.platform_audit_service, "log", side_effect=spy):
            _run(purge.purge_agent_now(AGENT, actor=ADMIN))
        assert seen == {"ownership_rows_at_audit": 1, "container_removed_at_audit": 0}

    def test_no_audit_row_means_nothing_is_destroyed(self, world):
        _seed()
        purge = world["purge"]
        with patch.object(purge.platform_audit_service, "log", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                _run(purge.purge_agent_now(AGENT, actor=ADMIN))
        assert exc.value.status_code == 503
        assert _rows_left()["agent_ownership"] == 1
        world["docker_utils"].container_remove.assert_not_awaited()
        world["docker_utils"].remove_agent_volumes.assert_not_awaited()


class TestDockerFault:
    def test_docker_unreadable_still_purges_rows(self, world):
        _seed()
        world["docker_service"].get_agent_container.side_effect = RuntimeError("daemon down")
        result = _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        assert result.purged is True
        assert _rows_left()["agent_ownership"] == 0

    def test_container_and_volume_errors_do_not_stop_the_purge(self, world):
        _seed()
        world["docker_utils"].container_remove.side_effect = RuntimeError("busy")
        world["docker_utils"].remove_agent_volumes.side_effect = RuntimeError("in use")
        result = _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        assert result.purged is True
        assert _rows_left() == {
            "agent_ownership": 0, "agent_sharing": 0,
            "agent_permissions": 0, "mcp_api_keys": 0,
        }


class TestRefusals:
    @pytest.mark.parametrize("actor", [
        User(id=2, username="creator", role="creator"),
        # an agent key resolves to its owner CARRYING the owner's role
        User(id=1, username="admin", role="admin", agent_name=OTHER, mcp_scope="agent"),
    ])
    def test_a_caller_who_is_not_a_human_admin_is_refused(self, world, actor):
        _seed()
        with pytest.raises(HTTPException) as exc:
            _run(world["purge"].purge_agent_now(AGENT, actor=actor))
        assert exc.value.status_code == 403
        assert _rows_left()["agent_ownership"] == 1
        assert _audit_rows() == 0
        world["docker_utils"].container_remove.assert_not_awaited()

    def test_system_agent_is_refused(self, world):
        _seed()
        _hrun("UPDATE agent_ownership SET is_system = 1 WHERE agent_name = :n", n=AGENT)
        with pytest.raises(HTTPException) as exc:
            _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        assert exc.value.status_code == 403
        assert _rows_left()["agent_ownership"] == 1

    def test_ephemeral_agent_is_refused(self, world):
        _seed(ephemeral=True)
        with pytest.raises(HTTPException) as exc:
            _run(world["purge"].purge_agent_now(AGENT, actor=ADMIN))
        assert exc.value.status_code == 409
        assert _rows_left()["agent_ownership"] == 1

    def test_no_row_is_a_noop(self, world):
        result = _run(world["purge"].purge_agent_now("never-existed", actor=ADMIN))
        assert result.purged is False
        assert result.audit_id is None
        assert _audit_rows("never-existed") == 0
        world["docker_utils"].container_remove.assert_not_awaited()
