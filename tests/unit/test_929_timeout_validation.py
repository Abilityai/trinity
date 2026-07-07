"""
Tests for Issue #929 — write-time validation of schedule timeout vs agent cap.

Two surfaces:
  * `db.get_max_active_schedule_timeout(agent)` / `find_active_schedules_exceeding_timeout`
    — read accessors used by the agent-cap-lowering check.
  * `routers/schedules._enforce_timeout_below_agent_cap` — the inline guard
    fired on `POST/PUT /api/agents/{name}/schedules`.

Tests bypass FastAPI TestClient: the router helper is a pure function over
`db.*`, and the DB accessors are exercised directly against an ephemeral
SQLite file routed via the same `db.connection.DB_PATH` monkeypatch pattern
as `test_agent_soft_delete.py`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# Modules this test stubs into sys.modules — must be restored after each test
# so other test files in the same pytest session get clean imports.
# Precedent: `tests/unit/test_telegram_webhook_backfill.py`.
_STUBBED_MODULE_NAMES = ["passlib", "passlib.context", "routers.schedules"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules entries we mutate; restore after each test."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _build_passlib_stub_modules():
    """Build (passlib, passlib.context) module pair with a no-op CryptContext.

    Returned for the caller to register via `monkeypatch.setitem` so the
    sys.modules write goes through monkeypatch and the lint stays green.
    """
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")

    class _CryptContext:
        def __init__(self, **_):
            pass

        def hash(self, pw):
            return f"stub${pw}"

        def verify(self, pw, hashed):
            return hashed == f"stub${pw}"

    context.CryptContext = _CryptContext
    return passlib, context


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


@pytest.fixture
def tmp_agent_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Runs on
    SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL. Returns the backend
    marker (leading positional arg the seed helpers accept).

    NOTE: the prior fixture monkeypatched the legacy db.connection.DB_PATH seam,
    which the #300 Core conversion no longer reads — so the production accessor
    queried the wrong DB and the offenders assertion failed (masked while the
    suite was dead). Routing through db_backend (DATABASE_URL + engine) fixes it.
    """
    return db_backend


def _seed_agent(_db, name: str, cap_seconds: int = 3600) -> None:
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, execution_timeout_seconds) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z', :cap)",
        n=name, cap=cap_seconds,
    )


def _seed_schedule(
    _db,
    agent_name: str,
    schedule_id: str,
    timeout_seconds: int,
    deleted: bool = False,
) -> None:
    _hrun(
        "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, "
        "owner_id, created_at, updated_at, timeout_seconds, deleted_at) "
        "VALUES (:sid, :a, :sid, '0 * * * *', 'test', 1, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z', :to, :deleted)",
        sid=schedule_id, a=agent_name, to=timeout_seconds,
        deleted='2026-05-01T00:00:00Z' if deleted else None,
    )


# ---------------------------------------------------------------------------
# DB accessor tests
# ---------------------------------------------------------------------------


def test_find_active_schedules_exceeding_timeout_returns_offenders(tmp_agent_db):
    """Returns id/name/timeout dicts only for schedules above the ceiling."""
    from db.schedules import ScheduleOperations

    _seed_agent(tmp_agent_db, "alice")
    _seed_schedule(tmp_agent_db, "alice", "s_under", 1200)
    _seed_schedule(tmp_agent_db, "alice", "s_at", 1800)
    _seed_schedule(tmp_agent_db, "alice", "s_over1", 3000)
    _seed_schedule(tmp_agent_db, "alice", "s_over2", 5400)
    _seed_schedule(tmp_agent_db, "alice", "s_dead_over", 7200, deleted=True)

    ops = ScheduleOperations(user_ops=None, agent_ops=None)
    offenders = ops.find_active_schedules_exceeding_timeout("alice", 1800)

    offender_ids = {o["id"] for o in offenders}
    assert offender_ids == {"s_over1", "s_over2"}
    # Sorted DESC by timeout so the largest offender is first.
    assert offenders[0]["id"] == "s_over2"
    assert offenders[0]["timeout_seconds"] == 5400


def test_find_active_schedules_exceeding_timeout_empty_when_all_under(tmp_agent_db):
    from db.schedules import ScheduleOperations

    _seed_agent(tmp_agent_db, "alice")
    _seed_schedule(tmp_agent_db, "alice", "s1", 600)
    _seed_schedule(tmp_agent_db, "alice", "s2", 1200)

    ops = ScheduleOperations(user_ops=None, agent_ops=None)
    assert ops.find_active_schedules_exceeding_timeout("alice", 1800) == []


# ---------------------------------------------------------------------------
# Router-helper test (`_enforce_timeout_below_agent_cap`)
# ---------------------------------------------------------------------------
#
# The helper calls `db.get_execution_timeout(agent_name)` then raises a
# 400 HTTPException with a structured detail dict when over-cap. We don't
# need a TestClient — patch `db.get_execution_timeout` for isolation.


# Load routers/schedules.py directly via importlib — same pattern as
# `test_voice_auth.py` / `test_monitoring_router_signatures.py`. Going through
# `from routers import schedules` drags routers/__init__.py and 50+ siblings
# that need passlib, docker_service, twilio, slack_sdk, etc.
import importlib.util as _ilu

_sched_path = _BACKEND / "routers" / "schedules.py"


def _load_sched_router(monkeypatch):
    """Lazily load routers/schedules.py with passlib stubbed.

    `monkeypatch.setitem` keeps the sys.modules writes auditable for
    `tests/lint_sys_modules.py`; the autouse `_restore_sys_modules` fixture
    above is the safety net for the same names.
    """
    passlib, context = _build_passlib_stub_modules()
    monkeypatch.setitem(sys.modules, "passlib", passlib)
    monkeypatch.setitem(sys.modules, "passlib.context", context)
    try:
        spec = _ilu.spec_from_file_location("routers.schedules", str(_sched_path))
        module = _ilu.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "routers.schedules", module)
        spec.loader.exec_module(module)
        return module
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend venv required (no `routers.schedules` import): {exc}")


def test_enforce_helper_allows_at_or_below_cap(monkeypatch):
    """Schedule timeout == cap and < cap both succeed silently."""
    sched_router = _load_sched_router(monkeypatch)
    # raising=False guards against sibling unit tests (test_904_*) that
    # swap `db` for a method-light stub before this test loads.
    monkeypatch.setattr(
        sched_router.db, "get_execution_timeout", lambda _name: 3600, raising=False
    )
    sched_router._enforce_timeout_below_agent_cap("alice", 3600)
    sched_router._enforce_timeout_below_agent_cap("alice", 60)


def test_enforce_helper_rejects_above_cap_with_structured_detail(monkeypatch):
    """Above cap → HTTP 400 with `error=schedule_timeout_exceeds_agent_cap`."""
    from fastapi import HTTPException

    sched_router = _load_sched_router(monkeypatch)
    monkeypatch.setattr(
        sched_router.db, "get_execution_timeout", lambda _name: 3600, raising=False
    )
    with pytest.raises(HTTPException) as exc_info:
        sched_router._enforce_timeout_below_agent_cap("alice", 7200)

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error"] == "schedule_timeout_exceeds_agent_cap"
    assert detail["agent_cap_seconds"] == 3600
    assert detail["requested_seconds"] == 7200
    assert "Raise the agent cap" in detail["message"]


def test_enforce_helper_raises_typeerror_on_none(monkeypatch):
    """Regression pin for the #913 × #929 interaction.

    After #913 made `ScheduleCreate.timeout_seconds` Optional, the helper
    is no longer safe to call unconditionally — `None > int` raises
    TypeError in Python 3. This pins the helper's actual behavior so the
    write-side `is not None` guards in `create_schedule`/`update_schedule`
    can't silently regress without breaking this test (which would expose
    the guard as the contract).
    """
    sched_router = _load_sched_router(monkeypatch)
    monkeypatch.setattr(
        sched_router.db, "get_execution_timeout", lambda _name: 3600, raising=False
    )
    with pytest.raises(TypeError):
        sched_router._enforce_timeout_below_agent_cap("alice", None)


def test_create_schedule_path_skips_enforcement_when_timeout_none(monkeypatch):
    """`create_schedule` must not call the enforcer when caller omits timeout.

    Eugene's PR #922 review caught the missing `is not None` guard on the
    POST path: it would 500 with TypeError for every consumer that didn't
    explicitly set `timeout_seconds`. Pin the guard by asserting the route
    function does not call `_enforce_timeout_below_agent_cap` on None.

    We invoke the route function directly with stubbed dependencies — going
    through FastAPI TestClient would drag the whole router init chain.
    """
    import asyncio

    sched_router = _load_sched_router(monkeypatch)

    # Track whether the enforcer ran.
    enforcer_calls = []

    def fake_enforce(agent_name, requested_seconds):
        enforcer_calls.append((agent_name, requested_seconds))

    # #1445: create_schedule now gates on the caller's access AND a live agent
    # ownership row before the timeout enforcer runs. Stub both truthy so this
    # direct route-handler call reaches the enforcement path under test.
    monkeypatch.setattr(
        sched_router.db, "can_user_access_agent", lambda *_a, **_k: True, raising=False
    )
    monkeypatch.setattr(
        sched_router.db, "is_agent_live", lambda *_a, **_k: True, raising=False
    )

    monkeypatch.setattr(
        sched_router, "_enforce_timeout_below_agent_cap", fake_enforce
    )

    # Stub `db.create_schedule` to return a minimal Schedule-like object so
    # the route's `ScheduleResponse(**schedule.model_dump())` succeeds.
    class _FakeSchedule:
        def model_dump(self):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            return {
                "id": "sched-1",
                "agent_name": "alice",
                "name": "test",
                "cron_expression": "*/5 * * * *",
                "message": "hi",
                "enabled": True,
                "timezone": "UTC",
                "description": None,
                "created_at": now,
                "updated_at": now,
                "last_run_at": None,
                "next_run_at": None,
                "timeout_seconds": None,
                "allowed_tools": None,
                "model": None,
                "validation_enabled": False,
                "validation_prompt": None,
                "validation_timeout_seconds": 120,
            }

    monkeypatch.setattr(
        sched_router.db,
        "create_schedule",
        lambda *_a, **_k: _FakeSchedule(),
        raising=False,
    )

    # ScheduleCreate with timeout_seconds left at its default (None).
    schedule_data = sched_router.ScheduleCreate(
        name="test",
        cron_expression="*/5 * * * *",
        message="hi",
    )
    assert schedule_data.timeout_seconds is None, (
        "Test premise: ScheduleCreate.timeout_seconds defaults to None after #913"
    )

    fake_user = types.SimpleNamespace(username="owner", role="user")

    # Invoke the async route handler directly.
    asyncio.run(
        sched_router.create_schedule(
            name="alice",
            schedule_data=schedule_data,
            current_user=fake_user,
        )
    )

    assert enforcer_calls == [], (
        f"Enforcer must not run when timeout_seconds is None; "
        f"got calls: {enforcer_calls!r}. Eugene's guard regressed."
    )


def test_create_schedule_path_runs_enforcement_when_timeout_set(monkeypatch):
    """Counterpart: when caller DOES set `timeout_seconds`, the enforcer fires."""
    import asyncio

    sched_router = _load_sched_router(monkeypatch)

    enforcer_calls = []

    def fake_enforce(agent_name, requested_seconds):
        enforcer_calls.append((agent_name, requested_seconds))

    # #1445: create_schedule now gates on the caller's access AND a live agent
    # ownership row before the timeout enforcer runs. Stub both truthy so this
    # direct route-handler call reaches the enforcement path under test.
    monkeypatch.setattr(
        sched_router.db, "can_user_access_agent", lambda *_a, **_k: True, raising=False
    )
    monkeypatch.setattr(
        sched_router.db, "is_agent_live", lambda *_a, **_k: True, raising=False
    )

    monkeypatch.setattr(
        sched_router, "_enforce_timeout_below_agent_cap", fake_enforce
    )

    class _FakeSchedule:
        def model_dump(self):
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            return {
                "id": "sched-1",
                "agent_name": "alice",
                "name": "test",
                "cron_expression": "*/5 * * * *",
                "message": "hi",
                "enabled": True,
                "timezone": "UTC",
                "description": None,
                "created_at": now,
                "updated_at": now,
                "last_run_at": None,
                "next_run_at": None,
                "timeout_seconds": 600,
                "allowed_tools": None,
                "model": None,
                "validation_enabled": False,
                "validation_prompt": None,
                "validation_timeout_seconds": 120,
            }

    monkeypatch.setattr(
        sched_router.db,
        "create_schedule",
        lambda *_a, **_k: _FakeSchedule(),
        raising=False,
    )

    schedule_data = sched_router.ScheduleCreate(
        name="test",
        cron_expression="*/5 * * * *",
        message="hi",
        timeout_seconds=600,
    )

    fake_user = types.SimpleNamespace(username="owner", role="user")

    asyncio.run(
        sched_router.create_schedule(
            name="alice",
            schedule_data=schedule_data,
            current_user=fake_user,
        )
    )

    assert enforcer_calls == [("alice", 600)]


# ---------------------------------------------------------------------------
# #1445 creation gate — no-orphan + access-first ordering (enumeration oracle)
# ---------------------------------------------------------------------------


def test_create_schedule_denies_access_before_existence_check(monkeypatch):
    """#1445: a caller without access gets a UNIFORM 403 whether or not the
    agent exists — the access check runs BEFORE `is_agent_live`, so a low-priv
    user can't probe agent existence via 404-vs-403 (Decision #5). Pinned so a
    future reorder can't silently open the enumeration oracle."""
    import asyncio
    from fastapi import HTTPException

    sched_router = _load_sched_router(monkeypatch)

    # No access. is_agent_live would say the agent EXISTS — the access check
    # must still win, and it must be a 403 (not the 404 that leaks existence).
    monkeypatch.setattr(
        sched_router.db, "can_user_access_agent", lambda *_a, **_k: False, raising=False
    )
    live_calls = []
    monkeypatch.setattr(
        sched_router.db,
        "is_agent_live",
        lambda *a, **k: live_calls.append(a) or True,
        raising=False,
    )
    # If the gate is wrong and falls through, this would blow up loudly.
    monkeypatch.setattr(
        sched_router.db, "create_schedule", lambda *_a, **_k: None, raising=False
    )

    schedule_data = sched_router.ScheduleCreate(
        name="test", cron_expression="*/5 * * * *", message="hi",
    )
    fake_user = types.SimpleNamespace(username="probe", role="user")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            sched_router.create_schedule(
                name="alice", schedule_data=schedule_data, current_user=fake_user,
            )
        )
    assert exc_info.value.status_code == 403
    assert live_calls == [], (
        "is_agent_live must NOT run before the access check — that would leak "
        "existence to a low-priv caller"
    )


def test_create_schedule_missing_agent_returns_404(monkeypatch):
    """#1445: an authorised caller (admin) creating a schedule on a non-live
    agent gets a fail-loud 404 — the no-orphan gate. `create_schedule` must
    never be reached for a dead agent."""
    import asyncio
    from fastapi import HTTPException

    sched_router = _load_sched_router(monkeypatch)

    monkeypatch.setattr(
        sched_router.db, "can_user_access_agent", lambda *_a, **_k: True, raising=False
    )
    monkeypatch.setattr(
        sched_router.db, "is_agent_live", lambda *_a, **_k: False, raising=False
    )
    create_calls = []
    monkeypatch.setattr(
        sched_router.db,
        "create_schedule",
        lambda *a, **k: create_calls.append(a) or None,
        raising=False,
    )

    schedule_data = sched_router.ScheduleCreate(
        name="test", cron_expression="*/5 * * * *", message="hi",
    )
    fake_user = types.SimpleNamespace(username="admin", role="admin")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            sched_router.create_schedule(
                name="ghost", schedule_data=schedule_data, current_user=fake_user,
            )
        )
    assert exc_info.value.status_code == 404
    assert create_calls == [], "db.create_schedule must not run for a dead agent"


# ---------------------------------------------------------------------------
# Orthogonal SIGKILL error_classifier message (agent-server)
# ---------------------------------------------------------------------------
#
# Under approach A the agent cap can never silently truncate a schedule, so
# the legacy "schedule/agent timeout exceeded" disjunction is dead. Pin the
# new wording so a future edit can't silently regress it.


def _load_error_classifier():
    import importlib

    base_image = Path(__file__).resolve().parents[2] / "docker" / "base-image"
    if not (base_image / "agent_server" / "services" / "error_classifier.py").exists():
        pytest.skip("agent_server tree not present")
    if str(base_image) not in sys.path:
        sys.path.insert(0, str(base_image))
    try:
        return importlib.import_module("agent_server.services.error_classifier")
    except ImportError as exc:
        pytest.skip(f"error_classifier import failed: {exc}")


def test_sigkill_message_drops_schedule_agent_disjunction():
    """SIGKILL detail must surface the schedule timeout unambiguously (#929).

    The old wording — `"schedule/agent timeout exceeded"` — was misleading
    because under approach A the agent cap can never silently truncate a
    schedule (write-time validation refuses it). Guard against a regression
    that re-introduces the disjunction.
    """
    classifier = _load_error_classifier()
    _, detail = classifier._classify_signal_exit(-9, metadata=None)

    assert "schedule timeout exceeded" in detail
    assert "schedule/agent" not in detail, (
        f"SIGKILL message regressed to ambiguous disjunction: {detail!r}"
    )
    # Remediation hint mentions both knobs so the operator knows where to look.
    assert "timeout_seconds" in detail
    assert "execution_timeout_seconds" in detail


def test_sigkill_message_handles_sigterm_and_shell_encoded_signals():
    """Shell-encoded (128+N) and negative signal exits both flow through
    the same classification path; both should produce the cleaned wording."""
    classifier = _load_error_classifier()

    for return_code in (-15, 143, 137):
        status_code, detail = classifier._classify_signal_exit(return_code, metadata=None)
        assert status_code == 504
        assert "schedule/agent" not in detail
        assert "OOM kill" in detail
