"""The full-suite harness cannot silently rot back (#2080).

Every defect this issue fixed was invisible: collection aborted before any test
ran, a tier skipped itself, a `sys.modules` shadow swallowed a new backend
module. None of them failed anything — they removed coverage while the summary
line stayed the same shape.

So the harness gets its own guards. These are cheap static assertions over the
runner and its helpers; each one corresponds to a specific way the suite went
quiet, and each fails loudly rather than reducing what runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TESTS = _REPO / "tests"
_RUN_FULL = _TESTS / "run-full.sh"

pytestmark = pytest.mark.unit


def _run_full() -> str:
    return _RUN_FULL.read_text()


# ---------------------------------------------------------------------------
# The shadowing that swallowed src/backend/utils/*
# ---------------------------------------------------------------------------

def test_tests_utils_package_is_gone():
    """`tests/utils` shadowed the backend's `utils` package.

    `pythonpath` puts `tests` first, so `utils` resolved to the test helpers and
    every backend module under `src/backend/utils/` was invisible. Adding
    `safe_yaml.py` there was enough to break ~1,000 tests with
    `ModuleNotFoundError: No module named 'utils.safe_yaml'` — a failure in the
    harness that reads exactly like a product regression.

    The fix is structural (renamed to `tests/testkit`), so the guard is too:
    re-creating the package must fail here rather than in a thousand unrelated
    tests six months from now.
    """
    assert not (_TESTS / "utils").exists(), (
        "tests/utils is back — it shadows src/backend/utils on the pythonpath, "
        "and every module added to the backend package becomes unimportable. "
        "Test helpers belong in tests/testkit."
    )
    assert (_TESTS / "testkit" / "__init__.py").exists(), "tests/testkit is missing"


def test_no_test_imports_the_helpers_under_the_old_name():
    """A stale `from utils.api_client import ...` would resolve to the backend
    package and fail confusingly — or, worse, silently pick up a same-named
    backend module later."""
    offenders = []
    for path in _TESTS.rglob("*.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        # This file states the forbidden pattern in order to search for it —
        # the same read-the-prose trap that has bitten guards in this repo
        # before, so it excludes itself explicitly rather than by luck.
        if path.resolve() == Path(__file__).resolve():
            continue
        for num, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if re.search(r"\b(from|import)\s+utils\.(api_client|assertions|cleanup)\b", line):
                offenders.append(f"{path.relative_to(_REPO)}:{num}")
    assert not offenders, f"test helpers imported under the old `utils` name: {offenders}"


# ---------------------------------------------------------------------------
# The directory that poisoned collection of its siblings
# ---------------------------------------------------------------------------

def test_no_test_directory_has_a_non_identifier_name():
    """`tests/git-sync/` carried an `__init__.py` under a name that is not a
    valid Python identifier, so its `conftest.py` landed in `sys.modules` under
    the bare key `conftest` and collided with the root one. The visible symptom
    was `tests/integration/conftest.py` binding its `from conftest import ...`
    to the wrong module — five files failing to collect, and `set -e` aborting
    the whole run before the unit tier."""
    bad = []
    for path in _TESTS.rglob("__init__.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        pkg = path.parent.name
        if not pkg.isidentifier():
            bad.append(str(path.parent.relative_to(_REPO)))
    assert not bad, (
        f"package directories whose names are not importable: {bad} — their "
        "conftest collides with the root conftest under the bare `conftest` key"
    )


# ---------------------------------------------------------------------------
# The runner's own contract
# ---------------------------------------------------------------------------

def test_every_test_directory_is_covered_by_a_tier():
    """A new top-level test directory must not be silently uncovered.

    The old runner ran `pytest --ignore=unit --ignore=process_engine`, so any
    directory added later was included by accident or excluded by accident,
    with nothing saying which.
    """
    text = _run_full()
    skip = {"__pycache__", "reports", "harness", "manual", "deploy", "fixtures",
            "node_modules", "testkit", ".venv", ".pytest_cache"}
    missing = []
    for child in sorted(_TESTS.iterdir()):
        if not child.is_dir() or child.name in skip or child.name.startswith("."):
            continue
        if not list(child.rglob("test_*.py")):
            continue
        if f"{child.name}/" not in text:
            missing.append(child.name)
    assert not missing, (
        f"test directories no tier in run-full.sh names: {missing} — add a tier "
        "(or an explicit --ignore with a reason)"
    )


def test_every_tier_reports_skips_so_the_audit_can_see_them():
    """`-rs` is what makes a skip visible. Without it the audit reads a dot-line
    and certifies a run in which whole tiers quietly did nothing."""
    text = _run_full()
    assert "-rs" in text, "run-full.sh no longer passes -rs; the skip audit goes blind"
    assert "audit_skips.py" in text, "the skip-audit gate is not invoked"


def test_the_runner_bounds_every_tier_with_a_thread_timeout():
    """A blocking read must cost one test, not a run. `signal` is specifically
    excluded: re-entering the interpreter from a handler is what turned the
    web-terminal hang into an INTERNALERROR instead of a timeout."""
    text = _run_full()
    assert "--timeout-method=thread" in text
    assert "--timeout=" in text


def test_a_tier_that_collects_nothing_is_a_failure():
    """pytest exits 5 for "no tests ran". Treating that as success is how a
    mis-typed path or a renamed directory becomes a green tier."""
    text = _run_full()
    assert re.search(r"\b5\)\s*record .*FAIL", text), (
        "run-full.sh no longer fails on pytest's exit code 5 (no tests collected)"
    )


def test_pytest_config_pins_the_thread_timeout_method():
    """The runner passes it per tier, but someone running pytest by hand must
    get the same protection — the hang was found by a hand-run, not the runner.
    """
    cfg = (_REPO / "pyproject.toml").read_text()
    assert 'timeout_method = "thread"' in cfg
    assert re.search(r"^timeout = \d+", cfg, re.M)


# ---------------------------------------------------------------------------
# The audit's own honesty
# ---------------------------------------------------------------------------

def test_every_allowlisted_skip_reason_carries_a_justification():
    """The allowlist is the one place a skip is permitted to hide, so an entry
    without a stated reason is the loophole re-opening."""
    import sys

    sys.path.insert(0, str(_TESTS))
    from harness.audit_skips import ALLOWED_SKIP_REASONS

    assert ALLOWED_SKIP_REASONS, "the allowlist is empty — every skip would fail"
    for token, why in ALLOWED_SKIP_REASONS:
        assert token and token == token.lower(), f"{token!r} must be lowercase (matched case-insensitively)"
        assert why and len(why) > 15, f"{token!r} has no real justification: {why!r}"


def test_the_audit_refuses_to_certify_an_empty_log_dir(tmp_path):
    """"Nothing ran" must not read as "nothing was wrong" — that is the whole
    class of bug this issue is about, one level up."""
    import sys

    sys.path.insert(0, str(_TESTS))
    from harness.audit_skips import main

    assert main(["audit_skips.py", str(tmp_path)]) == 1


def test_the_audit_fails_an_unallowlisted_skip(tmp_path):
    log = tmp_path / "tier.log"
    log.write_text(
        "SKIPPED [1] tests/unit/test_x.py:12: TEST_AGENT_NAME environment variable not set\n"
    )
    import sys

    sys.path.insert(0, str(_TESTS))
    from harness.audit_skips import main

    assert main(["audit_skips.py", str(tmp_path)]) == 1


def test_the_audit_passes_an_allowlisted_skip(tmp_path):
    log = tmp_path / "tier.log"
    log.write_text(
        "SKIPPED [1] tests/unit/test_y.py:9: no Slack workspace configured\n"
    )
    import sys

    sys.path.insert(0, str(_TESTS))
    from harness.audit_skips import main

    assert main(["audit_skips.py", str(tmp_path)]) == 0
