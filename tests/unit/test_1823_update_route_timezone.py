"""#1823 D4 — `PUT` must refuse an unregisterable timezone BEFORE it writes the row.

The create route's failure was loud (a raw 500). The update route's was worse:
`PUT /api/agents/{name}/schedules/{id}` with `timezone: "Europe/Kiev"` passed
validation entirely, because `validate_timezone` probed only pytz — which
bundles its own complete database and accepts every legacy alias in every
environment. The alias was WRITTEN to the row and the failure deferred to
scheduler registration, where the schedule simply never fires and its
`next_run_at` freezes. Verified live on the issue thread.

That is why the #1823 fix had to land inside `validate_timezone` rather than only
in `validate_cron_expression`'s except-clause: an except-clause fix satisfies
"never a 500" on create and leaves this route wide open.

These tests drive the route handler directly. The rejection must happen before
`db.update_schedule`, so every case asserts on the write NOT happening, not just
on the status code — a 400 raised after a successful write would be worse than
today's behaviour, not better.

Router loaded via importlib with passlib stubbed, the same way
`test_929_timeout_validation.py` does it: `from routers import schedules` drags
`routers/__init__.py` and 50+ siblings needing docker_service, twilio, slack_sdk.
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import sys
import types
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException


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


_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_SCHED_PATH = _BACKEND / "routers" / "schedules.py"


def _build_passlib_stub_modules():
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


def _load_sched_router(monkeypatch):
    passlib, context = _build_passlib_stub_modules()
    monkeypatch.setitem(sys.modules, "passlib", passlib)
    monkeypatch.setitem(sys.modules, "passlib.context", context)
    try:
        spec = _ilu.spec_from_file_location("routers.schedules", str(_SCHED_PATH))
        module = _ilu.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "routers.schedules", module)
        spec.loader.exec_module(module)
        return module
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend venv required (no `routers.schedules` import): {exc}")


class _StoredSchedule:
    """The row `db.get_schedule` returns, and what a successful update echoes."""

    def __init__(self, *, agent_name="alice", tz="UTC", cron="0 4 * * *"):
        self.agent_name = agent_name
        self.timezone = tz
        self.cron_expression = cron

    def model_dump(self):
        now = datetime.now(timezone.utc)
        return {
            "id": "sched-1",
            "agent_name": self.agent_name,
            "name": "daily",
            "cron_expression": self.cron_expression,
            "message": "/daily",
            "enabled": True,
            "timezone": self.timezone,
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


def _wire(monkeypatch, router, stored: _StoredSchedule):
    """Point the route at `stored` and record every write attempt."""
    writes: list[tuple] = []
    monkeypatch.setattr(
        router.db, "get_schedule", lambda _sid: stored, raising=False
    )
    monkeypatch.setattr(
        router.db,
        "update_schedule",
        lambda *a, **k: writes.append(a) or stored,
        raising=False,
    )
    return writes


def _put(router, updates):
    user = types.SimpleNamespace(username="owner", role="user", connector_agent=None, mcp_scope=None)
    return asyncio.run(
        router.update_schedule(
            name="alice", schedule_id="sched-1", updates=updates, current_user=user,
        )
    )


class _NoTzDatabase:
    """`zoneinfo` with an empty database — see test_1472's copy for the rationale."""

    ZoneInfoNotFoundError = zoneinfo.ZoneInfoNotFoundError

    @staticmethod
    def ZoneInfo(key):  # noqa: N802 — mirrors the stdlib name
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")


def test_put_rejects_nonexistent_timezone_before_writing(monkeypatch):
    """A zone that exists nowhere: 400, and the row is untouched."""
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule())

    with pytest.raises(HTTPException) as exc_info:
        _put(router, router.ScheduleUpdateRequest(timezone="Not/AZone"))

    assert exc_info.value.status_code == 400
    assert writes == [], "the route wrote the row before/despite rejecting it"


def test_put_rejects_unresolvable_timezone_before_writing(monkeypatch):
    """The D4 case itself: pytz knows the zone, this runtime cannot resolve it.

    Fault-injected — on a tz-complete runtime no real zone reaches this branch
    (0 of 597 pytz zones are unresolvable), which is the whole point of the
    packaging half of #1823. What is under test is that the update route now
    refuses such a zone at the API instead of persisting it and deferring the
    failure to a fire time that never comes.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule())

    from services import schedule_validation
    monkeypatch.setattr(schedule_validation, "zoneinfo", _NoTzDatabase)

    with pytest.raises(HTTPException) as exc_info:
        _put(router, router.ScheduleUpdateRequest(timezone="Europe/Kiev"))

    assert exc_info.value.status_code == 400
    detail = str(exc_info.value.detail)
    assert "Europe/Kiev" in detail
    assert "tzdata" in detail, (
        f"the operator must be told this is missing tz data, not a typo: {detail!r}"
    )
    assert writes == [], (
        "the unresolvable zone was written to the row — this is D4, the silent "
        "persist that is worse than the create path's 500"
    )


def test_put_accepts_a_resolvable_legacy_alias(monkeypatch):
    """The accept branch, end to end, with no injection.

    #1823 supports legacy aliases rather than rejecting them (rejecting would
    strand every stored row). So on a tz-complete runtime this must go through —
    and if it ever does not, the environment lost its tz data rather than the
    validator gaining a rule.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule())

    _put(router, router.ScheduleUpdateRequest(timezone="Europe/Kiev"))

    assert len(writes) == 1, (
        "a resolvable legacy alias must be accepted; this runtime appears to "
        "lack the IANA backward-compatibility links (#1823)"
    )
