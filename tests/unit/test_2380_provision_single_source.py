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
_SET_DOMAIN = _ROOT / "scripts" / "deploy" / "set-domain.sh"
_CARD = (
    _ROOT / "src" / "frontend" / "src" / "components" / "onboarding" / "HardeningGuide.vue"
)


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


def _firewall_rules() -> list[str]:
    return [
        line.strip()
        for line in _FIREWALL.read_text().splitlines()
        if re.match(r"^\s*iptables -A TRINITY-FW\b", line)
    ]


def test_firewall_returns_established_before_it_drops() -> None:
    """Order is the property. A catch-all DROP ahead of the conntrack RETURN
    closes the published ports *and* kills every container's outbound internet,
    because the replies arrive inbound on the public interface."""
    rules = _firewall_rules()
    assert rules, "TRINITY-FW is no longer populated by -A rules"
    catch_all = next(i for i, r in enumerate(rules) if r == "iptables -A TRINITY-FW -j DROP")
    established = next(i for i, r in enumerate(rules) if "RELATED,ESTABLISHED" in r)
    bridges = [i for i, r in enumerate(rules) if "-i docker0" in r or "-i br+" in r]
    assert established < catch_all, "container replies must RETURN before the DROP"
    assert bridges and max(bridges) < catch_all, "container-originated traffic must RETURN before the DROP"
    assert catch_all == len(rules) - 1, "the catch-all DROP must be the last rule in the chain"


def test_containers_cannot_reach_the_cloud_metadata_service() -> None:
    """The droplet's user-data is served verbatim from link-local for the life of
    the machine, and on a script-installed instance it carries the Trinity admin
    password and the operator's Claude subscription token. An agent container is
    exactly the untrusted-code case, so the range is blocked outbound.

    Two things are asserted, and the ordering one is the load-bearing half: the
    DROP has to sit AHEAD of the bridge RETURNs, or container traffic returns out
    of the chain before ever reaching it and the rule is decoration."""
    rules = _firewall_rules()
    link_local = [i for i, r in enumerate(rules) if "169.254.0.0/16" in r and r.endswith("-j DROP")]
    assert link_local, "no link-local DROP — containers can read the instance metadata service"
    bridges = [i for i, r in enumerate(rules) if "-i docker0" in r or "-i br+" in r]
    assert bridges, "the bridge RETURNs are gone; this test's ordering claim is meaningless"
    assert max(link_local) < min(bridges), (
        "the link-local DROP must precede the bridge RETURNs, or container "
        "traffic RETURNs before it is ever evaluated"
    )
    # The RFC 3927 range, not the well-known single host: the property is
    # "link-local, host-adjacent, not routable", and every cloud's metadata
    # endpoint lives in it.
    assert not any(
        r.endswith("-j DROP") and "169.254.169.254" in r and "/16" not in r for r in rules
    ), "block the RFC 3927 range, not one magic address"


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


def test_the_card_names_a_command_that_exists() -> None:
    """The hardening card tells the operator to run a specific path on the
    server. That is a cross-tree reference from a Vue template to a shell script,
    which nothing else checks — move or rename the script and the card keeps
    confidently printing a command that does not exist, on a first login, as the
    one instruction it gives.

    Asserted from the CARD's text rather than a constant, because the card is
    what the operator copies."""
    if not _CARD.exists():  # OSS checkout without the frontend tree
        return
    card = _CARD.read_text()
    assert "set-domain.sh" in card, "the card no longer names the domain command"
    # The path as printed, minus the install prefix the droplet uses.
    assert "/opt/trinity/scripts/deploy/set-domain.sh" in card
    assert _SET_DOMAIN.exists(), (
        "the card tells operators to run scripts/deploy/set-domain.sh and it is not there"
    )


def test_set_domain_puts_the_old_config_back_when_it_fails() -> None:
    """Every failure path after the rewrite must restore. A half-applied domain
    switch is strictly worse than not starting: the operator had a working
    instance on an IP certificate, and would be left with a name that does not
    resolve to a certificate and an IP that no longer has a site block."""
    body = _SET_DOMAIN.read_text()
    assert "restore()" in body, "no restore path"
    # A backup is taken before the file is written, not after.
    backup_at = body.index('cp -p /etc/caddy/Caddyfile "$BACKUP"')
    write_at = body.index("cat > /etc/caddy/Caddyfile")
    assert backup_at < write_at, "the Caddyfile is rewritten before it is backed up"
    # Each post-rewrite failure calls restore before dying.
    for failure in ("caddy validate", "would not reload", "No valid certificate"):
        assert failure in body
    assert body.count("restore") >= 4, "a failure path is missing its restore"


def test_set_domain_refuses_before_it_touches_anything_if_dns_is_wrong() -> None:
    """The whole point of the preflight: issuance validates over the name, so a
    stale A record turns a working instance into a broken one. The check has to
    precede the backup, or 'refuses' is just 'reverts'."""
    body = _SET_DOMAIN.read_text()
    dns_at = body.index("getent ahostsv4")
    backup_at = body.index('cp -p /etc/caddy/Caddyfile "$BACKUP"')
    assert dns_at < backup_at, "the DNS preflight runs after the config is touched"
