"""One installer, three callers — and a firewall rule with no list to maintain (#2380).

Provisioning a bare cloud VM for Trinity existed in THREE copies: the Packer
bakery (build time), the Packer first-boot script (per droplet), and a
hand-written script pasted into the DigitalOcean deploy doc. They had already
drifted — the ``DOCKER-USER`` DROP list in one of them was missing ``8081``
(#2281 review C1), so the login page answered plain HTTP past the certificate
and past the ``http→https`` redirect, on the very image whose headline design
note is that everything a user needs is served by Caddy on 80/443.

Both halves of that failure are structural, so both are guarded here:

1. **One implementation.** ``scripts/deploy/start.sh --provision`` owns machine
   setup (Docker, pinned Caddy, ufw, the firewall unit) and site setup (the
   instance's own IP, the Caddyfile, the certificate, the ``.env`` keys). The
   Packer scripts must CALL it, never re-implement it — a fourth copy is caught
   by asserting the packer tree installs no packages and writes no Caddyfile.

2. **No port list.** ``docker-firewall.sh`` no longer enumerates what to block;
   it drops everything entering a container and returns early only for replies
   to container-initiated connections and for traffic from Docker's own bridges.
   The replaced test policed the list against compose; there is no list now, so
   what is worth pinning is the ORDER — an ``ESTABLISHED`` rule after the
   ``DROP`` would close the ports and take the agents' outbound internet with
   them, which no test of "is the port blocked" would notice.

Pure stdlib: no docker daemon, no backend import.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_FIREWALL = _ROOT / "scripts" / "deploy" / "docker-firewall.sh"
_FIRSTBOOT = (
    _ROOT / "packer" / "digitalocean" / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
)
_BAKERY = _ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh"


def _code(path: Path) -> str:
    """Executable lines only. These files carry long explanatory comments that
    NAME what was removed ("the Docker/ufw gap", "DROP_PORTS=..."), which is the
    point of them — a grep over the whole file would flag the documentation of
    the fix as the bug."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_firstboot_calls_the_shared_installer_for_the_site_phase() -> None:
    body = _FIRSTBOOT.read_text()
    assert "scripts/deploy/start.sh" in body
    assert "--provision" in body and "--site-only" in body
    # The provenance travels as an ARGUMENT. Hardcoding it inside the installer
    # is what made a doc-driven install claim to be a marketplace one (#2380).
    assert "--provenance do-marketplace" in body


def test_bakery_calls_the_shared_installer_for_the_machine_phase() -> None:
    body = _BAKERY.read_text()
    assert "./scripts/deploy/start.sh --provision --cloud digitalocean --machine-only" in body


def test_packer_tree_does_not_re_implement_provisioning() -> None:
    """No fourth copy: the packer scripts install nothing and configure nothing."""
    for path in (_FIRSTBOOT, _BAKERY):
        body = _code(path)
        for forbidden in (
            "docker-ce",          # Docker install
            "caddy=",             # pinned Caddy install
            "/etc/caddy/Caddyfile",  # site configuration
            "acme_ca",            # certificate issuance
            "ufw ",               # host firewall
        ):
            assert forbidden not in body, f"{path.name} re-implements provisioning: {forbidden!r}"
    # The bakery still pulls images — that IS its job, and the one thing the
    # shared installer must not do at build time.
    assert "docker pull" in _code(_BAKERY)


def test_firewall_has_no_port_list() -> None:
    body = _code(_FIREWALL)
    assert "DROP_PORTS" not in body
    assert "multiport" not in body, "a --dports list is a maintained list by another name"


def test_firewall_returns_established_before_it_drops() -> None:
    """Order is the property. A DROP ahead of the conntrack RETURN closes the
    published ports *and* kills every container's outbound internet, because the
    replies arrive inbound on the public interface."""
    rules = [
        line.strip()
        for line in _FIREWALL.read_text().splitlines()
        if re.match(r"^\s*iptables -A TRINITY-FW\b", line)
    ]
    assert rules, "TRINITY-FW is no longer populated by -A rules"
    drop = next(i for i, r in enumerate(rules) if r.endswith("-j DROP"))
    established = next(i for i, r in enumerate(rules) if "RELATED,ESTABLISHED" in r)
    bridges = [i for i, r in enumerate(rules) if "-i docker0" in r or "-i br+" in r]
    assert established < drop, "container replies must RETURN before the DROP"
    assert bridges and max(bridges) < drop, "container-originated traffic must RETURN before the DROP"
    assert drop == len(rules) - 1, "the DROP must be the last rule in the chain"


def test_provision_is_off_by_default_and_refuses_a_workstation() -> None:
    """``--provision`` installs packages, resets the firewall and claims 80/443,
    so it must refuse anywhere that is not a root shell on a cloud VM. Run for
    real rather than grepped: the guard has to sit ahead of everything that
    writes, and only executing it proves that."""
    proc = subprocess.run(
        ["bash", str(_START), "--provision"],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
        timeout=60,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--provision:" in proc.stderr, proc.stderr
    # Whichever guard fires first on this host, none of them may have run the
    # install: no compose, no package manager, no .env.
    assert "Starting services" not in proc.stdout


def test_start_sh_is_still_syntactically_valid() -> None:
    subprocess.run([sys.executable and "bash", "-n", str(_START)], check=True)
