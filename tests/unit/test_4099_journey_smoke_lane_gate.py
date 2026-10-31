"""#4099: journey-smoke's PR run is gated to lane C, and the gate fails open.

Since trinity#2945 the journey tier is Tier 2 — it runs on every push burst to
`dev` and `dev-ci-status.yml` carries its verdict. On a PR it is needed only
for lane C (auth, credentials, untrusted-input parsers, image build, CI, the
enterprise seam). The workflow still booted a stack on EVERY PR to `dev`.

Three properties are load-bearing, each pinned here:

1. The trigger stays unconditional and the decision lives in a `changes` job,
   so a skipped heavy job still REPORTS a conclusion. A `paths-ignore` would
   leave the status absent — the #1237 / trinity#2384 "never reported, never
   mergeable" trap.
2. The gate fails OPEN: a `changes` job that failed, or a push / dispatch
   event, runs the tier. "Unable to decide" must never mean "skip".
3. The path set is lane C as `/validate-pr` defines it, plus the tier itself
   and the deploy scripts; the `journeys` label is the manual opt-in.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "journey-smoke.yml"


@pytest.fixture(scope="module")
def wf():
    text = WORKFLOW.read_text()
    return text, yaml.safe_load(text)


def test_the_trigger_is_still_unconditional(wf):
    _, doc = wf
    on = doc.get("on") or doc.get(True)
    assert "paths" not in on["pull_request"] and "paths-ignore" not in on["pull_request"], (
        "a path filter on the trigger leaves the check ABSENT on PRs it filters out, "
        "which GitHub holds at 'Expected' forever (#1237)"
    )


def test_a_changes_job_decides_and_only_on_pull_requests(wf):
    _, doc = wf
    changes = doc["jobs"]["changes"]
    assert changes["if"].strip() == "github.event_name == 'pull_request'"
    assert "lane_c" in changes["outputs"]


def test_the_heavy_job_is_gated_and_fails_open(wf):
    _, doc = wf
    job = doc["jobs"]["journey-smoke"]
    assert job["needs"] == "changes" or job["needs"] == ["changes"]
    cond = " ".join(job["if"].split())
    assert cond.startswith("always()"), "without always() a skipped `changes` job skips the tier too"
    for clause in (
        "github.event_name == 'push'",
        "github.event_name == 'workflow_dispatch'",
        "needs.changes.outputs.lane_c == 'true'",
        "needs.changes.result == 'failure'",
        "contains(github.event.pull_request.labels.*.name, 'journeys')",
    ):
        assert clause in cond, f"gate is missing the clause {clause!r}"


def test_the_changes_step_fails_open_and_covers_lane_c(wf):
    text, doc = wf
    run = doc["jobs"]["changes"]["steps"][0]["run"]
    assert "fail_open" in run and "lane_c=true" in run
    # The lane C set `/validate-pr` classifies on, plus the tier and deploy scripts.
    m = re.search(r"LANE_C='([^']+)'", run)
    assert m, "the path regex must be a single quoted LANE_C= literal so it can be diffed against validate-pr"
    pattern = re.compile(m.group(1))
    for path in (
        "src/backend/dependencies.py",
        "src/backend/services/credential_encryption.py",
        "src/backend/utils/safe_yaml.py",
        "src/backend/routers/auth.py",
        "docker/backend/Dockerfile",
        "docker-compose.prod.yml",
        ".github/workflows/journey-smoke.yml",
        "src/backend/enterprise",
        "tests/journeys/test_j01.py",
        "tests/conftest.py",
        "scripts/deploy/start.sh",
    ):
        assert pattern.search(path), f"lane C path {path} would skip the tier"
    for path in (
        "docs/memory/architecture.md",
        "src/backend/services/report_service.py",
        "src/frontend/src/views/AgentDetail.vue",
        "tests/unit/test_4099_journey_smoke_lane_gate.py",
    ):
        assert not pattern.search(path), f"{path} is not lane C and must not boot the stack"
