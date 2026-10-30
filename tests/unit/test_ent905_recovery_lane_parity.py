"""J24 recovery-lane wiring parity (trinity-enterprise#905).

The J24 chain is one journey spread over four files that can only drift apart
silently: the harness (``tests/system_chains/test_j24_recovery_chain.py``)
hard-codes the pilot agent's name, the workflow bakes the SAME name into the
booted stack's ``PULL_MODE_PILOT_AGENTS``, the runner names it in its
preflight, and the catalog points the J24 row at the harness. Rename the agent
in one place and the chain stops reporting anything louder than NOT RUN — on a
lane nobody watches between release cuts. This is the #1891 parity pattern
(config scanned per-PR because config cannot be executed per-PR), source-text
by necessity and supplementary to the chain itself (tdd guide: source-text as
a pin layered on execution, never the only guard).

Two rules of the lane are also pinned because breaking them is a one-line edit
with a security or cost consequence:

* **dispatch-only** — the workflow must never gain a ``pull_request`` or
  ``schedule`` trigger: it writes a real Anthropic key into the booted stack's
  ``.env`` (#1896 is why credentials are forbidden on PR-triggered runs, and
  nightly is deliberately keyless);
* **the credential gate stays first** — without it a keyless dispatch run
  reports NOT RUN, which reads as "nothing to say" and is exactly how a fork
  reached a release train green (#2828's lesson, applied to this lane).
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

HARNESS = REPO / "tests/system_chains/test_j24_recovery_chain.py"
RUNNER = REPO / "scripts/system/run_recovery.sh"
WORKFLOW = REPO / ".github/workflows/recovery-journey.yml"
CATALOG = REPO / "tests/journeys/catalog.yaml"

PILOT = "pytest-ephemeral-journey-b9pull"
SECRET = "JOURNEY_ANTHROPIC_API_KEY"


def test_the_four_files_exist():
    for path in (HARNESS, RUNNER, WORKFLOW, CATALOG):
        assert path.exists(), f"J24 lane file missing: {path.relative_to(REPO)}"


def test_pilot_agent_name_agrees_across_the_lane():
    harness = HARNESS.read_text()
    assert f'PULL_NAME = "{PILOT}"' in harness, (
        "the harness no longer pins the pilot name this guard checks — update "
        "PILOT here and in every lane file in the same commit")
    for path in (RUNNER, WORKFLOW):
        assert PILOT in path.read_text(), (
            f"{path.relative_to(REPO)} does not name the pilot agent "
            f"'{PILOT}' — the booted stack's PULL_MODE_PILOT_AGENTS and the "
            f"harness's fixed name must agree or every pilot leg is NOT RUN")


def test_catalog_points_j24_at_the_chain_harness():
    catalog = yaml.safe_load(CATALOG.read_text())
    j24 = next((j for j in catalog["journeys"] if j["id"] == "J24"), None)
    assert j24 is not None, "catalog.yaml has no J24 row"
    assert j24["harness"] == "tests/system_chains/test_j24_recovery_chain.py", (
        f"J24's harness moved to {j24['harness']!r} — move this guard's "
        f"HARNESS constant and the runner's pytest target with it")


def test_workflow_is_dispatch_only_and_gates_on_the_credential():
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the bare key `on:` as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch"}, (
        f"recovery-journey.yml triggers are {sorted(triggers)} — this lane "
        f"writes a real model key into the stack's .env, so it must stay "
        f"workflow_dispatch-only (#1896)")
    text = WORKFLOW.read_text()
    assert f"secrets.{SECRET}" in text, (
        f"the workflow no longer reads secrets.{SECRET}")
    steps = doc["jobs"]["recovery-journey"]["steps"]
    assert "credential" in (steps[0].get("name") or "").lower(), (
        "the require-the-model-credential gate is no longer the FIRST step — "
        "a keyless run would boot, report NOT RUN and read as nothing to say")


def test_runner_drives_both_phases_of_the_harness():
    text = RUNNER.read_text()
    assert "test_j24_recovery_chain.py" in text, (
        "run_recovery.sh no longer targets the J24 harness")
    assert '-k "not depilot"' in text and '-k "depilot"' in text, (
        "run_recovery.sh lost a phase: legs 1-5 run pre-flip "
        '(-k "not depilot"), leg 6 post-flip (-k "depilot")')
