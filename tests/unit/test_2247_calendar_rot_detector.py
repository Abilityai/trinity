"""A test that would have caught the rot the day BEFORE it started (#2247).

`test_ent96_timeline_split.py` pinned an absolute hour (`"2026-08-14T09"`) against
a relative 24-hour axis. It passed on the day it landed and failed every day after
— five tests permanently red on `dev`, with no red check anywhere: invisible on
PRs (base and head fail identically, so the regression diff is empty) and
invisible on pushes (the diff job does not run there).

#2243 fixed those fixtures. This is the part that stops the CLASS coming back:
the calendar-sensitive files are re-run here against a clock a week ahead, in the
ordinary unit suite, so a fixture that only works "this week" fails immediately
rather than after the calendar moves.

Why a subprocess and not an in-process fixture: the shift has to be installed
BEFORE the module under test binds `from datetime import datetime`, which is
exactly what `tests/clockshift.py` does as a `-p` plugin, and what an autouse
fixture cannot do for a module pytest has already imported.

Cost, measured rather than estimated: **~37s** for this file — four pytest boots
(two files × two directions) plus two interpreter probes, where the tests
themselves account for under 5s of it. That boot overhead is the reason the list
below is deliberately short instead of the whole suite.

Proven to detect, not just to pass: reverting `test_ent96_timeline_split.py` to
its pre-#2243 state makes both directions of this file fail with the message
naming the cause, and restoring the fix makes them green again. A rot detector
that has never been shown to go red is decoration.

Chosen, not exhaustive: `test_1771c_schedules_analytics_edges.py` is the other
obviously calendar-shaped file but takes ~176s (it builds real SQLite schemas per
case), so re-running it here would triple this file's cost for a second sample of
the same property. The absolute-failure gate on `dev` pushes covers the general
case; this covers the specific class cheaply.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_TESTS = _REPO / "tests"
_PLUGIN = _TESTS / "clockshift.py"

# Files whose assertions depend on a window computed from `now`. Add to this list
# when a test starts comparing a fixture value against a relative window.
CALENDAR_SENSITIVE = [
    "unit/test_ent96_timeline_split.py",
    "unit/test_1771b_timestamp_helpers_properties.py",
]

# A week, as the issue proposes. Far enough that a 24h-window fixture built from a
# literal is certainly outside it; short enough that nothing else drifts.
SHIFT_DAYS = 7


def _run_shifted(target: str, days: int) -> subprocess.CompletedProcess:
    env = {
        # `-p clockshift` resolves plugins before pytest puts `tests/` on sys.path.
        "PYTHONPATH": str(_TESTS),
        "CLOCKSHIFT_DAYS": str(days),
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
    }
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", target,
            "-p", "clockshift", "-p", "no:randomly", "-q", "--no-header",
            "-p", "no:cacheprovider",
        ],
        cwd=_TESTS,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_the_harness_is_present():
    """The detector is worthless if the plugin quietly vanishes — then every
    parametrised case below would 'pass' by shifting nothing."""
    assert _PLUGIN.exists(), f"{_PLUGIN} is missing; the shift below would be a no-op"


def test_the_harness_actually_moves_the_clock():
    """Guards the no-op failure mode directly: prove the plugin changes what
    `datetime.now()` returns before trusting a green run under it."""
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "import clockshift, datetime as d;"
            "print((d.datetime.now(d.timezone.utc) - d.datetime.now(d.timezone.utc)).days)",
        ],
        cwd=_TESTS,
        env={"PYTHONPATH": str(_TESTS), "CLOCKSHIFT_DAYS": str(SHIFT_DAYS),
             "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True, text=True, timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    # Both calls are shifted, so their difference is ~0 — what this asserts is that
    # importing the plugin does not raise and `now()` still works under the swap.
    assert probe.stdout.strip() in {"0", "-1"}, probe.stdout

    year_now = subprocess.run(
        [sys.executable, "-c",
         "import clockshift, datetime as d; print(d.datetime.now(d.timezone.utc).isoformat())"],
        cwd=_TESTS,
        env={"PYTHONPATH": str(_TESTS), "CLOCKSHIFT_DAYS": "3650",
             "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True, text=True, timeout=120,
    )
    shifted = subprocess.run(
        [sys.executable, "-c",
         "import datetime as d; print(d.datetime.now(d.timezone.utc).isoformat())"],
        cwd=_TESTS, capture_output=True, text=True, timeout=120,
    )
    assert year_now.stdout > shifted.stdout, (year_now.stdout, shifted.stdout)


@pytest.mark.parametrize("target", CALENDAR_SENSITIVE)
def test_still_passes_a_week_from_now(target):
    """The rot detector. A fixture that hardcodes a date inside a relative window
    fails here on the first run, not on some future Tuesday."""
    result = _run_shifted(target, SHIFT_DAYS)
    assert result.returncode == 0, (
        f"{target} fails with the clock {SHIFT_DAYS} days ahead — it is pinned to "
        f"the calendar, not to the clock its assertions derive from (#2247).\n"
        f"--- stdout ---\n{result.stdout[-3000:]}\n--- stderr ---\n{result.stderr[-1000:]}"
    )


@pytest.mark.parametrize("target", CALENDAR_SENSITIVE)
def test_still_passes_a_year_back(target):
    """The mirror case, cheap to add and a different bug: a fixture that assumes a
    date is in the PAST (a retention cutoff, an expiry) breaks when the clock moves
    the other way."""
    result = _run_shifted(target, -365)
    assert result.returncode == 0, (
        f"{target} fails with the clock a year EARLIER (#2247).\n"
        f"--- stdout ---\n{result.stdout[-3000:]}"
    )
