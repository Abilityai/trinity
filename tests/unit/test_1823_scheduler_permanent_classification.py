"""#1823 — an unresolvable timezone is a PERMANENT `_add_job` failure.

`src/scheduler/service.py::_add_job` splits registration failures into two arms
(#1472): **permanent** — a config that can never register, recorded in
`_permanent_add_failures`, logged once, not retried — and **transient**, which
returns without recording so the 60s sync loop tries again.

The permanent arm caught `(pytz.exceptions.UnknownTimeZoneError, ValueError)`.
`zoneinfo.ZoneInfoNotFoundError` — what APScheduler's `astimezone()` raises for a
legacy alias the image's tz database lacks — is neither: it is a `KeyError`
subclass. So it fell through to the broad `except Exception`, i.e. the transient
arm, and #1472's own bounded-retry machinery was defeated by the very failure it
was built to bound:

  * never snapshotted into `_permanent_add_failures` -> retried every 60s forever
  * `logger.error(...(transient, will retry)...)` on every sync tick -> log flood
  * `next_run_at` frozen -> the "Next: Nd ago" symptom #1472 existed to remove
  * canary E-06 fires on the row every cycle

The packaging fix (this issue's other half) means no *legacy alias* reaches this
arm any more. It still matters: any zone this runtime cannot resolve — a future
IANA removal, a differently-built image — now fails bounded instead of storming.

`src/scheduler` is a standalone package that cannot import the backend, so the
service is driven directly with an injected fake DB and scheduler; the assertion
is about which arm the branch takes. Structure mirrors
`test_1994_process_schedule_lock_audit.py`.
"""

from __future__ import annotations

import os
import sys
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The `src.scheduler` namespace import resolves only with the repo root on
# sys.path — true for a repo-root `pytest` run but NOT in CI, whose rootdir is
# `tests/`. Appended (never inserted at 0) so the repo root cannot shadow the
# conftest-managed `src/backend` entries. Mirrors test_1994 / test_1808.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589 made the Redis
# credentials mandatory), so they must exist before the package is imported.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _service_module():
    import src.scheduler.service as scheduler_service

    return scheduler_service


class _FakeAPScheduler:
    """Only the surface `_add_job` touches."""

    def __init__(self):
        self.added: list[str] = []

    def add_job(self, *_args, **kwargs):
        self.added.append(kwargs.get("id"))


class _FakeDB:
    def __init__(self):
        self.run_time_writes: list[tuple] = []

    def update_schedule_run_times(self, schedule_id, next_run_at=None, **_kwargs):
        self.run_time_writes.append((schedule_id, next_run_at))


def _service(monkeypatch):
    """A `SchedulerService` with just enough state for `_add_job`.

    Built with `object.__new__` rather than the real constructor: `__init__`
    wires Redis, a lock manager and an AsyncIOScheduler, none of which this
    branch reads, and all of which would only add ways for the test to be flaky
    about something it is not asserting.
    """
    module = _service_module()
    svc = object.__new__(module.SchedulerService)
    svc.scheduler = _FakeAPScheduler()
    svc.db = _FakeDB()
    svc._permanent_add_failures = set()
    return module, svc


def _schedule(module, *, tz: str = "Europe/Kiev", cron: str = "0 4 * * *"):
    now = datetime.now(timezone.utc)
    return module.Schedule(
        id="sched-1823",
        agent_name="alice",
        name="daily",
        cron_expression=cron,
        message="/daily",
        enabled=True,
        timezone=tz,
        description=None,
        owner_id=1,
        created_at=now,
        updated_at=now,
    )


def _raise_zoneinfo_not_found(**_kwargs):
    """What APScheduler's `astimezone()` does for a key the runtime lacks.

    Injected rather than provoked with a real zone: on a tz-complete runtime
    (which CI and the fixed images both are) NO real zone reaches this arm — 0 of
    597 pytz zones are unresolvable. The classification is what is under test,
    so the exception is supplied directly.
    """
    raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key Europe/Kiev")


def test_unresolvable_timezone_is_classified_permanent(monkeypatch, caplog):
    """ZoneInfoNotFoundError -> permanent arm: recorded, bounded, not retried."""
    module, svc = _service(monkeypatch)
    monkeypatch.setattr(module, "CronTrigger", _raise_zoneinfo_not_found)
    schedule = _schedule(module)

    with caplog.at_level("ERROR"):
        assert svc._add_job(schedule) is False

    assert schedule.id in svc._permanent_add_failures, (
        "an unresolvable timezone must be recorded as permanent, or the sync "
        "loop retries it every 60s forever (#1823)"
    )
    assert "permanent" in caplog.text
    assert "transient" not in caplog.text, (
        "the failure was still routed to the transient arm — the retry storm "
        "and the frozen next_run_at are back"
    )


def test_permanent_failure_is_logged_only_once(monkeypatch, caplog):
    """The bound is the point: a second attempt records nothing new and is silent.

    #1472's contract — log ONCE, then stay quiet — is what turns a broken row
    from a per-tick ERROR flood into a single diagnosable line.
    """
    module, svc = _service(monkeypatch)
    monkeypatch.setattr(module, "CronTrigger", _raise_zoneinfo_not_found)
    schedule = _schedule(module)

    with caplog.at_level("ERROR"):
        svc._add_job(schedule)
        first = caplog.text.count("permanent")
        svc._add_job(schedule)
        second = caplog.text.count("permanent")

    assert first == 1
    assert second == 1, "the permanent failure logged again on a repeat tick"


def test_transient_failure_is_still_transient(monkeypatch, caplog):
    """A jobstore hiccup must NOT be reclassified.

    The permanent arm suppresses retries, so widening it (e.g. to a bare
    `KeyError`, which would have caught ZoneInfoNotFoundError for free) would
    permanently park schedules whose registration failed for a recoverable
    reason. This is the guard on that temptation.
    """
    module, svc = _service(monkeypatch)

    def _boom(**_kwargs):
        raise RuntimeError("jobstore unavailable")

    monkeypatch.setattr(module, "CronTrigger", _boom)
    schedule = _schedule(module)

    with caplog.at_level("ERROR"):
        assert svc._add_job(schedule) is False

    assert svc._permanent_add_failures == set(), (
        "a transient failure must not be recorded as permanent — the sync loop "
        "would stop retrying a recoverable condition"
    )
    assert "transient" in caplog.text


def test_bare_keyerror_is_not_reclassified_as_permanent(monkeypatch, caplog):
    """A KeyError that is NOT a ZoneInfoNotFoundError stays transient.

    ZoneInfoNotFoundError subclasses KeyError, so `except KeyError` would have
    "fixed" #1823 while quietly parking every schedule that hit an unrelated
    dict-key bug inside APScheduler. Pin the narrow catch from the other side.
    """
    module, svc = _service(monkeypatch)

    def _boom(**_kwargs):
        raise KeyError("some_internal_field")

    monkeypatch.setattr(module, "CronTrigger", _boom)
    schedule = _schedule(module)

    with caplog.at_level("ERROR"):
        assert svc._add_job(schedule) is False

    assert svc._permanent_add_failures == set()
    assert "transient" in caplog.text


def test_successful_registration_clears_a_prior_permanent_flag(monkeypatch):
    """Reconfiguring a broken schedule un-parks it (#1472 recovery path).

    Directly relevant to #1823's back-compat story: a row stored under a legacy
    alias that was parked before the packaging fix must register — and clear its
    flag — once the image can resolve the zone, with no operator action.
    """
    module, svc = _service(monkeypatch)
    schedule = _schedule(module, tz="UTC")
    svc._permanent_add_failures.add(schedule.id)

    assert svc._add_job(schedule) is True
    assert svc._permanent_add_failures == set()
    assert svc.scheduler.added == [svc._get_job_id(schedule.id)]


def test_resolvable_legacy_alias_registers(monkeypatch):
    """A legacy alias registers for real on a tz-complete runtime.

    No injection here — this is the one case that exercises the real
    `pytz.timezone` -> `CronTrigger` -> `astimezone()` chain end to end, and it
    is the assertion that goes red inside an image missing `tzdata-legacy`.
    """
    module, svc = _service(monkeypatch)
    schedule = _schedule(module, tz="Europe/Kiev")

    assert svc._add_job(schedule) is True, (
        "this runtime cannot register a schedule under the legacy alias "
        "'Europe/Kiev' — the IANA backward-compatibility links are missing "
        "(#1823); see tests/unit/test_1823_tz_capability_parity.py"
    )
    assert svc._permanent_add_failures == set()
