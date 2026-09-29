"""The AWS AMI bundle (#3004, PROV-019): template shape, the cleanup that
replaces DigitalOcean's img_check, and the MOTD on an AWS instance.

`packer build` needs AWS credentials and ~15 minutes, so it runs on nobody's PR.
These pin what review and the Marketplace scan would otherwise catch a week
later. Pure stdlib.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AWS = _ROOT / "packer" / "aws"
_HCL = _AWS / "trinity.pkr.hcl"
_CLEANUP = _AWS / "scripts" / "90-aws-cleanup.sh"
_SUBMIT = _AWS / "scripts" / "mp-submit.sh"
_MOTD = _ROOT / "packer" / "digitalocean" / "files" / "etc" / "update-motd.d" / "99-trinity"
_SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _hcl() -> str:
    return _HCL.read_text()


def _code(path: Path) -> str:
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

def test_the_ami_is_what_marketplace_accepts() -> None:
    t = _code(_HCL)
    assert re.search(r'region\s*=\s*"us-east-1"', t), "Marketplace AMIs are submitted from us-east-1"
    assert '"099720109477"' in t, "the base must be Canonical's own Ubuntu"
    assert "ubuntu-noble-24.04-amd64-server-" in t
    assert re.search(r'imds_support\s*=\s*"v2.0"', t)
    assert re.search(r"ena_support\s*=\s*true", t)
    assert re.search(r"encrypt_boot\s*=\s*false", t) and re.search(r"encrypted\s*=\s*false", t), (
        "Marketplace rejects encrypted snapshots"
    )
    assert re.search(r'volume_type\s*=\s*"gp3"', t) and re.search(r"volume_size\s*=\s*25\b", t)
    assert re.search(r'ami_groups\s*=\s*\["all"\]', t), "the Launch Stack lane needs a public AMI"


def test_the_image_tag_must_be_pinned() -> None:
    assert 'var.image_tag != "latest"' in _hcl()


def test_the_bundle_is_shared_with_digitalocean_not_copied() -> None:
    t = _code(_HCL)
    assert '"${path.root}/../digitalocean/files/"' in t
    assert '"${path.root}/../digitalocean/scripts/01-provision.sh"' in t
    assert '"TRINITY_CLOUD=aws"' in t
    assert not (_AWS / "files").exists(), "a second files tree is how the DO copies drifted (#2380)"
    for rel in ("../digitalocean/files", "../digitalocean/scripts/01-provision.sh",
                "scripts/90-aws-cleanup.sh", "scripts/mp-submit.sh"):
        assert (_AWS / rel).exists(), rel


def test_scripts_run_as_root_on_an_ubuntu_login() -> None:
    t = _code(_HCL)
    assert 'ssh_username  = "ubuntu"' in t
    shells = re.findall(r'provisioner "shell" \{(.*?)\n  \}', t, re.S)
    scripted = [s for s in shells if "scripts" in s]
    assert scripted and all("sudo -E env {{ .Vars }} bash" in s for s in scripted), (
        "every scripted provisioner must run under sudo AND keep {{ .Vars }}: without it "
        "TRINITY_CLOUD / TRINITY_IMAGE_TAG never reach 01-provision.sh"
    )


def test_provisioner_order_wait_first_cleanup_last() -> None:
    t = _code(_HCL)
    wait = t.index("cloud-init status --wait")
    upload = t.index('provisioner "file"')
    bake = t.index("01-provision.sh")
    clean = t.index("90-aws-cleanup.sh")
    assert wait < upload < bake < clean
    assert 'provisioner "' not in t[clean:], (
        "the cleanup must be the last provisioner — anything after it can reintroduce "
        "what it removed"
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def test_cleanup_restores_the_first_boot_hook_cloud_init_clean_deletes() -> None:
    """`cloud-init clean` removes /var/lib/cloud/* except seed/, which includes
    scripts/per-instance/001-trinity. Without the restore every instance boots
    into an unconfigured machine and nothing fails the build."""
    c = _code(_CLEANUP)
    clean = c.index("cloud-init clean --logs --machine-id")
    save = c.index('cp -p "$HOOK" /dev/shm/001-trinity')
    restore = c.index('install -D -m 0755 /dev/shm/001-trinity "$HOOK"')
    check = c.index('[ -x "$HOOK" ] || fail')
    assert save < clean < restore < check


def test_cleanup_leaves_the_journal_to_journalctl() -> None:
    """Truncating live journal files corrupts them; journalctl vacuums them."""
    c = _code(_CLEANUP)
    assert "-path /var/log/journal -prune" in c
    assert "journalctl --vacuum-time" in c


def test_the_bakery_refuses_a_tag_without_aws_support() -> None:
    """Tags before #3004 reject `--cloud aws`; fail at the top of the build with
    the reason, not ten minutes in with start.sh's generic refusal."""
    bake = _code(_ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh")
    check = bake.index("digitalocean|aws) ;;' scripts/deploy/start.sh")
    assert check < bake.index("./scripts/deploy/start.sh --provision")
    assert "first release containing AWS support" in bake


def test_cleanup_removes_and_verifies_what_marketplace_checks() -> None:
    c = _code(_CLEANUP)
    for removal in ("-name authorized_keys -type f -delete", "rm -f /etc/ssh/ssh_host_*",
                    ".bash_history"):
        assert removal in c, removal
    for check in ("passwordauthentication no", "passwd -S root", "/etc/shadow",
                  "-name setup-claim", "/opt/trinity/.env", "/etc/machine-id",
                  "/etc/trinity/cloud"):
        assert check in c, f"the cleanup no longer checks {check!r}"
    assert re.search(r'if \[ "\$FAIL" != "0" \]; then.*?exit 1', c, re.S), (
        "a failed check must fail the build"
    )
    # sshd -T refuses to run without host keys, so the check precedes the delete.
    assert c.index("passwordauthentication no") < c.index("rm -f /etc/ssh/ssh_host_*")


def test_shell_scripts_parse() -> None:
    for p in (_CLEANUP, _SUBMIT):
        subprocess.run(["bash", "-n", str(p)], check=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_submit_is_a_no_op_without_a_product_id(tmp_path) -> None:
    r = subprocess.run(["bash", str(_SUBMIT)], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": _SYS_PATH})
    assert r.returncode == 0, r.stderr
    assert "not submitted" in r.stdout


def test_submit_defaults_to_a_dry_run() -> None:
    c = _code(_SUBMIT)
    assert "INTENT=VALIDATE" in c
    assert '[ "${TRINITY_AWS_SUBMIT:-}" = "apply" ] && INTENT=APPLY' in c


# ---------------------------------------------------------------------------
# MOTD on an AWS instance
# ---------------------------------------------------------------------------

def _motd(tmp_path: Path, source: str, cloud: str) -> str:
    state = tmp_path / "state"
    state.mkdir()
    (state / "public-ip").write_text("203.0.113.7\n")
    (state / "tls-status").write_text("ok\n")
    (state / "admin-credentials").write_text(f"source={source}\n")
    (state / "cloud").write_text(f"{cloud}\n")
    b = tmp_path / "bin"
    b.mkdir()
    (b / "curl").write_text("#!/bin/sh\nexit 7\n")
    (b / "curl").chmod(0o755)
    script = _MOTD.read_text().replace("STATE_DIR=/etc/trinity", f"STATE_DIR='{state}'", 1)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{b}:{_SYS_PATH}"})
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_motd_on_an_unclaimed_aws_instance_asks_for_the_instance_id(tmp_path):
    out = _motd(tmp_path, "instance-id", "aws")
    assert "instance ID" in out
    assert "label: aws-marketplace" in out and "AWS does not build or support Trinity." in out
    assert "DigitalOcean" not in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_motd_on_digitalocean_is_unchanged(tmp_path):
    out = _motd(tmp_path, "browser", "digitalocean")
    assert "label: do-marketplace" in out
    assert "DigitalOcean does not build or support Trinity." in out
