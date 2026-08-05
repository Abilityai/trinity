"""#1896 — the live-instance suite has a CI home, and it stays honest.

~93 of the `test_*.py` files in `tests/` root need a RUNNING Trinity (the
fixtures create and delete real agents against `TRINITY_API_URL`), and no CI
job ran them, so they gated nothing. `integration-nightly.yml` is that home.

These are static guards on the workflow, in the shape of
`test_1941_nightly_merge_depth.py` — the properties below are ones whose
absence is invisible until the night it matters:

* the suite membership is DIRECTORY-based, so #1895's relocations shrink it
  automatically and nobody has to remember to edit a file list;
* the job that runs untrusted PR code holds no write token;
* an infrastructure failure cannot be reported to an author as "your PR
  introduced regressions" (#1941's class, which this workflow could reproduce
  through the diff script's fail-closed-on-missing-XML behaviour);
* the merge is not shallow (#1941 again — the same trap, a second workflow).
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "integration-nightly.yml"
)


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _test_job():
    return _doc()["jobs"]["test"]


def _runs(job) -> str:
    return "\n".join(
        str(s.get("run", "")) for s in job["steps"] if isinstance(s, dict)
    )


def _commands(text: str) -> str:
    """Shell body minus comment lines — assert on what executes, not on prose."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def test_the_workflow_exists_and_parses():
    assert WORKFLOW.exists(), "the live suite lost its CI home again (#1896)"
    assert _doc()["jobs"].keys() >= {"discover", "test", "comment"}


def test_membership_is_directory_based_not_a_file_list():
    """The AC that keeps this from rotting: `--ignore=unit`, exactly as
    `tests/run-core.sh` defines the same suite. A hand-listed set would drift
    the moment #1895 moves a file, and the drift is silent."""
    cmds = _commands(_runs(_test_job()))
    assert "--ignore=unit" in cmds
    assert "--ignore=process_engine" in cmds
    assert "pytest" in cmds
    # No per-file enumeration: a `test_*.py` literal in the invocation would
    # mean the membership is hand-maintained again.
    import re

    assert not re.search(r"test_\w+\.py", cmds), (
        "the suite is being enumerated file by file; keep it directory-based"
    )


def test_the_suite_runs_on_both_sides_for_attribution():
    """AC 3: base and head, so a pre-existing failure does not read as new."""
    cmds = _commands(_runs(_test_job()))
    assert "run_side base" in cmds and "run_side head" in cmds
    assert "diff-pytest-failures.py" in cmds


def test_missing_junit_fails_the_job_instead_of_blaming_the_pr():
    """`diff-pytest-failures.py` is fail-closed on a missing input XML, so a
    stack that never booted would otherwise surface to the author as "this PR
    introduced regressions" — an infrastructure failure wearing a PR's name,
    which is exactly what #1941 was filed about."""
    cmds = _commands(_runs(_test_job()))
    assert "-s \"$xml\"" in cmds or "! -s \"$xml\"" in cmds, (
        "the JUnit-existence check is gone; a failed boot would be reported as "
        "a regression (#1896 / #1941)"
    )
    assert "Not reporting a regression" in _runs(_test_job())


def test_collection_errors_do_not_zero_out_the_run():
    """Measured, not defensive: the root suite currently has 3 modules that fail
    to IMPORT. Without `--continue-on-collection-errors` pytest aborts the whole
    session ("Interrupted: N errors during collection") and writes NO JUnit XML,
    so this job would fail every night for every PR and report nothing. With it,
    the errors land on both sides and cancel in the diff, while a NEW collection
    error a PR introduces still surfaces."""
    cmds = _commands(_runs(_test_job()))
    assert "--continue-on-collection-errors" in cmds


def test_the_cli_package_is_installed_for_test_cli_modules():
    """`test_cli_*.py` imports `trinity_cli`, which is deliberately kept out of
    requirements-test.txt (an `-e ./src/cli` line there breaks GitHub's
    dependency-graph updater — tests/setup-env.sh explains it). Without the
    on-demand install those modules fail to import and their tests never run,
    which is the same silent-no-coverage this issue exists to end."""
    cmds = _commands(_runs(_test_job()))
    assert "src/cli" in cmds


def test_each_side_gets_a_pristine_stack():
    """AC 4: the suite MUTATES its instance, so the head side must not inherit
    agents, volumes or a dirty DB from the base side."""
    cmds = _commands(_runs(_test_job()))
    assert "docker compose down -v" in cmds, (
        "without `-v` the second side inherits the first side's volumes"
    )


def test_the_merge_is_not_shallow():
    """#1941, in a second workflow: two shallow fetches share no ancestor, and
    every PR reads as conflicting."""
    job = _test_job()
    checkouts = [
        s for s in job["steps"]
        if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the merge job no longer checks out a base"
    for step in checkouts:
        assert (step.get("with") or {}).get("fetch-depth") == 0

    for line in _commands(_runs(job)).splitlines():
        if "git fetch" in line and "pull/" in line:
            assert "--depth" not in line, f"shallow PR-head fetch: {line.strip()}"


def test_conflict_verdict_requires_unmerged_paths():
    """Same rule as #1941: a non-zero `git merge` is not proof of a conflict."""
    cmds = _commands(_runs(_test_job()))
    assert "merge_conflict=true" in cmds
    before = cmds.split("merge_conflict=true")[0]
    assert "--diff-filter=U" in before


def test_the_job_running_pr_code_has_no_write_token():
    """The inherited security architecture: untrusted PR code must never share
    a job with a write-capable token. `comment` holds the write scope and never
    checks out PR code."""
    test_perms = _test_job().get("permissions") or {}
    assert test_perms == {"contents": "read"}, (
        f"the PR-code job gained scope beyond contents:read: {test_perms}"
    )

    comment = _doc()["jobs"]["comment"]
    steps = str(comment["steps"])
    assert "actions/checkout" not in steps, (
        "the write-scoped comment job must not check out PR code"
    )


def test_it_is_not_wired_into_the_pull_request_merge_path():
    """AC 2: nightly, not per-PR. A mutating ~93-file suite in the merge path
    is a cost and a flake source; `workflow_dispatch` keeps it runnable on
    demand for a single PR."""
    triggers = _doc()[True] if True in _doc() else _doc()["on"]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers


def test_runtime_is_bounded_and_the_escalation_is_written_down():
    """AC 5: a stated budget, and a documented answer for exceeding it — so the
    next person raises the ceiling only after sharding, not instead of it."""
    job = _test_job()
    assert isinstance(job.get("timeout-minutes"), int)
    assert job["timeout-minutes"] <= 60, "an unbounded mutating nightly is the bug"
    header = WORKFLOW.read_text(encoding="utf-8")
    assert "RUNTIME BUDGET" in header
    assert "shard" in header.lower()


def test_local_workflow_is_untouched():
    """AC 6: `tests/run-core.sh` remains the supported local path. This issue
    adds a CI home; it does not migrate the local one."""
    run_core = WORKFLOW.parents[2] / "tests" / "run-core.sh"
    assert run_core.exists()
    body = run_core.read_text(encoding="utf-8")
    assert "--ignore=unit" in body, (
        "run-core.sh no longer defines the same suite the nightly runs — the "
        "two must agree or the local path stops reproducing CI (#1896)"
    )


# ---------------------------------------------------------------------------
# #2029 — an absent verdict must not render as "clean"
# ---------------------------------------------------------------------------


def _status_step():
    """The step that writes `status-pr*.json`, located by what it writes rather
    than by name, so renaming it doesn't silently skip these assertions."""
    for _name, job in _doc()["jobs"].items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "status-pr" in str(step.get("run", "")):
                return step
    pytest.fail("no step writes status-pr*.json any more")


def test_an_unknown_verdict_writes_no_status_file():
    """This workflow's own missing-JUnit guard (`exit 1`) routes straight into
    the #2029 defect: the job dies, `Write status JSON` still runs under
    `if: always()`, the unset merge output stringifies to a clean verdict, and
    the sticky comment says ✅ for a suite that never ran.

    The guard makes the earlier comment's claim — "posts nothing for this PR
    rather than something false" — actually true.
    """
    body = _commands(_status_step()["run"])
    assert 'if [ -z "$merge_conflict" ]' in body, (
        "the status step writes a verdict even when none was produced (#2029)"
    )
    assert "exit 0" in body[body.index('if [ -z "$merge_conflict" ]'):].split("fi")[0]


def test_regression_defaults_to_false_only_on_a_real_conflict():
    body = _commands(_status_step()["run"])
    branch = body[body.index('if [ -z "$regression" ]'):]
    assert '"$merge_conflict" = "true"' in branch[:400], (
        "regression falls back to false without checking the merge actually "
        "conflicted, so a skipped diff reads as clean (#2029)"
    )


def test_the_status_step_still_runs_on_failure():
    """`if: always()` is what lets the step see the failed case at all —
    removing it would suppress genuine conflict verdicts too."""
    assert _status_step().get("if") == "always()"
