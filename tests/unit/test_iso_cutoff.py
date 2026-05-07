"""
Tests for `iso_cutoff()` in utils/helpers.py (issue #476).

`iso_cutoff(hours)` returns a past-cutoff ISO timestamp in the same format as
`utc_now_iso()` so SQL filters on ISO-Z TEXT columns can compare
lexicographically without falling off the `T`/space separator mismatch.

Pinning:
- Format parity with utc_now_iso (length, suffix, parse round-trip)
- Monotonic lexicographic ordering (iso_cutoff(N) > iso_cutoff(N+1))
- Accuracy vs wall clock for small N
"""

from __future__ import annotations

import sys
from pathlib import Path

# Mirror the sys.path bootstrap used by other unit tests in this tree.
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.helpers"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from utils.helpers import iso_cutoff, parse_iso_timestamp, utc_now, utc_now_iso  # noqa: E402


class TestFormatParity:
    """iso_cutoff must byte-for-byte match the utc_now_iso format."""

    def test_length_matches_utc_now_iso(self):
        now_iso = utc_now_iso()
        cutoff_iso = iso_cutoff(1)
        assert len(now_iso) == len(cutoff_iso)
        # Canonical length: YYYY-MM-DDTHH:MM:SS.ffffffZ = 27 chars
        assert len(cutoff_iso) == 27

    def test_ends_with_Z_suffix(self):
        assert iso_cutoff(0).endswith("Z")
        assert iso_cutoff(2).endswith("Z")
        assert iso_cutoff(24).endswith("Z")

    def test_uses_T_separator_not_space(self):
        cutoff = iso_cutoff(1)
        # Position 10 is the date/time separator in ISO 8601
        assert cutoff[10] == "T"
        assert " " not in cutoff

    def test_round_trip_via_parse_iso_timestamp(self):
        cutoff = iso_cutoff(3)
        dt = parse_iso_timestamp(cutoff)
        # Parsed datetime must be ~3h before now (±1s for test runtime)
        delta = (utc_now() - dt).total_seconds()
        assert 3 * 3600 - 1 < delta < 3 * 3600 + 1


class TestLexicographicOrdering:
    """The fix relies on lexicographic ordering of ISO-Z strings to match
    chronological ordering. Pin that here."""

    def test_iso_cutoff_zero_matches_now_within_tolerance(self):
        before = utc_now_iso()
        cutoff = iso_cutoff(0)
        after = utc_now_iso()
        # cutoff should fall in [before, after] lexicographically
        assert before <= cutoff <= after

    def test_larger_hours_means_earlier_timestamp(self):
        # 3h ago < 1h ago < 0h ago (lexicographic == chronological for this format)
        assert iso_cutoff(3) < iso_cutoff(1)
        assert iso_cutoff(1) < iso_cutoff(0)

    def test_very_old_cutoff_still_valid_format(self):
        # 25h ago crosses UTC midnight from any execution time
        cutoff = iso_cutoff(25)
        assert len(cutoff) == 27
        assert cutoff.endswith("Z")
        assert cutoff[10] == "T"
        # And is comparable lexicographically with a fresh now
        assert cutoff < utc_now_iso()


class TestMinutesKwarg:
    """`minutes=` keyword extends iso_cutoff to sub-hour windows.

    Added when wiring CANARY-001 E-02's 30-minute terminal-row window
    onto iso_cutoff (snapshot._collect_terminal_executions).
    """

    def test_minutes_only_returns_sub_hour_cutoff(self):
        cutoff = iso_cutoff(minutes=30)
        # Same canonical format
        assert len(cutoff) == 27
        assert cutoff.endswith("Z")
        assert cutoff[10] == "T"
        # ~30 minutes before now (±1s for test runtime)
        dt = parse_iso_timestamp(cutoff)
        delta = (utc_now() - dt).total_seconds()
        assert 30 * 60 - 1 < delta < 30 * 60 + 1

    def test_minutes_is_keyword_only(self):
        # Positional second arg must NOT bind to minutes — it should
        # raise TypeError. This pins the signature so future refactors
        # don't accidentally make `iso_cutoff(2, 30)` mean "2h30m".
        import pytest
        with pytest.raises(TypeError):
            iso_cutoff(2, 30)  # type: ignore[misc]

    def test_hours_and_minutes_combine(self):
        cutoff = iso_cutoff(1, minutes=15)
        dt = parse_iso_timestamp(cutoff)
        delta = (utc_now() - dt).total_seconds()
        # 1h15m == 4500s
        assert 4500 - 1 < delta < 4500 + 1

    def test_minutes_zero_equals_hours_only(self):
        # Generated within the same wall-clock millisecond — strings may
        # differ in the last digit, but should be within 1s of each other.
        a = iso_cutoff(2)
        b = iso_cutoff(2, minutes=0)
        da = parse_iso_timestamp(a)
        db = parse_iso_timestamp(b)
        assert abs((da - db).total_seconds()) < 1

    def test_larger_minutes_means_earlier_timestamp(self):
        # Lexicographic == chronological still holds for sub-hour windows
        assert iso_cutoff(minutes=30) < iso_cutoff(minutes=1)
        assert iso_cutoff(minutes=1) < iso_cutoff(minutes=0)

    def test_no_args_equals_now_within_tolerance(self):
        # iso_cutoff() with no args is equivalent to iso_cutoff(0)
        before = utc_now_iso()
        cutoff = iso_cutoff()
        after = utc_now_iso()
        assert before <= cutoff <= after
