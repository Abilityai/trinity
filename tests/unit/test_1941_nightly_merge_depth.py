"""#1941 — the nightly's merge check must be able to find a common ancestor.

`backend-unit-nightly.yml` merges each open PR into `dev` and runs the suite on
the result. It shallow-fetched BOTH sides (`fetch-depth: 1` on the checkout,
`--depth=1` on `pull/N/head`), so the two single-commit histories shared no
ancestor and git exited `fatal: refusing to merge unrelated histories`. The
step's `if !` recorded that as `merge_conflict=true`.

The failure mode is what makes this worth a guard: **the detector's output was
independent of its input.** Every open PR was flagged — 9/9 at the time of
writing, 8 of them `MERGEABLE` per GitHub — so a genuinely conflicting PR read
exactly like the eight false ones, the suite never ran for anybody, and the
regression signal this job exists to produce silently defaulted to "clean".

A comment at the call site does not prevent a regression; this does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "backend-unit-nightly.yml"
)


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _merge_job():
    """The job that merges a PR head into dev (identified by the merge step, so
    a rename of the job doesn't silently skip this whole file)."""
    for name, job in _doc()["jobs"].items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "git merge" in str(step.get("run", "")):
                return name, job
    pytest.fail("no job in backend-unit-nightly.yml runs `git merge` any more")


def _commands(run: str) -> str:
    """Shell body with comment lines stripped.

    Deliberate: the fix's own explanatory comment says "no --depth", and a naive
    substring search over the raw block matches it and passes while the real
    command is shallow again. Assert against what the shell executes.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.strip().startswith("#")
    )


def test_base_checkout_is_full_depth():
    """`fetch-depth: 0`. Any positive depth reintroduces the bug."""
    _, job = _merge_job()
    checkouts = [
        s for s in job["steps"]
        if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the merge job no longer checks out a base to merge into"
    for step in checkouts:
        depth = (step.get("with") or {}).get("fetch-depth")
        assert depth == 0, (
            f"checkout uses fetch-depth={depth!r}; the merge needs full history "
            "to find the ancestor it shares with the PR head (#1941)"
        )


def test_pr_head_fetch_is_not_shallow():
    """Deepening only the base still leaves the head with no reachable ancestor."""
    _, job = _merge_job()
    for step in job["steps"]:
        if not isinstance(step, dict):
            continue
        cmds = _commands(str(step.get("run", "")))
        for line in cmds.splitlines():
            if "git fetch" in line and "pull/" in line:
                assert "--depth" not in line and "--shallow" not in line, (
                    f"the PR-head fetch is shallow again: {line.strip()!r} (#1941)"
                )


def test_conflict_verdict_requires_actual_unmerged_paths():
    """A non-zero `git merge` is not proof of a content conflict — that
    conflation is the bug. The verdict must be gated on unmerged paths so an
    infrastructure failure can never be reported as a PR conflict again."""
    _, job = _merge_job()
    merge_step = next(
        s for s in job["steps"]
        if isinstance(s, dict) and "git merge" in str(s.get("run", ""))
    )
    body = _commands(str(merge_step["run"]))
    assert "merge_conflict=true" in body
    conflict_branch = body.split("merge_conflict=true")[0]
    assert "--diff-filter=U" in conflict_branch, (
        "`merge_conflict=true` must be guarded by a check for unmerged paths "
        "(`git diff --name-only --diff-filter=U`), otherwise any git failure "
        "is reported to every PR author as a conflict (#1941)"
    )


def test_never_papers_over_it_with_allow_unrelated_histories():
    """The tempting one-line 'fix'. It makes the merge succeed by grafting two
    unrelated trees together, so the suite would then run against a worktree
    that is not the real merge result — a green light that means nothing."""
    body = _commands(WORKFLOW.read_text(encoding="utf-8"))
    assert "--allow-unrelated-histories" not in body


def test_remediation_text_is_scoped_to_the_conflict_branch():
    """`git merge dev` is right for a real conflict and wrong for everything
    else; it must stay inside the merge_conflict branch of the comment step."""
    doc = _doc()
    bodies = [
        str(step.get("with", {}).get("script", ""))
        for job in doc["jobs"].values()
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
    ]
    script = "\n".join(b for b in bodies if "merge_conflict" in b)
    if not script:
        pytest.skip("comment step no longer inlines the script")
    advice = [m.start() for m in re.finditer(r"git merge \\?`?dev", script)]
    for pos in advice:
        preceding = script[:pos]
        # The advice may appear in the regression branch too ("Reproduce
        # locally: git merge dev && tests/run-core.sh") — that one is correct.
        assert "status.merge_conflict" in preceding or "status.regression" in preceding, (
            "`git merge dev` remediation appears outside the conflict/regression "
            "branches, so it would be shown to PRs with neither (#1941)"
        )


# ---------------------------------------------------------------------------
# #2029 — an absent verdict must not render as "clean"
# ---------------------------------------------------------------------------


def _status_step():
    """The step that writes `status-pr*.json`, found by what it writes."""
    for _name, job in _doc()["jobs"].items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "status-pr" in str(step.get("run", "")):
                return step
    pytest.fail("no step writes status-pr*.json any more")


def _comment_job():
    for name, job in _doc()["jobs"].items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "github-script" in str(step.get("uses", "")):
                return name, job
    pytest.fail("no job posts the sticky comment any more")


def _branch_body(script: str, opener: str) -> str:
    """The lines of a shell `if` branch, ending at a line that is exactly `fi`.

    Bounded on a TOKEN, not on the substring "fi" (#2462). The original form,
    `guard.split("fi")[0]`, truncated at the first occurrence anywhere — and
    "fi" occurs inside ordinary English: it fired the moment a warning message
    in that branch used the word "unverified", cutting the slice before the
    `exit 0` the assertion looks for and reporting a guard that was in fact
    present. Same family as this file's own comment-stripping: a check that
    matches inside words tests the prose, not the code.
    """
    lines = script.splitlines()
    start = next(i for i, l in enumerate(lines) if opener in l)
    out = []
    for line in lines[start + 1:]:
        if line.strip() == "fi":
            break
        out.append(line)
    return "\n".join(out)


def test_an_unknown_merge_verdict_writes_no_status_file():
    """The #2029 defect: this step is `if: always()`, so it also runs when the
    job died before the merge produced an answer. The unset output stringifies
    to '', `'' == "true"` is false, and the JSON became
    `{merge_conflict: false, regression: false}` — byte-identical to a clean
    run, which the comment job renders as a green tick.

    Writing nothing is the honest answer: the comment job enumerates the files
    that exist, so a missing one leaves that PR's sticky untouched.
    """
    body = _commands(_status_step()["run"])
    assert 'if [ -z "$merge_conflict" ]' in body, (
        "the status step no longer refuses to write on an unknown merge "
        "verdict — a failed job posts a false clean again (#2029)"
    )
    guard = _branch_body(body, 'if [ -z "$merge_conflict" ]')
    assert "exit 0" in guard, (
        "the unknown-verdict branch does not exit before writing the file"
    )


def test_regression_defaults_to_false_only_when_the_merge_conflicted():
    """`regression` legitimately has no value when the merge conflicted — the
    diff never ran because there was nothing to test. Any OTHER empty value
    means the diff step was skipped by a job failure, which is the same
    unknown-verdict case and must not become a clean verdict."""
    body = _commands(_status_step()["run"])
    idx = body.index('if [ -z "$regression" ]')
    branch = body[idx:]
    assert '"$merge_conflict" = "true"' in branch.split("fi")[0] or \
           '"$merge_conflict" = "true"' in branch[:400], (
        "regression falls back to false without checking that the merge "
        "actually conflicted (#2029)"
    )


def test_the_status_step_still_runs_on_failure():
    """`if: always()` is what lets the step observe the failed case at all.
    Dropping it would 'fix' #2029 by never writing a status for a failed job —
    and also never writing one for a genuine conflict."""
    assert _status_step().get("if") == "always()"


def test_the_comment_job_is_not_gated_on_the_whole_matrix():
    """Pins a deliberate decision, not an omission.

    #2029 offers a coarser belt — gate the comment step on
    `needs.test.result == 'success'`. It is not applied, and should not be:
    `test` is a MATRIX job, so its result is 'failure' when any single leg
    fails, and one PR's infrastructure hiccup would suppress the report for
    every other PR in the sweep. That trades a false green for a silence that
    is equally wrong and hits PRs that were fine.

    The per-PR guard is precise and complete on its own — a leg with no verdict
    writes no file, and the all-legs-failed case falls out of the same
    mechanism.
    """
    _name, job = _comment_job()
    for step in job.get("steps") or []:
        if "github-script" in str(step.get("uses", "")):
            assert "needs.test.result" not in str(step.get("if", "")), (
                "the sticky-comment step is gated on the whole matrix — one "
                "failed leg now silences every other PR's report (#2029)"
            )
