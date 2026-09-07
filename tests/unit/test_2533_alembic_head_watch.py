"""#2533 — the pre-merge Alembic head check must not be stale-by-construction.

`schema-parity`'s single-head guard is correct and was never wrong: it runs
unconditionally, and `actions/checkout` on a `pull_request` event already
resolves `refs/pull/N/merge`, so it tests the merge result. It is **stale**.
GitHub recomputes that ref when the base advances but does NOT re-trigger
workflows, so #2526's last green run described a base that no longer existed —
75 minutes later `dev` gained a revision with the same parent, and the merged
graph had two heads. `alembic upgrade head` is singular and resolves its target
before applying anything, so that graph applies ZERO revisions on PostgreSQL.

`alembic-head-watch.yml` re-evaluates open migration PRs against the LIVE `dev`
tip. These are static guards over that workflow, in the shape of
`test_1941_nightly_merge_depth.py` / `test_2462_nightly_budget.py`, plus
behavioural tests that actually EXECUTE the verdict module — the one path that
can publish a green tick for a check that never ran, and the one place a
comment cannot prevent a regression.

Every string assertion runs against the workflow with comment lines STRIPPED.
That is deliberate and load-bearing: this workflow's own header says it "cannot
push" and "never checks out the PR", so a naive substring search matches the
prose and passes while the shell does the opposite.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "alembic-head-watch.yml"
VERDICT = REPO_ROOT / "scripts" / "ci" / "alembic-head-verdict.js"
GUARD = REPO_ROOT / "scripts" / "ci" / "check_alembic_heads.py"

OSS_LINE = "src/backend/migrations/versions"
ENT_LINE = "src/backend/enterprise/backend/migrations/versions"


# --- helpers ------------------------------------------------------------------


@pytest.fixture(scope="module")
def doc():
    assert WORKFLOW.is_file(), f"{WORKFLOW} is missing"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(doc):
    jobs = doc["jobs"]
    assert len(jobs) == 1, (
        "alembic-head-watch is deliberately ONE job — no PR-authored code ever "
        "executes, so the nightly's three-job split has no reason to exist here. "
        "If a job was added, re-justify it against the header's security argument."
    )
    return next(iter(jobs.values()))


def _uncommented(text: str) -> str:
    """Shell body with comment lines stripped — see the module docstring."""
    return "\n".join(
        line for line in str(text).splitlines() if not line.strip().startswith("#")
    )


def _runs(job) -> str:
    """Every `run:` block in the job, comments stripped, joined."""
    return "\n".join(
        _uncommented(step["run"])
        for step in job["steps"]
        if isinstance(step, dict) and "run" in step
    )


def _script(job) -> str:
    """The `actions/github-script` body, comments stripped."""
    for step in job["steps"]:
        if isinstance(step, dict) and str(step.get("uses", "")).startswith(
            "actions/github-script"
        ):
            return _uncommented((step.get("with") or {}).get("script", ""))
    pytest.fail("no actions/github-script step in alembic-head-watch.yml")


def _node(expr: str) -> dict:
    """Evaluate JS against the REAL verdict module and return its JSON result."""
    script = textwrap.dedent(
        f"""
        const m = require({str(VERDICT)!r});
        const {{ verdictFor, parseGuardOutput, MARKER, CONTEXT, MAX_DESCRIPTION }} = m;
        console.log(JSON.stringify((() => {{ {expr} }})()));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


# --- the trigger --------------------------------------------------------------


class TestTheTriggerIsPrecise:
    """A cron is not the mechanism. The staleness window opens at exactly one
    moment — `dev` gains a revision — and a PR opened or pushed after that gets
    a fresh `pull_request` run of its own."""

    def test_push_is_restricted_to_dev_and_the_version_line(self, doc):
        push = doc[True]["push"] if True in doc else doc["on"]["push"]
        assert push["branches"] == ["dev"], (
            "the primary trigger must be a push to `dev` — that is the event "
            "that invalidates every open migration PR's last green run"
        )
        assert any(OSS_LINE in p for p in push["paths"]), (
            "the push trigger lost its path filter and now fires on every "
            "commit to dev (#2533 item 6: own workflow, own path filter)"
        )

    def test_a_backstop_and_a_manual_arm_both_exist(self, doc):
        on = doc[True] if True in doc else doc["on"]
        assert "schedule" in on, "the dropped-run backstop is gone"
        assert "workflow_dispatch" in on

    def test_a_dispatch_can_be_scoped_to_one_pr(self, doc):
        on = doc[True] if True in doc else doc["on"]
        inputs = (on["workflow_dispatch"] or {}).get("inputs") or {}
        assert "pr_number" in inputs, (
            "verifying a change to this workflow again means sweeping every "
            "open PR and posting bot comments on other people's work — the "
            "demonstration must not have a wider blast radius than the change "
            "(#2462's lesson, applied here)"
        )

    def test_the_pull_request_arm_is_a_self_test_that_publishes_nothing(self, doc, job):
        """`workflow_dispatch` cannot reach a workflow that exists only on a
        feature branch — GitHub resolves dispatchable workflows from the default
        branch. Without this arm, a change here is unverifiable until after it
        merges."""
        on = doc[True] if True in doc else doc["on"]
        assert "pull_request" in on, "the self-test arm is gone"
        paths = on["pull_request"]["paths"]
        assert any("alembic-head-watch.yml" in p for p in paths), (
            "the self-test arm must be path-filtered to this workflow's own "
            "files — unfiltered, it would run on every PR in the repo"
        )
        assert job["env"]["DRY_RUN"] == "${{ github.event_name == 'pull_request' }}", (
            "the self-test arm no longer forces DRY_RUN, so it would post real "
            "statuses and comments — including from fork PRs, whose read-only "
            "token makes every write 403"
        )

    def test_the_pr_filter_is_bypassed_only_for_the_self_test(self, job):
        runs = _runs(job)
        assert 'if [ "$GITHUB_EVENT_NAME" = "pull_request" ]' in runs, (
            "the self-test no longer bypasses the versions/** filter — a "
            "workflow-only change touches no revision, so the rehearsal would "
            "discover nothing and rehearse nothing"
        )


# --- it can never push --------------------------------------------------------


class TestItCanNeverPush:
    """The whole design rests on this. `git merge-tree --write-tree` makes no
    commit and touches neither the working tree nor the index, which is what
    lets a single job hold `pull-requests: write` beside a merged PR tree."""

    @pytest.mark.parametrize(
        "forbidden", ["git push", "git commit", "git update-ref", "git switch", "git checkout"]
    )
    def test_no_write_side_git_command_anywhere(self, job, forbidden):
        assert forbidden not in _runs(job), (
            f"`{forbidden}` appeared in alembic-head-watch.yml. This workflow "
            "holds pull-requests/statuses:write in the same job as a merged PR "
            "tree, and that is only safe while it never materialises or "
            "publishes PR content."
        )

    def test_the_merge_is_merge_tree_not_a_worktree_merge(self, job):
        runs = _runs(job)
        assert "git merge-tree --write-tree" in runs
        bare_merge = re.search(r"git merge(?!-tree)\b", runs)
        assert bare_merge is None, (
            "a working-tree `git merge` came back. merge-tree is not a style "
            "choice: its exit contract (0 clean / 1 conflict / else error) is "
            "what distinguishes a conflicting PR from an infrastructure "
            "failure, which is the distinction #1941 got wrong"
        )

    def test_the_conflict_and_error_arms_are_distinguished(self, job):
        runs = _runs(job)
        assert '"$rc" -eq 1' in runs and '"$rc" -ne 0' in runs, (
            "merge-tree's exit code is no longer split into conflict (1) vs "
            "error (other). Collapsing them reports infrastructure failures as "
            "PR conflicts — #1941's exact defect"
        )


# --- the merge must be able to find a common ancestor (#1941) ------------------


class TestTheMergeCanFindACommonAncestor:
    def _checkouts(self, job):
        return [
            s
            for s in job["steps"]
            if isinstance(s, dict)
            and str(s.get("uses", "")).startswith("actions/checkout")
        ]

    def test_checkout_is_full_depth(self, job):
        checkouts = self._checkouts(job)
        assert checkouts, "the workflow no longer checks out a base to merge into"
        for step in checkouts:
            depth = (step.get("with") or {}).get("fetch-depth")
            assert depth == 0, (
                f"fetch-depth is {depth!r}. With a depth, the PR head and this "
                "base share no ancestor, merge-tree cannot find a merge base, "
                "and the detector's output stops depending on its input (#1941)"
            )

    def test_the_base_is_dev_and_the_pr_is_never_checked_out(self, job):
        for step in self._checkouts(job):
            with_ = step.get("with") or {}
            assert with_.get("ref") == "dev", (
                "the checkout must pin `dev`: the guard applied has to be the "
                "one dev enforces, not the one a PR proposes"
            )
            assert with_.get("persist-credentials") is False, (
                "persist-credentials must be false — PR objects are fetched "
                "into this workspace and must never coexist with a git credential"
            )

    def test_the_pr_head_is_fetched_via_the_fork_safe_ref(self, job):
        assert "pull/$n/head" in _runs(job), (
            "`pull/N/head` works for same-repo and fork PRs alike; "
            "`origin/<branch>` works only for the former and can collide with a "
            "base-repo branch name"
        )


# --- the #2068 guard is reused unmodified -------------------------------------


class TestTheGuardIsReusedUnmodified:
    def test_both_version_lines_are_passed(self, job):
        runs = _runs(job)
        assert GUARD.name in runs
        assert OSS_LINE in runs and ENT_LINE in runs, (
            "the guard must be pointed at BOTH version-lines, exactly as "
            "schema-parity does"
        )

    def test_the_guard_comes_from_the_workspace_not_the_merged_tree(self, job):
        assert f'"$GITHUB_WORKSPACE/scripts/ci/{GUARD.name}"' in _runs(job), (
            "the guard is being run from the merged tree, so a PR could edit "
            "the assertion applied to itself"
        )

    def test_the_enterprise_arm_never_fails_on_absence(self, job):
        """`src/backend/enterprise` is a gitlink, so its version path cannot
        resolve on public CI. Absence is the NORMAL state and must skip loudly,
        never fail — byte-for-byte what schema-parity does today."""
        runs = _runs(job)
        assert f'git rev-parse -q --verify "$tree:{ENT_LINE}"' in runs, (
            "the enterprise extraction is no longer guarded by an existence "
            "check, so an absent submodule fails the run instead of skipping"
        )

    def test_the_guard_script_is_not_modified_by_this_workflow(self, job):
        runs = _runs(job)
        assert f"> {GUARD.name}" not in runs and f"sed -i" not in runs, (
            "nothing here may rewrite check_alembic_heads.py — #2533 reuses it "
            "unchanged, and test_2068 is its regression lock"
        )


# --- it cannot die quietly ----------------------------------------------------


class TestItCannotDieQuietly:
    def test_a_sweep_with_prs_but_no_verdicts_fails_the_run(self, job):
        assert "core.setFailed" in _script(job), (
            "a run that discovered migration PRs and produced no verdict at all "
            "exits 0 again — the watcher is allowed to say nothing ABOUT A PR, "
            "never to say nothing at all and pass (#2462)"
        )

    def test_a_forked_dev_evaluates_no_pr(self, job):
        runs = _runs(job)
        assert "no PR evaluated" in runs, (
            "a `dev` that is itself multi-head must suppress every PR verdict. "
            "Flagging them all would reproduce #1941 in a second workflow: a "
            "detector whose output is independent of its input"
        )

    def test_each_run_says_what_it_did(self, job):
        assert "GITHUB_STEP_SUMMARY" in _runs(job)
        assert "core.summary" in _script(job), (
            "a run that checked nothing and a run that died must not look the "
            "same from the outside (#2462 AC 3)"
        )

    def test_the_files_api_fails_open_toward_evaluating(self, job):
        assert "evaluating it anyway" in _runs(job), (
            "the PR file listing no longer fails open. An extra sub-second "
            "check costs nothing; a skipped one is the entire bug"
        )

    def test_a_dispatch_input_cannot_be_injected(self, job):
        runs = _runs(job)
        assert "grep -qE '^[0-9]+$'" in runs, (
            "pr_number is no longer validated as digits before reaching the shell"
        )
        assert "${{ inputs.pr_number }}" not in runs, (
            "pr_number is interpolated directly into a run block — it must "
            "arrive via `env:` so a quote in the value cannot break out"
        )


# --- permissions --------------------------------------------------------------


class TestLeastPrivilege:
    def test_top_level_permissions_are_empty(self, doc):
        assert doc.get("permissions") == {} or doc.get("permissions") is None

    def test_the_job_declares_exactly_what_it_uses(self, job):
        perms = job["permissions"]
        assert perms["contents"] == "read", "this workflow must never write contents"
        assert perms["statuses"] == "write"      # the merge-click alarm
        assert perms["pull-requests"] == "write"  # the sticky comment
        assert "actions" not in perms and "packages" not in perms


# --- the verdict module, EXECUTED ---------------------------------------------


class TestTheVerdictLogicIsAModuleATestCanRun:
    """Inline `github-script` bodies cannot be unit-tested, and this is the one
    path that can publish a green tick for a check that never ran."""

    def test_the_workflow_loads_the_module(self, job):
        assert VERDICT.is_file(), f"{VERDICT} is missing"
        assert VERDICT.name in _script(job), (
            "the verdict logic moved back inline, where no test can execute it"
        )

    def test_the_module_is_commonjs(self):
        """`actions/github-script` runs its body under `require()`, so an ESM
        export is unloadable by the only caller that matters — and it fails at
        RUN time, in the step that posts."""
        src = VERDICT.read_text(encoding="utf-8")
        assert "module.exports" in src
        assert not re.search(r"^export\s+(function|const)", src, re.MULTILINE)

    @needs_node
    def test_a_conflict_publishes_no_status(self):
        v = _node(
            "return verdictFor({prNumber: 1, headSha: 'abc', outcome: 'conflict'});"
        )
        assert v["status"] is None, (
            "a conflicting PR was never evaluated. `success` is a false "
            "all-clear (#2029) and `failure` blames it for the wrong thing"
        )
        assert v["comment"] is not None and v["createIfMissing"] is True

    @needs_node
    def test_an_unknown_outcome_publishes_nothing_at_all(self):
        v = _node(
            "return verdictFor({prNumber: 1, headSha: 'abc', outcome: 'unknown'});"
        )
        assert v["status"] is None and v["comment"] is None, (
            "absence of a verdict is its own state — it must leave whatever is "
            "already on the PR exactly as it is (#2029)"
        )

    @needs_node
    def test_an_unrecognised_outcome_fails_closed(self):
        v = _node("return verdictFor({prNumber: 1, outcome: 'banana'});")
        assert v["status"] is None and v["comment"] is None

    @needs_node
    def test_clean_never_creates_a_sticky(self):
        v = _node(
            "return verdictFor({prNumber: 1, headSha: 'abc', outcome: 'clean',"
            " devHead: '0055_x'});"
        )
        assert v["status"]["state"] == "success"
        assert v["createIfMissing"] is False, (
            "a green comment on every migration PR is noise the real signal "
            "then hides in; a clean PR may only RESOLVE an existing sticky"
        )
        assert v["comment"] is not None, "an existing sticky must still be resolvable"

    @needs_node
    def test_dry_run_publishes_nothing_for_any_outcome(self):
        for outcome in ("clean", "fork", "conflict"):
            v = _node(
                "return verdictFor({prNumber: 1, headSha: 'abc', outcome: "
                f"{outcome!r}, dryRun: true}});"
            )
            assert v["status"] is None and v["comment"] is None, (
                f"dryRun leaked a publish for outcome={outcome} — the "
                "pull_request self-test would post on every PR that edits this "
                "workflow, and 403 from a fork"
            )
            assert v["summary"], "dry run must still report what it evaluated"

    @needs_node
    def test_a_fork_description_fits_githubs_140_char_cap(self):
        heads = ", ".join(f"0056_a_very_long_revision_name_number_{i}" for i in range(8))
        detail = "resolves to 8 heads\n" + "\n".join(
            f"  • 0056_a_very_long_revision_name_number_{i}  (f.py)" for i in range(8)
        )
        v = _node(
            "return verdictFor({prNumber: 1, headSha: 'abc', outcome: 'fork',"
            f" detail: {detail!r}}});"
        )
        assert len(v["status"]["description"]) <= 140, (
            "GitHub truncates a commit-status description past 140 chars; "
            "doing it here keeps the tail legible"
        )
        assert heads  # the fixture is what makes the cap reachable

    @needs_node
    def test_a_fork_whose_output_cannot_be_parsed_still_yields_a_failure(self):
        """`parseGuardOutput` reads another tool's printed shape, which is
        brittle by nature. It must degrade to a correct count-less verdict,
        never to a crash and never to a green one."""
        v = _node(
            "return verdictFor({prNumber: 1, headSha: 'abc', outcome: 'fork',"
            " detail: 'something entirely unexpected'});"
        )
        assert v["status"]["state"] == "failure"
        assert v["comment"] is not None

    @needs_node
    def test_it_parses_the_REAL_guard_output(self, tmp_path):
        """The coupling neither file can see: `parseGuardOutput` reads what
        `check_alembic_heads.py` actually prints. Reproduces #2526's fork —
        two revisions sharing `0049_execution_turn_integrity`."""
        versions = tmp_path / "versions"
        versions.mkdir()
        for name, parent in (
            ("0049_execution_turn_integrity", None),
            ("0050_agent_canvases", "0049_execution_turn_integrity"),
            ("0050_agent_loops_terminal_driven", "0049_execution_turn_integrity"),
        ):
            down = f'"{parent}"' if parent else "None"
            (versions / f"{name}.py").write_text(
                f'"""{name}"""\nrevision = "{name}"\ndown_revision = {down}\n',
                encoding="utf-8",
            )
        proc = subprocess.run(
            ["python3", str(GUARD), str(versions)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, "the fixture is not actually a fork"
        detail = proc.stdout + proc.stderr

        parsed = _node(f"return parseGuardOutput({detail!r});")
        assert parsed["heads"] == [
            "0050_agent_canvases",
            "0050_agent_loops_terminal_driven",
        ], (
            "the guard's output shape changed and the parser no longer reads "
            "it — the PR comment would lose the head names"
        )
        assert parsed["forkPoint"] == "0049_execution_turn_integrity"

        v = _node(
            "return verdictFor({prNumber: 2526, headSha: 'abc', outcome: 'fork',"
            f" detail: {detail!r}, devHead: '0050_agent_canvases'}});"
        )
        assert "0050_agent_canvases" in v["status"]["description"]
        # The fix instruction must name the real dev head — what #2526 did.
        assert "rechain this PR's revision off `0050_agent_canvases`" in v["comment"]["body"]
