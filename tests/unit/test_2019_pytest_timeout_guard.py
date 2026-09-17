"""#2019 — a hanging unit test must name itself, not kill the shard.

`backend-unit-test.yml` runs the unit suite under a 25-minute job timeout. It
installs `pytest-timeout` (`tests/requirements-test.txt`) and never used it, so
a single stalled test consumed the whole budget, the job was cancelled by
GitHub, the `Upload JUnit XML` step was skipped, and the failure surfaced as
`##[error]The operation was canceled` against whichever PR happened to be
running.

Four occurrences in one day across **all three seeds and both sides** (#2010
head/99999, #1952 base/67890, #1976 base/67890, #2018 head/12345) — including
the base side, which is plain `dev` and therefore nothing to do with the PR
being reddened. Re-running the identical seed passed, so it is timing- rather
than order-dependent: something that usually returns fast and occasionally
blocks. The #1952 log shows a 3m37s gap with zero output between two progress
lines, which is a stall, not a slow runner.

The cost of the missing flag was not the lost minutes; it was that every
occurrence had to be diagnosed by hand from a log and produced no evidence
about which test was responsible. I misdiagnosed it once for exactly that
reason.

These tests pin the flag, the method, and the artifact upload, so the guard
cannot be silently dropped in a future edit of the workflow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "backend-unit-test.yml"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def workflow() -> str:
    assert _WORKFLOW.is_file(), f"{_WORKFLOW} is missing"
    return _WORKFLOW.read_text(encoding="utf-8")


def _step(workflow: str, title: str) -> str:
    """Exactly ONE step's body, bounded by indentation.

    An earlier version of this helper sliced from the step title to the next
    `- name:` — which runs off the end of the last step in a job and swallows
    the following job. That is not hypothetical: it made the `if: always()`
    assertion below match the `diff` job's own `if: always() && ...` and pass
    with the guard deleted. Caught by mutation-testing this file.

    Bounding on indentation instead: a step's body is every line indented
    deeper than its `- name:` marker.
    """
    lines = workflow.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith(f"- name: {title}"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def _command(workflow: str, title: str) -> str:
    """A step's runnable lines, with YAML comments stripped.

    Load-bearing, not tidiness. The step this file guards *documents itself* —
    its comment block names `--timeout`, `--timeout-method=thread` and
    `|| true` while explaining why they are there. A substring assertion over
    the raw step therefore passes on the prose after the flag has been deleted
    from the command, which is precisely the failure this file exists to
    prevent. Caught by mutation-testing: removing `|| true` from the command
    left the test green because the comment still said it.

    The same trap is recorded in `docs/memory/learnings.md` for the #1871
    `containers_run` guard and the ent#314 loader sweep. Third occurrence.
    """
    out = []
    for line in _step(workflow, title).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def _pytest_step(workflow: str) -> str:
    """The `Run unit suite` step's COMMAND, comments removed.

    Scoped rather than searched whole-file: the nightly-style steps and the
    self-test invocation also contain `pytest`, and a whole-file grep would
    pass on any of them while the suite step itself lost the flag.
    """
    return _command(workflow, "Run unit suite")


class TestTheTimeoutIsWired:

    def test_the_unit_suite_passes_a_per_test_timeout(self, workflow):
        step = _pytest_step(workflow)
        assert "--timeout=" in step, (
            "the unit suite runs with no per-test timeout — one hanging test "
            "again consumes the 25-minute job budget and dies without naming "
            "itself (#2019)"
        )

    def test_the_timeout_does_not_kill_the_run(self, workflow):
        """`signal`, NOT `thread` — a correction to what the issue proposed.

        `thread` dumps stacks and then kills the process, so the run aborts,
        the remaining tests never execute, and the JUnit is incomplete — the
        diff job fails anyway, just with a named culprit in the log. `signal`
        raises inside the offending test and the suite CONTINUES, so the
        complete JUnit lets the diff job report the hang as an ordinary new
        failure by name, which is the outcome worth having.

        The logs support that shape: #1952 kept making progress after its
        3m37s gap, so these are stalls that release rather than one permanent
        hang.
        """
        step = _pytest_step(workflow)
        assert "--timeout-method=thread" not in step, (
            "`thread` aborts the whole run on the first stall, losing the "
            "complete JUnit the diff job needs (#2019)"
        )

    def test_the_timeout_clears_the_slowest_real_test(self, workflow):
        """The cap has to sit above every legitimate test AND above runner
        variance, or it becomes a new source of red.

        Measured from a green run's JUnit, not guessed: the slowest legitimate
        unit tests are `test_start_agent_skip_inject`'s two retry cases at
        ~60.6s, then 39.9s and 26.6s in `test_1083_result_callback`. The 60s
        this issue originally proposed would have failed the first two on every
        PR. Runner variance is the whole premise of #2019 — one shard ran 2.5x
        slower than its siblings — so a 60.6s test can legitimately reach
        ~150s.

        Upper bound: the cap must still be small enough that a stall fails
        rather than eating the 25-minute job budget.
        """
        step = _pytest_step(workflow)
        match = re.search(r"--timeout=(\d+)", step)
        assert match, "no numeric --timeout value found"
        seconds = int(match.group(1))
        assert seconds >= 180, (
            f"--timeout={seconds}s is under the ~150s a legitimate 60.6s test "
            "can reach on a slow runner — this would red every PR"
        )
        assert seconds <= 600, (
            f"--timeout={seconds}s is too close to the 25-minute job budget to "
            "prevent the failure it is for"
        )

    def test_the_plugin_is_actually_installed(self):
        """The flag is inert without the plugin, and pytest ignores unknown
        `--timeout` only if some other plugin claims it — so pin the dep."""
        reqs = (_REPO / "tests" / "requirements-test.txt").read_text()
        assert "pytest-timeout" in reqs


class TestTheArtifactSurvives:

    def test_junit_upload_runs_even_when_pytest_ends_abnormally(self, workflow):
        """Without `if: always()` the upload is skipped on cancellation, so the
        diff job loses that side's XML entirely and fails a second time with a
        different message. The timeout should make this unreachable; belt and
        braces, because the diff job is deliberately fail-closed on a missing
        artifact and that is the right behaviour to keep."""
        step = _command(workflow, "Upload JUnit XML")
        assert "if: always()" in step, (
            "the JUnit upload is skipped when the pytest step is cancelled, so "
            "the regression-diff job sees a missing artifact instead of the "
            "real result (#2019)"
        )


class TestTheSuiteStepIsStillWhatWeThinkItIs:

    def test_the_step_still_runs_the_unit_directory_under_three_seeds(self, workflow):
        """Guards the guard: if the step is restructured, the assertions above
        could pass against something that no longer runs the unit suite."""
        step = _pytest_step(workflow)
        assert "unit/" in step and "--randomly-seed=" in step

    def test_the_step_still_tolerates_the_660_baseline(self, workflow):
        """`|| true` is load-bearing — the documented #660 failures make pytest
        exit non-zero, and the diff job is what fails the workflow. A timeout
        that turned the step fatal would red every PR."""
        step = _pytest_step(workflow)
        assert "|| true" in step


def test_the_assertions_read_the_command_not_the_comment(workflow):
    """The step documents its own flags in prose, so every assertion above is
    one careless helper away from testing the comment block instead of the
    command. Pin the stripping directly."""
    raw = _step(workflow, "Run unit suite")
    cmd = _command(workflow, "Run unit suite")

    assert "#" in raw, "the step no longer carries the comment this guards against"
    assert not any(l.strip().startswith("#") for l in cmd.splitlines())
    assert "python -m pytest" in cmd, "comment stripping ate the command"


# ---------------------------------------------------------------------------
# #2461 — the same step, the other half of the budget problem
# ---------------------------------------------------------------------------
#
# These live here rather than in a `test_2461_*.py` of their own because they
# assert about the SAME step, and `_step` / `_command` above already carry two
# mutation-found corrections (indentation bounding, comment stripping). A
# second file would either import from this one or grow a second copy of both
# helpers — and a copy is how one of them silently stops matching the other.
#
# The defect: a healthy shard finishes in ~15 minutes, a starved one does not
# finish in 45 and dies as `The operation was canceled`. Measured over the 18
# runs before the fix, 78 shards: success min 13m / median 15m / p90 17m /
# max 30m, with 5-6 shards killed at exactly the cap — ~8% of shards, so ~40%
# of runs carried at least one spurious red, landing on whichever PR was open
# INCLUDING the `base` side, which is plain `dev`.

class TestTheSuiteRunsInParallel:

    def test_the_unit_suite_is_parallelised(self, workflow):
        step = _pytest_step(workflow)
        assert re.search(r"-n\s+(auto|\d+)", step), (
            "the unit suite runs serially again — a starved runner returns to "
            "reaching the 45-minute cap, and the red lands on whichever PR is "
            "open (#2461)"
        )

    def test_distribution_keeps_a_file_on_one_worker(self, workflow):
        """`loadfile`, not the default `load`.

        The suite manipulates sys.modules and pins env at module import, so
        within-file ordering is load-bearing in places. Across files the
        isolation improves — workers are separate processes and the unit
        conftest already pins TRINITY_DB_PATH per PID.

        A future move to `load` is a legitimate next lever for balance, but it
        needs those within-file assumptions audited first, so it must not
        happen as a silent edit.
        """
        step = _pytest_step(workflow)
        assert "--dist loadfile" in step, (
            "distribution mode changed away from loadfile — see #2461 before "
            "loosening this"
        )

    def test_xdist_is_installed_for_the_run(self, workflow):
        """A `-n` flag with no plugin is an unrecognised-argument error, i.e.
        the whole shard fails rather than running serially. Pin both halves so
        neither can be dropped alone."""
        install = _command(workflow, "Install test dependencies")
        assert "pytest-xdist" in install, (
            "the run passes -n but xdist is not installed — pytest exits on an "
            "unrecognised argument and every shard fails (#2461)"
        )

    def test_the_cap_is_not_raised_instead(self, workflow):
        """Parallelising is the alternative to buying minutes, not a companion
        to it. 45 was already the second raise (25 -> 45 on 2026-08-25); a
        third would mean the suite is growing faster than anyone is measuring
        it, and the next lever is the balance of the distribution, not the
        budget."""
        job = workflow.split("  test:", 1)[1]
        caps = re.findall(r"^\s*timeout-minutes:\s*(\d+)", job, re.MULTILINE)
        assert caps, "the unit-suite job lost its timeout-minutes entirely"
        assert int(caps[0]) <= 45, (
            f"unit-suite job cap raised to {caps[0]}m — #2461 exists because "
            "raising it is the move that stopped working"
        )
