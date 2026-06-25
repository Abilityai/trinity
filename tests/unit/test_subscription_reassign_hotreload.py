"""
Unit tests for subscription hot-reload on the router paths (#1089).

Two producer paths beyond the SUB-003 auto-switch:
  - Manual reassignment (PUT /api/subscriptions/agents/{name}, T4): a sub→sub
    swap hot-reloads the token in place (in-flight turns survive, no
    container_stop) under the #799 per-agent lock; an auth-MODE change
    (none/api-key → subscription) still recreates so ANTHROPIC_API_KEY is
    dropped and the OAuth token is baked into Config.Env.
  - Key rollover (POST /api/subscriptions upsert, T5): re-registering a
    subscription's token fans a best-effort hot-reload out to every running
    agent on that subscription; one agent's failure never fails the upsert nor
    blocks the others.

Module: src/backend/routers/subscriptions.py
        src/backend/services/subscription_auto_switch.py

NOTE: `routers.subscriptions` (and `db_models`) are imported lazily inside the
fixtures rather than at module top. tests/unit/test_subscription_auto_switch_pingpong.py
pops `utils` from sys.modules at collection time; importing the full `routers`
package (which transitively needs `utils.url_validation`) at MODULE TOP here
would then fail collection if that file is collected first. The conftest's
autouse `_restore_unit_sys_modules` restores `utils` before each test runs, so
a fixture-time (test-run) import is safe regardless of collection order.
"""

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest


def _live_auto_switch():
    """Return the live ``services.subscription_auto_switch`` object the endpoint
    actually calls into, resolved via ``sys.modules`` — NOT the ``services``
    package attribute.

    ``routers/subscriptions.py`` reaches the hot-reload helpers through a
    function-local ``from services.subscription_auto_switch import (...)``, which
    binds from ``sys.modules["services.subscription_auto_switch"]``. A plain
    ``import services.subscription_auto_switch as x`` instead binds ``x`` from the
    ``services`` *package attribute*. The conftest's autouse #762 fixture restores
    ``sys.modules["services"]`` before/after every test, and under some
    pytest-randomly orderings (seed 99999) the package attribute and the
    ``sys.modules`` submodule entry drift to two different module objects. Patching
    the package-attribute object then misses the one the endpoint calls, so the
    real helper runs and the test flakes (#1089). Resolving via ``sys.modules``
    keeps the fixture's patch in lockstep with the endpoint regardless of order.
    """
    import services.subscription_auto_switch  # noqa: F401 — ensure it is imported
    return sys.modules["services.subscription_auto_switch"]


@pytest.fixture
def owner_user():
    u = MagicMock()
    u.username = "owner"
    u.role = "user"
    return u


@pytest.fixture
def admin_user():
    u = MagicMock()
    u.username = "admin"
    u.role = "admin"
    return u


@pytest.fixture
def manual_env(monkeypatch):
    """Stub the db + the local-import targets the manual-reassign endpoint
    reaches, and spy both the hot-reload helper and the recreate path so each
    test can assert exactly which one ran."""
    import routers.subscriptions as rs  # lazy: see module docstring

    fake_db = MagicMock()
    fake_db.can_user_share_agent.return_value = True
    sub = MagicMock()
    sub.id = "sub-b"
    sub.name = "sub-B"
    fake_db.get_subscription_by_name.return_value = sub
    monkeypatch.setattr(rs, "db", fake_db)

    container = object()
    docker_service = types.ModuleType("services.docker_service")
    docker_service.get_agent_container = lambda name: container
    docker_service.get_agent_status_from_container = (
        lambda c: types.SimpleNamespace(status="running")
    )
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service)

    stop_calls: list = []
    docker_utils = types.ModuleType("services.docker_utils")

    async def _stop(c):
        stop_calls.append(c)

    docker_utils.container_stop = _stop
    monkeypatch.setitem(sys.modules, "services.docker_utils", docker_utils)

    start_calls: list = []
    agent_service = types.ModuleType("services.agent_service")

    async def _start(name):
        start_calls.append(name)

    agent_service.start_agent_internal = _start
    monkeypatch.setitem(sys.modules, "services.agent_service", agent_service)

    auto_switch = _live_auto_switch()

    hot_calls: list = []

    async def _hot(name):
        hot_calls.append(name)
        return "hot_reloaded"

    monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token", _hot)

    lock_acquired: list = []

    async def _lock(name):
        lock_acquired.append(name)
        return asyncio.Lock()

    monkeypatch.setattr(auto_switch, "agent_switch_lock", _lock)

    return types.SimpleNamespace(
        rs=rs,
        db=fake_db,
        container=container,
        stop_calls=stop_calls,
        start_calls=start_calls,
        hot_calls=hot_calls,
        lock_acquired=lock_acquired,
    )


class TestManualReassignHotReload:
    """T4 — PUT /api/subscriptions/agents/{name}."""

    @pytest.mark.asyncio
    async def test_sub_to_sub_hot_reloads_no_container_stop(self, manual_env, owner_user):
        """A sub→sub swap on a running agent hot-reloads the token under the
        per-agent lock — the container is NOT stopped/recreated."""
        manual_env.db.get_agent_subscription_id.return_value = "sub-a"  # already on a sub

        result = await manual_env.rs.assign_subscription_to_agent(
            agent_name="agent-x",
            subscription_name="sub-B",
            current_user=owner_user,
        )

        assert manual_env.hot_calls == ["agent-x"]  # hot-reload taken
        assert manual_env.stop_calls == []  # NO container recreate
        assert manual_env.start_calls == []
        assert manual_env.lock_acquired == ["agent-x"]  # under the #799 lock
        assert result["restart_result"] == "hot_reloaded"
        # DB switched before applying the token
        manual_env.db.assign_subscription_to_agent.assert_called_once_with("agent-x", "sub-b")

    @pytest.mark.asyncio
    async def test_mode_change_none_to_sub_still_recreates(self, manual_env, owner_user):
        """An auth-mode change (no prior subscription → subscription) keeps the
        recreate path so ANTHROPIC_API_KEY is dropped and the token is baked in."""
        manual_env.db.get_agent_subscription_id.return_value = None  # api-key/none → sub

        result = await manual_env.rs.assign_subscription_to_agent(
            agent_name="agent-x",
            subscription_name="sub-B",
            current_user=owner_user,
        )

        assert manual_env.hot_calls == []  # hot-reload NOT taken
        assert manual_env.stop_calls == [manual_env.container]  # recreate path
        assert manual_env.start_calls == ["agent-x"]
        assert manual_env.lock_acquired == ["agent-x"]  # still serialized
        assert result["restart_result"] == "success"

    @pytest.mark.asyncio
    async def test_old_sub_snapshot_read_under_lock(self, manual_env, owner_user, monkeypatch):
        """#1089 TOCTOU: the agent's CURRENT subscription is snapshotted AFTER the
        per-agent switch lock is entered, never before. Reading it outside the
        lock lets a concurrent auto-switch change the assignment between the read
        and the assign, so the recreate-vs-hot-reload branch could be chosen
        against a stale `old_sub_id`."""
        auto_switch = _live_auto_switch()

        order: list[str] = []

        class _RecordingLock:
            async def __aenter__(self):
                order.append("lock_enter")
                return self

            async def __aexit__(self, *exc):
                order.append("lock_exit")
                return False

        async def _lock(name):
            return _RecordingLock()

        monkeypatch.setattr(auto_switch, "agent_switch_lock", _lock)

        def _read_sub(name):
            order.append("read_sub")
            return "sub-a"  # already on a sub → hot-reload branch

        manual_env.db.get_agent_subscription_id.side_effect = _read_sub

        await manual_env.rs.assign_subscription_to_agent(
            agent_name="agent-x",
            subscription_name="sub-B",
            current_user=owner_user,
        )

        # the snapshot read happens strictly INSIDE the lock window
        assert order == ["lock_enter", "read_sub", "lock_exit"]
        assert manual_env.hot_calls == ["agent-x"]  # branch chosen off the in-lock read

    @pytest.mark.asyncio
    async def test_non_owner_rejected(self, manual_env, owner_user):
        """Owner/admin gate is unchanged — a non-owner gets 403 before any switch."""
        from fastapi import HTTPException

        manual_env.db.can_user_share_agent.return_value = False

        with pytest.raises(HTTPException) as exc:
            await manual_env.rs.assign_subscription_to_agent(
                agent_name="agent-x",
                subscription_name="sub-B",
                current_user=owner_user,
            )
        assert exc.value.status_code == 403
        assert manual_env.hot_calls == []
        assert manual_env.stop_calls == []


@pytest.fixture
def register_env(monkeypatch):
    """Stub the db + the key-rollover fan-out for the register/upsert endpoint."""
    import routers.subscriptions as rs  # lazy: see module docstring

    fake_db = MagicMock()
    fake_db.get_user_by_username.return_value = {"id": 1}
    created = MagicMock()
    created.id = "sub-x"
    created.name = "sub-X"
    fake_db.create_subscription.return_value = created
    monkeypatch.setattr(rs, "db", fake_db)

    # register_subscription 503s without an encryption key configured.
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "0" * 64)

    auto_switch = _live_auto_switch()

    fanout_calls: list = []

    async def _fanout(sub_id):
        fanout_calls.append(sub_id)
        return {}

    monkeypatch.setattr(auto_switch, "reload_subscription_for_all_agents", _fanout)

    return types.SimpleNamespace(
        rs=rs, db=fake_db, created=created, fanout_calls=fanout_calls, auto_switch=auto_switch
    )


class TestRegisterKeyRollover:
    """T5 / F1 — POST /api/subscriptions upsert fans a hot-reload out to every
    running agent on that subscription, best-effort."""

    @pytest.mark.asyncio
    async def test_upsert_fans_out_hot_reload(self, register_env, admin_user):
        from db_models import SubscriptionCredentialCreate

        request = SubscriptionCredentialCreate(name="sub-X", token="sk-ant-oat01-rolled")

        result = await register_env.rs.register_subscription(request, current_user=admin_user)

        assert result is register_env.created
        assert register_env.fanout_calls == ["sub-x"]  # fanned out to the upserted sub id

    @pytest.mark.asyncio
    async def test_fan_out_failure_does_not_fail_upsert(self, register_env, admin_user, monkeypatch):
        """Best-effort: a fan-out blow-up is logged and swallowed — the upsert
        still succeeds and returns the registered subscription."""
        from db_models import SubscriptionCredentialCreate

        async def _boom(sub_id):
            raise RuntimeError("redis down")

        monkeypatch.setattr(register_env.auto_switch, "reload_subscription_for_all_agents", _boom)

        request = SubscriptionCredentialCreate(name="sub-X", token="sk-ant-oat01-rolled")

        result = await register_env.rs.register_subscription(request, current_user=admin_user)

        assert result is register_env.created  # upsert NOT failed by the fan-out error

    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, register_env, owner_user):
        """register_subscription is admin-only — a non-admin gets 403 before any
        create or key-rollover fan-out (mirrors the owner gate on reassign)."""
        from fastapi import HTTPException
        from db_models import SubscriptionCredentialCreate

        request = SubscriptionCredentialCreate(name="sub-X", token="sk-ant-oat01-rolled")

        with pytest.raises(HTTPException) as exc:
            await register_env.rs.register_subscription(request, current_user=owner_user)

        assert exc.value.status_code == 403
        assert register_env.fanout_calls == []  # never reached the rollover fan-out
        register_env.db.create_subscription.assert_not_called()
