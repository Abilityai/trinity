"""#1472 — schedule cron + timezone validation must match the scheduler's parser.

The dedicated scheduler registers a job via a strict 5-field ``_parse_cron`` +
APScheduler ``CronTrigger`` + ``pytz`` timezone. The API previously used a looser
bare ``croniter()`` that accepted ``@daily``/``@hourly`` macros, 6-field
seconds-crons, quartz ``L``/``#`` tokens, and ANY timezone string. Those cleared
the API but failed ``_add_job`` — the schedule was silently orphaned and its
``next_run_at`` froze ("Next: Nd ago"). ``validate_cron_expression`` /
``validate_timezone`` must reject exactly what the scheduler would.

#1823 finished restoring that contract on its timezone half. #1472 mirrored the
scheduler's *visible first step* (``pytz.timezone``) and missed that APScheduler's
``astimezone()`` re-resolves the zone by IANA key through the stdlib ``zoneinfo``
— so a legacy alias like ``Europe/Kiev``, which pytz always accepts (it bundles
its own complete database), reached ``CronTrigger`` and raised
``ZoneInfoNotFoundError``. That is a ``KeyError`` subclass, invisible to the
``(ValueError, TypeError)`` rescue, so it escaped as a 500. Both validators now
run one shared probe against ``zoneinfo``, the resolver that actually binds.
"""

import zoneinfo

import pytest
import pytz
from apscheduler.triggers.cron import CronTrigger

from services import schedule_validation
from services.schedule_validation import (
    ScheduleValidationError,
    validate_cron_expression,
    validate_timezone,
)


@pytest.mark.parametrize(
    "cron,tz",
    [
        ("0 4 * * *", "UTC"),
        ("0 0 * * 7", "UTC"),          # dow=7 (Sunday) — must survive dow translation
        ("*/15 * * * *", "UTC"),
        # #1823: a legacy IANA alias. This stays VALID — the images now ship the
        # backward-compatibility links (`tzdata-legacy` + the `tzdata` wheel), so
        # it is no longer environment-dependent. Measured on this interpreter:
        # `set(pytz.all_timezones) - zoneinfo.available_timezones()` is empty.
        # If this case ever goes red, the environment lost its tz data — see
        # `tests/unit/test_1823_tz_capability_parity.py`, which says so directly.
        ("0 4 * * *", "Europe/Kiev"),  # non-UTC zone, legacy alias
        ("0 4 * * 1-5", "UTC"),        # weekday range
    ],
)
def test_accepts_valid(cron, tz):
    validate_cron_expression(cron, tz)  # must not raise


@pytest.mark.parametrize(
    "cron",
    [
        "@daily",           # macro croniter accepts, scheduler rejects
        "@hourly",
        "*/5 * * * * *",    # 6-field seconds cron
        "0 0 L * *",        # quartz last-day
        "0 0 * * 1#2",      # quartz nth-weekday
        "0 4 * *",          # 4 fields
        "",                 # empty
    ],
)
def test_rejects_bad_cron(cron):
    with pytest.raises(ScheduleValidationError):
        validate_cron_expression(cron, "UTC")


def test_rejects_bad_timezone_combined():
    with pytest.raises(ScheduleValidationError):
        validate_cron_expression("0 4 * * *", "Not/AZone")


def test_validate_timezone():
    validate_timezone("UTC")
    validate_timezone("Europe/Kiev")
    validate_timezone("")  # empty allowed — the scheduler defaults to UTC
    validate_timezone(None)  # type: ignore[arg-type]
    with pytest.raises(ScheduleValidationError):
        validate_timezone("Not/AZone")


# ---------------------------------------------------------------------------
# #1823 — the two halves must agree (AC 3)
# ---------------------------------------------------------------------------

# Canonical names, legacy aliases (the #1823 class), the odd shapes that could
# plausibly break a `.zone`-keyed probe, plus the nonsense and empty cases.
_TZ_CORPUS = (
    "UTC",
    "Europe/London",
    "America/New_York",
    "Asia/Tokyo",
    "Europe/Kiev",      # legacy alias -> Europe/Kyiv (the issue's exact key)
    "Europe/Kyiv",
    "Asia/Calcutta",    # legacy alias -> Asia/Kolkata
    "Asia/Saigon",      # legacy alias -> Asia/Ho_Chi_Minh
    "US/Eastern",       # legacy top-level region
    "GMT",              # no region prefix
    "Etc/GMT+5",        # fixed offset, inverted sign
    "Not/AZone",        # nonsense
    "",                 # empty -> scheduler defaults to UTC
    None,               # unset -> same
)


def _scheduler_would_register(tz) -> bool:
    """Exactly what ``src/scheduler/service.py::_add_job`` does with the stored
    timezone, run for real — never a second `zoneinfo` probe.

    Mirrors the production line
    ``pytz.timezone(schedule.timezone) if schedule.timezone else pytz.UTC``
    followed by ``CronTrigger(timezone=...)``, so the comparison below covers the
    whole chain including APScheduler's own ``astimezone()`` re-resolution.
    """
    try:
        timezone = pytz.timezone(tz) if tz else pytz.UTC
        CronTrigger(
            minute="0", hour="4", day="*", month="*", day_of_week="*",
            timezone=timezone,
        )
    except Exception:  # noqa: BLE001 — any raise means "would not register"
        return False
    return True


@pytest.mark.parametrize("tz", _TZ_CORPUS)
def test_validate_timezone_agrees_with_crontrigger(tz):
    """`validate_timezone` raises IFF the scheduler could not register the zone.

    Honest statement of this test's power. Because `validate_timezone` probes
    `zoneinfo` and APScheduler resolves through `zoneinfo` too, and because
    `pytz.timezone(x).zone == x` for every alias in the corpus, the two sides key
    on the same string — so today this is *close to* tautological. What it can
    still catch, all of which are this bug's recurrence modes:

      * a future pytz release that normalises `.zone` away from the input key
        (the probe would then be checking the wrong string);
      * an APScheduler change to its resolution strategy — `astimezone()` is the
        exact function that caused #1823;
      * a regression that reverts `validate_timezone` to a pytz-only probe.

    What it explicitly CANNOT do is prove this environment can resolve any given
    zone; it passes in a tz-complete environment and in a broken one alike, since
    both sides move together. That assertion lives in
    `tests/unit/test_1823_tz_capability_parity.py`, not here.
    """
    registers = _scheduler_would_register(tz)
    try:
        validate_timezone(tz)
        rejected = False
    except ScheduleValidationError:
        rejected = True

    assert rejected != registers, (
        f"validate_timezone and the scheduler disagree about {tz!r}: "
        f"validator {'rejects' if rejected else 'accepts'}, "
        f"CronTrigger {'registers' if registers else 'refuses'}"
    )


@pytest.mark.parametrize("tz", _TZ_CORPUS)
def test_validate_cron_expression_agrees_with_crontrigger(tz):
    """Same pin for the create route's sole validator.

    `POST /api/agents/{name}/schedules` calls ONLY `validate_cron_expression`;
    the update route calls `validate_timezone`. Pinning one and not the other
    is how the two routes came to enforce different timezone contracts.
    """
    registers = _scheduler_would_register(tz)
    try:
        validate_cron_expression("0 4 * * *", tz)
        rejected = False
    except ScheduleValidationError:
        rejected = True

    assert rejected != registers, (
        f"validate_cron_expression and the scheduler disagree about {tz!r}"
    )


def test_validate_cron_expression_delegates_to_validate_timezone(monkeypatch):
    """The create path shares the update path's probe — one contract, not two.

    Structural, on purpose: an equivalence test cannot tell "delegates" from
    "happens to agree", and "happens to agree" is exactly the state #1823 was
    born in. If someone re-inlines a pytz-only probe here, both halves still
    agree on every real zone and every behavioural test above stays green.
    """
    seen = []
    monkeypatch.setattr(
        schedule_validation, "validate_timezone", lambda tz: seen.append(tz)
    )
    schedule_validation.validate_cron_expression("0 4 * * *", "Europe/Kiev")
    assert seen == ["Europe/Kiev"], (
        "validate_cron_expression no longer routes its timezone check through "
        "validate_timezone — the create and update routes can now diverge"
    )


# ---------------------------------------------------------------------------
# #1823 — the escaping exception is converted, never re-raised (AC 2)
# ---------------------------------------------------------------------------
#
# SCOPE, stated so a later reader does not over-read these two tests.
#
# With the packaging fix in place there is NO real input that reaches this code:
# 0 of the 597 pytz zones are unresolvable on a tz-complete runtime. They are
# therefore FAULT-INJECTED, and what they prove is that *our handler maps the
# exception type to ScheduleValidationError* — our code. They prove nothing
# whatsoever about whether an environment has its tz data; that is
# `tests/unit/test_1823_tz_capability_parity.py`'s job, and inside the image it
# is the only place AC 1 is provable.
#
# The handlers are kept anyway because AC 2 requires "never a 500" independent
# of packaging: the defect was an exception class slipping through an
# except-clause, so the regression to pin is the class, not the message.


class _NoTzDatabase:
    """Stand-in for the `zoneinfo` module with an empty database.

    Substituted for the module attribute `schedule_validation.zoneinfo` rather
    than patching the stdlib, so nothing else in the process is affected. The
    real exception class is re-exported because the code under test catches
    through this same attribute.
    """

    ZoneInfoNotFoundError = zoneinfo.ZoneInfoNotFoundError

    @staticmethod
    def ZoneInfo(key):  # noqa: N802 — mirrors the stdlib name
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")


def test_unresolvable_zone_is_converted_not_raised(monkeypatch):
    """`validate_timezone` converts ZoneInfoNotFoundError, never lets it escape.

    A ZoneInfoNotFoundError escaping a validator is a 500 at the route. This is
    the exact shape of #1823's headline defect, one function along.
    """
    monkeypatch.setattr(schedule_validation, "zoneinfo", _NoTzDatabase)

    with pytest.raises(ScheduleValidationError) as exc_info:
        schedule_validation.validate_timezone("Europe/Kiev")

    # Not a ZoneInfoNotFoundError / KeyError leaking through the ValueError base.
    assert not isinstance(exc_info.value, KeyError)
    message = str(exc_info.value)
    assert "Europe/Kiev" in message
    # Distinguishable from plain "no such zone": this one names an image defect.
    assert "tzdata" in message, (
        f"the unresolvable-zone error must point at the missing tz data, not "
        f"read as 'you typed a bad zone': {message!r}"
    )


def test_crontrigger_zoneinfo_error_is_converted_not_raised(monkeypatch):
    """`validate_cron_expression`'s backstop converts it too (AC 2).

    Injected at `CronTrigger` rather than at the probe, so it exercises the arm
    that fires when the probe passed and APScheduler still refused — the FUTURE
    divergence the backstop exists for, not a re-test of the probe.
    """
    def _boom(**_kwargs):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key Europe/Kiev")

    monkeypatch.setattr(schedule_validation, "CronTrigger", _boom)

    with pytest.raises(ScheduleValidationError) as exc_info:
        schedule_validation.validate_cron_expression("0 4 * * *", "Europe/Kiev")

    assert not isinstance(exc_info.value, KeyError)
    assert "Europe/Kiev" in str(exc_info.value)


def test_crontrigger_keyerror_is_not_relabelled_as_a_timezone_problem(monkeypatch):
    """A genuine KeyError inside CronTrigger must NOT be caught here.

    `ZoneInfoNotFoundError` subclasses `KeyError`, so the tempting widening is
    `except KeyError` — which would swallow a real dict-key bug in APScheduler
    and report it to the operator as "unknown timezone", sending them after the
    wrong thing entirely. Pin the narrow catch.
    """
    def _boom(**_kwargs):
        raise KeyError("some_internal_field")

    monkeypatch.setattr(schedule_validation, "CronTrigger", _boom)

    with pytest.raises(KeyError):
        schedule_validation.validate_cron_expression("0 4 * * *", "UTC")
