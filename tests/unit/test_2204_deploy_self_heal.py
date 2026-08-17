"""Static guard: the dev deploy self-heals, serialises, and cannot fail silently (#2204).

``Deploy to Dev`` failed on every run for five days — 39+ consecutive failures,
no successful deploy in between, and **no alert of any kind**. CI stayed green,
PRs merged cleanly, issues auto-promoted to ``status-in-dev``, and the instance
sat on a five-day-old build. It was the SECOND occurrence of that class; the
first ran four days and thirty merges.

The outage's root cause was a corrupted database (the backend crash-looped
before uvicorn could bind, so every other service sat in ``Created`` behind
``depends_on: service_healthy``). That is not what this guard is about — a
corrupted file is not a thing a workflow can prevent. What this guard pins is
the set of properties that decide whether the NEXT such outage is noticed in
minutes or in days:

  1. **Deploys are serialised.** Up to four runs were measured executing
     concurrently against the one dev VM, each running ``docker compose build
     --no-cache`` then ``up -d`` on the same host and project.
  2. **An interrupted run cannot wedge its successors.** Compose renames the
     outgoing container to ``<12-hex>_<name>`` before starting the replacement;
     a run killed in between leaves that backup resident and every later deploy
     hits a name conflict at the identical point.
  3. **A green deploy means the instance is correct**, not merely that the job
     exited 0. Agent-creation failures are logged and never raised, so a deploy
     can report success with the fleet incomplete (observed: an agent lost an
     SSH port race, #2215, and the deploy stayed green).
  4. **A red deploy is announced.** Nothing watched this workflow.

Rule 2's guards are the subtle half and the reason this file exists rather than
a code comment. ``set -e`` is active for the whole SSH script, ``grep`` exits 1
when nothing matches, and ``docker rm -f`` exits 1 when given no arguments — so
an *unguarded* reaper would abort every HEALTHY deploy, which is the common
path. A fix for a wedged pipeline that wedges the pipeline harder is the
failure mode worth a CI gate.

Rule 1's direction is likewise load-bearing: ``cancel-in-progress: true`` would
kill a deploy mid-recreate, which is exactly what strands the backup container
in rule 2. The flag being present is not the property; its VALUE is.

Pure static check over the workflow file — no backend imports, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy-dev.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _doc(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else _text())


def _deploy_script(doc: dict) -> str:
    """The shell that actually runs on the dev VM."""
    for step in doc["jobs"]["deploy"]["steps"]:
        with_ = step.get("with") or {}
        if "script" in with_:
            return with_["script"]
    raise AssertionError("deploy job has no ssh-action step carrying a `script:`")


# --------------------------------------------------------------------------
# The checks, written as functions so the meta-test can feed them mutated input.
# Each raises AssertionError with a message naming the property, not the line.
# --------------------------------------------------------------------------

def check_deploys_are_serialised(doc: dict) -> None:
    conc = doc.get("concurrency")
    assert conc, (
        "deploy-dev.yml has no `concurrency:` group — concurrent deploys run "
        "`docker compose up -d` against one host (#2204)"
    )
    assert conc.get("group"), "`concurrency.group` must name a group"
    # PyYAML maps `false` -> False. An absent key is ALSO wrong: the GitHub
    # default is false, but relying on a default for a property whose inverse
    # re-creates the bug is how it silently flips later.
    assert conc.get("cancel-in-progress") is False, (
        "`cancel-in-progress` must be explicitly false: cancelling a deploy "
        "mid-recreate is precisely what strands a <12-hex>_<name> backup "
        "container and wedges every later run (#2204)"
    )


def check_reaper_present_and_guarded(script: str) -> None:
    assert "^[0-9a-f]{12}_trinity-" in script, (
        "no reaper for stale compose backup containers — an interrupted deploy "
        "can wedge every successor (#2204)"
    )
    reaper = script[script.index("^[0-9a-f]{12}_trinity-"):]
    reaper = reaper[: reaper.index("=== Restart ===")]

    assert "|| true" in reaper, (
        "the reaper's `grep` is unguarded: it exits 1 when nothing matches and "
        "`set -e` is active, so a deploy with NO orphans — the common case — "
        "would abort (#2204)"
    )
    assert "xargs -r" in reaper, (
        "the reaper must pipe through `xargs -r`: `docker rm -f` with no "
        "arguments exits 1, aborting every healthy deploy under `set -e` (#2204)"
    )


def check_reaper_runs_before_up(script: str) -> None:
    assert script.index("^[0-9a-f]{12}_trinity-") < script.index("up -d"), (
        "the reaper must run BEFORE `docker compose up -d` — reaping after the "
        "conflict has already failed the run accomplishes nothing (#2204)"
    )


def check_seeding_failure_fails_the_deploy(script: str) -> None:
    assert "Failed to create agent" in script, (
        "nothing asserts post-deploy state: agent-creation errors are logged, "
        "never raised, so a deploy reports success with an incomplete fleet "
        "(#2204 / #2215)"
    )
    block = script[script.index("=== Agent seeding check"):]
    block = block[: block.index("=== Error check ===")]
    assert "exit 1" in block, (
        "the seeding check only PRINTS. That is the `Error check` block's "
        "behaviour (it ends in `|| echo \"No errors\"`) and is exactly the "
        "silence #2204 is about — it must fail the deploy"
    )


def check_failure_is_announced(doc: dict) -> None:
    job = doc["jobs"].get("notify-failure")
    assert job, "no job announces a failed deploy — a red deploy can run silently (#2204)"
    assert job.get("needs") == "deploy" or "deploy" in (job.get("needs") or []), (
        "the notifier must depend on `deploy` so it observes that job's outcome"
    )
    # `always()` would also fire on `cancelled`, and with cancel-in-progress:false
    # a superseded PENDING run is cancelled — filing an issue for that is noise
    # that trains people to ignore the alert. See learnings.md 2026-07-28.
    assert job.get("if") == "failure()", (
        "the notifier must be `if: failure()`, not `always()` — a cancelled "
        "superseded run is not a deploy failure (#2204)"
    )
    assert (job.get("permissions") or {}).get("issues") == "write", (
        "the notifier needs `permissions: issues: write`; job-level permissions "
        "REPLACE the workflow-level block, so it must be declared on the job"
    )


def check_notifier_does_not_spam(doc: dict) -> None:
    run = doc["jobs"]["notify-failure"]["steps"][0]["run"]
    assert "--label deploy-failure --state open" in run, (
        "the notifier must look for an already-open deploy-failure issue; "
        "re-filing per failure would have produced 39 issues in the outage "
        "this fixes (#2204)"
    )
    assert "exit 0" in run, "the notifier must no-op when a failure is already tracked"


# --------------------------------------------------------------------------
# Live assertions
# --------------------------------------------------------------------------

def test_deploys_are_serialised():
    check_deploys_are_serialised(_doc())


def test_reaper_present_and_guarded():
    check_reaper_present_and_guarded(_deploy_script(_doc()))


def test_reaper_runs_before_up():
    check_reaper_runs_before_up(_deploy_script(_doc()))


def test_seeding_failure_fails_the_deploy():
    check_seeding_failure_fails_the_deploy(_deploy_script(_doc()))


def test_failure_is_announced():
    check_failure_is_announced(_doc())


def test_notifier_does_not_spam():
    check_notifier_does_not_spam(_doc())


def test_notify_job_is_not_inside_the_ssh_script():
    """The alert must not share a failure domain with the thing it reports on.

    An alert curled from inside the SSH session is unreachable in exactly the
    states worth alerting about — Tailscale down, SSH refused, host wedged. A
    job-level `if: failure()` fires even when the host never answered.
    """
    doc = _doc()
    assert "notify-failure" in doc["jobs"]
    script = _deploy_script(doc)
    assert "gh issue create" not in script, (
        "the failure alert moved inside the SSH script; it then cannot fire "
        "when the host is unreachable (#2204)"
    )


# --------------------------------------------------------------------------
# Meta-test: prove the guard actually goes red on the pre-fix content.
#
# learnings.md 2026-08-05: "when verifying that a new guard would have caught
# the bug, delete the real line and watch it go red." A guard asserted only
# against the fixed tree documents today's state instead of closing the class.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mutation,checker,uses_script",
    [
        # Pre-fix: no concurrency block at all.
        (lambda t: t.replace("concurrency:\n  group: deploy-dev\n  cancel-in-progress: false\n", ""),
         check_deploys_are_serialised, False),
        # The flip that re-creates the bug: cancelling mid-recreate.
        (lambda t: t.replace("cancel-in-progress: false", "cancel-in-progress: true"),
         check_deploys_are_serialised, False),
        # The Gemini-caught trap: reaper present but grep unguarded.
        (lambda t: t.replace("_trinity-' || true", "_trinity-'"),
         check_reaper_present_and_guarded, True),
        # Same class, one command over: `docker rm -f` with no args.
        (lambda t: t.replace("xargs -r sudo docker rm -f", "xargs sudo docker rm -f"),
         check_reaper_present_and_guarded, True),
        # Pre-fix: the seeding check prints instead of failing.
        (lambda t: t.replace(
            "              printf '%s\\n' \"$BOOT_LOG\" | grep 'Failed to create agent' | head -10\n              exit 1\n",
            "              printf '%s\\n' \"$BOOT_LOG\" | grep 'Failed to create agent' | head -10\n"),
         check_seeding_failure_fails_the_deploy, True),
        # `always()` would fire on cancelled runs too.
        (lambda t: t.replace("    if: failure()\n", "    if: always()\n"),
         check_failure_is_announced, False),
    ],
)
def test_guard_rejects_pre_fix_content(mutation, checker, uses_script):
    original = _text()
    mutated = mutation(original)
    assert mutated != original, (
        "the mutation did not change the file — the meta-test would pass "
        "against an unmodified tree and prove nothing"
    )
    doc = _doc(mutated)
    target = _deploy_script(doc) if uses_script else doc
    with pytest.raises(AssertionError):
        checker(target)
