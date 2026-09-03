"""Static guard: the 1-Click build waits for cloud-init before it touches apt (#2281).

Bug: ``trinity.pkr.hcl`` carried no ``cloud-init status --wait`` provisioner, so
``scripts/01-provision.sh`` opened with ``apt-get update`` while Ubuntu 24.04's
boot-time ``apt-daily`` / ``unattended-upgrades`` units still held the dpkg lock.
Packer's SSH is available well before those finish, so the build died with::

    E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1406
    Script exited with non-zero exit status: 100

Observed 2/2 on ``v0.9.5-rc2``, the first tag whose GHCR images made a build
possible at all — so the whole bundle had shipped without one successful run.

The remedy is DigitalOcean's own, not an invention: their reference template
(``marketplace-partners/marketplace-image.json``, the same repo
``90-cleanup-and-check.sh`` already pins by SHA) opens with exactly this
provisioner.

ORDER is the substance, which is why this asserts position and not presence. A
wait that runs after the ``file`` provisioner or after ``01-provision.sh`` is
inert, and the symptom of getting it wrong is a build that fails *intermittently*
on someone else's machine and reads as a flake — so it gets retried rather than
fixed, which is precisely what happened here before the cause was found.

``packer build`` cannot be the guard: it needs a DO token and ~15 minutes, so it
runs on nobody's PR. Repo-side, in the existing unit job, pure stdlib.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _ROOT / "packer" / "digitalocean" / "trinity.pkr.hcl"

_WAIT_RE = re.compile(r"""inline\s*=\s*\[\s*["']cloud-init status --wait["']\s*\]""")
_FILE_PROVISIONER_RE = re.compile(r'provisioner\s+"file"\s*\{')
_PROVISION_SCRIPT_RE = re.compile(r'scripts/01-provision\.sh')


def _text() -> str:
    return _TEMPLATE.read_text()


def test_template_waits_for_cloud_init() -> None:
    assert _WAIT_RE.search(_text()), (
        "trinity.pkr.hcl no longer runs `cloud-init status --wait`. Without it the "
        "first apt-get races Ubuntu's boot-time apt-daily/unattended-upgrades for "
        "the dpkg lock and the build dies with apt exit 100."
    )


def test_wait_precedes_every_other_provisioner() -> None:
    t = _text()
    wait = _WAIT_RE.search(t)
    assert wait, "no cloud-init wait to order (see test_template_waits_for_cloud_init)"

    first_file = _FILE_PROVISIONER_RE.search(t)
    assert first_file, "trinity.pkr.hcl no longer has a file provisioner"
    assert wait.start() < first_file.start(), (
        "the cloud-init wait must come BEFORE the file provisioner — a wait that "
        "runs later cannot protect the apt calls that follow it."
    )

    script = _PROVISION_SCRIPT_RE.search(t)
    assert script, "trinity.pkr.hcl no longer runs scripts/01-provision.sh"
    assert wait.start() < script.start(), (
        "the cloud-init wait must come BEFORE 01-provision.sh, whose first action "
        "is `apt-get update`."
    )
