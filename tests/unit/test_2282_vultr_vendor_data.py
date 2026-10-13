"""The Vultr imageless install: one installer, a fourth caller, and a boot that
says what happened (#2282).

Vultr's Marketplace app is **imageless** — Vultr runs
``scripts/deploy/vultr-vendor-data.sh`` once, as root, on a clean Ubuntu, and
there is no snapshot carrying anything. Three properties follow, and each one
here failed somewhere before:

1. **No fourth copy of provisioning.** The script CALLS
   ``start.sh --provision`` (PROV-012). The DigitalOcean lane shipped three
   drifting copies of that logic and a port missing from one of them (#2281
   review C1); `test_2380_provision_single_source.py` now scans this file too.

2. **The environment start.sh actually reads.** ``TRINITY_IMAGE_TAG`` must be
   EXPORTED: `start.sh` persists it into `.env` only when it is in the
   environment, and `resolve_image_tag` otherwise falls back to the `latest`
   docker tag — turning every later ``--hosted`` run into an unplanned upgrade.
   Same for ``ADMIN_PASSWORD_SOURCE=browser`` (ent#580), which is what keeps
   `/setup` open for the first visitor and keeps every password out of Vultr's
   metadata service.

3. **A failed boot is visible.** A marketplace customer's only surface is the
   web UI, and a failed boot leaves nothing answering on it. `set -e` exits
   silently, so the marker is written from a trap and the banner is installed
   before anything can fail.

Pure stdlib: no docker, no backend import, no network.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "scripts" / "deploy" / "vultr-vendor-data.sh"
_START = _ROOT / "scripts" / "deploy" / "start.sh"


def _code(path: Path) -> str:
    """Executable lines only — these files explain what they deliberately do
    NOT do, so a grep over the comments flags the documentation as the bug."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_the_script_is_syntactically_valid_and_executable() -> None:
    assert _VENDOR.stat().st_mode & 0o111, "vendor data must be executable"
    subprocess.run(["bash", "-n", str(_VENDOR)], check=True, capture_output=True)


def test_it_calls_the_shared_installer_with_the_marketplace_provenance() -> None:
    body = _code(_VENDOR)
    assert "./scripts/deploy/start.sh" in body
    for flag in ("--provision", "--cloud vultr", "--provenance vultr-marketplace",
                 "--hosted", "--unattended"):
        assert flag in body, f"vendor data must pass {flag!r}"


def test_it_does_not_re_implement_provisioning() -> None:
    """No fourth copy: no package install, no web server, no firewall here."""
    body = _code(_VENDOR)
    for forbidden in ("docker-ce", "caddy=", "/etc/caddy/Caddyfile", "acme_ca", "ufw "):
        assert forbidden not in body, f"vendor data re-implements provisioning: {forbidden!r}"


def test_the_image_tag_is_exported_not_merely_assigned() -> None:
    assert re.search(r"^export TRINITY_IMAGE_TAG$", _code(_VENDOR), re.M), (
        "start.sh only persists TRINITY_IMAGE_TAG when it is in the ENVIRONMENT; "
        "a bare assignment silently installs the `latest` images instead"
    )


def test_the_release_is_resolved_and_pre_releases_are_excluded() -> None:
    body = _code(_VENDOR)
    assert "releases/latest" in body, "must resolve the latest stable release"
    assert "releases?" not in body and "/releases\"" not in body, (
        "the plain releases list includes pre-releases; /releases/latest does not"
    )
    # A stale fallback is the defect being avoided: a successful install of the
    # wrong release is invisible to the customer and to us.
    assert "exit 1" in body


def test_no_admin_password_is_generated_or_carried() -> None:
    body = _code(_VENDOR)
    assert "ADMIN_PASSWORD_SOURCE=browser" in body
    assert not re.search(r"\bADMIN_PASSWORD=", body), (
        "ent#580: the first visitor claims the instance; a password here would "
        "also sit in Vultr's metadata service for the life of the machine"
    )
    for secret in ("sk-ant-", "setup-token", "TOKEN=", "API_KEY="):
        assert secret not in body, f"vendor data must carry no credential: {secret!r}"


def test_the_clone_is_guarded_so_a_retry_resumes() -> None:
    body = _code(_VENDOR)
    assert '[ -d "${TRINITY_DIR}/.git" ] ||' in body, (
        "git clone into an existing directory fails; a retry after a failed "
        "boot must resume instead"
    )


def test_the_umask_lets_uid_1000_read_the_checkout() -> None:
    body = _code(_VENDOR)
    assert "umask 022" in body
    assert "umask 077" not in body, (
        "the backend runs as UID 1000 and bind-mounts ./config read-only; a "
        "root-only clone empties the template catalog with no error"
    )


def test_a_failed_boot_writes_the_marker_and_the_banner_reads_it(tmp_path: Path) -> None:
    """Behavioural: run the real script against a stub checkout whose start.sh
    fails, then run the banner it installed."""
    state, trinity, motd = tmp_path / "state", tmp_path / "opt", tmp_path / "motd"
    (trinity / "scripts" / "deploy").mkdir(parents=True)
    (trinity / ".git").mkdir()
    start = trinity / "scripts" / "deploy" / "start.sh"
    start.write_text("#!/bin/bash\necho 'boom' >&2\nexit 1\n")
    start.chmod(0o755)

    body = _VENDOR.read_text().replace("/etc/update-motd.d", str(motd))
    script = tmp_path / "vendor-data.sh"
    script.write_text(body)

    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TRINITY_STATE_DIR": str(state),
            "TRINITY_DIR": str(trinity),
            "TRINITY_LOG": str(tmp_path / "install.log"),
            "TRINITY_IMAGE_TAG": "v0.0.0-test",  # no network resolve in a test
        },
    )

    assert proc.returncode != 0
    assert (state / "firstboot-failed").exists(), "a failed boot must leave the marker"
    assert "FAILED" in proc.stdout

    # The installed banner hardcodes /etc/trinity, which no test may touch.
    probe = tmp_path / "banner.sh"
    probe.write_text((motd / "99-trinity").read_text().replace("/etc/trinity", str(state)))
    banner = subprocess.run(
        ["bash", str(probe)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    rendered = banner.stdout
    assert "First boot did NOT complete" in rendered
    assert "/var/log/trinity-install.log" in rendered
    # Never a password, on any path.
    assert "password you supplied" not in rendered


def test_a_successful_boot_marks_itself_ready(tmp_path: Path) -> None:
    """Without the marker every finished install still reads "Still installing"."""
    state, trinity, motd = tmp_path / "state", tmp_path / "opt", tmp_path / "motd"
    (trinity / "scripts" / "deploy").mkdir(parents=True)
    (trinity / ".git").mkdir()
    start = trinity / "scripts" / "deploy" / "start.sh"
    start.write_text("#!/bin/bash\nexit 0\n")
    start.chmod(0o755)
    (state).mkdir()
    (state / "public-ip").write_text("203.0.113.10\n")

    script = tmp_path / "vendor-data.sh"
    script.write_text(_VENDOR.read_text().replace("/etc/update-motd.d", str(motd)))
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "TRINITY_STATE_DIR": str(state),
            "TRINITY_DIR": str(trinity),
            "TRINITY_LOG": str(tmp_path / "install.log"),
            "TRINITY_IMAGE_TAG": "v0.0.0-test",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert (state / "ready").exists(), "a finished install must mark itself ready"
    assert not (state / "firstboot-failed").exists()
    assert "Trinity is ready at" in proc.stdout


def test_the_banner_distinguishes_installing_from_ready_and_failed(tmp_path: Path) -> None:
    """First boot pulls ~1.6 GB. For those minutes the web UI refuses
    connections, and a banner that prints a confident URL reads as "broken"."""
    motd, state = tmp_path / "motd", tmp_path / "state"
    motd.mkdir(), state.mkdir()
    src = _VENDOR.read_text()
    banner_body = src[src.index("cat > /etc/update-motd.d/99-trinity <<'BANNER'\n") :].split("\nBANNER\n")[0]
    banner_body = banner_body.split("\n", 1)[1].replace("/etc/trinity", str(state))
    probe = tmp_path / "banner.sh"
    probe.write_text(banner_body)

    def render() -> str:
        return subprocess.run(
            ["bash", str(probe)], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        ).stdout

    (state / "public-ip").write_text("203.0.113.10\n")
    mid = render()
    assert "Still installing" in mid
    assert "not answering yet" in mid, "the URL must be marked as not serving yet"

    (state / "ready").touch()
    (state / "tls-status").write_text("ok\n")
    done = render()
    assert "Still installing" not in done
    assert "https://203.0.113.10" in done

    (state / "firstboot-failed").touch()
    failed = render()
    assert "First boot did NOT complete" in failed, "failure outranks ready"


def test_a_retry_clears_the_previous_verdict(tmp_path: Path) -> None:
    """Stale markers make the banner describe the previous boot."""
    body = _code(_VENDOR)
    assert 'rm -f "${STATE_DIR}/ready" "${STATE_DIR}/firstboot-failed"' in body


def test_the_resolver_dependencies_are_installed_not_assumed() -> None:
    """A missing curl would otherwise surface as "could not resolve the latest
    release" — pointing at GitHub for a problem on the machine."""
    body = _code(_VENDOR)
    assert "command -v curl" in body and "command -v git" in body
    assert body.index("command -v curl") < body.index("releases/latest"), (
        "curl must be ensured BEFORE the resolve that needs it"
    )


def test_the_banner_is_installed_before_anything_can_fail() -> None:
    # Line-wise: "./scripts/deploy/start.sh" also appears inside the banner's
    # own heredoc (the retry hint), so a whole-file index compares the wrong two.
    lines = [line.strip() for line in _code(_VENDOR).splitlines()]
    call = lines.index("install_banner")
    install = next(i for i, line in enumerate(lines) if line.startswith("./scripts/deploy/start.sh"))
    assert call < install, (
        "the banner must exist before the install can fail, or a failed boot "
        "has no surface at all"
    )


# --- start.sh: Vultr is a supported cloud -------------------------------------

def _cloud_guard(cloud: str) -> subprocess.CompletedProcess[str]:
    """Run the --cloud acceptance branch alone. A full run dies earlier on the
    root check, which would make every case look identical."""
    src = _START.read_text()
    # rindex: the FIRST `case "$PROVISION_CLOUD" in` is provision_metadata_ip's.
    guard = src[src.rindex('    case "$PROVISION_CLOUD" in') : src.index("    # The metadata service is the")]
    harness = (
        'provision_die() { echo "die: $1" >&2; exit 1; }\n'
        f'PROVISION_CLOUD="{cloud}"\n' + guard + '\necho accepted\n'
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


@pytest.mark.parametrize("cloud", ["digitalocean", "vultr"])
def test_supported_clouds_are_accepted(cloud: str) -> None:
    assert _cloud_guard(cloud).returncode == 0, f"--cloud {cloud} must be accepted"


@pytest.mark.parametrize("cloud", ["", "hetzner"])
def test_other_clouds_are_refused_and_the_message_lists_both(cloud: str) -> None:
    proc = _cloud_guard(cloud)
    assert proc.returncode != 0
    assert "digitalocean, vultr" in proc.stderr


def test_the_vultr_metadata_read_needs_nothing_installed() -> None:
    """The IP read doubles as the "am I on a cloud VM" guard and runs BEFORE
    provision_machine installs packages. Stock Ubuntu has curl, not jq."""
    src = _code(_START)
    fn = src[src.index("provision_metadata_ip() {") : src.index("provision_default_provenance() {")]
    assert "http://169.254.169.254/v1/interfaces/0/ipv4/address" in fn, (
        "Vultr's plain-text value endpoint — /v1.json would need a JSON parser"
    )
    assert "jq" not in fn and "python3" not in fn
    assert "v1.json" not in fn


def test_every_apt_call_in_the_machine_phase_waits_for_the_dpkg_lock() -> None:
    """On an imageless first boot, Ubuntu's own apt-daily/unattended-upgrades
    hold the lock; a non-interactive apt-get exits 100 under `set -e`."""
    src = _START.read_text()
    machine = src[src.index("provision_machine() {") : src.index("provision_private_cidrs() {")]
    assert "DPkg::Lock::Timeout" in _code(_START)
    for line in machine.splitlines():
        stripped = line.strip()
        if stripped.startswith("apt-get "):
            raise AssertionError(f"bare apt-get in provision_machine: {stripped!r}")
