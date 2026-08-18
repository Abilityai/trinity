"""#2246 — an OSS-only dev deploy must be loud, and its diagnostic must name the right layer.

Every retained ``Deploy to Dev`` run (200, 2026-07-29 → 2026-08-17) carried
``Enterprise submodule init failed`` and deployed the dev instance unentitled:
``enterprise_features`` empty, every ``requires_entitlement`` route 403, gated Vue
hidden. The job reported success each time, so the recorded gitlink was never
exercised anywhere before prod.

Two independent defects kept it invisible, and this file pins the fix for both:

  1. **The diagnostic named the wrong layer.** The clone died at SSH host-key
     verification — one layer BEFORE authentication — while the workflow printed
     "confirm the dev VM has read access". Following that hint installs a deploy
     key and produces the identical warning, which is a plausible reason it sat
     for weeks. So the remedy is now CLASSIFIED from the clone's own output, and
     the host-key case must not mention the access remedies.
  2. **The loud check guarded the louder failure.** #2068's ``::error::``
     assertions live inside ``if [ -f …/enterprise/backend/__init__.py ]``, so
     they catch *mounted but not registered* and structurally cannot fire on *not
     mounted at all* — the state actually in production on dev. A ``::warning::``
     does not change a run's conclusion, and nobody reads a green run's log.

The classifier half is a real behavioural test (the script runs, on the exact
output from the issue). The workflow half is a static guard, in the shape of
``test_2204_deploy_self_heal.py`` — a workflow cannot be executed in a unit test,
and the property worth pinning is the DIRECTION of the decision (fail, not warn),
which a future edit could silently reverse.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "ci" / "classify-submodule-failure.sh"
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-dev.yml"

# Verbatim from the run that still had logs when #2246 was filed. Note it also
# contains "Could not read from remote repository" — the string an authorization
# failure prints too, which is why ordering in the classifier is load-bearing.
HOST_KEY_OUTPUT = """Cloning into '/home/deploy/trinity/src/backend/enterprise'...
Host key verification failed.
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
fatal: clone of 'git@github.com:Abilityai/trinity-enterprise.git' into submodule path '/home/deploy/trinity/src/backend/enterprise' failed
Failed to clone 'src/backend/enterprise' a second time, aborting
"""

AUTH_OUTPUT = """Cloning into '/home/deploy/trinity/src/backend/enterprise'...
git@github.com: Permission denied (publickey).
fatal: Could not read from remote repository.
Failed to clone 'src/backend/enterprise' a second time, aborting
"""

AUTH_HTTPS_OUTPUT = """Cloning into '/home/deploy/trinity/src/backend/enterprise'...
remote: Repository not found.
fatal: repository 'https://github.com/Abilityai/trinity-enterprise.git/' not found
"""

NETWORK_OUTPUT = """Cloning into '/home/deploy/trinity/src/backend/enterprise'...
ssh: Could not resolve hostname github.com: Temporary failure in name resolution
fatal: Could not read from remote repository.
"""


def classify(text: str) -> str:
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "-"], input=text, capture_output=True, text=True, timeout=30
    )
    # A classifier that fails the deploy it is diagnosing would be worse than the
    # bug: the caller decides, always.
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def klass(text: str) -> str:
    first = classify(text).splitlines()[0]
    assert first.startswith("CLASS: "), first
    return first[len("CLASS: ") :]


# ---------------------------------------------------------------------------
# The classifier: right layer, right remedy
# ---------------------------------------------------------------------------

def test_the_real_failure_is_classified_as_host_key_not_access():
    assert klass(HOST_KEY_OUTPUT) == "host-key"


def test_the_host_key_remedy_does_not_send_anyone_after_credentials():
    """The whole defect: an authorization remedy for a pre-authorization failure."""
    out = classify(HOST_KEY_OUTPUT).lower()
    assert "known_hosts" in out
    assert "ssh-keyscan" in out
    # The old wording, which cost someone an afternoon proving it wrong.
    assert "read access" not in out
    assert "deploy key" not in out
    assert "org membership" not in out
    # And it says so explicitly rather than leaving the reader to infer it.
    assert "not an access problem" in out


@pytest.mark.parametrize(
    "output", [AUTH_OUTPUT, AUTH_HTTPS_OUTPUT], ids=["ssh-publickey", "https-not-found"]
)
def test_a_genuine_credential_failure_still_says_access(output):
    out = classify(output)
    assert klass(output) == "authorization"
    assert "deploy key" in out.lower()


def test_a_dns_failure_is_neither_of_the_above():
    """Host-key output also contains 'Could not read from remote repository', so a
    naive classifier bins everything together."""
    assert klass(NETWORK_OUTPUT) == "network"


def test_silence_is_its_own_class_because_update_none_exits_zero():
    """#1443 made this submodule `update = none`; a skipped submodule prints
    nothing and exits 0, so an empty capture is a distinct diagnosis."""
    out = classify("")
    assert klass("") == "no-output"
    assert "update = none" in out or "'checkout'" in out


def test_an_unrecognised_failure_is_admitted_not_guessed():
    out = classify("fatal: something nobody has seen before\n")
    assert klass("fatal: something nobody has seen before") == "unknown"
    assert "would be a guess" in out
    # It hands back the evidence rather than swallowing it.
    assert "something nobody has seen before" in out


# ---------------------------------------------------------------------------
# The workflow: the OSS-only state now changes the run's conclusion
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def workflow() -> str:
    return _WORKFLOW.read_text()


def test_the_not_mounted_branch_exists_at_all(workflow):
    """The #2068 assertions are guarded on the marker file; without an else-branch
    the not-mounted state has no assertion anywhere in the workflow."""
    registration = workflow.split("=== Enterprise registration")[1]
    assert "::error::Deployed in OSS-only mode" in registration


def test_an_oss_only_deploy_fails_the_run(workflow):
    """DIRECTION, not presence: demoting this back to a bare warning is exactly
    how the bug hid for 200 runs.

    Scanned by line rather than by splitting on ``fi`` — the error message itself
    contains "classi**fi**ed", which is the kind of thing that makes a static
    guard pass or fail for the wrong reason.
    """
    lines = workflow.splitlines()
    idx = next(
        i for i, line in enumerate(lines) if "::error::Deployed in OSS-only mode" in line
    )
    following = [line.strip() for line in lines[idx + 1 : idx + 4]]
    assert "exit 1" in following, following


def test_the_escape_hatch_is_opt_in_and_not_the_default(workflow):
    """A VM with no enterprise access can demote it — but must say so explicitly."""
    assert 'vars.DEPLOY_ALLOW_OSS_ONLY }}" = "true"' in workflow
    # Being a repo variable rather than an edit keeps the default loud.
    assert workflow.count("DEPLOY_ALLOW_OSS_ONLY") >= 2


def test_the_clone_output_is_captured_and_classified(workflow):
    """The cause had to be reconstructed from one unexpired run because the clone's
    own output was discarded."""
    submodule = workflow.split("=== Submodule (enterprise)")[1].split("=== Volume ownership")[0]
    assert "classify-submodule-failure.sh" in submodule
    assert 'ENT_LOG=$(mktemp)' in submodule
    # The old unconditional access hint must be gone from the workflow itself.
    assert "confirm the dev VM has read access" not in workflow


def test_the_pat_transport_never_persists_the_token(workflow):
    """HTTPS+PAT clears host-key AND authorization at once, but a token written
    into the VM's .git/config would outlive the deploy."""
    submodule = workflow.split("=== Submodule (enterprise)")[1].split("=== Volume ownership")[0]
    assert "insteadOf" in submodule
    assert "git -c " in submodule
    # `git config submodule.<path>.url <token URL>` is the persisting form.
    assert "config submodule.src/backend/enterprise.url" not in submodule


def test_the_deploy_is_not_aborted_before_the_instance_is_up(workflow):
    """Failing at the submodule step would leave the VM half-updated (the pull has
    already happened) and fix nothing; the run goes red after the health check."""
    submodule = workflow.split("=== Submodule (enterprise)")[1].split("=== Volume ownership")[0]
    assert "exit 1" not in submodule
