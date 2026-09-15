"""#2767 — the status promoter works for fork PRs, and stays safe doing it.

On `pull_request` a PR opened from a FORK gets a read-only `GITHUB_TOKEN`, so
every label write was refused ("Resource not accessible by integration") and the
issue stranded in `status-in-progress` with its fix already on `dev`. The run
reported SUCCESS, because the refusal was a `core.warning`. It only ever bit
external contributors.

The fix is `pull_request_target`, which carries the base repo's write token —
and which is the standard privilege-escalation footgun. These are static guards
on the two properties that make it safe HERE, in the shape of
`test_1896_integration_nightly_workflow.py`:

  * the workflow never CHECKS OUT PR code;
  * the workflow never EXECUTES anything (`run:`).

The escalation always needs both — fetch the stranger's code, then run it. If a
future edit adds either, this file goes red and the trigger must go back to
`pull_request` with a repo-scoped PAT for the label calls.
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
    / "issue-status-on-merge.yml"
)


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc):
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    return doc.get("on") if "on" in doc else doc[True]


def _steps():
    return [s for s in _doc()["jobs"]["mark-in-dev"]["steps"] if isinstance(s, dict)]


def _script() -> str:
    return "\n".join(str(s.get("with", {}).get("script", "")) for s in _steps())


def _code() -> str:
    """The script minus `//` comment lines — assert on what EXECUTES, not on
    prose (`test_1896`'s `_commands` precedent). Without this, a comment saying
    "collected, not thrown" satisfies a `"throw" not in src` check and a comment
    saying "there is NO actions/checkout" satisfies a checkout check: the
    guard then passes because of the sentence claiming the property rather than
    because of the property."""
    return "\n".join(
        line for line in _script().splitlines() if not line.strip().startswith("//")
    )


def _yaml_code() -> str:
    """The workflow minus `#` comment lines, for the same reason."""
    return "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


# --- the fix ----------------------------------------------------------------

def test_the_trigger_carries_a_write_token_for_fork_prs():
    trig = _triggers(_doc())
    assert "pull_request_target" in trig, (
        "a fork PR's `pull_request` token is read-only, so label writes are refused"
    )
    assert "pull_request" not in trig, "both triggers would double-run the promotion"
    cfg = trig["pull_request_target"]
    assert cfg["types"] == ["closed"]
    assert cfg["branches"] == ["dev"]


def test_a_refused_label_write_turns_the_run_red():
    """The defect was a green run hiding a stranded issue."""
    src = _code()
    assert "core.setFailed" in src, "a refused write must fail the job"
    assert "failures.push" in src, "failures must be collected, not swallowed"


def test_every_issue_is_still_attempted_before_the_job_fails():
    """One unlabelable issue must not strand the others, so the failure is
    raised AFTER the loop rather than thrown at the first miss."""
    src = _code()
    loop = src.index("for (const num of issues)")
    assert src.index("core.setFailed") > loop, "setFailed must come after the loop"
    assert "throw" not in src, "a throw inside the loop would skip later issues"


def test_a_missing_status_in_progress_is_still_not_a_failure():
    """An issue that was never picked up simply has no label to remove."""
    src = _code()
    assert "e.status === 404" in src
    assert "was not present" in src


# --- what makes pull_request_target safe here -------------------------------

def test_the_workflow_never_checks_out_pr_code():
    """Half of the escalation. Without a checkout there is no stranger's code
    on the runner at all."""
    uses = [str(s.get("uses", "")) for s in _steps()]
    assert not any(u.startswith("actions/checkout") for u in uses), (
        "a checkout under pull_request_target puts fork code next to a write "
        "token — revert to pull_request + a PAT instead"
    )
    assert "actions/checkout" not in _yaml_code(), (
        "a checkout step was added outside the parsed steps list"
    )


def test_the_workflow_never_executes_anything():
    """The other half. No `run:` means nothing from the PR can be executed even
    if it somehow reached the runner."""
    runs = [s.get("run") for s in _steps() if s.get("run")]
    assert runs == [], f"pull_request_target workflow gained a run step: {runs}"


def test_untrusted_pr_text_only_ever_reaches_a_number_regex():
    """`title`/`body` are attacker-controlled on a fork PR. They must not flow
    anywhere but the issue-number match."""
    src = _code()
    assert "pr.title" in src and "pr.body" in src
    # the only consumer of `haystack`
    assert src.count("haystack") == 2, "haystack should be built once and matched once"
    assert "matchAll(pattern)" in src
    for forbidden in ("exec(", "eval(", "child_process", "require("):
        assert forbidden not in src, f"{forbidden} would make the PR text executable"


def test_the_token_stays_minimal():
    """Escalating the trigger must not quietly escalate the scopes too."""
    perms = _doc()["permissions"]
    assert perms == {"issues": "write", "pull-requests": "read"}, perms
    assert "contents" not in perms, "no write access to the repo contents is needed"


def test_only_a_merged_pr_promotes_anything():
    assert _doc()["jobs"]["mark-in-dev"]["if"] == "github.event.pull_request.merged == true"


def test_the_safety_argument_is_written_down_next_to_the_trigger():
    """A future editor needs to know WHY this trigger is allowed here — the
    reasoning is the control, and an undocumented pull_request_target is one
    refactor away from being unsafe."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target" in text
    for cue in ("checkout", "run:", "#2767"):
        assert cue in text, f"the comment block should mention {cue!r}"
