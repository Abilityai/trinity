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


def test_ufw_and_iptables_persistent_are_never_both_installed() -> None:
    """`ufw` Breaks `iptables-persistent`, so apt silently removes one (#2281).

    Restored from test_2281_firstboot_port_exposure.py, which 6104b0e5 deleted
    with the port list although this guard was never about the list. ufw's
    control file carries an unversioned ``Breaks: iptables-persistent,
    netfilter-persistent``: installing either to persist DOCKER-USER makes apt
    REMOVE ufw, and the install then dies at ``ufw --force reset`` with an error
    pointing at the wrong line. Machine setup now lives in start.sh, so every
    provisioning file is checked, not only the bakery.
    """
    for path in (_START, _BAKERY, _FIRSTBOOT, _FIREWALL):
        live = [
            line
            for line in _code(path).splitlines()
            if "iptables-persistent" in line or "netfilter-persistent" in line
        ]
        assert not live, (
            f"{path.name} references iptables-persistent/netfilter-persistent outside "
            f"a comment: {live}. ufw Breaks both, so installing one removes ufw. "
            "Reboot persistence is trinity-docker-firewall.service's job."
        )
    assert re.search(r"apt-get install\b[^\n]*\bufw\b", _code(_START)), (
        "start.sh --provision no longer installs ufw"
    )


def test_firewall_rules_are_reapplied_on_every_boot() -> None:
    """Without iptables-persistent, a unit is what survives a reboot (#2281).

    Restored from test_2281_firstboot_port_exposure.py (deleted in 6104b0e5).
    Rules applied once at provision time are gone after the first reboot, which
    silently reopens every Docker-published port — the reopening #2281 review I1
    already fixed once. start.sh --provision now writes the unit instead of the
    Packer tree shipping it, so its heredoc is what gets checked.
    """
    body = _code(_START)
    unit = re.search(
        r"trinity-docker-firewall\.service <<'?UNIT'?\n(.*?)\n\s*UNIT\n", body, re.S
    )
    assert unit, "start.sh --provision no longer writes the boot-time firewall unit"
    text = unit.group(1)
    assert 'local _fw="${PWD}/scripts/deploy/docker-firewall.sh"' in body
    assert "ExecStart=${_fw}" in text, "the unit does not run docker-firewall.sh"
    assert "After=docker.service" in text, (
        "the unit must be ordered after docker.service — DOCKER-USER does not "
        "exist until Docker creates it."
    )
    assert "WantedBy=multi-user.target" in text, "the unit is not enabled at boot"
    assert re.search(r"systemctl enable\b[^\n]*trinity-docker-firewall\.service", body), (
        "the unit is written but never enabled, so it never runs."
    )


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


def test_the_provisioned_caddyfile_gates_on_demand_tls_on_the_backend() -> None:
    """Adding a domain has to be finishable from the browser (#2380).

    Trinity runs in a container with no way to rewrite a Caddyfile or reload a
    web server, so for a while the honest instruction was a root shell command —
    which a non-engineer following a deploy guide does not have in the loop. Caddy
    asking the backend inverts the direction without moving any privilege.

    The `ask` gate is not optional garnish. `on_demand` without it makes the
    instance obtain a certificate for ANY hostname anyone points at its address,
    until the Let's Encrypt account is rate-limited and the operator's own
    renewals start failing."""
    body = _code(_START)
    caddyfile = re.search(r"<<CADDY\n(.*?)\nCADDY\n", body, re.S)
    assert caddyfile, "start.sh no longer writes a Caddyfile"
    caddy = caddyfile.group(1)

    assert "on_demand_tls" in caddy, "no on-demand TLS — adding a domain needs a host shell again"
    assert "ask http://127.0.0.1:8000/api/public/tls-allowed" in caddy, (
        "on-demand TLS has no ask gate"
    )

    # The catch-all site is what a saved domain actually lands on, and it is
    # asserted as a BLOCK: `on_demand` anywhere in the file is satisfied by the
    # global `on_demand_tls` option above, so deleting this site — which is the
    # whole feature — passed the previous spelling of this test.
    site = re.search(r"^https:// \{\n(.*?)^\}", caddy, re.S | re.M)
    assert site, "the catch-all https:// site is gone — a saved domain reaches nothing"
    assert re.search(r"tls \{[^}]*\bon_demand\b", site.group(1), re.S), (
        "the catch-all site does not request an on-demand certificate"
    )
    assert "reverse_proxy 127.0.0.1:8081" in site.group(1), (
        "the catch-all site serves no backend"
    )

    # The bare-IP site keeps its own short-lived certificate: it is what the
    # instance answers to before any domain exists.
    ip_site = re.search(r"^https://\$\{ip\} \{\n(.*?)^\}", caddy, re.S | re.M)
    assert ip_site, "the bare-IP site is gone"
    assert "profile shortlived" in ip_site.group(1), (
        "the IP site no longer asks for a short-lived certificate"
    )


def test_the_ask_endpoint_allows_exactly_the_configured_host() -> None:
    """The whole security model of on-demand TLS is this allowlist.

    Substring matching is the trap worth naming: `evil-example.com` contains
    `example.com`, so a naive check hands an attacker a certificate request for
    their own name from someone else's server."""
    src = (_ROOT / "src" / "backend" / "routers" / "public.py").read_text()
    assert "/tls-allowed" in src, "the ask endpoint Caddy calls does not exist"
    section = src[src.index("async def tls_allowed") : src.index("async def tls_allowed") + 3000]
    assert "urlparse" in section, "the configured URL must be parsed, not substring-matched"
    assert "requested != allowed" in section, "the comparison is not an exact match"
    # Fails closed on every branch that cannot prove the name.
    assert section.count("404") >= 4, "a refusal path is missing"


# ---------------------------------------------------------------------------
# The ask gate, executed rather than grepped.
# ---------------------------------------------------------------------------
# Exec-sliced out of routers/public.py, the pattern test_926/test_2380 already
# use: importing that router pulls the whole backend graph, and this is a pure
# function over one settings read. A static check cannot tell an exact match
# from a substring match, and that difference is the entire security model.


def _load_ask_gate(configured: str, raises: bool = False):
    import asyncio
    import types

    src = (_ROOT / "src" / "backend" / "routers" / "public.py").read_text()
    start = src.index("async def tls_allowed")
    end = src.index("\n\n\n", start)
    snippet = src[start:end]

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail

    def _get_url():
        if raises:
            raise RuntimeError("settings unavailable")
        return configured

    ns: dict = {
        "HTTPException": _HTTPException,
        "settings_service": types.SimpleNamespace(get_public_chat_url=_get_url),
    }
    exec(snippet, ns)
    fn = ns["tls_allowed"]

    def call(domain: str):
        try:
            return asyncio.run(fn(domain=domain)), None
        except _HTTPException as e:
            return None, e.status_code

    return call


def test_ask_gate_authorises_only_the_configured_hostname() -> None:
    ask = _load_ask_gate("https://trinity.example.com")

    ok, status = ask("trinity.example.com")
    assert ok and ok["authorized"] is True, status

    for hostile in (
        "example.com",                      # parent domain
        "evil-trinity.example.com",         # prefix games
        "trinity.example.com.attacker.net",  # suffix games
        "trinity.example.com.evil",
        "attacker.net",
        "",
    ):
        ok, status = ask(hostile)
        assert ok is None and status == 404, f"issued for {hostile!r}"


def test_ask_gate_is_case_and_trailing_dot_insensitive() -> None:
    """A DNS name is case-insensitive and may arrive fully qualified with a
    trailing dot. Refusing those would look like a random failure to issue."""
    ask = _load_ask_gate("https://Trinity.Example.COM")
    for spelling in ("trinity.example.com", "TRINITY.example.com", "trinity.example.com."):
        ok, _ = ask(spelling)
        assert ok, f"refused {spelling!r}, which is the same name"


def test_ask_gate_refuses_when_nothing_is_configured_or_readable() -> None:
    """Fails CLOSED. An instance with no Public URL set must not be a
    certificate-issuing service for whoever points DNS at it, and a failed
    settings read must not authorise a name it could not verify."""
    unset, _ = _load_ask_gate("")("anything.example.com")
    assert unset is None

    blew_up, status = _load_ask_gate("https://trinity.example.com", raises=True)("trinity.example.com")
    assert blew_up is None and status == 404
