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
