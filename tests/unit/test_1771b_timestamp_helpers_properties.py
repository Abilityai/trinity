"""#1771 slice b (target 2) — Hypothesis properties for the timestamp helpers.

Companion to ``test_1771b_timestamp_helpers_edges.py`` (the discrete cases).
Targets ``src/backend/utils/helpers.py`` and its vendored mirror
``src/scheduler/utils.py``; both are loaded by path for the reasons documented
in the edges file.

WHAT THESE PROPERTIES DO AND DO NOT CLAIM
-----------------------------------------
P4 shows the ``to_utc_iso`` FORMATTER is order-preserving — the *function-level*
prerequisite of architecture Invariant #16. It does **not** verify Invariant #16,
which is a *column-level* property ("every writer of a given column emits the
same shape"). Two column-level divergences are known and deliberately untested
here: ``agent_schedules.next_run_at`` (deliberately mixed-format — Invariant
#16's own honest caveat) and ``agent_activities.created_at`` (``DEFAULT
CURRENT_TIMESTAMP`` stores the space-separated shape; reported as finding F8).
Do not upgrade "the formatter is order-preserving" into "Invariant #16 is
verified" — they are different claims.

P1 is a **future-regression guard, not a discovery tool**: the two ``to_utc_iso``
bodies are currently the same three operations written differently, so no input
inside the strategy bounds can distinguish them. Its value is that it fails the
day someone edits one copy and not the other.

CI DETERMINISM (why profiles, not raw randomness)
-------------------------------------------------
The unit CI gate is a base-vs-head *failing-test-ID diff*, so a property that
fails on a rare random draw is a new failing ID in head only — it fails the PR
for a reason unrelated to the change. The default ``ci`` profile is therefore
derandomized with no example database; ``HYPOTHESIS_PROFILE=explore`` restores
randomized search with 25x the examples and ``print_blob`` for reproducing any
counterexample. Every matrix row a property subsumes is ALSO pinned as an
``@example`` so it survives regardless of profile.

Note: ``pytest-randomly``'s three CI seeds randomize test *ordering*, not
Hypothesis input generation — under ``ci`` all three seeds feed these properties
identical inputs.

STRATEGY BOUNDS — each justified by reachability or platform portability, never
by "it was red":

* ``min_value=datetime(1000, 1, 2)`` — ``%Y`` zero-padding below year 1000 is
  platform-libc behaviour, and fixed width is the prerequisite for
  lexicographic==chronological ordering. ``1000-01-01`` is NOT sufficient: with a
  ``+05:00`` offset it converts to ``'0999-12-31T19:00:00.000000Z'``. The bound
  must hold on the CONVERTED value, and ``1000-01-02`` is verified sufficient
  against the extreme ``+23:59``.
* ``max_value=datetime(9998, 12, 31)`` — an aware datetime within one UTC offset
  of ``datetime.max`` raises ``OverflowError`` on ``astimezone``. That is a
  genuine domain edge, not a bug; it is pinned as a discrete test below rather
  than swept.
* ISO-string years 1001..9997 — REQUIRED for P5's error-parity claim to be true
  at all. The two parsers are asymmetric at the domain edge: the backend keeps an
  offset-aware value as-is while the scheduler must ``astimezone``, so
  ``'0001-01-01T00:00:00+01:00'`` parses on the backend and raises
  ``OverflowError`` on the scheduler (matrix row E6). A >1-day margin at each end
  means no offset in +/-23:59 can push the conversion out of range. The asymmetry
  is recorded as a finding, not swept under the bound.
* ``iso_cutoff`` windows 0..~1 century — ``iso_cutoff(10**9)`` raises
  ``OverflowError`` (pinned discretely in the edges file). Real call sites pass
  ``retention_days * 24`` from an operator setting; a century is far past any
  sane window while staying inside the datetime domain. Negative values are an
  explicit discrete case, never swept.
"""

from __future__ import annotations

import importlib.util
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, event, example, given, settings
from hypothesis import strategies as st

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_HELPERS = _ROOT / "src" / "backend" / "utils" / "helpers.py"
_SCHEDULER_UTILS = _ROOT / "src" / "scheduler" / "utils.py"

_Z_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

UTC = timezone.utc


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BACKEND = _load(_BACKEND_HELPERS, "_helpers_backend_1771b_props")
SCHEDULER = _load(_SCHEDULER_UTILS, "_utils_scheduler_1771b_props")

# --------------------------------------------------------------------------
# Hypothesis profiles — see the module docstring.
# --------------------------------------------------------------------------
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    # A CI runner under load must not turn a data-generation stall into a
    # head-only red — the exact failure mode the `ci` profile exists to prevent.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "explore",
    max_examples=5000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "ci"))


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------
_MIN_DT = datetime(1000, 1, 2)
_MAX_DT = datetime(9998, 12, 31)

NAIVE_DT = st.datetimes(min_value=_MIN_DT, max_value=_MAX_DT)

# Real IANA zones deliberately: they contribute DST transitions, quarter-hour
# offsets (+05:45), historical LMT offsets carrying SECONDS, and both `fold`
# values. Fixed-offset-only inputs would reduce P4 to "a zero-padded format is
# order-preserving", which cannot fail.
AWARE_DT = st.one_of(
    st.datetimes(min_value=_MIN_DT, max_value=_MAX_DT, timezones=st.timezones()),
    st.datetimes(
        min_value=_MIN_DT,
        max_value=_MAX_DT,
        timezones=st.timedeltas(
            min_value=timedelta(hours=-23, minutes=-59),
            max_value=timedelta(hours=23, minutes=59),
        ).map(timezone),
    ),
)

ANY_DT = st.one_of(NAIVE_DT, AWARE_DT)

# Same-awareness pairs only: `naive < aware` raises TypeError, so a mixed pair
# has no chronology oracle at all (that case is pinned discretely as row A13).
SAME_AWARENESS_PAIRS = st.one_of(
    st.tuples(NAIVE_DT, NAIVE_DT),
    st.tuples(AWARE_DT, AWARE_DT),
)


def _instant(dt: datetime) -> datetime:
    """The chronology oracle.

    MUST convert through ``astimezone(utc)`` for aware inputs. Bare ``a < b`` is
    NOT chronology: PEP 495 compares two aware datetimes carrying the SAME zone
    object by wall clock and ignores ``fold``, so the two halves of a DST-repeat
    hour compare EQUAL while denoting instants an hour apart. Using ``a < b`` as
    the oracle makes P4 fail against *correct* code — see the edges file,
    ``test_A14_python_comparison_is_not_a_chronology_oracle_for_fold_pairs``.
    """
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt


# Fold pins reused across properties (Europe/London, 2026-10-25 01:30 repeats).
#
# NO try/except FALLBACK, deliberately: `zoneinfo` needs a tz database (system,
# or the `tzdata` package now declared in tests/requirements-test.txt). If it is
# missing, this raises `ZoneInfoNotFoundError` at import and names the zone.
# A graceful fallback to fixed offsets would be worse than an error — the fold
# pins and `st.timezones()` are precisely what keep the ordering property from
# being vacuous, so degrading them silently would turn a real guard into a
# permanent green that proves nothing.
from zoneinfo import ZoneInfo  # noqa: E402

_LONDON = ZoneInfo("Europe/London")
_FOLD_0 = datetime(2026, 10, 25, 1, 30, tzinfo=_LONDON, fold=0)
_FOLD_1 = datetime(2026, 10, 25, 1, 30, tzinfo=_LONDON, fold=1)


# ==========================================================================
# P1 — cross-copy oracle (REGRESSION GUARD, not a discovery tool)
# ==========================================================================


@given(ANY_DT)
@example(
    datetime(2026, 1, 15, 16, 15, tzinfo=timezone(timedelta(hours=5, minutes=45)))
)  # A4
@example(_FOLD_0)  # A6
@example(_FOLD_1)  # A6
@example(datetime(2026, 1, 15, 10, 30))  # naive branch
def test_P1_to_utc_iso_agrees_across_the_vendored_mirror(dt):
    """P1 — the backend and scheduler ``to_utc_iso`` emit identical output.

    HONEST FRAMING: the two bodies are today the same three operations (naive
    check, astimezone, strftime) written with if/else vs an early return, so no
    input inside these bounds can distinguish them. This property is a
    **future-regression guard** — it fails the day one copy is edited and the
    other is not, which is the #1474 desync class. It is NOT evidence that the
    mirror was "verified equivalent for all inputs".
    """
    assert BACKEND.to_utc_iso(dt) == SCHEDULER.to_utc_iso(dt)


@pytest.mark.parametrize(
    "dt",
    [
        pytest.param(
            datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5))),
            id="F3-overflow-top-of-domain",
        ),
        pytest.param(
            datetime(1, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=5))),
            id="F3-overflow-bottom-of-domain",
        ),
    ],
)
def test_F3_both_copies_agree_on_the_overflow_domain_edge(dt):
    """Matrix row F3 — parity includes FAILURE modes.

    Pinned discretely rather than swept because the strategy bounds deliberately
    exclude the overflow region (a swept bound that produced overflows would be
    testing CPython's datetime domain, not Trinity's helpers).
    """
    with pytest.raises(OverflowError):
        BACKEND.to_utc_iso(dt)
    with pytest.raises(OverflowError):
        SCHEDULER.to_utc_iso(dt)


# ==========================================================================
# P2 — invariant preservation: the canonical output shape
# ==========================================================================


@given(ANY_DT)
@example(datetime(2026, 1, 15, 10, 30, 0, 0))  # A7 microsecond == 0
@example(datetime(2026, 1, 15, 10, 30, 0, 999999))  # A8 microsecond == max
@example(datetime(1000, 1, 2))  # lower bound
@example(datetime(9998, 12, 31, 23, 59, 59, 999999))  # upper bound
def test_P2_to_utc_iso_always_emits_the_canonical_27_char_shape(dt):
    """P2 — output always matches ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$``.

    Fixed width is not cosmetic: it is exactly what makes lexicographic
    comparison equal chronological comparison, which is what Invariant #16's SQL
    windows rely on. A variable-width year or a dropped microsecond field would
    silently break every ``WHERE ts > :cutoff``.
    """
    out = BACKEND.to_utc_iso(dt)
    assert _Z_ISO.match(out), f"non-canonical: {out!r}"
    assert len(out) == 27


# ==========================================================================
# P3 — metamorphic: iso_cutoff monotonicity
# ==========================================================================

# >= 1 minute apart: `iso_cutoff` calls `datetime.now()` afresh on every call
# (no Hypothesis setting makes that deterministic), so the window gap must
# dominate the microseconds of elapsed test time between the two calls.
_HOURS = st.integers(min_value=0, max_value=24 * 365 * 100)


@given(_HOURS, st.integers(min_value=1, max_value=24 * 365 * 100))
@example(1, 2)  # C4
@example(0, 1)
@example(24 * 365 * 100 - 1, 1)  # C6 century-scale
def test_P3_a_larger_window_yields_an_earlier_cutoff(h1, extra):
    """P3 — for ``0 <= h1 < h2``, ``iso_cutoff(h2)`` sorts before
    ``iso_cutoff(h1)`` BOTH lexicographically and chronologically.

    The lexicographic half is the one production depends on: the cutoff is bound
    into SQL as a string and compared against a stored TEXT column.
    """
    h2 = h1 + extra
    later = BACKEND.iso_cutoff(hours=h1)
    earlier = BACKEND.iso_cutoff(hours=h2)
    assert earlier < later, f"lexicographic: {earlier!r} !< {later!r}"
    assert BACKEND.parse_iso_timestamp(earlier) < BACKEND.parse_iso_timestamp(later)


# ==========================================================================
# P4 — metamorphic: to_utc_iso is an ORDER-PRESERVING map
# ==========================================================================


@given(SAME_AWARENESS_PAIRS)
@example(
    (datetime(2026, 1, 15, 23, 59, 59, 999999), datetime(2026, 1, 16, 0, 0))
)  # G2 day
@example(
    (datetime(2026, 12, 31, 23, 59, 59, 999999), datetime(2027, 1, 1, 0, 0))
)  # G2 year
@example(
    (datetime(2026, 1, 15, 10, 30, 0, 0), datetime(2026, 1, 15, 10, 30, 0, 1))
)  # G2 usec
@example((_FOLD_0, _FOLD_1))  # A6/A14 — the fold pair the naive oracle gets wrong
def test_P4_to_utc_iso_preserves_chronological_order(pair):
    """P4 — string order equals instant order.

    This is the FUNCTION-LEVEL prerequisite of Invariant #16, not Invariant #16
    itself (see the module docstring). What it can actually catch: a change that
    drops the aware->UTC conversion (formatting local wall clock instead of the
    instant), or any move away from fixed-width zero-padded fields.

    ORACLE: ``_instant()``, i.e. ``astimezone(utc)`` — NOT bare ``a < b``.
    """
    a, b = pair
    try:
        ia, ib = _instant(a), _instant(b)
        sa, sb = BACKEND.to_utc_iso(a), BACKEND.to_utc_iso(b)
    except OverflowError:  # outside the strategy bounds only via an @example
        assume(False)
        return

    if ia != ib:
        event("distinct instants")
    assert (ia < ib) == (
        sa < sb
    ), f"order not preserved: {a!r} vs {b!r} -> instants({ia < ib}) strings({sa < sb})"
    assert (ia == ib) == (sa == sb)


# ==========================================================================
# P5 — cross-copy parser agreement over the stored-shape zoo
# ==========================================================================


@st.composite
def iso_zoo(draw):
    """The shapes Trinity actually stores — a hand-built composite, NOT
    ``st.text()``.

    Random text would test CPython's ``fromisoformat``, which is not Trinity's
    code. The shapes that matter are enumerated: canonical Z, Z without
    fraction, bare naive, the SQLite space-separated shape, date-only, and an
    offset-bearing value (what the backend writes into ``next_run_at``).

    The ``offset`` shape draws a sub-minute-resolution offset, so it renders
    ``+HH:MM:SS[.ffffff]`` — parseable on 3.11+ only, the same assumption the
    edges file's D14 row carries an explicit ``skipif`` for. That is deliberate
    (``pytz`` historical LMT offsets carry seconds and reach ``next_run_at``);
    see the "Python < 3.11" note in the edges file's module docstring.
    """
    dt = draw(
        st.datetimes(min_value=datetime(1001, 1, 1), max_value=datetime(9997, 12, 31))
    )
    offset = draw(
        st.timedeltas(
            min_value=timedelta(hours=-23, minutes=-59),
            max_value=timedelta(hours=23, minutes=59),
        )
    )
    shape = draw(
        st.sampled_from(
            ["z_full", "z_nofrac", "naive", "space", "date", "offset", "utc"]
        )
    )
    if shape == "z_full":
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if shape == "z_nofrac":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if shape == "naive":
        return dt.isoformat()
    if shape == "space":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if shape == "date":
        return dt.strftime("%Y-%m-%d")
    if shape == "utc":
        return dt.isoformat() + "+00:00"
    return dt.replace(tzinfo=timezone(offset)).isoformat()


@given(iso_zoo())
@example("2026-01-15T10:30:00.123456Z")  # E1/D1
@example("2026-01-15 10:30:00")  # D6 — the SQLite datetime('now') shape
@example("2026-01-15T13:30:00+03:00")  # D4/E1
@example("2026-01-15")  # D7
def test_P5_both_parsers_agree_on_the_instant_and_scheduler_stays_naive(raw):
    """P5 — for every accepted shape the two read helpers denote the SAME
    instant, and ``parse_scheduler_ts`` is ALWAYS naive (rows E2/E3).

    Naive-ness is the scheduler's whole reason for having its own parser: its
    row models are naive and its duration math is ``datetime.utcnow() - x``, so
    an aware result would raise on the subtraction (#1474).

    Error parity (row E4) is asserted on the discrete rejected zoo in the edges
    file rather than here: within these bounds nothing raises, and the one place
    parity genuinely BREAKS is the domain edge deliberately excluded by the year
    bound (row E6, reported as a finding).
    """
    got_backend = BACKEND.parse_iso_timestamp(raw)
    got_scheduler = SCHEDULER.parse_scheduler_ts(raw)

    assert (
        got_scheduler.tzinfo is None
    ), f"scheduler parse must be naive: {got_scheduler!r}"
    assert got_backend.astimezone(UTC).replace(tzinfo=None) == got_scheduler


# ==========================================================================
# P6 — round-trip + idempotence
# ==========================================================================


@given(ANY_DT)
@example(datetime(2026, 1, 15, 10, 30, 0, 123456, tzinfo=UTC))  # H1
@example(datetime(2026, 1, 15, 10, 30))  # H1 naive
@example(_FOLD_1)  # H1 across a DST fold
def test_P6_parse_of_to_utc_iso_preserves_the_instant(dt):
    """P6/H1+H3 — ``parse(to_utc_iso(dt))`` preserves the instant on BOTH read
    helpers (aware UTC on the backend, naive UTC on the scheduler)."""
    rendered = BACKEND.to_utc_iso(dt)
    expected = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    assert BACKEND.parse_iso_timestamp(rendered) == expected
    assert SCHEDULER.parse_scheduler_ts(rendered) == expected.replace(tzinfo=None)


@given(iso_zoo())
@example("2026-01-15T10:30:00Z")  # H2 — normalizes to .000000Z, so NOT identity
@example("2026-01-15 10:30:00")
def test_P6_to_utc_iso_of_a_parse_is_idempotent(raw):
    """P6/H2 — ``to_utc_iso(parse(s))`` is IDEMPOTENT but not the identity: it
    normalizes (``'…T10:30:00Z'`` -> ``'…T10:30:00.000000Z'``). Idempotence is
    what makes the #1474 read-boundary normalization safe to apply to a column
    holding a mix of already-normalized and legacy rows."""
    once = BACKEND.to_utc_iso(BACKEND.parse_iso_timestamp(raw))
    twice = BACKEND.to_utc_iso(BACKEND.parse_iso_timestamp(once))
    assert once == twice
    assert _Z_ISO.match(once)


# ==========================================================================
# P8 — the composed production predicate
# ==========================================================================


@given(
    st.integers(min_value=1, max_value=24 * 365 * 10),
    st.timedeltas(min_value=timedelta(days=-3650), max_value=timedelta(days=3650)),
)
@example(24, timedelta(hours=-1))  # inside the window
@example(24, timedelta(hours=1))  # outside the window
@example(168, timedelta(microseconds=1))  # G2 — one microsecond past the cutoff
def test_P8_lexicographic_window_filter_matches_real_recency(hours, delta):
    """P8 — ``to_utc_iso(dt) > iso_cutoff(h)`` iff ``dt`` really is inside the
    window. This is the COMPOSITION every call site performs
    (``WHERE ts > :cutoff``); every other property here tests one function alone.

    Non-vacuity: this is the property that fails if ``iso_cutoff`` and
    ``to_utc_iso`` ever stop emitting the SAME shape — precisely the #476 bug,
    where the cutoff was space-separated and the rows were ISO-Z.

    The cutoff string is captured once and the candidate is derived from the
    PARSED cutoff, so the assertion never races the wall clock.
    """
    cutoff = BACKEND.iso_cutoff(hours=hours)
    cutoff_dt = BACKEND.parse_iso_timestamp(cutoff)
    candidate = cutoff_dt + delta
    assume(
        datetime(1001, 1, 1, tzinfo=UTC)
        < candidate
        < datetime(9997, 12, 31, tzinfo=UTC)
    )

    lexicographic = BACKEND.to_utc_iso(candidate) > cutoff
    chronological = candidate > cutoff_dt
    assert lexicographic == chronological, (
        f"window filter disagrees with chronology: hours={hours} delta={delta} "
        f"cutoff={cutoff!r} candidate={BACKEND.to_utc_iso(candidate)!r}"
    )


# ==========================================================================
# P7 — REAL BUG: the ReminderCreate validator accepts input the parser rejects
# ==========================================================================

try:
    from models import ReminderCreate  # type: ignore

    _HAVE_MODELS = True
except Exception:  # pragma: no cover - backend venv absent
    _HAVE_MODELS = False


def _validator_accepts(raw: str) -> bool:
    try:
        ReminderCreate(message="m", fire_at=raw)
        return True
    except Exception:
        return False


# The divergence class: the validator normalizes with `v.replace("Z", "+00:00")`
# — EVERY 'Z' — while `parse_iso_timestamp` strips only a TRAILING 'Z'. So a
# mid-string 'Z' that `replace` turns into a valid offset is accepted upstream
# and then rejected by the parser.
_Z_INJECTED = st.builds(
    lambda base, i: base[:i] + "Z" + base[i:],
    st.sampled_from(
        [
            "2026-01-15T10:30:00.123456Z",
            "2026-01-15T10:30:00Z",
            "2026-01-15T10:30:00+00:00",
            "2026-01-15T10:30:00",
            "2026-01-15 10:30:00",
            "2026-01-15",
        ]
    ),
    st.integers(min_value=0, max_value=27),
)

_KNOWN_DIVERGENT = [
    "2026-01-15T10Z:30:00",
    "2026-01-15T10:30Z:00",
    "2026-01-15 10Z:30:00",
    "2026-01-15 10:30Z:00",
]


@pytest.mark.skipif(
    not _HAVE_MODELS, reason="backend venv required for models.ReminderCreate"
)
@given(st.one_of(st.sampled_from(_KNOWN_DIVERGENT), _Z_INJECTED))
@example("2026-01-15T10Z:30:00")
@example("2026-01-15T10:30Z:00")
@example("2026-01-15 10Z:30:00")
@example("2026-01-15 10:30Z:00")
# The MECHANICAL non-vacuity floor (#1831) — an ACCEPTED input, so the assertion
# body below is guaranteed to execute at least once no matter what happens to the
# strategy. Keep it: all four examples above are rejected post-fix, and every
# accepted draw `_Z_INJECTED` produces comes from `i >= len(base)` (i.e. a plain
# trailing-'Z' append), so narrowing `st.integers(max_value=27)` to the base
# lengths — a plausible "magic number" cleanup — would silently drop acceptance to
# 0% and leave this property green and worthless. This example is a member of the
# uppercase-'Z'-as-separator class #1831 deliberately started accepting.
@example("2026-01-15Z10:30:00")
def test_P7_validator_acceptance_implies_the_parser_can_parse(raw):
    """P7 — whatever ``ReminderCreate`` accepts, ``parse_iso_timestamp`` must be
    able to parse. The property **now holds by construction** (#1831):
    ``_check_fire_at_iso`` no longer re-implements the normalization, it calls
    ``parse_iso_timestamp`` itself, so "the validator accepted it" and "the
    parser can parse it" are the same statement rather than two hand-maintained
    rules that happened to agree. A pass is therefore close to definitional —
    what it still guards is a FUTURE re-introduction of a local normalization in
    the validator, at inputs no fixed list enumerates.

    Reachability (why this is a bug and not an unreachable-input note):
    ``services/reminder_service.py::_resolve_fire_at`` calls
    ``parse_iso_timestamp(data.fire_at)`` with NO try/except, under a comment
    that asserts the very invariant this test used to falsify ("fire_at already
    validated ISO-8601 by ReminderCreate"). ``fire_at`` is agent-supplied through
    the MCP ``set_reminder`` tool (``z.string().optional()``), so a malformed
    value reaches it from outside the platform and — before #1831 — escaped as a
    500 rather than a 4xx.

    NON-VACUITY — read this before trusting a green: post-fix the four historical
    ``@example`` inputs are all *rejected*, so they pin nothing here; they are
    retained as executable documentation of the divergence class, and are pinned
    positively as must-reject assertions in
    ``tests/unit/test_1296_reminders.py::test_1831_mid_string_z_fire_at_rejected``,
    which is where a regression is now actually caught. The ``event()`` below
    reports the validator-acceptance share (~21%), and a 0% share means this
    property passed vacuously — but be honest about the SHAPE of that 21%:
    measured over the full ``_Z_INJECTED`` (base, i) grid on py3.11/3.13/3.14,
    **every** accepted draw comes from ``i >= len(base)`` — a degenerate
    trailing-'Z' append — and **zero** true mid-string insertions are accepted.
    So the assertion effectively runs on a handful of canonical strings. The fifth
    ``@example`` above is therefore the mechanical floor: it is accepted, so the
    body executes at least once regardless of what the strategy later becomes.

    A rejected draw takes an early ``return`` rather than ``assume()``: under the
    derandomized ``ci`` profile (which does NOT suppress ``filter_too_much``)
    filtering ~79% of draws trips a ``FailedHealthCheck``, and the pass/fail of a
    suppressed variant would be re-rolled by any edit to this function's source
    digest. Returning early makes the health check structurally unreachable while
    asserting exactly the same implication.
    """
    # NOT ``assume()`` — see the last docstring paragraph. Converting this back to
    # ``assume(_validator_accepts(raw))`` reintroduces a DETERMINISTIC
    # ``FailedHealthCheck: filter_too_much`` red (~79% of draws are filtered) and
    # re-arms source-digest seed sensitivity under ``derandomize=True``. (#1831)
    if not _validator_accepts(raw):
        return
    event("validator accepted")
    BACKEND.parse_iso_timestamp(raw)  # must not raise
