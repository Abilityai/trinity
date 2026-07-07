"""#1472 — schedule cron + timezone validation must match the scheduler's parser.

The dedicated scheduler registers a job via a strict 5-field ``_parse_cron`` +
APScheduler ``CronTrigger`` + ``pytz`` timezone. The API previously used a looser
bare ``croniter()`` that accepted ``@daily``/``@hourly`` macros, 6-field
seconds-crons, quartz ``L``/``#`` tokens, and ANY timezone string. Those cleared
the API but failed ``_add_job`` — the schedule was silently orphaned and its
``next_run_at`` froze ("Next: Nd ago"). ``validate_cron_expression`` /
``validate_timezone`` must reject exactly what the scheduler would.
"""

import pytest

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
        ("0 4 * * *", "Europe/Kiev"),  # non-UTC zone
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
