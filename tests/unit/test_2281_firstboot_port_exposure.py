"""Static guard: the 1-Click DROP list covers every port compose publishes (#2281).

Bug (#2281 review C1): first boot moves the SPA to ``FRONTEND_PORT=8081`` so Caddy
can own 80/443 and terminate TLS in front of it — but the ``DOCKER-USER`` DROP list
that closes the Docker-past-ufw gap listed ``8000,8080,8686`` and the OTel ports and
*not* 8081. ``docker-compose.hosted.yml`` publishes the frontend with the short
syntax ``"${FRONTEND_PORT:-80}:8080"``, which binds ``0.0.0.0``, so the login page
answered plain HTTP on ``http://<droplet-ip>:8081`` — past the certificate and past
the ``http→https`` permanent redirect, on the image whose headline design note is
that everything a user needs is served by Caddy on 80/443.

The list is hand-maintained against a compose file in another directory, which is
this repo's most-shipped bug shape (#1039, #1056, #1707, #1871, #2381): a port is
added to one file and never reaches the other, and nothing fails — the droplet
boots, Trinity works, and one more service is quietly on the public internet.

``packer build`` cannot catch it either: it is deferred to the first release with
GHCR images, and even then the rule is *installed* correctly — it simply does not
cover the port. So the guard is repo-side, in the existing unit job.

Pure stdlib + PyYAML: no docker daemon, no backend import.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_HOSTED = _ROOT / "docker-compose.hosted.yml"
_FIRSTBOOT = (
    _ROOT / "packer" / "digitalocean" / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
)
# The DROP list and the iptables calls moved out of firstboot.sh into a script
# shared with trinity-docker-firewall.service, so the rules survive a reboot
# without iptables-persistent (which apt cannot install beside ufw). FRONTEND_PORT
# is still first boot's, so this test reads both files.
_FIREWALL = (
    _ROOT / "packer" / "digitalocean" / "files" / "opt" / "trinity-firstboot"
    / "docker-firewall.sh"
)

# `DROP_PORTS=8000,8080,8081,...` — one variable feeds both the -C probe and the
# -I insert, so there is exactly one list to read.
_DROP_RE = re.compile(r"^DROP_PORTS=([0-9,]+)\s*$", re.MULTILINE)
# `set_env FRONTEND_PORT 8081`
_FRONTEND_PORT_RE = re.compile(r"^set_env\s+FRONTEND_PORT\s+(\d+)\s*$", re.MULTILINE)
# `${FRONTEND_PORT:-80}:8080` / `8000:8000` / `127.0.0.1:8686:8686`
_HOST_PORT_RE = re.compile(r"^(?:(?P<ip>[\d.]+):)?(?P<host>\$\{[^}]+\}|\d+):\d+")


@pytest.fixture(scope="module")
def firstboot() -> str:
    return _FIRSTBOOT.read_text()


@pytest.fixture(scope="module")
def firewall() -> str:
    return _FIREWALL.read_text()


@pytest.fixture(scope="module")
def drop_ports(firewall: str) -> set[int]:
    m = _DROP_RE.search(firewall)
    assert m, "docker-firewall.sh no longer declares a single DROP_PORTS list"
    return {int(p) for p in m.group(1).split(",")}


@pytest.fixture(scope="module")
def frontend_port(firstboot: str) -> int:
    m = _FRONTEND_PORT_RE.search(firstboot)
    assert m, "firstboot.sh no longer sets FRONTEND_PORT"
    return int(m.group(1))


def _published_host_ports(frontend_port: int) -> dict[str, set[int]]:
    """Host ports each hosted-compose service publishes, resolved for this droplet.

    Only bindings that reach an external interface count: an explicit ``127.0.0.1:``
    host IP is already unreachable from the internet and needs no DROP rule.
    """
    compose = yaml.safe_load(_HOSTED.read_text())
    out: dict[str, set[int]] = {}
    for name, svc in (compose.get("services") or {}).items():
        for entry in svc.get("ports") or []:
            if isinstance(entry, dict):  # long syntax
                host_ip = entry.get("host_ip")
                published = entry.get("published")
                if host_ip and host_ip.startswith("127."):
                    continue
                if published is not None:
                    out.setdefault(name, set()).add(int(published))
                continue
            m = _HOST_PORT_RE.match(str(entry))
            assert m, f"unparsed port entry on {name}: {entry!r}"
            if (m.group("ip") or "").startswith("127."):
                continue
            host = m.group("host")
            if host.startswith("${"):
                # The only variable in play is FRONTEND_PORT, and first boot sets
                # it explicitly — so the value that matters is the droplet's, not
                # the `:-80` default any other install would get.
                assert "FRONTEND_PORT" in host, f"unknown port variable on {name}: {host}"
                out.setdefault(name, set()).add(frontend_port)
            else:
                out.setdefault(name, set()).add(int(host))
    return out


def test_every_published_port_is_dropped(drop_ports: set[int], frontend_port: int) -> None:
    published = _published_host_ports(frontend_port)
    missing = {
        name: sorted(ports - drop_ports)
        for name, ports in published.items()
        if ports - drop_ports
    }
    assert not missing, (
        "docker-compose.hosted.yml publishes these host ports on 0.0.0.0 and the "
        "1-Click DOCKER-USER DROP list does not cover them, so they are reachable "
        f"from the internet on every droplet: {missing}. Add them to DROP_PORTS in "
        "packer/digitalocean/files/opt/trinity-firstboot/docker-firewall.sh."
    )


def test_frontend_port_is_dropped(drop_ports: set[int], frontend_port: int) -> None:
    """The regression itself, named — the SPA is the one that must not leak.

    Subsumed by the parity test above, kept separate because a future refactor of
    the compose parsing must not be able to quietly stop covering this case.
    """
    assert frontend_port in drop_ports, (
        f"first boot moves the SPA to :{frontend_port} so Caddy owns 80/443, but "
        "the DROP list leaves it exposed — plain HTTP, no redirect, no certificate."
    )


def test_probe_and_insert_share_one_list(firewall: str) -> None:
    """A -C probe that differs from the -I insert never matches its own rule.

    It would then insert a duplicate DROP on every re-run of first boot, which is
    the documented recovery step for a failed boot.
    """
    dports = re.findall(r'--dports\s+(\S+)', firewall)
    assert dports, "docker-firewall.sh no longer installs a multiport DROP rule"
    assert set(dports) == {'"$DROP_PORTS"'}, (
        f"the DROP rule's port list is spelled more than one way: {sorted(set(dports))}"
    )


def test_ufw_and_iptables_persistent_are_never_both_installed() -> None:
    """`ufw` Breaks `iptables-persistent`, so apt silently removes one (#2281).

    The build installed ufw, then installed iptables-persistent to make the
    DOCKER-USER rules survive a reboot. `ufw`'s control file carries an
    unversioned ``Breaks: iptables-persistent, netfilter-persistent``, so apt
    resolved that by REMOVING ufw — reporting it plainly and continuing — and the
    build died ~90 lines later at ``ufw --force reset`` with "command not found",
    five and a half minutes in, after pulling all five images.

    Both halves of that are worth guarding. Reintroducing the package would
    uninstall the firewall DigitalOcean's own img_check.sh looks for, and the
    error it eventually produces points at the wrong line entirely.
    """
    provision = (
        _ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh"
    ).read_text()
    live = [
        line
        for line in provision.splitlines()
        if not line.lstrip().startswith("#")
        and ("iptables-persistent" in line or "netfilter-persistent" in line)
    ]
    assert not live, (
        "01-provision.sh references iptables-persistent/netfilter-persistent "
        f"outside a comment: {live}. `ufw` Breaks both, so installing one removes "
        "ufw. Reboot persistence is trinity-docker-firewall.service's job."
    )
    assert "ufw" in provision, "01-provision.sh no longer installs ufw"


def test_firewall_rules_are_reapplied_on_every_boot() -> None:
    """Without iptables-persistent, a unit is what survives a reboot.

    cloud-init's per-instance hook runs once per droplet, so first boot alone
    leaves the ports open again after the first reboot — the same silent
    reopening #2281 review I1 already fixed once.
    """
    unit = (
        _ROOT / "packer" / "digitalocean" / "files" / "etc" / "systemd" / "system"
        / "trinity-docker-firewall.service"
    )
    assert unit.is_file(), "the boot-time firewall unit is missing"
    text = unit.read_text()
    assert "ExecStart=/opt/trinity-firstboot/docker-firewall.sh" in text
    assert "After=docker.service" in text, (
        "the unit must be ordered after docker.service — DOCKER-USER does not "
        "exist until Docker creates it."
    )
    assert "WantedBy=multi-user.target" in text, "the unit is not enabled at boot"

    provision = (
        _ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh"
    ).read_text()
    assert "systemctl enable trinity-docker-firewall.service" in provision, (
        "the unit is shipped but never enabled, so it never runs."
    )
