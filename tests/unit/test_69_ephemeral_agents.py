"""
Tests for Ephemeral "Ghost" Agents (trinity-enterprise#69).

Covers the OSS lifecycle mechanics: schema columns (live-select, the 4-file
rule guard), the Ephemeral db mixin accessors through the REAL engine
(db_harness — never a wholesale-mocked ``database``), the DatabaseManager
facade delegations (manual facade — learnings 2026-07-06), the
CapacityManager admission gate, the ephemeral key fence, the Part 2
parent-control guards, the apply_result budget hook's fail-open discipline,
and the discard primitive's idempotency/crash-convergence at the unit level.

Related flow: docs/memory/feature-flows/ephemeral-agents.md
"""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from db_harness import (  # noqa: E402
    db_backend,  # noqa: F401 - pytest fixture
    seed_agent,
    seed_execution,
    seed_schedule,
    seed_user,
)

# ---------------------------------------------------------------------------
# #762/#1446 leak defense: sibling test modules install session-persistent
# sys.modules stubs (module-level `setdefault`, never restored) for
# `database` / `services.*` / `dependencies`. Under pytest-randomly, one of
# them executing before this file makes our call-time imports resolve to a
# stub (MagicMock db, SimpleNamespace dependencies) and these tests fail in
# the full suite while passing in isolation. Capture the REAL module objects
# at collection time (all known offenders sort after `test_69_*`
# alphabetically, so collection imports here are leak-free) and re-own them
# per test via `monkeypatch.setitem` (auto-restored, last-write-wins).
# ---------------------------------------------------------------------------
_OWNED_MODULE_NAMES = (
    "database",
    "dependencies",
    "models",
    "utils.helpers",
    "redis_breaker_util",
    "services",
    "services.settings_service",
    "services.capacity_manager",
    "services.task_execution_service",
    "services.platform_audit_service",
    "services.agent_service",
    "services.agent_service.ephemeral",
    "services.docker_service",
    "services.docker_utils",
    "services.agent_runtime_state",
    "db",
    "db.engine",
    "db.tables",
    "db.agents",
    "db.users",
    "db.schedules",
    "db.permissions",
    "db.agent_cleanup",
    "db.agent_settings",
    "db.agent_settings.ephemeral",
)
_REAL_MODULES = {
    name: importlib.import_module(name) for name in _OWNED_MODULE_NAMES
}


@pytest.fixture(autouse=True)
def _own_real_modules(monkeypatch):
    for name, mod in _REAL_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _register_ghost(
    agent_ops,
    name="ghost-ab12",
    owner="owner",
    max_executions=2,
    expires_at="2099-01-01T00:00:00.000000Z",
    spawned_by=None,
    spawned_by_key_id=None,
):
    assert agent_ops.register_agent_owner(
        name,
        owner,
        is_ephemeral=True,
        ephemeral_max_executions=max_executions,
        ephemeral_expires_at=expires_at,
        spawned_by_agent=spawned_by,
        spawned_by_key_id=spawned_by_key_id,
        max_parallel_tasks=1,
    )


@pytest.fixture
def agent_ops(db_backend):  # noqa: F811 - fixture chain
    """Real AgentOperations over the harness engine (no mocked database)."""
    from db.agents import AgentOperations
    from db.users import UserOperations

    seed_user(1, "owner")
    return AgentOperations(UserOperations())


# ---------------------------------------------------------------------------
# Schema: live-select of every new column (the tables.py trap — learnings
# 2026-06-23: schema-parity does NOT cover tables.py)
# ---------------------------------------------------------------------------


def test_new_columns_live_select(db_backend):  # noqa: F811
    from sqlalchemy import select

    from db.engine import get_engine
    from db.tables import agent_ownership

    seed_user(1, "owner")
    seed_agent("plain-1", owner_id=1)
    stmt = select(
        agent_ownership.c.is_ephemeral,
        agent_ownership.c.ephemeral_max_executions,
        agent_ownership.c.ephemeral_expires_at,
        agent_ownership.c.spawned_by_agent,
        agent_ownership.c.spawned_by_key_id,
    ).where(agent_ownership.c.agent_name == "plain-1")
    with get_engine().connect() as conn:
        row = conn.execute(stmt).mappings().first()
    assert row is not None
    assert not row["is_ephemeral"]  # NULL/0 for durable agents


# ---------------------------------------------------------------------------
# Ephemeral mixin accessors (real engine)
# ---------------------------------------------------------------------------


def test_register_and_read_back_ephemeral(agent_ops):
    _register_ghost(agent_ops, spawned_by="parent-1", spawned_by_key_id="key-123")
    info = agent_ops.get_agent_ephemeral_info("ghost-ab12")
    assert info is not None
    assert info["is_ephemeral"] is True
    assert info["ephemeral_max_executions"] == 2
    assert info["ephemeral_expires_at"].startswith("2099-")
    assert info["spawned_by_agent"] == "parent-1"
    assert info["spawned_by_key_id"] == "key-123"
    assert info["owner_id"] == 1


def test_durable_agent_reads_not_ephemeral_with_provenance(agent_ops):
    assert agent_ops.register_agent_owner(
        "durable-child",
        "owner",
        spawned_by_agent="parent-1",
        spawned_by_key_id="key-123",
    )
    info = agent_ops.get_agent_ephemeral_info("durable-child")
    assert info["is_ephemeral"] is False
    assert info["spawned_by_agent"] == "parent-1"


def test_mark_discard_intent_expires_now_and_refuses_durable(agent_ops):
    _register_ghost(agent_ops)
    assert agent_ops.mark_ephemeral_discard_intent("ghost-ab12") is True
    info = agent_ops.get_agent_ephemeral_info("ghost-ab12")
    # 2099 stamp replaced with a now-ish (2026+) timestamp
    assert info["ephemeral_expires_at"] < "2099-"
    seed_agent("plain-1", owner_id=1)
    assert agent_ops.mark_ephemeral_discard_intent("plain-1") is False


def test_budget_usage_counts_terminal_and_active(agent_ops):
    _register_ghost(agent_ops)
    seed_schedule("s1", agent_name="ghost-ab12")
    seed_execution("s1", "ghost-ab12", exec_id="e1", status="success")
    seed_execution("s1", "ghost-ab12", exec_id="e2", status="failed")
    seed_execution("s1", "ghost-ab12", exec_id="e3", status="cancelled")
    seed_execution("s1", "ghost-ab12", exec_id="e4", status="skipped")  # NOT counted
    seed_execution("s1", "ghost-ab12", exec_id="e5", status="running")
    seed_execution("s1", "ghost-ab12", exec_id="e6", status="queued")
    usage = agent_ops.count_ephemeral_budget_usage("ghost-ab12")
    assert usage == {"terminal": 3, "active": 2}


def test_find_discardable_by_ttl_and_budget(agent_ops):
    # Expired TTL ghost
    _register_ghost(agent_ops, name="ghost-old", max_executions=None,
                    expires_at="2020-01-01T00:00:00.000000Z")
    # Live ghost under budget
    _register_ghost(agent_ops, name="ghost-live", max_executions=5)
    # Live ghost OVER budget
    _register_ghost(agent_ops, name="ghost-spent", max_executions=1)
    seed_schedule("s2", agent_name="ghost-spent")
    seed_execution("s2", "ghost-spent", exec_id="sp1", status="success")

    names = set(agent_ops.find_discardable_ephemeral_agents(limit=50))
    assert "ghost-old" in names
    assert "ghost-spent" in names
    assert "ghost-live" not in names
    # limit=0 is a no-op
    assert agent_ops.find_discardable_ephemeral_agents(limit=0) == []


def test_purge_allows_live_ephemeral_refuses_live_durable(agent_ops):
    _register_ghost(agent_ops)
    seed_agent("plain-1", owner_id=1)
    assert agent_ops.purge_ephemeral_agent_ownership("plain-1") is False
    assert agent_ops.get_agent_owner("plain-1") is not None  # untouched
    assert agent_ops.purge_ephemeral_agent_ownership("ghost-ab12") is True
    assert agent_ops.get_agent_ephemeral_info("ghost-ab12") is None
    # name is immediately reusable — no soft-delete reservation
    assert agent_ops.is_agent_name_reserved("ghost-ab12") is False
    # idempotent re-run
    assert agent_ops.purge_ephemeral_agent_ownership("ghost-ab12") is False


def test_purge_cascades_child_rows(agent_ops):
    from db.permissions import PermissionOperations
    from db.users import UserOperations

    _register_ghost(agent_ops, spawned_by="parent-1", spawned_by_key_id="k1")
    perms = PermissionOperations(UserOperations(), agent_ops)
    assert perms.add_permission("parent-1", "ghost-ab12", "spawn:parent-1")
    assert agent_ops.purge_ephemeral_agent_ownership("ghost-ab12") is True
    assert perms.get_permitted_agents("parent-1") == []


def test_count_live_ephemeral_for_owner(agent_ops):
    assert agent_ops.count_live_ephemeral_agents_for_owner(1) == 0
    _register_ghost(agent_ops, name="ghost-a")
    _register_ghost(agent_ops, name="ghost-b")
    seed_agent("plain-1", owner_id=1)  # durable — not counted
    assert agent_ops.count_live_ephemeral_agents_for_owner(1) == 2


def test_fail_all_nonterminal_for_agent(db_backend):  # noqa: F811
    from db.agents import AgentOperations
    from db.schedules import ScheduleOperations
    from db.users import UserOperations
    from db_harness import scalar

    seed_user(1, "owner")
    seed_schedule("s3", agent_name="ghost-x")
    seed_execution("s3", "ghost-x", exec_id="q1", status="queued")
    seed_execution("s3", "ghost-x", exec_id="r1", status="running")
    seed_execution("s3", "ghost-x", exec_id="p1", status="pending_retry")
    seed_execution("s3", "ghost-x", exec_id="ok1", status="success")

    user_ops = UserOperations()
    ops = ScheduleOperations(user_ops, AgentOperations(user_ops))
    assert ops.fail_all_nonterminal_for_agent("ghost-x", "ghost_discarded") == 3
    # the real terminal is untouched (CAS-style status filter)
    assert scalar(
        "SELECT status FROM schedule_executions WHERE id = 'ok1'"
    ) == "success"
    assert scalar(
        "SELECT error FROM schedule_executions WHERE id = 'r1'"
    ) == "ghost_discarded"
    assert scalar(
        "SELECT completed_at FROM schedule_executions WHERE id = 'q1'"
    ) is not None


# ---------------------------------------------------------------------------
# Facade delegations (manual facade — a missing one-liner is a runtime
# AttributeError that MagicMock'ed-database tests can never see)
# ---------------------------------------------------------------------------


def test_database_manager_delegations_exist():
    from database import DatabaseManager

    mgr = DatabaseManager.__new__(DatabaseManager)
    mgr._agent_ops = MagicMock()
    mgr._schedule_ops = MagicMock()

    mgr.get_agent_ephemeral_info("a")
    mgr._agent_ops.get_agent_ephemeral_info.assert_called_once_with("a")
    mgr.mark_ephemeral_discard_intent("a")
    mgr._agent_ops.mark_ephemeral_discard_intent.assert_called_once_with("a")
    mgr.count_ephemeral_budget_usage("a")
    mgr._agent_ops.count_ephemeral_budget_usage.assert_called_once_with("a")
    mgr.find_discardable_ephemeral_agents(7)
    mgr._agent_ops.find_discardable_ephemeral_agents.assert_called_once_with(7)
    mgr.count_live_ephemeral_agents_for_owner(1)
    mgr._agent_ops.count_live_ephemeral_agents_for_owner.assert_called_once_with(1)
    mgr.purge_ephemeral_agent_ownership("a")
    mgr._agent_ops.purge_ephemeral_agent_ownership.assert_called_once_with("a")
    mgr.fail_all_nonterminal_for_agent("a", "why")
    mgr._schedule_ops.fail_all_nonterminal_for_agent.assert_called_once_with("a", "why")
    mgr.register_agent_owner("a", "u", is_ephemeral=True, max_parallel_tasks=1)
    mgr._agent_ops.register_agent_owner.assert_called_once_with(
        "a", "u", False, False, is_ephemeral=True, max_parallel_tasks=1
    )


# ---------------------------------------------------------------------------
# CapacityManager admission gate (raised BEFORE any Redis/slot work)
# ---------------------------------------------------------------------------


@pytest.fixture
def gated_capacity(monkeypatch):
    """A CapacityManager whose Redis/slot layers must never be touched.

    Uses the collection-time _REAL_MODULES object — a bare import at fixture
    time can resolve a sibling test's leaked sys.modules stub (see
    _own_real_modules).
    """
    cm = _REAL_MODULES["services.capacity_manager"]

    manager = cm.CapacityManager.__new__(cm.CapacityManager)
    # Any slot/overflow access blows up the test — the gate must fire first.
    manager._slots = MagicMock()
    manager._slots.acquire_slot = AsyncMock(
        side_effect=AssertionError("gate must deny before slot work")
    )
    manager._redis = MagicMock()
    manager._backlog = MagicMock()
    return manager


@pytest.mark.asyncio
async def test_acquire_denies_expired_ghost(gated_capacity, monkeypatch):
    from database import db
    from services.capacity_manager import EphemeralBudgetExhausted

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {
            "is_ephemeral": True,
            "ephemeral_expires_at": "2020-01-01T00:00:00.000000Z",
            "ephemeral_max_executions": None,
        },
    )
    with pytest.raises(EphemeralBudgetExhausted) as exc:
        await gated_capacity.acquire(
            agent_name="ghost-a", execution_id="e1", max_concurrent=1
        )
    assert exc.value.reason == "expired"


@pytest.mark.asyncio
async def test_acquire_denies_budget_exhausted_counting_active(gated_capacity, monkeypatch):
    from database import db
    from services.capacity_manager import EphemeralBudgetExhausted

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {
            "is_ephemeral": True,
            "ephemeral_expires_at": "2099-01-01T00:00:00.000000Z",
            "ephemeral_max_executions": 3,
        },
    )
    # terminal+active >= max: 2 terminal + 1 running == 3 → deny (overshoot bound)
    monkeypatch.setattr(
        db, "count_ephemeral_budget_usage",
        lambda name: {"terminal": 2, "active": 1},
    )
    with pytest.raises(EphemeralBudgetExhausted) as exc:
        await gated_capacity.acquire(
            agent_name="ghost-a", execution_id="e1", max_concurrent=1
        )
    assert exc.value.reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_acquire_passes_durable_and_underbudget_ghost(gated_capacity, monkeypatch):
    from database import db

    gated_capacity._slots.acquire_slot = AsyncMock(return_value=True)
    # Durable agent: gate is a no-op
    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda name: None)
    result = await gated_capacity.acquire(
        agent_name="plain-a", execution_id="e1", max_concurrent=1
    )
    assert result.state == "admitted"
    # Under-budget ghost admits too
    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {
            "is_ephemeral": True,
            "ephemeral_expires_at": "2099-01-01T00:00:00.000000Z",
            "ephemeral_max_executions": 3,
        },
    )
    monkeypatch.setattr(
        db, "count_ephemeral_budget_usage",
        lambda name: {"terminal": 1, "active": 0},
    )
    result = await gated_capacity.acquire(
        agent_name="ghost-a", execution_id="e2", max_concurrent=1
    )
    assert result.state == "admitted"


@pytest.mark.asyncio
async def test_acquire_gate_fails_open_on_db_error(gated_capacity, monkeypatch):
    from database import db

    gated_capacity._slots.acquire_slot = AsyncMock(return_value=True)
    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    result = await gated_capacity.acquire(
        agent_name="ghost-a", execution_id="e1", max_concurrent=1
    )
    assert result.state == "admitted"


# ---------------------------------------------------------------------------
# Ephemeral key fence (dependencies)
# ---------------------------------------------------------------------------


def _req(method: str, path: str):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


@pytest.fixture
def ghost_fence(monkeypatch):
    # _REAL_MODULES objects, not bare imports — fixture-time imports can
    # resolve a sibling's leaked sys.modules stub (see _own_real_modules).
    dependencies = _REAL_MODULES["dependencies"]
    db = _REAL_MODULES["database"].db

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {"is_ephemeral": name.startswith("ghost")},
    )
    return dependencies._enforce_ephemeral_key_fence


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/agents/ghost-a/heartbeat"),
        ("POST", "/api/agents/ghost-a/executions/e-123/result"),
        ("POST", "/api/agents/ghost-a/reports"),
        ("POST", "/api/notifications"),
        ("GET", "/api/agents/ghost-a"),
        ("GET", "/api/agents/ghost-a/info"),
    ],
)
def test_fence_allows_own_surface(ghost_fence, method, path):
    ghost_fence(_req(method, path), "ghost-a")  # no raise


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/agents/sibling/heartbeat"),      # not self
        ("POST", "/api/agents/ghost-a/reports/extra"),  # not an allowed shape
        ("POST", "/api/agents"),                        # chain-spawn
        ("GET", "/api/agents/sibling/files/download"),  # sibling file read
        ("POST", "/api/agents/ghost-a/chat"),           # chat-as-owner
        ("DELETE", "/api/agents/sibling"),              # sibling delete
        ("PUT", "/api/agents/ghost-a/autonomy"),
    ],
)
def test_fence_denies_everything_else(ghost_fence, method, path):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        ghost_fence(_req(method, path), "ghost-a")
    assert exc.value.status_code == 403


def test_fence_noop_for_durable_agents_and_fails_open(monkeypatch):
    import dependencies
    from database import db

    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda name: None)
    dependencies._enforce_ephemeral_key_fence(_req("POST", "/api/agents"), "plain-a")
    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    dependencies._enforce_ephemeral_key_fence(_req("POST", "/api/agents"), "ghost-a")


# ---------------------------------------------------------------------------
# Part 2 guards
# ---------------------------------------------------------------------------


def _user(agent_name=None):
    from models import User

    return User(id=1, username="owner", email="o@example.com", role="admin",
                agent_name=agent_name)


def test_reject_agent_principal():
    from fastapi import HTTPException

    from dependencies import reject_agent_principal

    reject_agent_principal(_user(agent_name=None))  # human/system: pass
    with pytest.raises(HTTPException) as exc:
        reject_agent_principal(_user(agent_name="parent-1"))
    assert exc.value.status_code == 403


def test_enforce_agent_spawn_scope_matrix(monkeypatch):
    from fastapi import HTTPException

    from database import db
    from dependencies import enforce_agent_spawn_scope

    # Human principal: no-op regardless of target
    enforce_agent_spawn_scope(_user(agent_name=None), "any-agent")

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {
            "is_ephemeral": True,
            "spawned_by_agent": "parent-1",
            "spawned_by_key_id": "key-123",
        },
    )
    monkeypatch.setattr(
        db, "get_agent_mcp_api_key",
        lambda name: SimpleNamespace(id="key-123"),
    )
    # Matching name AND key id → allowed
    enforce_agent_spawn_scope(_user(agent_name="parent-1"), "ghost-a")

    # Wrong caller name → 403
    with pytest.raises(HTTPException):
        enforce_agent_spawn_scope(_user(agent_name="other-agent"), "ghost-a")

    # Right name, wrong (recycled) key id → 403 — name-only match is forgeable
    monkeypatch.setattr(
        db, "get_agent_mcp_api_key",
        lambda name: SimpleNamespace(id="key-NEW"),
    )
    with pytest.raises(HTTPException):
        enforce_agent_spawn_scope(_user(agent_name="parent-1"), "ghost-a")

    # No provenance at all → 403
    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda name: None)
    with pytest.raises(HTTPException):
        enforce_agent_spawn_scope(_user(agent_name="parent-1"), "ghost-a")


# ---------------------------------------------------------------------------
# apply_result budget hook: fail-open + triggers discard at budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_hook_fails_open_on_db_error(monkeypatch):
    from database import db
    from services.task_execution_service import _maybe_discard_exhausted_ephemeral

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    await _maybe_discard_exhausted_ephemeral("ghost-a")  # no raise


@pytest.mark.asyncio
async def test_budget_hook_triggers_discard_at_budget(monkeypatch):
    import services.agent_service.ephemeral as ephemeral_mod
    from database import db
    from services.task_execution_service import _maybe_discard_exhausted_ephemeral

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {"is_ephemeral": True, "ephemeral_max_executions": 2},
    )
    monkeypatch.setattr(
        db, "count_ephemeral_budget_usage",
        lambda name: {"terminal": 2, "active": 0},
    )
    fake_discard = AsyncMock()
    monkeypatch.setattr(ephemeral_mod, "discard_ephemeral_agent", fake_discard)
    await _maybe_discard_exhausted_ephemeral("ghost-a")
    fake_discard.assert_awaited_once_with("ghost-a", reason="budget_exhausted")


@pytest.mark.asyncio
async def test_budget_hook_noop_under_budget_and_for_durable(monkeypatch):
    import services.agent_service.ephemeral as ephemeral_mod
    from database import db
    from services.task_execution_service import _maybe_discard_exhausted_ephemeral

    fake_discard = AsyncMock()
    monkeypatch.setattr(ephemeral_mod, "discard_ephemeral_agent", fake_discard)

    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda name: None)
    await _maybe_discard_exhausted_ephemeral("plain-a")

    monkeypatch.setattr(
        db, "get_agent_ephemeral_info",
        lambda name: {"is_ephemeral": True, "ephemeral_max_executions": 5},
    )
    monkeypatch.setattr(
        db, "count_ephemeral_budget_usage",
        lambda name: {"terminal": 2, "active": 0},
    )
    await _maybe_discard_exhausted_ephemeral("ghost-a")
    fake_discard.assert_not_awaited()


# ---------------------------------------------------------------------------
# Discard primitive: idempotent, crash-convergent, refuses durable agents
# ---------------------------------------------------------------------------


@pytest.fixture
def discard_env(agent_ops, monkeypatch):
    """Real DB through the harness; Docker/capacity/audit/Redis stubbed.

    All patches target the `_REAL_MODULES` objects DIRECTLY — never string
    targets. A string target resolves through `sys.modules` at patch time,
    which under some pytest-randomly orderings is a sibling test's leaked
    stale entry, so the patch lands on the wrong module object while
    discard's call-time import (under the `_own_real_modules` pin) resolves
    the real one → the mock is never hit (CI seed 12345). Object-form
    patching + the pin makes patch target and call-time import provably the
    same object, regardless of fixture instantiation order.
    """
    ephemeral_mod = _REAL_MODULES["services.agent_service.ephemeral"]
    facade = _REAL_MODULES["database"].db

    # The primitive reads through the database facade — point the facade's
    # ops at the harness-backed instances (the facade object was constructed
    # against the conftest tmp DB; the harness re-pointed get_engine()).
    monkeypatch.setattr(facade, "_agent_ops", agent_ops, raising=False)
    from db.schedules import ScheduleOperations
    from db.users import UserOperations

    monkeypatch.setattr(
        facade,
        "_schedule_ops",
        ScheduleOperations(UserOperations(), agent_ops),
        raising=False,
    )

    # No Redis in unit tests → lock fail-open path
    monkeypatch.setattr(ephemeral_mod, "get_breaker_redis", lambda: None)

    # Docker: container present-then-removed by default
    fake_container = MagicMock()
    monkeypatch.setattr(
        _REAL_MODULES["services.docker_service"],
        "get_agent_container",
        lambda name: fake_container,
    )
    remove_mock = AsyncMock()
    monkeypatch.setattr(
        _REAL_MODULES["services.docker_utils"], "container_remove", remove_mock
    )

    # capacity overflow cancel
    cap = MagicMock()
    cap.cancel_all_overflow = AsyncMock(return_value=0)
    monkeypatch.setattr(
        _REAL_MODULES["services.capacity_manager"],
        "get_capacity_manager",
        lambda: cap,
    )

    # Redis runtime-state clear
    clear_mock = AsyncMock()
    monkeypatch.setattr(
        _REAL_MODULES["services.agent_runtime_state"],
        "clear_agent_runtime_state",
        clear_mock,
    )

    # Audit: patch the singleton INSTANCE's `log` method. Both alternatives
    # proved order-flaky under pytest-randomly: replacing the module attribute
    # loses to sibling stub leaks rebinding the module, and asserting the
    # persisted audit_log row depends on the real service's internals across
    # orderings (CI seed 12345 caught a 0-row run). The `_own_real_modules`
    # pin guarantees discard's call-time import resolves OUR real module,
    # whose attribute is this exact singleton object — so an instance-method
    # patch is deterministic.
    audit_log_mock = AsyncMock()
    monkeypatch.setattr(
        _REAL_MODULES["services.platform_audit_service"].platform_audit_service,
        "log",
        audit_log_mock,
    )

    return SimpleNamespace(
        remove=remove_mock, clear=clear_mock, capacity=cap, audit_log=audit_log_mock
    )


@pytest.mark.asyncio
async def test_discard_full_path_and_idempotent_rerun(discard_env, agent_ops):
    from services.agent_service.ephemeral import discard_ephemeral_agent

    _register_ghost(agent_ops, name="ghost-d1")
    seed_schedule("sd", agent_name="ghost-d1")
    seed_execution("sd", "ghost-d1", exec_id="rd", status="running")

    assert await discard_ephemeral_agent("ghost-d1", reason="test") is True
    # row purged, name free
    assert agent_ops.get_agent_ephemeral_info("ghost-d1") is None
    assert agent_ops.is_agent_name_reserved("ghost-d1") is False
    # in-flight execution terminal-ized
    from db_harness import scalar

    assert scalar(
        "SELECT status FROM schedule_executions WHERE id = 'rd'"
    ) == "failed"
    # ordering side effects ran
    discard_env.remove.assert_awaited()
    discard_env.clear.assert_awaited()
    # the audit seam fired with the discard action
    discard_env.audit_log.assert_awaited_once()
    assert discard_env.audit_log.await_args.kwargs.get("event_action") == "ephemeral_discard"

    # re-run converges to no-op without raising
    assert await discard_ephemeral_agent("ghost-d1", reason="test") is False


@pytest.mark.asyncio
async def test_discard_half_discarded_state_still_purges(discard_env, agent_ops, monkeypatch):
    """Crash residue (row live, container gone) must be discardable."""
    from services.agent_service.ephemeral import discard_ephemeral_agent

    _register_ghost(agent_ops, name="ghost-d2")
    monkeypatch.setattr(
        "services.docker_service.get_agent_container", lambda name: None
    )
    assert await discard_ephemeral_agent("ghost-d2", reason="resume") is True
    assert agent_ops.get_agent_ephemeral_info("ghost-d2") is None


@pytest.mark.asyncio
async def test_discard_refuses_durable_agent(discard_env, agent_ops):
    from services.agent_service.ephemeral import discard_ephemeral_agent

    seed_agent("plain-2", owner_id=1)
    assert await discard_ephemeral_agent("plain-2", reason="test") is False
    assert agent_ops.get_agent_owner("plain-2") is not None


@pytest.mark.asyncio
async def test_discard_lock_contention_skips(discard_env, agent_ops, monkeypatch):
    import services.agent_service.ephemeral as ephemeral_mod
    from services.agent_service.ephemeral import discard_ephemeral_agent

    _register_ghost(agent_ops, name="ghost-d3")
    fake_redis = MagicMock()
    fake_redis.set.return_value = False  # another discard holds the lock
    monkeypatch.setattr(ephemeral_mod, "get_breaker_redis", lambda: fake_redis)
    assert await discard_ephemeral_agent("ghost-d3", reason="test") is False
    # nothing purged
    assert agent_ops.get_agent_ephemeral_info("ghost-d3") is not None


# ---------------------------------------------------------------------------
# Quota reservation
# ---------------------------------------------------------------------------


def test_quota_reserve_atomic_incr_and_cap(monkeypatch):
    import services.agent_service.ephemeral as ephemeral_mod

    fake_redis = MagicMock()
    counter = {"v": 0}

    def _incr(key):
        counter["v"] += 1
        return counter["v"]

    def _decr(key):
        counter["v"] -= 1
        return counter["v"]

    fake_redis.incr.side_effect = _incr
    fake_redis.decr.side_effect = _decr
    monkeypatch.setattr(ephemeral_mod, "get_breaker_redis", lambda: fake_redis)
    # fresh-counter reseed reads the DB count
    monkeypatch.setattr(
        "database.db.count_live_ephemeral_agents_for_owner", lambda owner_id: 0
    )

    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=2) is True   # 1
    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=2) is True   # 2
    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=2) is False  # over cap
    assert counter["v"] == 2  # the failed INCR was rolled back
    ephemeral_mod.release_ephemeral_slot(1)
    assert counter["v"] == 1
    # cap<=0 → unlimited, untracked
    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=0) is True
    assert counter["v"] == 1


def test_quota_reserve_falls_back_to_db_when_redis_down(monkeypatch):
    import services.agent_service.ephemeral as ephemeral_mod

    monkeypatch.setattr(ephemeral_mod, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(
        "database.db.count_live_ephemeral_agents_for_owner", lambda owner_id: 4
    )
    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=5) is True
    assert ephemeral_mod.try_reserve_ephemeral_slot(1, cap=4) is False
