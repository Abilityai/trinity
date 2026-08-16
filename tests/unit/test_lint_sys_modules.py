"""Tests for tests/lint_sys_modules.py (Issue #762).

Covers the lint's positive (must-flag) and negative (must-not-flag) cases,
plus the baseline-diff ratchet semantics. Synthetic source fixtures are
written to a tempdir and passed through the lint's AST machinery directly —
no subprocess, no real file walks.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from lint_sys_modules import (
    BASELINE_FILE,
    _check_file,
    _has_stubbed_module_names_helper,
    collect_findings,
    diff_against_baseline,
    findings_per_file,
    load_baseline,
    write_baseline,
)
import ast


# Override the backend-requiring autouse fixtures from package conftest;
# these are pure-unit tests of the lint script itself.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


def _check_source(src: str, tmp_path: Path, name: str = "test_sample.py") -> list:
    """Helper: write `src` to tmp_path/name and run the lint checker on it."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(src))
    return _check_file(p)


# ---------------------------------------------------------------------------
# Positive (must-flag) cases
# ---------------------------------------------------------------------------


def test_bare_assign_is_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        sys.modules["foo"] = object()
        """,
        tmp_path,
    )
    assert len(findings) == 1
    assert "bare assignment to sys.modules" in findings[0].message


def test_bare_del_is_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        if "foo" in sys.modules:
            del sys.modules["foo"]
        """,
        tmp_path,
    )
    assert len(findings) == 1
    assert "bare `del sys.modules" in findings[0].message


def test_setdefault_is_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        sys.modules.setdefault("foo", object())
        """,
        tmp_path,
    )
    assert len(findings) == 1
    assert "setdefault" in findings[0].message


def test_pop_is_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        sys.modules.pop("foo", None)
        """,
        tmp_path,
    )
    assert len(findings) == 1
    assert "pop" in findings[0].message


def test_update_is_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        sys.modules.update({"foo": object()})
        """,
        tmp_path,
    )
    assert len(findings) == 1
    assert "update" in findings[0].message


def test_assign_inside_function_is_flagged(tmp_path):
    """Pollution from inside a test function/fixture also leaks across the
    session and must be flagged — the lint walks the whole AST, not just the
    module level."""
    findings = _check_source(
        """
        import sys

        def test_bad():
            sys.modules["foo"] = object()
        """,
        tmp_path,
    )
    assert len(findings) == 1


def test_multiple_violations_are_each_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        sys.modules["a"] = 1
        sys.modules["b"] = 2
        del sys.modules["c"]
        sys.modules.pop("d")
        """,
        tmp_path,
    )
    assert len(findings) == 4


# ---------------------------------------------------------------------------
# Negative (must-not-flag) cases
# ---------------------------------------------------------------------------


def test_monkeypatch_setitem_is_not_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        def test_good(monkeypatch):
            monkeypatch.setitem(sys.modules, "foo", object())
        """,
        tmp_path,
    )
    assert findings == []


def test_monkeypatch_delitem_is_not_flagged(tmp_path):
    findings = _check_source(
        """
        import sys
        def test_good(monkeypatch):
            monkeypatch.delitem(sys.modules, "foo", raising=False)
        """,
        tmp_path,
    )
    assert findings == []


def test_read_access_is_not_flagged(tmp_path):
    """Reading sys.modules (for guards or assertions) is fine."""
    findings = _check_source(
        """
        import sys
        def test_good():
            if "foo" in sys.modules:
                pass
            x = sys.modules.get("bar")
            assert "baz" in sys.modules
        """,
        tmp_path,
    )
    assert findings == []


def test_unrelated_subscript_is_not_flagged(tmp_path):
    findings = _check_source(
        """
        import os
        os.environ["FOO"] = "bar"
        del os.environ["FOO"]
        """,
        tmp_path,
    )
    assert findings == []


def test_conftest_file_is_allowlisted():
    """conftest.py files under tests/ are allowed — they are the canonical
    place for snapshot/restore primitives."""
    from lint_sys_modules import _is_allowlisted_file

    real_conftest = Path(__file__).resolve().parent / "conftest.py"
    assert _is_allowlisted_file(real_conftest) is True
    not_conftest = Path(__file__).resolve()
    assert _is_allowlisted_file(not_conftest) is False


def test_stubbed_module_names_helper_exempts_whole_file(tmp_path):
    """Files with both `_STUBBED_MODULE_NAMES` and `_restore_sys_modules`
    are quarantined snapshot/restore patterns — full-file exemption.
    Precedent: tests/unit/test_telegram_webhook_backfill.py."""
    findings = _check_source(
        """
        import sys
        import pytest

        _STUBBED_MODULE_NAMES = ["foo", "bar"]

        @pytest.fixture(autouse=True)
        def _restore_sys_modules():
            saved = {k: sys.modules.get(k) for k in _STUBBED_MODULE_NAMES}
            try:
                yield
            finally:
                for k, v in saved.items():
                    if v is None:
                        sys.modules.pop(k, None)
                    else:
                        sys.modules[k] = v

        # Still flagged ABSENT the helper, but exempt here:
        sys.modules["foo"] = object()
        """,
        tmp_path,
    )
    assert findings == []


def test_only_one_helper_marker_is_not_exempt(tmp_path):
    """Having only `_STUBBED_MODULE_NAMES` (no `_restore_sys_modules` fixture)
    is NOT enough — the lint requires both. Prevents bypass via a misleading
    constant name without an actual restore."""
    findings = _check_source(
        """
        import sys
        _STUBBED_MODULE_NAMES = ["foo"]
        sys.modules["foo"] = object()
        """,
        tmp_path,
    )
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Helper detection unit tests
# ---------------------------------------------------------------------------


def test_helper_detector_returns_true_for_both_markers():
    src = textwrap.dedent(
        """
        _STUBBED_MODULE_NAMES = []
        def _restore_sys_modules(): pass
        """
    )
    assert _has_stubbed_module_names_helper(ast.parse(src)) is True


def test_helper_detector_returns_false_for_only_constant():
    src = textwrap.dedent(
        """
        _STUBBED_MODULE_NAMES = []
        """
    )
    assert _has_stubbed_module_names_helper(ast.parse(src)) is False


def test_helper_detector_returns_false_for_only_function():
    src = textwrap.dedent(
        """
        def _restore_sys_modules(): pass
        """
    )
    assert _has_stubbed_module_names_helper(ast.parse(src)) is False


# ---------------------------------------------------------------------------
# Baseline / ratchet semantics
# ---------------------------------------------------------------------------


def test_baseline_blocks_new_violation_in_clean_file(tmp_path):
    """Even if the baseline lists 0 for a (synthetic) file, a new violation
    introduced there is flagged."""
    from collections import Counter

    current = Counter({"tests/test_new.py": 1})
    baseline = Counter()  # no prior violations
    new, retired = diff_against_baseline(current, baseline)
    assert len(new) == 1
    assert "+1" in new[0]
    assert retired == []


def test_baseline_allows_existing_count(tmp_path):
    from collections import Counter

    current = Counter({"tests/test_known.py": 3})
    baseline = Counter({"tests/test_known.py": 3})
    new, retired = diff_against_baseline(current, baseline)
    assert new == []
    assert retired == []


def test_baseline_reports_retired(tmp_path):
    from collections import Counter

    current = Counter({"tests/test_known.py": 1})
    baseline = Counter({"tests/test_known.py": 3})
    new, retired = diff_against_baseline(current, baseline)
    assert new == []
    assert len(retired) == 1
    assert "-2" in retired[0]


def test_baseline_blocks_growth_in_known_file(tmp_path):
    from collections import Counter

    current = Counter({"tests/test_known.py": 5})
    baseline = Counter({"tests/test_known.py": 3})
    new, retired = diff_against_baseline(current, baseline)
    assert len(new) == 1
    assert "+2" in new[0]
    assert retired == []


def test_baseline_roundtrip(tmp_path):
    """write_baseline + load_baseline should preserve the per-file counts."""
    from collections import Counter

    baseline_path = tmp_path / "baseline.txt"
    original = Counter({"tests/test_a.py": 2, "tests/test_b.py": 7})
    write_baseline(original, baseline_path)
    loaded = load_baseline(baseline_path)
    assert loaded == original


def test_baseline_skips_comments_and_blanks(tmp_path):
    baseline_path = tmp_path / "baseline.txt"
    baseline_path.write_text(
        textwrap.dedent(
            """
            # this is a comment
            3 tests/test_a.py

            # another comment
            5 tests/test_b.py
            """
        ).strip()
        + "\n"
    )
    loaded = load_baseline(baseline_path)
    assert loaded == {"tests/test_a.py": 3, "tests/test_b.py": 5}


# ---------------------------------------------------------------------------
# Repo-state invariant: the committed baseline matches what the lint sees
# ---------------------------------------------------------------------------


def test_committed_baseline_matches_current_repo_state():
    """If this test fails, someone changed test code without updating the
    baseline. Run `python tests/lint_sys_modules.py --regenerate-baseline`
    after intentional changes, or fix the new violation."""
    current = findings_per_file(collect_findings())
    baseline = load_baseline()
    diff_new, diff_retired = diff_against_baseline(current, baseline)
    assert diff_new == [], (
        "New or grown sys.modules violations found:\n"
        + "\n".join(diff_new)
        + "\nFix the offending lines or regenerate the baseline."
    )
    # We don't fail on retired-only — that means cleanup happened and
    # the baseline can be safely regenerated.


def test_lint_baseline_file_exists():
    assert BASELINE_FILE.exists(), (
        "tests/lint_sys_modules_baseline.txt must be committed. "
        "Generate with `python tests/lint_sys_modules.py --regenerate-baseline`."
    )
