"""Schedule cron + timezone validation (#1472).

The dedicated scheduler (``src/scheduler/service.py``) registers a schedule by
splitting the cron into **exactly 5 fields** (``_parse_cron``), translating the
day-of-week from Unix-cron numbering to APScheduler named days
(``_cron_dow_to_apscheduler``), and constructing an APScheduler ``CronTrigger``
under a ``pytz`` timezone.

The API must validate with the SAME parser. A bare ``croniter()`` check (what the
router used before #1472) is looser: it accepts ``@daily``/``@hourly`` macros,
6-field seconds-crons, and quartz tokens (``L``/``#``/``W``), and never checks
the timezone at all. Any expression that clears croniter but fails the
scheduler's ``_add_job`` was silently orphaned — the schedule never fired and its
``next_run_at`` froze ("Next: Nd ago", #1472).

Keep this in parity with ``src/scheduler/service.py::_parse_cron`` /
``_cron_dow_to_apscheduler`` (guarded by
``tests/unit/test_1472_schedule_validation.py``).
"""

import pytz
from apscheduler.triggers.cron import CronTrigger

# Unix cron day-of-week (0/7=Sun) → APScheduler named days. Mirrors
# src/scheduler/service.py::_cron_dow_to_apscheduler.
_DOW_NAMES = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}


class ScheduleValidationError(ValueError):
    """Cron expression or timezone that would fail scheduler registration."""


def _dow_to_apscheduler(dow: str) -> str:
    def _token(t: str) -> str:
        try:
            return _DOW_NAMES[int(t)]
        except (ValueError, KeyError):
            return t  # already a named day or unrecognised — leave as-is
    if dow == "*" or "/" in dow:
        return dow
    if "," in dow:
        return ",".join(_token(t) for t in dow.split(","))
    if "-" in dow:
        lo, hi = dow.split("-", 1)
        return f"{_token(lo)}-{_token(hi)}"
    return _token(dow)


def validate_timezone(timezone: str) -> None:
    """Raise ScheduleValidationError if the timezone is not a valid pytz zone.
    An empty/None timezone is allowed (the scheduler defaults to UTC)."""
    if not timezone:
        return
    try:
        pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        raise ScheduleValidationError(f"Unknown timezone: {timezone!r}")


def validate_cron_expression(cron_expression: str, timezone: str = "UTC") -> None:
    """Validate a cron + timezone exactly as the dedicated scheduler will register
    it. Raises ScheduleValidationError on anything ``_add_job`` would reject
    (wrong field count, quartz tokens, out-of-range fields, unknown timezone)."""
    parts = (cron_expression or "").strip().split()
    if len(parts) != 5:
        raise ScheduleValidationError(
            f"Invalid cron expression: {cron_expression!r}. Expected exactly 5 "
            f"fields (minute hour day month day_of_week), got {len(parts)}. "
            f"Macros like @daily and 6-field seconds-crons are not supported."
        )

    tz = pytz.UTC
    if timezone:
        try:
            tz = pytz.timezone(timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            raise ScheduleValidationError(f"Unknown timezone: {timezone!r}")

    minute, hour, day, month, dow = parts
    try:
        CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=_dow_to_apscheduler(dow),
            timezone=tz,
        )
    except (ValueError, TypeError) as e:
        raise ScheduleValidationError(
            f"Invalid cron expression: {cron_expression!r} ({e})"
        )
