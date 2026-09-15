"""#2815 — the `status-needs-fix` label clears on the author's push, and the
workflow that does it stays in the only shape that is safe on `pull_request`.

A `/review`, `/validate-pr` or merge-train finding posted as a COMMENT never
moves `reviewDecision`, so a PR carrying one reads ✅ Ready in every queue view
and the next train re-validates it from scratch (about an hour of agent time
per PR, spent twice). The finding is now also a label; the skills sort it out
of Ready and the train holds it. This file pins the OTHER half — that the label
goes away by itself when the author pushes — and the three properties that make
a label-writing workflow safe to run on `pull_request` with a write token.

Static guards in the shape of `test_2767_issue_status_fork_prs.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "needs-fix-clear-on-push.yml"
)
LABEL = "status-needs-fix"


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc):
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    return doc.get("on") if "on" in doc else doc[True]


def _job():
    jobs = _doc()["jobs"]
    assert len(jobs) == 1, "one job — this workflow does exactly one thing"
    return next(iter(jobs.values()))


def _steps():
    return [s for s in _job()["steps"] if isinstance(s, dict)]


def _script() -> str:
    return "\n".join(str(s.get("with", {}).get("script", "")) for s in _steps())


def _code() -> str:
    """The script minus `//` comment lines — assert on what EXECUTES, not on
    prose (the `test_2767` / `test_1896` precedent: a comment saying "there is
    no checkout" satisfies a naive checkout check)."""
    return "\n".join(
        line for line in _script().splitlines() if not line.strip().startswith("//")
    )


def _yaml_code() -> str:
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


# --- what it does -----------------------------------------------------------

def test_the_author_push_is_the_trigger():
    """AC#3: a push by the author clears the state with no human step.
    `synchronize` is the event a push to the PR branch emits."""
    trig = _triggers(_doc())
    assert "pull_request" in trig
    assert trig["pull_request"]["types"] == ["synchronize"], (
        "only a push should clear the label — `opened`/`reopened` carry no fix"
    )


def test_it_removes_exactly_the_needs_fix_label():
    src = _code()
    assert "issues.removeLabel" in src
    assert f"name: '{LABEL}'" in src
    assert "addLabels" not in src, "this workflow only ever CLEARS the label"


def test_it_only_runs_when_the_label_is_present():
    """A job-level gate, so the ~every-push case costs no runner at all."""
    cond = _job().get("if", "")
    assert "labels.*.name" in cond and LABEL in cond, (
        "gate the JOB on the label — an ungated job spends a runner on every push"
    )


def test_a_fork_prs_read_only_token_is_a_warning_not_a_red_run():
    """`pull_request` on a fork carries a read-only token (#2767). That is not
    the author's fault and must not redden their PR; it is logged for the
    reviewer to clear by hand."""
    src = _code()
    assert "e.status === 403" in src
    assert "core.warning" in src
    # ...and anything ELSE is still a failure, so a real refusal is not hidden.
    assert "core.setFailed" in src


def test_an_already_absent_label_is_not_a_failure():
    """Raced with a manual removal — same end state."""
    src = _code()
    assert "e.status === 404" in src


# --- why pull_request, and why it is safe with a write token ---------------

def test_the_trigger_is_pull_request_not_target():
    """Two reasons, both load-bearing (#2814, #2767):
    `pull_request_target` is registered from the default branch and would be
    inert on `dev` until the next release; and on a same-repo PR the plain
    `pull_request` token can already write labels."""
    trig = _triggers(_doc())
    assert "pull_request_target" not in trig
    assert list(trig.keys()) == ["pull_request"]


def test_the_workflow_never_checks_out_pr_code():
    uses = [str(s.get("uses", "")) for s in _steps()]
    assert not any(u.startswith("actions/checkout") for u in uses)
    assert "actions/checkout" not in _yaml_code()


def test_the_workflow_never_executes_anything():
    runs = [s.get("run") for s in _steps() if s.get("run")]
    assert runs == [], f"label workflow gained a run step: {runs}"


def test_untrusted_pr_text_is_never_read():
    """The script reads the PR NUMBER and head SHA and nothing the author typed.
    A label-only workflow has no business with `title`/`body`."""
    src = _code()
    assert "pr.title" not in src and "pr.body" not in src


def test_permissions_are_the_one_write_it_needs():
    perms = _doc()["permissions"]
    assert perms == {"pull-requests": "write"}, (
        "labels on a PR are written through the pull-requests scope; nothing else"
    )
