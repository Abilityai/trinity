"""#2822 — the status promoter's trigger must be readable from `dev`.

#2769 moved this workflow from `pull_request` to `pull_request_target` so fork
PRs would get a token that can write labels. It then produced ZERO runs: six
PRs merged to `dev` and four issues were relabelled by hand.

Neither trigger could start a run, for opposite reasons:

  * `pull_request` is read from the PR's merge ref, which resolves against
    `dev` — and `dev`'s copy no longer declared it;
  * `pull_request_target` is read from the repository's DEFAULT branch
    (`GITHUB_REF` is the default branch, `GITHUB_SHA` its last commit) — and
    `main`'s copy still said `pull_request`, because `main` only receives
    release cuts.

So the trigger that decides whether a run starts was read from a branch the PR
never touches. That is the class, not the instance: **a workflow whose `on:`
block names a default-branch-registered event cannot take effect from `dev`,
and the failure is silent — no run, nothing red.**

`push` is read from the ref being pushed ("this includes workflows that are not
merged into the default branch"), so the fix takes effect on the merge that
lands it. These guards pin that, and — following
`test_2533_alembic_head_watch.py` — actually EXECUTE the workflow's own
`script:` under node, because the promotion logic changed shape too: a `push`
payload carries no pull request, so the merged PR is resolved from the commits.

Every string assertion runs against the script with `//` comment lines
STRIPPED, for `test_2767`'s reason: this workflow's header explains at length
what it does not do, and a naive substring search matches the prose.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue-status-on-merge.yml"

# Events whose workflow definition GitHub resolves from the repository's
# DEFAULT branch. Declaring one of these HERE, in a file that reaches `main`
# only at a release cut, is the #2822 defect: the trigger is inert for as long
# as the release cycle is, and nothing reports it.
DEFAULT_BRANCH_REGISTERED = frozenset(
    {
        "pull_request_target",
        "schedule",
        "workflow_run",
        "repository_dispatch",
        "issues",
        "issue_comment",
        "label",
        "status",
        "check_run",
        "check_suite",
    }
)

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc=None):
    doc = doc if doc is not None else _doc()
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    return doc.get("on") if "on" in doc else doc[True]


def _step():
    steps = [s for s in _doc()["jobs"]["mark-in-dev"]["steps"] if isinstance(s, dict)]
    for step in steps:
        if str(step.get("uses", "")).startswith("actions/github-script"):
            return step
    pytest.fail("no actions/github-script step in issue-status-on-merge.yml")


def _script() -> str:
    return str((_step().get("with") or {}).get("script", ""))


def _code() -> str:
    """The script minus `//` comment lines — assert on what EXECUTES."""
    return "\n".join(
        line for line in _script().splitlines() if not line.strip().startswith("//")
    )


# --- the trigger ------------------------------------------------------------


def test_no_trigger_is_registered_from_the_default_branch():
    """The #2822 regression, stated as its class.

    `main` receives release cuts only, so any trigger GitHub reads from the
    default branch is dead on `dev` until the next cut — and dead silently:
    there is no run to be red.
    """
    declared = set(_triggers())
    inert = declared & DEFAULT_BRANCH_REGISTERED
    assert not inert, (
        f"{sorted(inert)} is read from the DEFAULT branch, so this file cannot "
        f"take effect on `dev` until a release cut lands it on `main` — the "
        f"#2822 outage (zero runs across six merges). Use a trigger evaluated "
        f"from the ref that carries the file."
    )


def test_the_promoter_fires_on_the_merge_landing_on_dev():
    trig = _triggers()
    assert "push" in trig, (
        "`push` is read from the ref being pushed, so a change to this file "
        "takes effect on the merge that lands it"
    )
    assert trig["push"]["branches"] == ["dev"], (
        "an unfiltered push trigger would promote on `main` too, re-labelling "
        "released issues back to status-in-dev at every release cut"
    )


def test_the_two_dead_pull_request_triggers_are_gone():
    """Both are documented in the header as what NOT to go back to.

    `pull_request` gives a fork PR a read-only token (#2767); `pull_request_target`
    is registered from `main` (#2822). Either one alongside `push` would also
    double-run the promotion once it did reach `main`.
    """
    declared = set(_triggers())
    assert "pull_request" not in declared
    assert "pull_request_target" not in declared


def test_the_root_cause_is_recorded_next_to_the_trigger():
    """#2822 AC 1. The next person to change this trigger needs to know why
    the obvious one is wrong, or they will re-run the outage."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for cue in ("#2822", "default branch", "release cut"):
        assert cue in text, f"the header comment should explain {cue!r}"


# --- resolving the merged PR from a push ------------------------------------


def test_the_merged_pr_is_resolved_from_the_pushed_commits():
    """A `push` payload has no `pull_request`, so reading one would be
    `undefined` and every promotion would throw."""
    src = _code()
    assert "listPullRequestsAssociatedWithCommit" in src
    assert "context.payload.pull_request" not in src


def test_the_token_still_covers_the_new_call_and_nothing_more():
    """`GET /repos/{owner}/{repo}/commits/{sha}/pulls` needs `pull_requests`
    read — already declared. Nothing here needs `contents`."""
    assert _doc()["permissions"] == {"issues": "write", "pull-requests": "read"}


# --- behaviour: run the workflow's own script -------------------------------

_HARNESS = """
const calls = {listPulls: [], addLabels: [], removeLabel: [], info: [], failed: []};
const PULLS = __PULLS__;
const LABEL_ERRORS = __LABEL_ERRORS__;
const context = __CONTEXT__;

function boom(spec) {
  const e = new Error(spec.message || 'refused');
  e.status = spec.status;
  throw e;
}

const github = {rest: {
  repos: {
    listPullRequestsAssociatedWithCommit: async (p) => {
      calls.listPulls.push(p.commit_sha);
      return {data: PULLS[p.commit_sha] || []};
    },
  },
  issues: {
    addLabels: async (p) => {
      calls.addLabels.push({issue: p.issue_number, labels: p.labels});
      if ((LABEL_ERRORS.add || {})[String(p.issue_number)]) {
        boom(LABEL_ERRORS.add[String(p.issue_number)]);
      }
    },
    removeLabel: async (p) => {
      calls.removeLabel.push({issue: p.issue_number, name: p.name});
      if ((LABEL_ERRORS.remove || {})[String(p.issue_number)]) {
        boom(LABEL_ERRORS.remove[String(p.issue_number)]);
      }
    },
  },
}};

const core = {
  info: (m) => calls.info.push(String(m)),
  setFailed: (m) => calls.failed.push(String(m)),
};

(async () => {
__SCRIPT__
})().then(
  () => console.log('@@RESULT@@' + JSON.stringify(calls)),
  (e) => console.log('@@RESULT@@' + JSON.stringify(
    Object.assign({}, calls, {threw: String((e && e.stack) || e)}))),
);
"""


def _pr(number, *, base="dev", merged=True, title="", body=""):
    """The fields the script reads off a PR returned by the commits API."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "base": {"ref": base},
        "merged_at": "2026-09-15T12:00:00Z" if merged else None,
    }


def _run(*, commits, pulls, head_commit=..., ref="refs/heads/dev", label_errors=None):
    """Execute the workflow's real `script:` against stubbed Actions globals."""
    if head_commit is ...:
        head_commit = {"id": commits[-1]} if commits else None
    context = {
        "ref": ref,
        "sha": commits[-1] if commits else "0" * 40,
        "repo": {"owner": "abilityai", "repo": "trinity"},
        "payload": {
            "commits": [{"id": c} for c in commits],
            "head_commit": head_commit,
        },
    }
    js = (
        _HARNESS.replace("__PULLS__", json.dumps(pulls))
        .replace("__LABEL_ERRORS__", json.dumps(label_errors or {}))
        .replace("__CONTEXT__", json.dumps(context))
        .replace("__SCRIPT__", _script())
    )
    proc = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    marker = "@@RESULT@@"
    line = next(
        (l for l in proc.stdout.splitlines() if l.startswith(marker)), None
    )
    assert line, f"harness produced no result:\n{proc.stdout}\n{proc.stderr}"
    out = json.loads(line[len(marker) :])
    assert "threw" not in out, f"the workflow script threw: {out['threw']}"
    return out


@needs_node
def test_a_squash_merge_promotes_the_issue_its_pr_closes():
    """The whole point, and the thing that produced zero runs since 2026-09-14."""
    out = _run(
        commits=["c0ffee"],
        pulls={"c0ffee": [_pr(2830, body="Closes #2822")]},
    )
    assert out["addLabels"] == [{"issue": 2822, "labels": ["status-in-dev"]}]
    assert out["removeLabel"] == [{"issue": 2822, "name": "status-in-progress"}]
    assert out["failed"] == []


@needs_node
def test_the_keyword_is_read_from_the_pr_title_too():
    out = _run(
        commits=["c0ffee"],
        pulls={"c0ffee": [_pr(2830, title="fix(ci): trigger (Fixes #2822)")]},
    )
    assert out["addLabels"] == [{"issue": 2822, "labels": ["status-in-dev"]}]


@needs_node
def test_an_open_release_pr_cannot_promote_the_whole_backlog():
    """A commit on `dev` is also in the HEAD of the open `dev` -> `main`
    release PR, whose body closes every issue in the release. Reading that PR
    would relabel the entire released backlog back to `status-in-dev` on every
    single merge."""
    out = _run(
        commits=["c0ffee"],
        pulls={
            "c0ffee": [
                _pr(2830, body="Closes #2822"),
                _pr(2900, base="main", merged=False, body="Closes #1 Closes #2"),
            ]
        },
    )
    assert [c["issue"] for c in out["addLabels"]] == [2822]


@needs_node
def test_a_pr_that_closed_without_merging_promotes_nothing():
    out = _run(
        commits=["c0ffee"],
        pulls={"c0ffee": [_pr(2830, merged=False, body="Closes #2822")]},
    )
    assert out["addLabels"] == []
    assert out["failed"] == []


@needs_node
def test_every_pr_in_a_multi_commit_push_is_promoted():
    """`dev` normally takes one squash commit per push, but an admin
    back-merge carries several — promoting only the tip's PR would strand the
    rest exactly as silently as #2822 did."""
    out = _run(
        commits=["aaa", "bbb"],
        pulls={
            "aaa": [_pr(2830, body="Closes #2822")],
            "bbb": [_pr(2831, body="Resolves #2814")],
        },
    )
    assert sorted(c["issue"] for c in out["addLabels"]) == [2814, 2822]
    assert out["listPulls"] == ["aaa", "bbb"]


@needs_node
def test_a_push_with_no_commits_is_a_no_op_and_not_a_crash():
    out = _run(commits=[], pulls={}, head_commit=None)
    assert out["listPulls"] == []
    assert out["addLabels"] == []
    assert out["failed"] == []


@needs_node
def test_a_refused_label_write_still_turns_the_run_red():
    """#2767's guarantee, re-proven on the new trigger: every issue is still
    attempted, and the run ends RED rather than green-with-a-warning."""
    out = _run(
        commits=["c0ffee"],
        pulls={"c0ffee": [_pr(2830, body="Closes #2822, Closes #2814")]},
        label_errors={"add": {"2822": {"status": 403, "message": "Resource not accessible"}}},
    )
    assert [c["issue"] for c in out["addLabels"]] == [2822, 2814]
    assert [c["issue"] for c in out["removeLabel"]] == [2814]
    assert len(out["failed"]) == 1
    assert "#2822" in out["failed"][0]


@needs_node
def test_a_missing_status_in_progress_is_still_not_a_failure():
    out = _run(
        commits=["c0ffee"],
        pulls={"c0ffee": [_pr(2830, body="Closes #2822")]},
        label_errors={"remove": {"2822": {"status": 404, "message": "Label does not exist"}}},
    )
    assert out["failed"] == []
    assert any("was not present" in m for m in out["info"])
