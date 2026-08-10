"""Schedule cron + timezone validation (#1472, #1823).

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

**The timezone resolver is `zoneinfo`, not `pytz` (#1823).** The scheduler's real
resolution chain is::

    pytz.timezone(name) -> CronTrigger(timezone=<pytz obj>)
        -> APScheduler astimezone() -> zoneinfo.ZoneInfo(obj.zone)

so a zone registers iff **both** halves accept it. pytz bundles its *own*
complete tz database; `zoneinfo` depends on the system database plus the
optional `tzdata` wheel. pytz's zone set is therefore a superset, and `zoneinfo`
is the strictly narrower, actually-binding constraint. #1472 mirrored the
scheduler's *visible first step* (``pytz.timezone``) and missed the
re-resolution behind it, which is how ``Europe/Kiev`` — accepted by
``validate_timezone`` in every environment — reached ``CronTrigger`` and raised
``ZoneInfoNotFoundError``, a ``KeyError`` subclass that the ``(ValueError,
TypeError)`` rescue below did not catch, as an unhandled 500.

**One probe, not two.** ``validate_cron_expression`` *delegates* its timezone
check to ``validate_timezone`` rather than re-implementing it: the create route
calls only the former and the update route calls both, so two separate
mechanisms would mean two different timezone contracts on two routes — the
split-brain #1823 exists to delete.

Keep this in parity with ``src/scheduler/service.py::_parse_cron`` /
``_cron_dow_to_apscheduler`` (guarded by
``tests/unit/test_1472_schedule_validation.py``).
"""

import zoneinfo

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


def _unresolvable_timezone_error(timezone: str) -> ScheduleValidationError:
    """The error for a zone pytz knows but this runtime's IANA database lacks.

    Deliberately distinguishable from the plain "Unknown timezone" case: that one
    means *no such zone anywhere*, this one means *this image cannot resolve a
    real zone*, which is an image-packaging defect (#1823). Naming which is which
    is how the next occurrence is diagnosed in one read instead of one week.

    Built here so ``validate_timezone`` and ``validate_cron_expression``'s
    backstop cannot drift into two different messages for one condition.
    """
    return ScheduleValidationError(
        f"Unknown timezone: {timezone!r} — known to pytz but absent from this "
        f"runtime's IANA time zone database, so the scheduler cannot register "
        f"it. Legacy aliases (e.g. 'Europe/Kiev' for 'Europe/Kyiv') need the "
        f"backward-compatibility links: the 'tzdata-legacy' system package or "
        f"the 'tzdata' wheel."
    )


def validate_timezone(timezone: str) -> None:
    """Raise ScheduleValidationError unless BOTH resolvers the scheduler
    traverses accept the zone (#1472, #1823).

    An empty/None timezone is allowed (the scheduler defaults to UTC).

    The `zoneinfo` probe is keyed on ``pytz.timezone(tz).zone`` — the exact
    string APScheduler hands to ``ZoneInfo`` — rather than the raw input. The two
    are identical for every alias measured today; probing ``.zone`` costs nothing
    and stays correct if pytz ever starts normalising.
    """
    if not timezone:
        return
    try:
        tz = pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        raise ScheduleValidationError(f"Unknown timezone: {timezone!r}")

    # #1823: pytz carries its own complete database, so it accepts zones this
    # runtime cannot resolve. APScheduler's astimezone() re-resolves through
    # zoneinfo, so this — not the pytz probe above — is the binding constraint.
    try:
        zoneinfo.ZoneInfo(getattr(tz, "zone", None) or timezone)
    except zoneinfo.ZoneInfoNotFoundError:
        raise _unresolvable_timezone_error(timezone)


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

    # #1823: ONE timezone contract. This used to be a duplicated pytz-only probe,
    # which meant the create route (which calls only this function) and the
    # update route (which calls validate_timezone) enforced different rules for
    # the same field. Delegate instead — the create path inherits the zoneinfo
    # probe it never had, and there is exactly one place to change the contract.
    validate_timezone(timezone)
    tz = pytz.timezone(timezone) if timezone else pytz.UTC

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
    except zoneinfo.ZoneInfoNotFoundError:
        # #1823 backstop, unreachable via the delegation above for any zone the
        # probe agrees with. Kept because AC 2 ("never a 500") must hold for a
        # FUTURE divergence the probe does not anticipate — a new APScheduler
        # resolution strategy, or a zone that disappears between the two calls.
        #
        # Named, NOT a bare `KeyError`: ZoneInfoNotFoundError subclasses KeyError,
        # so a bare catch would swallow a genuine dict-key bug inside CronTrigger
        # and misreport it as "unknown timezone".
        raise _unresolvable_timezone_error(timezone)
    except (ValueError, TypeError) as e:
        raise ScheduleValidationError(
            f"Invalid cron expression: {cron_expression!r} ({e})"
        )
