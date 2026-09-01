"""#2462 — the nightly cross-PR regression net must be able to finish.

`backend-unit-nightly.yml` ran SIX full unit suites (three seeds x base and
head) inside a job with `timeout-minutes: 25`. One suite was ~15 minutes, so a
leg needed ~90 minutes of a 25-minute budget, and every leg for a mergeable PR
was cancelled at the cap — twelve consecutive nights of `cancelled` across the
whole queryable history, with no result for any mergeable PR in any of them.
The only legs that "succeeded" finished in 46 seconds: the merge-conflict
short-circuits, i.e. exactly the ones that ran nothing.

It failed silently by construction. The workflow is scheduled, so a cancel
notifies nobody and blocks no PR; and the diff step is gated on the suite step
completing, so a capped leg produces no artifact, no diff and no comment.
Silence and success are the same shape from outside.

These tests pin the four properties that make it finishable and honest, so a
future edit cannot quietly restore any one of them:

  1. a leg is ONE seed, not six suites;
  2. the suites are parallelised and individually timeout-bounded;
  3. the seed set has exactly one definition, shared by the matrix builder and
     the completeness rule that consumes it;
  4. a sweep that produces nothing FAILS rather than passing quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "backend-unit-nightly.yml"
_VERDICT = _REPO / "scripts" / "ci" / "nightly-verdict.js"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def workflow() -> str:
    assert _WORKFLOW.is_file(), f"{_WORKFLOW} is missing"
    return _WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, name: str) -> str:
    """One job's body, bounded by indentation.

    Same shape as the helper in test_2019_pytest_timeout_guard.py, and for the
    same mutation-found reason: slicing to the next `  <name>:` runs off the
    end of the last job and swallows whatever follows, so an assertion can pass
    against a different job entirely.
    """
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {name}:"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def _uncommented(block: str) -> str:
    """Runnable lines only.

    Load-bearing, exactly as in the #2019 guard: this workflow documents its
    own history at length, and the prose necessarily contains the strings the
    assertions look for — `timeout-minutes: 25`, `for seed in`, the seed
    literals. A raw substring check would pass on the comment describing the
    bug after the fix had been reverted.
    """
    return "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("#")
    )


class TestALegIsOneSeed:

    def test_the_matrix_carries_a_seed_dimension(self, workflow):
        assert "seed: ." in _uncommented(_job(workflow, "discover")), (
            "the matrix no longer fans out per seed — a leg is back to running "
            "every seed, which is the ~90-minutes-in-a-25-minute-budget shape "
            "that killed this workflow for twelve nights (#2462)"
        )

    def test_the_suite_step_no_longer_loops_over_seeds(self, workflow):
        run = _uncommented(_job(workflow, "test"))
        assert "for seed in" not in run, (
            "the suite step loops over seeds again — six suites in one leg "
            "(#2462)"
        )

    def test_the_leg_still_runs_both_sides(self, workflow):
        """Guards the guard: a leg that dropped one side would satisfy every
        timing assertion here and produce a diff against nothing."""
        run = _uncommented(_job(workflow, "test"))
        assert "junit-base-pr" in run and "junit-head-pr" in run
        assert "git switch --detach origin/dev" in run
        assert "git switch --detach \"$MERGE_SHA\"" in run


class TestTheSuiteCanFinish:

    def test_the_suites_are_parallelised(self, workflow):
        run = _uncommented(_job(workflow, "test"))
        assert re.search(r"-n\s+(auto|\d+)", run), (
            "the nightly runs the suite serially — with two suites per leg "
            "that is ~30 minutes of work before any runner variance (#2461/#2462)"
        )

    def test_xdist_is_installed(self, workflow):
        assert "pytest-xdist" in _uncommented(_job(workflow, "test")), (
            "-n is passed but xdist is not installed: pytest exits on an "
            "unrecognised argument and every leg fails"
        )

    def test_a_stalled_test_names_itself(self, workflow):
        """#2019's flag, which this workflow never got. Without it one stalled
        test consumes the leg and dies anonymously — the same failure, on the
        workflow where nobody is watching."""
        run = _uncommented(_job(workflow, "test"))
        assert "--timeout=" in run and "--timeout-method=signal" in run

    def test_the_budget_is_not_back_to_the_broken_one(self, workflow):
        job = _uncommented(_job(workflow, "test"))
        caps = re.findall(r"^\s*timeout-minutes:\s*(\d+)", job, re.MULTILINE)
        assert caps, "the test job lost its timeout-minutes"
        cap = int(caps[0])
        # ~22 min of measured work per leg (two ~9-min parallel suites plus
        # full-history checkout, merge and install). Below ~40 there is no room
        # for the runner variance actually measured on this repo (1.94x).
        assert 40 <= cap <= 90, f"test job cap is {cap}m — see the derivation in the workflow"


class TestOneDefinitionOfTheSeedSet:

    def test_the_seed_list_is_declared_once(self, workflow):
        assert "NIGHTLY_SEEDS:" in workflow
        # Every seed literal outside that declaration is a second definition
        # waiting to disagree with it: a fourth seed added to one place makes
        # every PR permanently unverified, a removed one silently lowers the bar.
        body = _uncommented(workflow)
        declarations = re.findall(r"NIGHTLY_SEEDS:\s*'([^']*)'", body)
        assert len(declarations) == 1, f"expected one seed declaration, found {declarations}"
        stray = [
            line for line in body.splitlines()
            if re.search(r"\b(12345|67890|99999)\b", line)
            and "NIGHTLY_SEEDS" not in line
        ]
        assert stray == [], f"seed literals outside the single declaration: {stray}"

    def test_both_consumers_read_it(self, workflow):
        assert "$NIGHTLY_SEEDS" in _uncommented(_job(workflow, "discover")), (
            "the matrix builder no longer reads the shared seed set"
        )
        assert "NIGHTLY_SEEDS" in _uncommented(_job(workflow, "comment")), (
            "the completeness rule no longer reads the shared seed set"
        )


class TestItCannotDieQuietly:

    def test_a_sweep_that_reports_nothing_fails_the_run(self, workflow):
        comment = _uncommented(_job(workflow, "comment"))
        assert "core.setFailed" in comment, (
            "a sweep that produced no status artifacts while PRs were open "
            "exits 0 again — that is the exact signature of the twelve dead "
            "nights, and it leaves no red mark anywhere (#2462)"
        )

    def test_the_verdict_logic_is_a_module_a_test_can_run(self, workflow):
        assert _VERDICT.is_file(), f"{_VERDICT} is missing"
        comment = _uncommented(_job(workflow, "comment"))
        assert "nightly-verdict.js" in comment, (
            "the verdict aggregation moved back inline, where no test can "
            "execute it — and it is the one path that can publish a green tick "
            "for a suite that never finished"
        )

    def test_the_module_is_commonjs(self):
        """`actions/github-script` loads the body under `require()`, so an ESM
        export here is unloadable by the only caller that matters — and it
        fails at RUN time, in the job that posts the comments."""
        src = _VERDICT.read_text(encoding="utf-8")
        assert "module.exports" in src
        assert not re.search(r"^export\s+(function|const)", src, re.MULTILINE)

    def test_each_leg_says_what_it_did(self, workflow):
        """AC 3 — a capped leg and a leg that legitimately had nothing to run
        were indistinguishable from outside."""
        assert "GITHUB_STEP_SUMMARY" in _uncommented(_job(workflow, "test"))


class TestTheDemonstrationHasABlastRadius:
    """A change to this workflow can only be verified by running it, and
    running it sweeps every open PR and posts bot comments on other people's
    work. The scoped dispatch exists so proving a fix costs one PR."""

    def test_a_manual_dispatch_can_be_scoped_to_one_pr(self, workflow):
        assert "pr_number:" in _uncommented(_job(workflow, "discover")) or \
            re.search(r"inputs:\s*\n\s*pr_number:", workflow), (
            "workflow_dispatch lost its single-PR scope — verifying a change "
            "again means sweeping every open PR (#2462)"
        )

    def test_the_scheduled_sweep_is_unscoped(self, workflow):
        """The filter must be opt-in. An always-on filter with an empty input
        would silently reduce the nightly to nothing — the same silence this
        issue is about, arriving by a different door."""
        discover = _uncommented(_job(workflow, "discover"))
        assert 'if [ -n "$only" ]' in discover, (
            "the PR filter is not guarded on a non-empty input"
        )

    def test_the_module_comes_from_this_workflows_own_commit(self, workflow):
        """`ref: dev` is wrong here in a way that only shows up when it matters.

        A dispatch from a branch runs THAT branch's workflow against `dev`'s
        module, so the two can disagree — and on the run that proved this fix,
        `dev` did not have the module at all and the comment job died with
        MODULE_NOT_FOUND. `github.sha` is the same trust level (this workflow
        has only `schedule` and `workflow_dispatch` triggers, both refs a repo
        writer chose) and cannot skew.
        """
        comment = _uncommented(_job(workflow, "comment"))
        assert "ref: ${{ github.sha }}" in comment, (
            "the trusted checkout no longer pins to this workflow's own commit "
            "— workflow and verdict module can then disagree (#2462)"
        )
