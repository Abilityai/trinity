"""#1823 — a cron-only `PUT` is validated against the row's OWN timezone.

`update_schedule` validated an incoming cron against the literal `"UTC"`. The
reasoning behind that — cron-field validity is timezone-independent — is correct,
and is preserved: passing the effective zone changes no cron verdict. What the
hardcoded literal also did, however, was make a CRON-ONLY `PUT`
(`updates.timezone is None`) never look at the zone the row actually stores. So
a row holding a zone the scheduler cannot register was mutated and returned
**200 OK** while still never firing — the API and the scheduler disagreeing,
which is precisely the contract #1472 exists to hold.

**This is a deliberate behaviour change, and the one regression in the #1823
work.** In an environment lacking the packaging fix, a cron-only `PUT` against
such a row goes 200 -> 400. Two facts bound it:

  * the row is ALREADY non-firing, so nothing that works today breaks — the
    change is between "silently preserve a dead schedule" and "say why";
  * once the packaging fix lands the branch is unreachable, because no pytz zone
    is unresolvable on a tz-complete runtime (measured: 0 of 597).

Kept separate from the rest of the fix on purpose, so it can be dropped on its
own if review disagrees with that trade.
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
    def __init__(self, *, tz="Europe/Kiev"):
        self.agent_name = "alice"
        self.timezone = tz
        self.cron_expression = "0 4 * * *"

    def model_dump(self):
        now = datetime.now(timezone.utc)
        return {
            "id": "sched-1",
            "agent_name": "alice",
            "name": "daily",
            "cron_expression": self.cron_expression,
            "message": "/daily",
            "enabled": True,
            # ScheduleResponse types this as a non-optional str, so the response
            # shape coerces even when the stored attribute is None — the route
            # reads `schedule.timezone`, which is what these tests are about.
            "timezone": self.timezone or "UTC",
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
    writes: list[tuple] = []
    monkeypatch.setattr(router.db, "get_schedule", lambda _sid: stored, raising=False)
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


class _MissingOneZone:
    """`zoneinfo` with exactly ONE key missing — a trixie image, in miniature.

    Deliberately selective rather than a database that fails on everything: a
    total shim makes `"UTC"` unresolvable too, so the 400 below would fire
    against the OLD hardcoded-`"UTC"` code as well and the test would pin
    nothing. Failing only the zone the row stores is what makes the assertion
    depend on the route reading that row.
    """

    MISSING = "Europe/Kiev"
    ZoneInfoNotFoundError = zoneinfo.ZoneInfoNotFoundError

    @classmethod
    def ZoneInfo(cls, key):  # noqa: N802 — mirrors the stdlib name
        if key == cls.MISSING:
            raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")
        return zoneinfo.ZoneInfo(key)


def test_cron_only_put_validates_against_the_stored_timezone(monkeypatch):
    """The zone handed to the validator is the ROW's, not the literal "UTC".

    Asserted on the call argument rather than on a verdict, because on a
    tz-complete runtime every zone in the corpus validates identically — a
    verdict-based test would pass against the old hardcoded literal too, and
    would therefore pin nothing.
    """
    router = _load_sched_router(monkeypatch)
    _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    seen: list[tuple] = []
    monkeypatch.setattr(
        router, "validate_cron_expression", lambda cron, tz: seen.append((cron, tz))
    )

    _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *"))

    assert seen == [("0 5 * * *", "Europe/Kiev")], (
        "a cron-only PUT is still validated under a hardcoded 'UTC', so a row "
        "storing an unregisterable zone is re-blessed with 200 OK (#1823)"
    )


def test_put_carrying_both_fields_uses_the_incoming_timezone(monkeypatch):
    """An explicit timezone in the same PUT wins over the stored one.

    The row is about to hold the incoming zone, so validating the cron under the
    old one would check a combination that will not exist a line later.
    """
    router = _load_sched_router(monkeypatch)
    _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    seen: list[tuple] = []
    monkeypatch.setattr(
        router, "validate_cron_expression", lambda cron, tz: seen.append((cron, tz))
    )

    _put(
        router,
        router.ScheduleUpdateRequest(
            cron_expression="0 5 * * *", timezone="Asia/Tokyo"
        ),
    )

    assert seen == [("0 5 * * *", "Asia/Tokyo")]


def test_an_explicitly_cleared_timezone_validates_under_utc(monkeypatch):
    """`timezone: ""` is a SET value meaning "scheduler default", not "absent".

    The zone handed to the validator must be the one the row will hold AFTER the
    write. `db.update_schedule` writes `""` through verbatim (it has no falsy
    guard) and `_add_job` reads `pytz.timezone(s.timezone) if s.timezone else
    pytz.UTC`, so the effective zone here is UTC — not the stored one this PUT is
    removing. An `or` chain cannot express that: `"" or schedule.timezone` picks
    the value being discarded, which is how the validator and the persisted row
    came to disagree.
    """
    router = _load_sched_router(monkeypatch)
    _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    seen: list[tuple] = []
    monkeypatch.setattr(
        router, "validate_cron_expression", lambda cron, tz: seen.append((cron, tz))
    )

    _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *", timezone=""))

    assert seen == [("0 5 * * *", "UTC")], (
        "an explicitly cleared timezone was resolved back to the stored zone — "
        "the validator is judging a zone the write removes (#1823)"
    )


def test_clearing_an_unregisterable_timezone_is_not_rejected(monkeypatch):
    """The repair gesture must go through, not 400.

    This is the harm the `or` chain caused, and the reason it is a defect rather
    than a stylistic nit. On a runtime lacking the tz links, a PUT that clears a
    stale `Europe/Kiev` to the default is the request that FIXES the row — and it
    was rejected against the very zone it was removing, leaving the schedule dead
    and inverting the justification for the 200->400 trade this file exists for.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    from services import schedule_validation
    monkeypatch.setattr(schedule_validation, "zoneinfo", _MissingOneZone)

    _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *", timezone=""))

    assert len(writes) == 1, "the repairing PUT was rejected instead of applied"


def test_falls_back_to_utc_when_the_row_stores_no_timezone(monkeypatch):
    """A NULL/empty stored zone still validates, under UTC — as the scheduler does.

    `_add_job` reads `pytz.timezone(schedule.timezone) if schedule.timezone else
    pytz.UTC`, so an empty stored zone means UTC there too. Without the `or
    "UTC"` tail this would pass None into the validator.
    """
    router = _load_sched_router(monkeypatch)
    _wire(monkeypatch, router, _StoredSchedule(tz=None))

    seen: list[tuple] = []
    monkeypatch.setattr(
        router, "validate_cron_expression", lambda cron, tz: seen.append((cron, tz))
    )

    _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *"))

    assert seen == [("0 5 * * *", "UTC")]


def test_cron_only_put_on_an_unregisterable_row_now_400s(monkeypatch):
    """The deliberate 200 -> 400. Named so a reviewer can weigh it directly.

    Fault-injected, because the branch is unreachable once the packaging fix
    lands. The row is already non-firing, so this trades a silent 200 that
    preserves a dead schedule for a named 400 that says which zone the runtime
    cannot resolve.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    from services import schedule_validation
    monkeypatch.setattr(schedule_validation, "zoneinfo", _MissingOneZone)

    with pytest.raises(HTTPException) as exc_info:
        _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *"))

    assert exc_info.value.status_code == 400
    assert "Europe/Kiev" in str(exc_info.value.detail)
    assert writes == [], "the row was mutated despite the rejection"


def test_cron_only_put_on_a_healthy_row_still_succeeds(monkeypatch):
    """The overwhelmingly common path is untouched — no new rejection surface.

    On a tz-complete runtime a stored legacy alias resolves, so a cron-only PUT
    against it goes through exactly as before. Passing the real zone changes no
    cron verdict; that is what makes the change above safe rather than merely
    justified.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    _put(router, router.ScheduleUpdateRequest(cron_expression="0 5 * * *"))

    assert len(writes) == 1


def test_a_bad_cron_is_still_rejected_under_a_valid_stored_timezone(monkeypatch):
    """The cron check itself did not soften — the zone only joins it.

    Guards the direction nobody watches: it would be easy to make the combined
    check tz-faithful and accidentally stop rejecting `@daily`.
    """
    router = _load_sched_router(monkeypatch)
    writes = _wire(monkeypatch, router, _StoredSchedule(tz="Europe/Kiev"))

    with pytest.raises(HTTPException) as exc_info:
        _put(router, router.ScheduleUpdateRequest(cron_expression="@daily"))

    assert exc_info.value.status_code == 400
    assert writes == []
