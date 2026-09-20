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

import os
import re
import subprocess
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


# ---------------------------------------------------------------------------
# The venv the runner bootstraps must not be a tracked path (#2082 follow-up)
# ---------------------------------------------------------------------------

def test_no_tracked_symlink_escapes_the_repo():
    """A tracked symlink pointing outside the worktree is broken for everyone else.

    #2082 committed `tests/.venv` as a symlink to
    `/home/<dev>/Desktop/abilityai/trinity/.venv`. Two failures, neither loud:

    1. It published a developer's local path and username to a public repo.
    2. It broke the very runner that PR exists to fix. `run-full.sh` guards
       creation with `[ ! -d .venv ]`, which is *false* for a dangling symlink,
       so it fell through to `python3 -m venv .venv` against an absolute path
       that does not exist on any other machine and exited 2.

    `.gitignore` did not stop it because `.venv/` and `.venv*/` carry trailing
    slashes and therefore match directories only — a symlink is not a directory.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-s"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    offenders = []
    for line in tracked:
        meta, _, path = line.partition("\t")
        if not meta.startswith("120000"):  # symlink blobs only
            continue
        target = (_REPO / path).parent / os.readlink(_REPO / path)
        try:
            target.resolve().relative_to(_REPO.resolve())
        except ValueError:
            offenders.append(f"{path} -> {os.readlink(_REPO / path)}")

    assert not offenders, (
        "tracked symlink(s) resolve outside the repository — these are dangling "
        "on every other machine and may leak a local path:\n  "
        + "\n  ".join(offenders)
    )


def test_runner_venv_is_not_tracked():
    """`tests/.venv` is created by the runner; it must never be a tracked path."""
    tracked = subprocess.run(
        ["git", "ls-files", "tests/.venv"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert tracked == "", (
        "tests/.venv is tracked; run-full.sh creates it per-machine and a tracked "
        "copy (dir, file or symlink) makes the bootstrap machine-specific"
    )


# ---------------------------------------------------------------------------
# The guard that truncated the run for three weeks (#2888)
# ---------------------------------------------------------------------------
# `for _d in tests/*/` was evaluated after the runner had `cd`'d into `tests/`,
# so it looked for `tests/tests/*/`, matched nothing, stayed a literal `*`,
# failed classification and `exit 1`'d before the api / standalone / postgres
# tiers. The guard now lives in `harness/check_test_dirs.py`, takes the
# directory explicitly, and is exercised here — the inline version was the one
# part of the harness with no test at all.


def _shell_array(text: str, name: str) -> list[str]:
    m = re.search(rf"^{name}=\(([^)]*)\)", text, re.M)
    assert m, f"{name}=( ... ) not found in run-full.sh"
    return m.group(1).split()


def _check_test_dirs():
    import sys

    sys.path.insert(0, str(_TESTS))
    from harness import check_test_dirs

    return check_test_dirs


def test_the_directory_guard_does_not_glob_relative_to_the_cwd():
    text = _run_full()
    # Matched as CODE (a `for ... in` over the glob), not as a substring: the
    # runner's own comment names the bad glob to explain it, which is the
    # read-the-prose trap this file already documents for itself.
    assert not re.search(r"^\s*for\s+\S+\s+in\s+tests/\*/", text, re.M), (
        "run-full.sh globs `tests/*/` again — after its own `cd` that is "
        "`tests/tests/*/`, matches nothing, and aborts the run on the literal `*`"
    )
    assert re.search(r'check_test_dirs\.py "\$TESTS_DIR"', text), (
        "the directory guard must be handed TESTS_DIR explicitly, never left to "
        "infer it from the cwd"
    )


def test_the_directory_guard_passes_on_the_real_tree():
    """Run the guard the way the runner does, over the real tests/ tree with the
    real owner lists. If this fails, so does every full-suite run."""
    text = _run_full()
    known = _shell_array(text, "TIER_DIRS") + _shell_array(text, "NON_TIER_DIRS")
    assert _check_test_dirs().main(["check_test_dirs.py", str(_TESTS), *known]) == 0


def _tree(tmp_path: Path, *dirs: str, with_test: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "tests"
    root.mkdir()
    for d in dirs:
        (root / d).mkdir(parents=True)
    for d in with_test:
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "test_x.py").write_text("def test_x():\n    pass\n")
    return root


def test_the_directory_guard_passes_a_fully_wired_tree(tmp_path):
    root = _tree(tmp_path, "harness", with_test=("unit", "integration"))
    assert _check_test_dirs().main(
        ["check_test_dirs.py", str(root), "unit", "integration", "harness"]
    ) == 0


def test_the_directory_guard_names_an_unwired_directory_that_holds_tests(tmp_path, capsys):
    """AC #2: the guard still fires for a genuinely unwired directory — the
    "add one temporarily" proof, made permanent."""
    root = _tree(tmp_path, with_test=("unit", "orphaned_tier"))
    rc = _check_test_dirs().main(["check_test_dirs.py", str(root), "unit"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "orphaned_tier/" in out
    assert "TIER_DIRS" in out


def test_the_directory_guard_ignores_directories_that_hold_no_tests(tmp_path):
    """`__pycache__`, `reports/`, the venv and a stale local checkout with only
    `node_modules` are on every developer machine and collected by nothing;
    naming them would fail every run — the same truncation, sign flipped."""
    root = _tree(
        tmp_path,
        "__pycache__", "reports", ".venv", ".pytest_cache",
        "stale_local_checkout/node_modules", "stale_local_checkout/__pycache__",
        with_test=("unit",),
    )
    (root / "stale_local_checkout" / "node_modules" / "test_lib.py").write_text("")
    assert _check_test_dirs().main(["check_test_dirs.py", str(root), "unit"]) == 0


def test_the_directory_guard_fails_a_stale_owner_entry(tmp_path, capsys):
    """A renamed tier directory keeps its `--ignore=` line (pytest ignores a
    missing path silently) while the new name is swept into `api`."""
    root = _tree(tmp_path, with_test=("unit",))
    rc = _check_test_dirs().main(["check_test_dirs.py", str(root), "unit", "renamed_away"])
    assert rc == 1
    assert "renamed_away/" in capsys.readouterr().out


def test_the_directory_guard_refuses_an_empty_owner_list(tmp_path):
    root = _tree(tmp_path, with_test=("unit",))
    assert _check_test_dirs().main(["check_test_dirs.py", str(root)]) == 2


def test_every_tier_the_runner_can_run_is_declared_for_the_ledger():
    """The end-of-run ledger fails a declared tier with no result row. It can
    only do that for tiers it knows about, so every `run_tier NAME` line — and
    the two tiers that record themselves by hand — must be in DECLARED_TIERS."""
    text = _run_full()
    declared = _shell_array(text, "DECLARED_TIERS")
    invoked = set(re.findall(r"^\s*run_tier\s+([a-z-]+)\s", text, re.M))
    invoked |= set(re.findall(r"^\s*record\s+(standalone|postgres)\s", text, re.M))
    assert set(declared) == invoked, (
        f"DECLARED_TIERS={sorted(declared)} but the runner invokes {sorted(invoked)}"
    )
    assert len(declared) == len(set(declared))


def test_a_declared_tier_with_no_result_row_fails_the_run():
    """AC #4: a tier the control flow never reached is named and fails the run,
    and an exit before the summary announces itself as an abort."""
    text = _run_full()
    assert re.search(r'record "\$_t" FAIL "tier NEVER RAN', text), (
        "run-full.sh no longer fails a declared tier that has no result row"
    )
    assert "trap on_exit EXIT" in text
    assert "ABORTED before the summary" in text
    # The abort message must be computed from what was recorded, not from a
    # flag one exit path sets — every exit path has to trip it.
    assert 'if [ "$SUMMARY_PRINTED" = 0 ]' in text
    assert text.count("SUMMARY_PRINTED=1") == 1
    assert text.index("SUMMARY_PRINTED=1") > text.index("tier NEVER RAN")


def test_a_deselected_tier_is_listed_as_skipped_not_passed():
    """`--tier unit` is a legitimate partial run; the summary must still say
    which tiers it did not cover, and the verdict must not read as full."""
    text = _run_full()
    assert 'record_deselected "$_t" "deselected by --tier"' in text
    assert 'record_deselected postgres "deselected by --no-pg"' in text
    assert "not a full-suite result" in text
