"""Run the suite as if the machine clock were a year ahead (#2243).

    PYTHONPATH=tests python -m pytest tests/unit -q -p clockshift

(`PYTHONPATH=tests` because `-p` resolves plugins before pytest has put `tests/`
on `sys.path`, and this file deliberately is not a `conftest` — it must not load
for runs that did not ask for it.)

Why this exists. Three sets of tests were permanently red on `dev` for one
reason: they asserted against literal calendar strings — an hour bucket
(`"2026-08-14T09"`) that the gap-filled axis stops reaching, and a `started_at`
anchor (`"2026-01-01T00:00:00.000000Z"`) whose `duration_ms = now − started_at`
grew past int32 and raised `NumericValueOutOfRange` on PostgreSQL. Both passed
the day they were written and failed every day after. A grep can find date-shaped
strings, but it cannot tell an inert fixture value from one that is compared
against *now* — so the class is closed by running the clock forward and reading
the failures, not by eyeballing literals.

Not a test dependency and not a `freezegun`: it shifts the two clocks test code
actually reads and adds nothing to `requirements`. Deliberately opt-in — it is a
diagnostic for "is this suite date-independent?", not something CI runs by
default.

Loaded with `-p` so it lands BEFORE test modules run their
`from datetime import datetime`, which is what makes the shift visible to code
that binds the name at import time. Override the distance with
`CLOCKSHIFT_DAYS` (e.g. `CLOCKSHIFT_DAYS=-400` to run a year in the past).

Known limits, stated so a green run is not over-read: it moves
`datetime.datetime.now/utcnow/today` and `time.time`. It does NOT move a
database's own clock (`CURRENT_TIMESTAMP`, `now()`), nor the FILESYSTEM's, nor any
C-level time source a dependency reads directly, so a test whose expectations come
from SQL-side or inode time is out of its reach.

The filesystem case is not hypothetical — a full-suite run at +365 days left
exactly four failures beyond the environment-specific set, all of it one root
cause, and it breaks in BOTH directions:

  * `test_1595_git_maintenance.py::TestReapStaleGitLitter::test_fresh_locks_kept`
    and `test_2216_backup_primitives.py::TestTmpSweep::test_sweeps_aged_orphan_keeps_fresh`
    write a file NOW (real mtime) and expect the code to judge it fresh; against a
    shifted `now` it looks a year old and gets reaped.
  * `test_2216_db_backup_service.py::TestPreflightAndFailure::test_prune_runs_{even_when_backup_fails,on_no_space_skip_too}`
    are the mirror: an artifact meant to be out-of-window looks freshly created,
    so the prune correctly leaves it and the assertion fails.

Those four tests are CORRECT; the instrument cannot see their clock. So a
mtime-driven sweep test is not evidence of calendar rot under this harness, and
shifting inode times (an `os.stat` interception) would be a much larger instrument
than the problem justifies. Read a shifted run's failures with that in mind.

One false-positive class is handled rather than documented, because a sweep whose
output has to be triaged by hand is not a verdict. `datetime` cannot be patched
in place (a C type with immutable attributes), so the shift installs a subclass —
which splits the class identity: a library imported AFTER the swap type-checks
against the subclass, while a value produced by code that imported `datetime`
BEFORE it is an instance of the original. PyJWT does exactly this
(`isinstance(payload["exp"], datetime)` in `api_jwt.encode`, guarding the
datetime→epoch conversion), so under a naive swap ~30 auth/session tests failed
with `TypeError: Object of type datetime is not JSON serializable` — a harness
artifact indistinguishable, in a failure list, from a real calendar bug. The
metaclass below makes `isinstance(any_real_datetime, _ShiftedDatetime)` true, so
both identities satisfy such a check.
"""
from __future__ import annotations

import datetime as _dt
import os
import time as _time

_DAYS = int(os.environ.get("CLOCKSHIFT_DAYS", "365"))
SHIFT = _dt.timedelta(days=_DAYS)

_real_datetime = _dt.datetime


class _AnyDatetime(type):
    """Make `isinstance(x, _ShiftedDatetime)` true for ANY real `datetime`.

    Without this the swap narrows a widely-used type test: a real `datetime`
    (built by a module that bound the name before the swap) is not an instance of
    the subclass, so a library checking `isinstance(value, datetime)` silently
    takes its else-branch. See the module docstring for the PyJWT case.
    """

    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_datetime)


class _ShiftedDatetime(_real_datetime, metaclass=_AnyDatetime):
    """`datetime` with the three "what time is it" constructors moved.

    A subclass, not a stub: instances stay real `datetime`s, so comparisons and
    arithmetic against values parsed from the database keep working.
    """

    @classmethod
    def now(cls, tz=None):
        return _real_datetime.now(tz) + SHIFT

    @classmethod
    def utcnow(cls):
        return _real_datetime.utcnow() + SHIFT

    @classmethod
    def today(cls):
        return _real_datetime.today() + SHIFT


_dt.datetime = _ShiftedDatetime

_real_time = _time.time
_time.time = lambda: _real_time() + SHIFT.total_seconds()


def pytest_report_header(config):  # noqa: ARG001 — pytest hook signature
    return f"clockshift: clocks moved {_DAYS:+d} days (#2243)"
