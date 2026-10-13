"""Tiered CI guard (#2945).

The merge path is split into three tiers, and each tier's shape is what makes
the speed-up safe rather than merely faster:

  Tier 1 (PR, blocking, ~8 min)   one head seed diffed against the CACHED
                                  `dev` baseline; base is never re-run when
                                  the base SHA's own push run already produced
                                  a JUnit.
  Tier 2 (push to dev, ~25 min)   the suites that used to run only on the PR
                                  (journey-smoke, frontend-e2e) now also run on
                                  `dev`, and `dev-ci-status.yml` folds them into
                                  ONE commit status + a moving `dev-green` tag.
  Tier 3 (nightly on dev)         the 3-seed matrix and the live integration
                                  suite, on `dev` itself rather than only as the
                                  base side of an open PR's diff.

Every assertion here pins a property that, if silently lost, turns "tested
later" into "not tested":

  * the PR job must still run ONE head seed with the per-test timeout (a
    matrix that quietly grows back to six jobs is the old cost; one that
    shrinks to zero is no gate at all);
  * the base baseline must fall back to an inline run when the cached
    artifact is missing — a stale or absent baseline would blame the PR for
    `dev`'s own failures, which is the exact false positive #715 built the
    base/head diff to avoid;
  * the regression diff must fail CLOSED when no base file arrived;
  * `dev-ci-status` must treat a CANCELLED run (superseded by the next push
    in a burst) as "no verdict", never as red, and must never move the tag on
    anything but an all-green SHA;
  * the last-green resolver must exist for the train and pick-task to call.

Pure text/YAML checks — the live consumer of these shapes is GitHub Actions,
which this suite cannot run; a real Actions run on the PR is the execution
evidence (the #2819 precedent in /validate-pr §5.4).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WF / name).read_text())


def _on(doc: dict) -> dict:
    # PyYAML parses the bare `on:` key as boolean True.
    return doc.get(True, doc.get("on"))


def _step(job: dict, title_substr: str) -> dict:
    for step in job.get("steps", []):
        if title_substr in str(step.get("name", "")):
            return step
    raise AssertionError(f"no step containing {title_substr!r} in job {job.get('name')!r}")


def _run(step: dict) -> str:
    """A step's runnable lines with YAML comments stripped — the #2019 lesson:
    a step that documents itself passes a substring check on prose alone."""
    return "\n".join(
        line for line in str(step.get("run", "")).splitlines() if not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Tier 1 — backend-unit-test.yml on a pull request
# ---------------------------------------------------------------------------


class TestTierOneUnitGate:
    @pytest.fixture(scope="class")
    def wf(self):
        return _load("backend-unit-test.yml")

    def test_pr_runs_exactly_one_head_seed(self, wf):
        matrix = wf["jobs"]["test"]["strategy"]["matrix"]
        assert matrix["side"] == ["head"], "the base side is cached, not re-run per PR (#2945)"
        assert len(matrix["seed"]) == 1, "one seed on the PR; the 3-seed matrix is Tier 3 (dev-nightly)"

    def test_head_seed_keeps_the_per_test_timeout(self, wf):
        cmd = _run(_step(wf["jobs"]["test"], "Run unit suite"))
        assert "--timeout=" in cmd and "--randomly-seed=" in cmd

    def test_baseline_is_fetched_from_the_base_shas_push_run(self, wf):
        job = wf["jobs"]["baseline"]
        assert job["if"].strip() == "github.event_name != 'push'"
        cmd = _run(_step(job, "Fetch cached"))
        assert "github.event.pull_request.base.sha" in cmd, "keyed on the exact base SHA, never 'newest dev run'"
        assert "junit-push" in cmd, "the artifact the push-side `absolute` job uploads"
        assert "event=push" in cmd
        assert "found" in str(job.get("outputs", {})), "the fallback job keys off this output"

    def test_baseline_accepts_a_red_dev_run_but_never_a_cancelled_one(self, wf):
        cmd = _run(_step(wf["jobs"]["baseline"], "Fetch cached"))
        # A red `dev` still yields the CORRECT baseline (that is the whole point of
        # a diff); a cancelled run (superseded mid-burst) uploaded nothing and must
        # not be selected, or the download fails and the fallback never fires.
        assert '.conclusion=="success"' in cmd and '.conclusion=="failure"' in cmd
        assert '.conclusion=="cancelled"' not in cmd

    def test_missing_baseline_falls_back_to_an_inline_base_run(self, wf):
        job = wf["jobs"]["base-run"]
        assert "needs.baseline.outputs.found != 'true'" in job["if"]
        assert "baseline" in job["needs"]
        cmd = _run(_step(job, "Run unit suite"))
        assert "--timeout=" in cmd and "--randomly-seed=" in cmd
        switch = _run(_step(job, "Switch to base"))
        assert "github.event.pull_request.base.sha" in switch, "the same SHA the lookup missed, not the moving tip"

    def test_diff_fails_closed_without_a_base_file(self, wf):
        job = wf["jobs"]["diff"]
        for dep in ("test", "baseline", "base-run"):
            assert dep in job["needs"]
        cmd = _run(_step(job, "Diff base vs head"))
        assert "junit-base-" in cmd and "exit 1" in cmd, "no base file must be a red diff, not an empty one"

    def test_push_side_still_publishes_the_baseline_artifact(self, wf):
        job = wf["jobs"]["absolute"]
        assert job["if"].strip() == "github.event_name == 'push'"
        upload = _step(job, "Upload JUnit")
        assert upload["with"]["name"] == "junit-push"
        assert upload["if"] == "always()", "a red dev's JUnit is still the right baseline"


# ---------------------------------------------------------------------------
# Tier 2 — the suites that used to be PR-only now also run on push to dev
# ---------------------------------------------------------------------------


class TestTierTwoRunsOnDev:
    @pytest.mark.parametrize("name", ["journey-smoke.yml", "frontend-e2e.yml"])
    def test_runs_on_push_to_dev(self, name):
        on = _on(_load(name))
        assert "dev" in on["push"]["branches"], f"{name} never ran on dev before #2945"

    def test_e2e_job_admits_the_push_event(self):
        wf = _load("frontend-e2e.yml")
        e2e = wf["jobs"]["e2e"]
        assert "github.event_name == 'push'" in e2e["if"]
        checkout = e2e["steps"][0]
        assert "push" in str(checkout["with"]["ref"]), "a push must check out the pushed SHA, not `dev` HEAD"

    def test_dev_ci_status_exists_and_is_scoped(self):
        wf = _load("dev-ci-status.yml")
        on = _on(wf)
        assert on["push"]["branches"] == ["dev"]
        perms = wf["permissions"]
        assert perms["statuses"] == "write" and perms["contents"] == "write" and perms["actions"] == "read"
        assert wf["concurrency"]["cancel-in-progress"] is True, "a burst collapses to its tail SHA"

    def test_dev_ci_status_polls_the_whole_tier_two_set(self):
        job = _load("dev-ci-status.yml")["jobs"]["status"]
        cmd = _run(_step(job, "Wait for Tier 2"))
        for wf_file in (
            "backend-unit-test.yml",
            "journey-smoke.yml",
            "frontend-e2e.yml",
            "deploy-dev.yml",
            "secret-scan.yml",
            "codeql.yml",
        ):
            assert wf_file in cmd, f"{wf_file} missing from the required Tier 2 set"

    def test_dev_ci_status_never_marks_a_superseded_sha_red(self):
        job = _load("dev-ci-status.yml")["jobs"]["status"]
        cmd = _run(_step(job, "Wait for Tier 2"))
        assert "cancelled" in cmd and "superseded" in cmd
        # The superseded path exits before any red verdict can be written.
        assert cmd.index("verdict=superseded") < cmd.index("verdict=failure")

    def test_dev_ci_status_writes_one_status_and_moves_one_tag(self):
        job = _load("dev-ci-status.yml")["jobs"]["status"]
        publish = _run(_step(job, "Publish verdict"))
        assert '"context": "dev-ci"' in publish or "context=dev-ci" in publish or "-f context=dev-ci" in publish
        tag = _run(_step(job, "Move dev-green"))
        assert "refs/tags/dev-green" in tag
        assert "steps.wait.outputs.verdict == 'success'" in _step(job, "Move dev-green")["if"]


# ---------------------------------------------------------------------------
# Tier 3 — nightly on dev itself
# ---------------------------------------------------------------------------


class TestTierThreeNightly:
    @pytest.fixture(scope="class")
    def wf(self):
        return _load("dev-nightly.yml")

    def test_is_scheduled_and_dispatchable(self, wf):
        on = _on(wf)
        assert "schedule" in on and "workflow_dispatch" in on

    def test_unit_matrix_is_three_seeds_on_dev(self, wf):
        job = wf["jobs"]["unit"]
        assert len(job["strategy"]["matrix"]["seed"]) == 3
        assert job["steps"][0]["with"]["ref"] == "dev", "schedule runs from main; the checkout must name dev"
        cmd = _run(_step(job, "Run unit suite"))
        assert "--randomly-seed=" in cmd and "--timeout=" in cmd

    def test_one_slack_message_per_scheduled_night(self, wf):
        job = wf["jobs"]["notify-slack"]
        for dep in ("unit", "integration"):
            assert dep in job["needs"], "the message reports both tiers, so it waits for both"
        assert "always()" in job["if"] and "github.event_name == 'schedule'" in job["if"], (
            "green AND red nights post; a silent morning must never mean 'green'"
        )
        cmd = _run(_step(job, "Post the verdict"))
        assert "SLACK_WEBHOOK_URL" in cmd and "exit 0" in cmd, "no secret → skip with a notice, never a red job"
        assert "curl" in cmd and "jq -n" in cmd, "plain incoming webhook, JSON built by jq"
        assert "dev-nightly green" in cmd and "dev-nightly red" in cmd

    def test_integration_runs_the_live_suite_on_dev(self, wf):
        job = wf["jobs"]["integration"]
        assert job["steps"][0]["with"]["ref"] == "dev"
        cmd = _run(_step(job, "Run the live suite"))
        assert "--ignore=unit" in cmd, "the live tiers, not the unit suite again"
        assert "start.sh" in cmd


# ---------------------------------------------------------------------------
# The resolver the train and pick-task call, and the learnings fragments
# ---------------------------------------------------------------------------


class TestConsumers:
    def test_last_green_resolver_exists_and_reads_both_markers(self):
        script = REPO / "scripts" / "ci" / "dev-last-green.sh"
        assert script.exists()
        assert os.access(script, os.X_OK), "invoked directly by the merge train"
        text = script.read_text()
        assert "refs/tags/dev-green" in text and "dev-ci" in text
        assert "merge-base --is-ancestor" in text, "a tag that is not on dev must not be trusted"

    def test_learnings_fragment_convention_is_documented(self):
        readme = REPO / "docs" / "memory" / "learnings" / "README.md"
        assert readme.exists()
        text = readme.read_text()
        assert re.search(r"YYYY-MM-DD-.*\.md", text), "the fragment filename pattern"
        assert "learnings.md" in text, "names the ledger the fragments fold into"
