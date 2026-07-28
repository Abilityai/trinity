"""#1713 — behavioral parity between the scheduler's timestamp helpers and the
backend canonical (Invariant #16, the #1474 bug class).

The standalone scheduler is a separate package/image and cannot import
``src/backend`` at runtime, so ``utc_now_iso`` / ``to_utc_iso`` are vendored into
``src/scheduler/utils.py``. `#1713`: that mirror declared "byte-for-byte"
vendoring with **no test**, and the claim was already false — ``to_utc_iso`` is
functionally identical but textually different (backend uses ``if/else`` +
comments; the mirror uses an early return), so a source-text diff cannot verify
it.

This guard asserts **agreement on OUTPUT**, not source equality: if a future edit
to the backend copy changes the emitted format (drops the ``Z``, changes the
separator, mishandles a tz), one of these fails and names the desync — which is
exactly the #1474 regression (naive strings a JS ``new Date(...)`` renders shifted
by the viewer's offset).

Sibling mirror ``src/scheduler/failure_classifier.py`` is deliberately NOT covered
here: its two copies are genuinely **byte-identical**, so its byte-parity test
(``test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity``) is valid and
already enforced + referenced from that file. Only ``utils.py`` needed a
behavioral guard.
"""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_HELPERS = _ROOT / "src" / "backend" / "utils" / "helpers.py"
_SCHEDULER_UTILS = _ROOT / "src" / "scheduler" / "utils.py"

# The one true shape both sides must emit: `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
_Z_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _load(path: Path, name: str):
    """Load a stdlib-only module in isolation (no package import), so the
    scheduler copy is exercised without pulling in the backend package."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def backend():
    assert _BACKEND_HELPERS.exists(), f"missing {_BACKEND_HELPERS}"
    return _load(_BACKEND_HELPERS, "_helpers_backend_1713")


@pytest.fixture(scope="module")
def scheduler():
    assert _SCHEDULER_UTILS.exists(), f"missing {_SCHEDULER_UTILS}"
    return _load(_SCHEDULER_UTILS, "_utils_scheduler_1713")


# Naive, aware-UTC, aware-non-UTC — all denote the SAME instant (10:30:00.123456Z).
_CASES = [
    pytest.param(datetime(2026, 1, 15, 10, 30, 0, 123456), id="naive-assumed-utc"),
    pytest.param(datetime(2026, 1, 15, 10, 30, 0, 123456, tzinfo=timezone.utc), id="aware-utc"),
    pytest.param(
        datetime(2026, 1, 15, 13, 30, 0, 123456, tzinfo=timezone(timedelta(hours=3))),
        id="aware-plus-3",
    ),
    pytest.param(
        datetime(2026, 1, 15, 5, 30, 0, 123456, tzinfo=timezone(timedelta(hours=-5))),
        id="aware-minus-5",
    ),
]

_EXPECTED = "2026-01-15T10:30:00.123456Z"


@pytest.mark.parametrize("dt", _CASES)
def test_to_utc_iso_agrees_across_naive_and_aware(dt, backend, scheduler):
    """AC: the two `to_utc_iso` agree across naive, aware-UTC, and aware-non-UTC —
    an output diff, not a source diff. All four inputs are the same instant."""
    b = backend.to_utc_iso(dt)
    s = scheduler.to_utc_iso(dt)
    assert b == s, (
        f"to_utc_iso desynced for {dt!r}: backend={b!r} scheduler={s!r}. "
        "Re-sync src/scheduler/utils.py::to_utc_iso from the backend copy."
    )
    # ...and both must be the canonical Z-suffixed shape (the #1474 invariant).
    assert _Z_ISO.match(s), f"scheduler.to_utc_iso emitted a non-Z shape: {s!r}"
    assert s == _EXPECTED, f"expected {_EXPECTED!r}, got {s!r}"


def test_utc_now_iso_emits_the_same_z_shape(backend, scheduler):
    """AC: both `utc_now_iso()` produce the Z-suffixed, T-separated ISO format.
    Can't compare instants (time moves), so assert the emitted shape matches."""
    b = backend.utc_now_iso()
    s = scheduler.utc_now_iso()
    assert _Z_ISO.match(b), f"backend.utc_now_iso shape drifted: {b!r}"
    assert _Z_ISO.match(s), f"scheduler.utc_now_iso shape drifted: {s!r}"
    # Same length + same structural template (digits blanked) — catches a
    # separator/precision/suffix change on either side.
    assert re.sub(r"\d", "0", b) == re.sub(r"\d", "0", s), (
        f"utc_now_iso format desynced: backend={b!r} scheduler={s!r}"
    )


def test_parse_scheduler_ts_returns_naive_utc(scheduler):
    """The read-side counterpart is scheduler-SPECIFIC: it must return **naive**
    UTC (preserving the historical naive row model + `aware − naive` duration
    math, #1474). This pins that on purpose so a well-meaning "make it aware"
    edit — which would raise on the duration subtraction — fails here, and so a
    parity refactor never accidentally couples it to the backend's aware parse."""
    parse = scheduler.parse_scheduler_ts
    # 'Z', explicit offset, and legacy-naive must all land on the SAME naive
    # instant (the offset one converted to UTC), tzinfo dropped.
    z = parse("2026-01-15T10:30:00.123456Z")
    off = parse("2026-01-15T13:30:00.123456+03:00")
    naive = parse("2026-01-15T10:30:00.123456")
    for got in (z, off, naive):
        assert got.tzinfo is None, f"parse_scheduler_ts must return naive UTC, got {got!r}"
    assert z == off == naive == datetime(2026, 1, 15, 10, 30, 0, 123456)
