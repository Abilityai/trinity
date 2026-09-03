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

# Matches the command wherever it sits in an `inline` list — the list holds more
# than one entry, so anchoring on a single-element block is too tight.
_WAIT_RE = re.compile(r"""["']cloud-init status --wait["']""")
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


_MKDIR_RE = re.compile(r'"mkdir -p /tmp/trinity-files"')


def test_upload_destination_is_created_before_the_file_provisioner() -> None:
    """Packer flattens the bundle when the destination does not exist (#2281).

    The `file` provisioner uploads `files/` to `/tmp/trinity-files`, and
    `01-provision.sh` then installs from `/tmp/trinity-files/opt/...`,
    `/etc/...`, `/var/...`. With the destination absent, Packer strips the
    top-level directory names and the tree arrives as `trinity-firstboot/`,
    `update-motd.d/`, `systemd/`, `lib/` — so every install fails with
    "cannot stat", five minutes into the build and after all five image pulls.

    Verified both ways against a live droplet. With `mkdir -p` first, the tree
    arrives as `opt/ etc/ var/` exactly as laid out.

    Nothing else could have caught this: the bundle-tracked guard checks git, not
    the upload, and `packer build` had never been run — the bundle shipped with
    install paths that could not resolve on any droplet.
    """
    t = _text()
    mkdir = _MKDIR_RE.search(t)
    assert mkdir, (
        "trinity.pkr.hcl no longer creates /tmp/trinity-files before uploading "
        "into it — Packer will flatten the bundle and every install will fail."
    )
    first_file = _FILE_PROVISIONER_RE.search(t)
    assert first_file, "trinity.pkr.hcl no longer has a file provisioner"
    assert mkdir.start() < first_file.start(), (
        "the mkdir must run BEFORE the file provisioner; afterwards it is useless."
    )
