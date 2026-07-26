"""#1771 slice b (target 2) — discrete edge cases for the timestamp helpers.

Targets: ``src/backend/utils/helpers.py`` (``utc_now_iso``, ``iso_cutoff``,
``to_utc_iso``, ``parse_iso_timestamp``) and its vendored scheduler mirror
``src/scheduler/utils.py`` (``utc_now_iso``, ``to_utc_iso``,
``parse_scheduler_ts``). Companion file: ``test_1771b_timestamp_helpers_properties.py``
(the Hypothesis properties). Matrix-row ids (``A7``, ``D6``, …) in the parametrize
ids map back to the edge-case matrix in ``.plan/issue-1771b.md`` §3.

WHY BY-PATH LOADING: ``tests/unit/pytest.ini`` sets ``norecursedirs = ..``, so
pytest picks ``tests/unit/`` as rootdir and the repo-root ``pythonpath``
(``src``) never applies — ``import scheduler.utils`` does NOT work here. Loading
both modules with ``importlib.util.spec_from_file_location`` is the prior art
from ``test_1713_scheduler_utils_parity.py``: it exercises the scheduler copy in
genuine isolation (the whole reason the mirror exists) and mutates no
``sys.modules`` (so the ``lint-sys-modules`` CI job and its baseline are
untouched).

SCOPE: these tests pin **observed behaviour of the helpers**. Several rows pin a
HAZARD — behaviour that is a footgun rather than a blessed contract. Those are
labelled inline; do not cite a HAZARD pin as a reason to reject a fix.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
- Hour-24 parsing (``'…T24:00:00Z'``): parses on 3.14, raises on 3.12/3.13.
  CI is 3.11, prod images are 3.13, dev machines vary — any assertion is
  version-flaky (dossier G7).
- Uniform formatting of ``agent_schedules.next_run_at``: it is DELIBERATELY
  mixed-format (scheduler writes ``Z``, backend writes an offset), which
  Invariant #16 calls out as an honest caveat and explains is safe because that
  column is only ever parse-compared in Python, never lexicographically in SQL.
  Asserting uniformity there would be a wrong test, not a found bug (dossier §7).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_HELPERS = _ROOT / "src" / "backend" / "utils" / "helpers.py"
_SCHEDULER_UTILS = _ROOT / "src" / "scheduler" / "utils.py"

# The one canonical shape both copies must emit: `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
_Z_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

UTC = timezone.utc


def _load(path: Path, name: str):
    """Load a stdlib-only module in isolation (no package import)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def backend():
    assert _BACKEND_HELPERS.exists(), f"missing {_BACKEND_HELPERS}"
    return _load(_BACKEND_HELPERS, "_helpers_backend_1771b")


@pytest.fixture(scope="module")
def scheduler():
    assert _SCHEDULER_UTILS.exists(), f"missing {_SCHEDULER_UTILS}"
    return _load(_SCHEDULER_UTILS, "_utils_scheduler_1771b")


# =============================================================================
# A. to_utc_iso — the write side
# =============================================================================

_TO_UTC_ISO_CASES = [
    # (input, expected output) — matrix rows A4/A5/A6/A7/A8
    pytest.param(
        datetime(
            2026,
            1,
            15,
            16,
            15,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=5, minutes=45)),
        ),
        "2026-01-15T10:30:00.123456Z",
        id="A4-quarter-hour-zone-plus-0545",
    ),
    pytest.param(
        datetime(
            2026,
            1,
            15,
            10,
            30,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=-9, minutes=-30)),
        ),
        "2026-01-15T20:00:00.123456Z",
        id="A4-half-hour-zone-minus-0930",
    ),
    pytest.param(
        # Europe/London in BST (UTC+1) — a real IANA zone, not a fixed offset.
        datetime(2026, 7, 1, 12, 0, 0, 0, tzinfo=ZoneInfo("Europe/London")),
        "2026-07-01T11:00:00.000000Z",
        id="A5-iana-dst-summer",
    ),
    pytest.param(
        # Same zone, winter — GMT (UTC+0). Same wall clock, different instant.
        datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=ZoneInfo("Europe/London")),
        "2026-01-01T12:00:00.000000Z",
        id="A5-iana-dst-winter",
    ),
    pytest.param(
        # A6: the DST-repeat hour. fold=0 is the FIRST 01:30 (still BST, +01:00).
        datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0),
        "2026-10-25T00:30:00.000000Z",
        id="A6-ambiguous-fold-0",
    ),
    pytest.param(
        # A6: fold=1 is the SECOND 01:30 (now GMT, +00:00) — one hour LATER.
        datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=1),
        "2026-10-25T01:30:00.000000Z",
        id="A6-ambiguous-fold-1",
    ),
    pytest.param(
        datetime(2026, 1, 15, 10, 30, 0, 0),
        "2026-01-15T10:30:00.000000Z",
        id="A7-microsecond-zero-still-six-digits",
    ),
    pytest.param(
        datetime(2026, 1, 15, 10, 30, 0, 999999),
        "2026-01-15T10:30:00.999999Z",
        id="A8-microsecond-max-no-rounding",
    ),
]


@pytest.mark.parametrize("dt,expected", _TO_UTC_ISO_CASES)
def test_to_utc_iso_discrete_cases(dt, expected, backend, scheduler):
    """A4/A5/A6/A7/A8 — quarter- and half-hour zones, IANA DST, the ambiguous
    fold pair, and both microsecond extremes. Asserted on BOTH copies, so a
    desync of the vendored mirror fails here too."""
    assert backend.to_utc_iso(dt) == expected
    assert (
        scheduler.to_utc_iso(dt) == expected
    ), "vendored mirror desynced — re-sync src/scheduler/utils.py::to_utc_iso"


@pytest.mark.parametrize("dt,expected", _TO_UTC_ISO_CASES)
def test_to_utc_iso_always_emits_the_canonical_27_char_shape(dt, expected, backend):
    """A9 — fixed width (27 chars), `T` at index 10, `Z` suffix, six fractional
    digits. Fixed width is the *prerequisite* of Invariant #16's lexicographic
    ordering: a variable-width format would break `WHERE ts > :cutoff`."""
    out = backend.to_utc_iso(dt)
    assert _Z_ISO.match(out), f"non-canonical shape: {out!r}"
    assert len(out) == 27
    assert out[10] == "T"
    assert out.endswith("Z")


def test_A6_fold_pair_are_one_hour_apart(backend):
    """A6 — the two `fold` values of the SAME wall clock denote instants an hour
    apart, and `to_utc_iso` renders that correctly. This is the row that forced
    the properties file to use an `astimezone(utc)` chronology oracle."""
    a = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    b = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=1)
    assert a.utcoffset() == timedelta(hours=1)
    assert b.utcoffset() == timedelta(0)
    delta = b.astimezone(UTC) - a.astimezone(UTC)
    assert delta == timedelta(hours=1)
    assert backend.to_utc_iso(a) == "2026-10-25T00:30:00.000000Z"
    assert backend.to_utc_iso(b) == "2026-10-25T01:30:00.000000Z"


def test_A14_python_comparison_is_not_a_chronology_oracle_for_fold_pairs(backend):
    """A14 — HAZARD PIN, not a contract.

    PEP 495: two aware datetimes carrying the SAME zone object compare by wall
    clock and IGNORE `fold`. So Python calls the fold pair EQUAL while their
    instants differ by an hour, and `to_utc_iso` (correctly) does not.

    Pinned so nobody re-derives the ordering property with `a < b` as the oracle
    — that oracle fails against *correct* code. The correct oracle is
    `a.astimezone(utc) < b.astimezone(utc)`; see the properties file, P4.
    """
    a = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=0)
    b = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/London"), fold=1)

    # Python's own comparison: they are "equal".
    assert a == b
    assert not (a < b)

    # The formatter disagrees — and the formatter is right.
    assert backend.to_utc_iso(a) != backend.to_utc_iso(b)

    # Therefore the naive oracle is FALSE here...
    assert ((a < b) == (backend.to_utc_iso(a) < backend.to_utc_iso(b))) is False
    # ...and the UTC-converted oracle is TRUE.
    assert (a.astimezone(UTC) < b.astimezone(UTC)) == (
        backend.to_utc_iso(a) < backend.to_utc_iso(b)
    )


def test_A13_normalization_makes_naive_and_aware_mutually_comparable(backend):
    """A13 — the *benefit* of normalizing through `to_utc_iso`.

    A naive and an aware datetime cannot be compared directly (`TypeError`), but
    their normalized strings can. This is why every read boundary funnels through
    `parse_iso_timestamp` + `to_utc_iso` (#1474) — and why the ordering property
    restricts itself to same-awareness pairs.
    """
    naive = datetime(2026, 1, 15, 10, 30, 0, 123456)
    aware = datetime(
        2026, 1, 15, 13, 30, 0, 123456, tzinfo=timezone(timedelta(hours=3))
    )

    with pytest.raises(TypeError):
        _ = naive < aware  # noqa: B015 — the raise IS the assertion

    # Same instant once normalized, and now directly comparable as strings.
    assert backend.to_utc_iso(naive) == backend.to_utc_iso(aware)


# =============================================================================
# B. utc_now_iso
# =============================================================================


def test_B3_successive_calls_are_non_decreasing_not_strictly_increasing(backend):
    """B3 — ordering from `utc_now_iso()` is NON-strict.

    On a coarse clock two successive calls can return the IDENTICAL string.
    Pinned so no future test asserts `a < b` on consecutive calls and flakes.
    (This is also why the monotonicity properties use windows >= 1 minute apart:
    the gap must dominate the microseconds of elapsed test time.)
    """
    samples = [backend.utc_now_iso() for _ in range(200)]
    assert samples == sorted(samples), "utc_now_iso went backwards"
    # Non-strictness is the *allowed* case — assert only `<=`, never `<`.
    for earlier, later in zip(samples, samples[1:]):
        assert earlier <= later


# =============================================================================
# C. iso_cutoff
# =============================================================================


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"hours": -1}, id="C5-negative-hours"),
        pytest.param({"minutes": -30}, id="C5-negative-minutes"),
        pytest.param({"hours": 1, "minutes": -90}, id="C9-mixed-sign-net-negative"),
    ],
)
def test_C5_C9_negative_and_mixed_sign_windows_return_a_FUTURE_cutoff(kwargs, backend):
    """C5/C9 — HAZARD PIN: observed behaviour, NOT a contract. See finding F3.

    `iso_cutoff` validates nothing: it just sums `timedelta(hours=…, minutes=…)`
    and subtracts. A negative (or net-negative mixed-sign) window therefore
    yields a cutoff in the FUTURE, silently — a `WHERE ts > :cutoff` filter then
    matches nothing instead of erroring.

    This test exists to DOCUMENT the footgun and to make any future change to it
    visible. It must NOT be cited as evidence that the behaviour is intended or
    that a validation fix would be a breaking change — `iso_cutoff`'s own
    docstring says "positive", and the reachable caller
    (`routers/agents.py`'s unvalidated `hours: int = 24`) is reported as
    finding F3 for its own issue.
    """
    before = backend.utc_now_iso()
    cutoff = backend.iso_cutoff(**kwargs)
    assert cutoff > before, "expected a future cutoff for a negative window"
    assert _Z_ISO.match(cutoff), "even the hazard path keeps the canonical shape"


def test_C9_mixed_sign_windows_cancel_exactly(backend):
    """C9 — `hours=1, minutes=-60` cancel to a zero window (~now), because the
    two arguments are summed into ONE timedelta rather than applied separately."""
    before = backend.utc_now_iso()
    cutoff = backend.iso_cutoff(hours=1, minutes=-60)
    after = backend.utc_now_iso()
    assert before <= cutoff <= after


@pytest.mark.parametrize(
    "hours",
    [
        pytest.param(10**8, id="C7-1e8-hours"),
        pytest.param(10**9, id="C7-1e9-hours"),
        pytest.param(10**15, id="C7-1e15-hours"),
    ],
)
def test_C7_huge_windows_raise_OverflowError(hours, backend):
    """C7 — a huge window underflows the datetime domain and raises
    `OverflowError` (two different messages, one exception type).

    This is the REALISTIC hostile input, not a negative one: retention windows
    come from an operator ops-setting that `update_ops_settings` writes through
    with **no upper range check**, and every retention call site already guards
    the `<= 0` case. No `iso_cutoff` call is individually try/except-wrapped, so
    outside `cleanup_service`'s per-sweep handler this surfaces as a 500.

    Probed at discrete magnitudes only — never swept: the exact threshold depends
    on `now`, so a swept boundary would flake.
    """
    with pytest.raises(OverflowError):
        backend.iso_cutoff(hours)


# =============================================================================
# D. parse_iso_timestamp — the read side
# =============================================================================

_PARSE_ACCEPTED = [
    # (input, expected instant as aware UTC) — matrix rows D2..D8
    pytest.param(
        "2026-01-15T10:30:00Z",
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        id="D2-Z-without-fraction",
    ),
    pytest.param(
        "2026-01-15T10:30:00+00:00",
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        id="D3-explicit-zero-offset",
    ),
    pytest.param(
        "2026-01-15T13:30:00+03:00",
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        id="D4-non-utc-offset",
    ),
    pytest.param(
        "2026-01-15T10:30:00",
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        id="D5-bare-naive-assumed-utc",
    ),
    pytest.param(
        "2026-01-15 10:30:00",
        datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        id="D6-space-separator-the-sqlite-shape",
    ),
    pytest.param(
        "2026-01-15",
        datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
        id="D7-date-only-midnight",
    ),
    pytest.param(
        "2026-01-15T10:30:00.1Z",
        datetime(2026, 1, 15, 10, 30, 0, 100000, tzinfo=UTC),
        id="D8-one-fractional-digit",
    ),
    pytest.param(
        "2026-01-15T10:30:00.1234567Z",
        datetime(2026, 1, 15, 10, 30, 0, 123456, tzinfo=UTC),
        id="D8-seven-fractional-digits-truncated",
    ),
]


@pytest.mark.parametrize("raw,expected", _PARSE_ACCEPTED)
def test_D_parse_iso_timestamp_accepts_the_stored_shape_zoo(raw, expected, backend):
    """D2-D8 — every shape Trinity actually stores parses to the right INSTANT.

    D6 is the load-bearing one: `'YYYY-MM-DD HH:MM:SS'` is exactly what SQLite's
    `datetime('now')` emits and what `agent_activities.created_at`'s
    `DEFAULT CURRENT_TIMESTAMP` writes — the #476 shape. It parses fine; the bug
    #476 fixed was never about parsing, it was about comparing it in SQL (see G3).
    """
    got = backend.parse_iso_timestamp(raw)
    assert got == expected, f"{raw!r} -> {got!r}, expected {expected!r}"


def test_D4_offset_input_keeps_its_own_tzinfo_not_utc(backend):
    """D4 — pins the ACTUAL behaviour, which contradicts the docstring.

    `parse_iso_timestamp`'s docstring promises "timezone-aware datetime in UTC",
    but an offset-bearing input is returned with THAT offset — `fromisoformat`'s
    result is only `.replace(tzinfo=utc)`'d when it is naive. The instant is
    correct, so every call site that compares instants or pipes into
    `to_utc_iso` is unaffected; a caller reading `.hour`/`.date()` off the result
    would be wrong. Reported as finding F1 (docstring defect) — deliberately NOT
    fixed here (AC4: no product-code changes).
    """
    got = backend.parse_iso_timestamp("2026-01-15T13:30:00+03:00")
    assert got.tzinfo == timezone(timedelta(hours=3))
    assert got.tzinfo != UTC
    # ...but the instant is right, which is why no caller breaks today.
    assert got == datetime(2026, 1, 15, 10, 30, tzinfo=UTC)


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="fromisoformat accepts offsets with seconds only on 3.11+; the "
    "scheduler package declares requires-python >= 3.10, so this is reported "
    "rather than asserted on 3.10",
)
@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param(
            "2026-01-15T10:30:00+05:45:30",
            datetime(2026, 1, 15, 4, 44, 30, tzinfo=UTC),
            id="D14-offset-with-seconds",
        ),
        pytest.param(
            "2026-01-15T10:30:00.123456+05:45:30",
            datetime(2026, 1, 15, 4, 44, 30, 123456, tzinfo=UTC),
            id="D14-offset-with-seconds-and-fraction",
        ),
    ],
)
def test_D14_offsets_carrying_seconds_parse(raw, expected, backend):
    """D14 — sub-minute offsets are REACHABLE, not exotic: `pytz` historical LMT
    offsets carry seconds, and the backend writes `pytz`+`croniter`
    `.isoformat()` values into `agent_schedules.next_run_at`."""
    assert backend.parse_iso_timestamp(raw) == expected


_PARSE_REJECTED = [
    pytest.param("", id="D10-empty"),
    pytest.param("   ", id="D10-whitespace-only"),
    pytest.param("Z", id="D10-bare-Z"),
    pytest.param("not-a-timestamp", id="D10-garbage"),
    pytest.param("2026-13-45T99:99:99Z", id="D10-out-of-range-fields"),
    pytest.param("2026-01-15T10:30:00+00:00Z", id="D10-double-suffix"),
    pytest.param(" 2026-01-15T10:30:00Z", id="D13-leading-space"),
    pytest.param("2026-01-15T10:30:00Z ", id="D13-trailing-space"),
    pytest.param("\t2026-01-15T10:30:00Z\n", id="D13-surrounding-whitespace"),
    pytest.param("2026-01-15T10:30:00.123456z", id="D9-lowercase-z"),
]


@pytest.mark.parametrize("raw", _PARSE_REJECTED)
def test_D_parse_iso_timestamp_rejects_with_ValueError(raw, backend):
    """D9/D10/D13 — malformed input raises `ValueError`.

    D13: neither helper calls `.strip()`, so surrounding whitespace is fatal.
    D9: lowercase `'z'` raises while `'Z'` succeeds — ISO 8601 permits lowercase,
    so this is a spec gap rather than a decided contract; pinned as observed
    behaviour and reported, not proposed as a fix.
    """
    with pytest.raises(ValueError):
        backend.parse_iso_timestamp(raw)


@pytest.mark.parametrize(
    "raw,exc",
    [
        pytest.param(None, AttributeError, id="D11-None"),
        pytest.param(123, AttributeError, id="D11-int"),
        pytest.param(datetime(2026, 1, 15), AttributeError, id="D11-datetime"),
        pytest.param(b"2026-01-15T10:30:00Z", TypeError, id="D11-bytes"),
    ],
)
def test_D11_non_string_input_does_not_raise_ValueError(raw, exc, backend):
    """D11 — pins the ACTUAL exception types.

    A non-`str` fails on `.endswith` with `AttributeError`/`TypeError`, NOT
    `ValueError`. So an `except ValueError` guard written against the docstring
    would silently not catch it. No call site can currently pass a non-`str`
    (every nullable input is guarded; the unguarded ones are NOT NULL columns) —
    this pins reality for whoever writes the next guard.
    """
    with pytest.raises(exc):
        backend.parse_iso_timestamp(raw)


# =============================================================================
# E. parse_scheduler_ts — the scheduler read side
# =============================================================================


@pytest.mark.parametrize("raw,expected", _PARSE_ACCEPTED)
def test_E_parse_scheduler_ts_returns_the_same_instant_but_naive(
    raw, expected, scheduler
):
    """E2/E3 — for every accepted shape the scheduler copy lands on the SAME
    instant as the backend copy, but **naive** (tzinfo dropped after conversion).

    Naive is the point, not an oversight: the scheduler's row models are naive
    and its duration math is `datetime.utcnow() - started_at`, so an aware read
    would raise on the subtraction (#1474).
    """
    got = scheduler.parse_scheduler_ts(raw)
    assert got.tzinfo is None, f"parse_scheduler_ts must return naive UTC, got {got!r}"
    assert got == expected.replace(tzinfo=None)


@pytest.mark.parametrize("raw", _PARSE_REJECTED)
def test_E4_parse_scheduler_ts_rejects_the_same_inputs(raw, scheduler):
    """E4 — error parity: the two copies reject the same shapes.

    (Parity is NOT total — it breaks at the datetime-domain edge, where an
    offset-bearing year-1/year-9999 string parses on the backend but overflows
    on the scheduler's `astimezone`. That asymmetry is unreachable in Trinity and
    is documented as matrix row E6 + the year bound in the properties file.)
    """
    with pytest.raises(ValueError):
        scheduler.parse_scheduler_ts(raw)


def test_E5_naive_result_is_safe_for_utcnow_subtraction(scheduler):
    """E5 — the raison d'être of `parse_scheduler_ts` returning naive: the
    scheduler's duration math must not raise `TypeError: can't subtract
    offset-naive and offset-aware datetimes`."""
    started = scheduler.parse_scheduler_ts("2026-01-15T10:30:00.123456Z")
    delta = (
        datetime.utcnow() - started
    )  # noqa: DTZ003 — naive-by-design, that IS the point
    assert isinstance(delta, timedelta)


# =============================================================================
# G. Invariant #16 — the hazards that justify the rule
# =============================================================================


def test_G3_space_separated_cutoff_vs_iso_z_row_compares_WRONG(backend):
    """G3 — HAZARD PIN: the #476 bug itself, reproduced.

    `' '` (0x20) sorts BEFORE `'T'` (0x54), so on the SAME DATE a SQLite-shaped
    cutoff and an ISO-Z row compare backwards: `WHERE ts > :cutoff` admits a row
    it must exclude. This is why Invariant #16 mandates `iso_cutoff()` over
    `datetime('now', …)`.

    Same-date is essential — a cross-date pair coincidentally agrees, which is
    exactly why #476 was subtle enough to ship.
    """
    sqlite_cutoff = "2026-01-15 12:00:00"  # noon, SQLite datetime('now') shape
    iso_z_row = "2026-01-15T08:00:00.000000Z"  # 08:00 — chronologically BEFORE

    # Chronologically the row is before the cutoff...
    assert backend.parse_iso_timestamp(iso_z_row) < backend.parse_iso_timestamp(
        sqlite_cutoff
    )
    # ...but lexicographically it sorts AFTER. `WHERE ts > ?` admits it. WRONG.
    assert iso_z_row > sqlite_cutoff

    # The control: both sides in the canonical shape compare correctly.
    canonical_cutoff = "2026-01-15T12:00:00.000000Z"
    assert (iso_z_row > canonical_cutoff) is False


def test_G4_offset_string_vs_Z_string_lexicographic_compare_is_WRONG(backend):
    """G4 — HAZARD PIN: why `next_run_at` must never be compared in SQL.

    Two strings denoting the same-ish window in different formats (`+03:00` vs
    `Z`) do not sort chronologically. `next_run_at` is deliberately mixed-format
    (Invariant #16's honest caveat) and is safe ONLY because it is parse-compared
    in Python. This pins what breaks the moment someone puts it in a
    `WHERE next_run_at > ?`.
    """
    offset_row = "2026-01-15T13:30:00+03:00"  # == 10:30 UTC
    z_cutoff = "2026-01-15T12:00:00.000000Z"  # == 12:00 UTC

    # Chronologically the row is BEFORE the cutoff.
    assert backend.parse_iso_timestamp(offset_row) < backend.parse_iso_timestamp(
        z_cutoff
    )
    # Lexicographically it sorts AFTER it. A SQL window filter would be wrong.
    assert offset_row > z_cutoff
