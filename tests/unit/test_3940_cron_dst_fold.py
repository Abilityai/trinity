"""#3940 — a cron's next fire time never goes backwards at a DST fall-back.

APScheduler 3.11.0's `CronTrigger.get_next_fire_time` returned a fire time
EARLIER than the previous one across the fold for a minute-stepped cron with
`hour='*'` in a zone leaving summer time (`*/30` in Europe/Lisbon at the EU
fall-back: 01:00Z, 00:30Z, 01:00Z, ...). APScheduler's job loop then never
advanced: `trinity-scheduler` pinned a core and grew without bound, and two
production hosts ran out of memory on 2026-10-25. `zoneinfo` zones loop the
same way; 3.11.1+ advances correctly.

Three layers are pinned here:

1. **The test environment runs the version production ships.** CI installs
   `tests/requirements-test.txt`; while that read `apscheduler>=3.11.0`, CI ran
   a fixed release while the images ran 3.11.0, so a fold test passed on a
   broken production. The installed version must equal the image pin, or
   layer 2 proves nothing about production (the #1823 lesson).
2. **The pinned version advances across both 2026 folds**, plain
   `CronTrigger`, `pytz` and `zoneinfo` zones, with spring-forward and UTC
   controls.
3. **The scheduler's own guard** (`src/scheduler/cron_trigger.py`): whatever
   APScheduler returns, a fire time that is not after the previous one is
   replaced by one that is, logged once per trigger. Exercised with a parent
   that goes backwards on purpose, because the fixed version never does.
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
import re
import sys
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytz
from apscheduler.triggers.cron import CronTrigger

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
# Same import shape as test_1823_scheduler_permanent_classification.py.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _exact_pin(rel: str, package: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(package)}\s*==\s*([0-9][0-9A-Za-z.\-+!]*)", re.IGNORECASE)
    for line in (_REPO / rel).read_text().splitlines():
        line = line.split("#", 1)[0].strip().rstrip("\\").strip()
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# 1. The test environment runs what production ships
# ---------------------------------------------------------------------------

def test_the_test_environment_pins_the_images_apscheduler():
    image = _exact_pin("docker/scheduler/requirements.txt", "APScheduler")
    tests = _exact_pin("tests/requirements-test.txt", "apscheduler")
    assert image is not None
    assert tests == image, (
        f"tests/requirements-test.txt must pin apscheduler=={image} exactly (it has "
        f"{tests!r}): a floor lets CI run a fixed release while the image runs a "
        f"broken one, and the fold tests below pass on a broken production (#3940)"
    )


def test_the_installed_apscheduler_is_the_images():
    image = _exact_pin("docker/scheduler/requirements.txt", "APScheduler")
    installed = importlib.metadata.version("apscheduler")
    assert installed == image, (
        f"this environment runs APScheduler {installed}, the scheduler image pins "
        f"{image}; reinstall tests/requirements-test.txt (#3940)"
    )


def test_the_image_pin_is_past_the_fold_bug():
    image = _exact_pin("docker/scheduler/requirements.txt", "APScheduler")
    assert tuple(int(p) for p in image.split(".")[:3]) >= (3, 11, 1), (
        f"APScheduler {image}: CronTrigger goes backwards at a DST fall-back "
        f"before 3.11.1, and the scheduler spins (#3940)"
    )


# ---------------------------------------------------------------------------
# 2. The pinned version advances across the folds
# ---------------------------------------------------------------------------

# (zone, trigger fields, a UTC instant just before the transition)
FOLDS = [
    ("Europe/Lisbon", {"minute": "*/30"}, datetime(2026, 10, 25, 0, 59, 59)),
    ("Europe/London", {"minute": "*/40"}, datetime(2026, 10, 25, 0, 59, 59)),
    ("Europe/Berlin", {"minute": "*/15"}, datetime(2026, 10, 25, 0, 59, 59)),
    ("America/New_York", {"minute": "*/30"}, datetime(2026, 11, 1, 5, 59, 59)),
    ("America/Los_Angeles", {"minute": "*/20"}, datetime(2026, 11, 1, 8, 59, 59)),
]
CONTROLS = [
    # spring-forward
    ("America/New_York", {"minute": "*/15"}, datetime(2026, 3, 8, 6, 59, 59)),
    ("Europe/Lisbon", {"minute": "*/30"}, datetime(2026, 3, 29, 0, 59, 59)),
    # hour-pinned across the fold, and UTC
    ("America/New_York", {"minute": "0", "hour": "*/4"}, datetime(2026, 11, 1, 3, 59, 59)),
    ("UTC", {"minute": "*/30"}, datetime(2026, 11, 1, 5, 59, 59)),
]
ZONES = {"pytz": pytz.timezone, "zoneinfo": zoneinfo.ZoneInfo}


def _fires(trigger, start_utc, n=10):
    """Fire times as APScheduler's job loop asks for them: each call is given
    the previous fire time, and `now` is that fire time."""
    out, previous, now = [], None, start_utc.replace(tzinfo=timezone.utc)
    for _ in range(n):
        nxt = trigger.get_next_fire_time(previous, now)
        assert nxt is not None
        out.append(nxt.astimezone(timezone.utc))
        previous = now = nxt
    return out


@pytest.mark.parametrize("kind", ZONES)
@pytest.mark.parametrize("zone,fields,start", FOLDS + CONTROLS)
def test_the_pinned_cron_trigger_advances_across_the_transition(kind, zone, fields, start):
    fires = _fires(CronTrigger(timezone=ZONES[kind](zone), **fields), start)
    backwards = [(a, b) for a, b in zip(fires, fires[1:]) if b <= a]
    assert not backwards, (
        f"{zone} {fields} ({kind}): next fire time went backwards "
        f"{[f'{a:%H:%M}->{b:%H:%M}' for a, b in backwards]} (#3940)"
    )


# ---------------------------------------------------------------------------
# 3. The scheduler's guard
# ---------------------------------------------------------------------------

def _guard():
    from src.scheduler import cron_trigger
    return cron_trigger


def test_the_guard_matches_cron_trigger_when_apscheduler_is_right():
    mod = _guard()
    for zone, fields, start in FOLDS + CONTROLS:
        plain = _fires(CronTrigger(timezone=pytz.timezone(zone), **fields), start)
        guarded = _fires(mod.MonotonicCronTrigger(timezone=pytz.timezone(zone), **fields), start)
        assert guarded == plain, f"{zone} {fields}"


def _backwards_parent():
    """APScheduler 3.11.0's behaviour at the EU fold, made deterministic: after
    01:00Z the parent answers 00:30Z, a fire time before the previous one."""
    real = CronTrigger.get_next_fire_time

    def broken(self, previous_fire_time, now):
        nxt = real(self, previous_fire_time, now)
        if previous_fire_time is not None and nxt is not None:
            return previous_fire_time - timedelta(minutes=30)
        return nxt

    return patch.object(CronTrigger, "get_next_fire_time", broken)


def test_the_guard_recovers_the_correct_schedule_from_a_backwards_parent():
    """The retry (the first fire time after previous + 1s, asked in UTC) is
    what 3.11.0 gets right at every fold, so the guard lands on the schedule a
    correct trigger produces: no clamp, no extra run of the user's job."""
    mod = _guard()
    start = datetime(2026, 10, 25, 0, 59, 59)
    correct = _fires(CronTrigger(timezone=pytz.timezone("Europe/Lisbon"), minute="*/30"), start, n=6)
    trigger = mod.MonotonicCronTrigger(timezone=pytz.timezone("Europe/Lisbon"), minute="*/30")
    with _backwards_parent():
        fires = _fires(trigger, start, n=6)
    assert fires == correct, [f"{f:%H:%M:%S}" for f in fires]


def test_the_guard_clamps_in_utc_when_the_retry_is_wrong_too():
    """Last resort: a parent that goes backwards on every question. The job
    fires a minute after the previous run, as an instant — inside the repeated
    hour, adding the minute in local time would land an hour EARLIER."""
    mod = _guard()

    def always_backwards(self, previous_fire_time, now):
        return (previous_fire_time or now) - timedelta(minutes=30)

    trigger = mod.MonotonicCronTrigger(timezone=pytz.timezone("Europe/Lisbon"), minute="*/30")
    first = trigger.get_next_fire_time(None, datetime(2026, 10, 25, 0, 59, 59, tzinfo=timezone.utc))
    assert first.fold == 1, "precondition: the second pass of the repeated 01:00"
    fires, previous = [], first
    with patch.object(CronTrigger, "get_next_fire_time", always_backwards):
        for _ in range(3):
            previous = trigger.get_next_fire_time(previous, previous)
            fires.append(previous.astimezone(timezone.utc))
    start = first.astimezone(timezone.utc)
    assert fires == [start + timedelta(minutes=n) for n in (1, 2, 3)]


def test_the_guard_compares_instants_not_wall_clocks():
    """3.11.0's answer at the EU fold: `01:30` wall time on the FIRST pass of
    the repeated hour (`fold=0`, 00:30Z) right after `01:00` on the SECOND pass
    (`fold=1`, 01:00Z). Both carry the same zone object, so Python compares
    them by wall clock (01:30 > 01:00) and a guard that compares the datetimes
    themselves lets the backwards instant through."""
    mod = _guard()
    trigger = mod.MonotonicCronTrigger(timezone=pytz.timezone("Europe/Lisbon"), minute="*/30")
    first = trigger.get_next_fire_time(None, datetime(2026, 10, 25, 0, 59, 59, tzinfo=timezone.utc))
    assert first.fold == 1, "precondition: the second pass of the repeated 01:00"
    backwards = first.replace(minute=30, fold=0)
    assert backwards > first and backwards.astimezone(timezone.utc) < first.astimezone(timezone.utc), (
        "precondition: later by wall clock, earlier as an instant")

    with patch.object(CronTrigger, "get_next_fire_time", lambda self, p, n: backwards):
        nxt = trigger.get_next_fire_time(first, first)
    assert nxt.astimezone(timezone.utc) > first.astimezone(timezone.utc)


def test_the_guard_logs_once_per_trigger(caplog):
    mod = _guard()
    trigger = mod.MonotonicCronTrigger(timezone=pytz.timezone("Europe/Lisbon"), minute="*/30")
    with _backwards_parent(), caplog.at_level(logging.WARNING, logger=mod.logger.name):
        _fires(trigger, datetime(2026, 10, 25, 0, 59, 59), n=6)
    warnings = [r for r in caplog.records if "#3940" in r.getMessage()]
    assert len(warnings) == 1
    assert "Europe/Lisbon" in warnings[0].getMessage()


def test_the_guard_passes_through_a_finished_trigger():
    """None means "no more fire times" (an end_date passed). The guard must not
    invent one, and must not turn a real value into None, which APScheduler
    reads as "remove this job"."""
    mod = _guard()
    trigger = mod.MonotonicCronTrigger(
        timezone=pytz.UTC, minute="*/30", end_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert trigger.get_next_fire_time(None, datetime(2026, 6, 1, tzinfo=timezone.utc)) is None


def test_the_guard_is_used_for_every_user_schedule():
    """`_add_job` registers the guarded trigger, not a bare CronTrigger."""
    import src.scheduler.service as service
    mod = _guard()
    added = []

    class _APS:
        def add_job(self, *_a, **kw):
            added.append(kw["trigger"])

    class _DB:
        def update_schedule_run_times(self, *_a, **_kw):
            pass

    svc = object.__new__(service.SchedulerService)
    svc.scheduler, svc.db, svc._permanent_add_failures = _APS(), _DB(), set()
    now = datetime.now(timezone.utc)
    schedule = service.Schedule(
        id="sched-3940", agent_name="alice", name="half-hourly", cron_expression="*/30 * * * *",
        message="/m", enabled=True, timezone="America/New_York", description=None, owner_id=1,
        created_at=now, updated_at=now,
    )
    assert svc._add_job(schedule) is True
    assert len(added) == 1 and isinstance(added[0], mod.MonotonicCronTrigger)
