"""trinity-enterprise#873 — the proxy trust is wired where the backend actually runs.

The bug shipped as a packaging gap: the image CMD carried uvicorn proxy flags
that every compose `command:` silently replaced, so what the code assumed and
what ran differed. These tests therefore read the EFFECTIVE configuration —
`docker compose config` output, rendered with a placeholder environment — and
feed the rendered backend environment to the real configuration loader, so the
reserved addresses the network hands out and the addresses the backend trusts
are checked against each other rather than each against a literal.

The raw-YAML checks run everywhere; the rendered checks need the Compose CLI
(no daemon) and skip without it — CI's container-security workflow renders the
same files.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from utils import client_ip as ci  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_PROD_LIKE = ("docker-compose.prod.yml", "docker-compose.hosted.yml")
_NOT_ON_INGRESS = ("redis", "scheduler", "vector", "logs-init", "otel-collector", "postgres")


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")


def _render(name: str, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
    env.update({
        "ADMIN_PASSWORD": secrets.token_urlsafe(18) + "Aa1!",
        "REDIS_PASSWORD": secrets.token_hex(24),
        "REDIS_BACKEND_PASSWORD": secrets.token_hex(24),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "TUNNEL_TOKEN": "placeholder",
    })
    env.update(extra)
    proc = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", name, "--profile", "tunnel",
         "config", "--format", "json"],
        cwd=_ROOT, capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _command(svc: dict) -> list:
    cmd = svc.get("command")
    return cmd.split() if isinstance(cmd, str) else list(cmd or [])


def _env(svc: dict) -> dict:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env)
    return env


def _ingress_net(cfg: dict) -> tuple:
    for key, net in cfg["networks"].items():
        if net.get("name") == "trinity-proxy-network":
            return key, net
    raise AssertionError("no trinity-proxy-network in the rendered file")


# ---------------------------------------------------------------------------
# Launchers: uvicorn's own forwarded-header processing is OFF everywhere
# ---------------------------------------------------------------------------

def _assert_launcher(cmd: list, where: str) -> None:
    assert "--no-proxy-headers" in cmd, f"{where}: uvicorn proxy processing is not explicitly off"
    assert "--proxy-headers" not in cmd, where
    assert not any(c.startswith("--forwarded-allow-ips") for c in cmd), where


def test_the_image_cmd_disables_uvicorn_forwarding():
    text = (_ROOT / "docker" / "backend" / "Dockerfile").read_text()
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", text, re.M).group(1))
    _assert_launcher(cmd, "docker/backend/Dockerfile CMD")


@pytest.mark.parametrize("name", ["docker-compose.yml", *_PROD_LIKE,
                                  "scripts/dev/aauth_prototype/compose.b.yml"])
def test_every_compose_backend_command_disables_uvicorn_forwarding(name):
    cfg = yaml.safe_load((_ROOT / name).read_text())
    _assert_launcher(_command(cfg["services"]["backend"]), name)


def test_the_python_launcher_disables_uvicorn_forwarding():
    text = (_ROOT / "src" / "backend" / "main.py").read_text()
    run = re.search(r"uvicorn\.run\((.*)\)", text).group(1)
    assert "proxy_headers=False" in run, run


# ---------------------------------------------------------------------------
# prod / hosted: the rendered topology and the backend's trust agree
# ---------------------------------------------------------------------------

@_needs_compose
@pytest.mark.parametrize("name", _PROD_LIKE)
@pytest.mark.parametrize("prefix,extra", [
    ("172.30.0", {}),
    ("172.31.40", {"TRINITY_PROXY_PREFIX": "172.31.40", "TRUSTED_PROXIES": "192.0.2.10",
                   "FRONTEND_BIND_ADDRESS": "127.0.0.1", "FRONTEND_PORT": "8081",
                   "CADDY_PROXY_PORT": "28082"}),
], ids=["defaults", "non-default"])
def test_rendered_ingress_topology_matches_the_backend_trust(name, prefix, extra):
    cfg = _render(name, **extra)
    svcs = cfg["services"]
    key, net = _ingress_net(cfg)
    ipam = net["ipam"]["config"][0]
    assert ipam == {"subnet": f"{prefix}.0/24", "ip_range": f"{prefix}.128/25",
                    "gateway": f"{prefix}.1"}, ipam

    def addr(svc):
        return (svcs[svc]["networks"].get(key) or {}).get("ipv4_address")

    # Reserved, outside the dynamic pool; the MCP server draws from the pool.
    assert (addr("frontend"), addr("cloudflared"), addr("backend")) == (
        f"{prefix}.2", f"{prefix}.3", f"{prefix}.4")
    assert key in svcs["mcp-server"]["networks"] and addr("mcp-server") is None
    pool = ipaddress.ip_network(ipam["ip_range"])
    for svc in ("frontend", "cloudflared", "backend"):
        assert ipaddress.ip_address(addr(svc)) not in pool, svc

    # The frontend left the agent network: no agent can be handed its address.
    assert list(svcs["frontend"]["networks"]) == [key]
    for svc in _NOT_ON_INGRESS:
        if svc in svcs:
            assert key not in (svcs[svc].get("networks") or {}), svc
    # Existing memberships kept.
    for svc in ("backend", "mcp-server", "cloudflared"):
        nets = set(svcs[svc]["networks"])
        assert {"trinity-platform", "trinity-agent", key} <= nets, (svc, nets)

    # Direct-to-backend tunnel rules reach the backend over the ingress bridge.
    hosts = svcs["cloudflared"].get("extra_hosts")
    hosts = hosts if isinstance(hosts, list) else [f"{h}:{a}" for h, a in (hosts or {}).items()]
    hosts = {h.replace("=", ":", 1).split(":", 1)[0]: h.replace("=", ":", 1).split(":", 1)[1]
             for h in hosts}
    assert hosts == {"backend": f"{prefix}.4", "trinity-backend": f"{prefix}.4"}, hosts

    # Both frontend listeners, and only the ordinary one may be non-loopback.
    # Unset FRONTEND_BIND_ADDRESS renders NO host_ip — every interface, IPv6
    # included, exactly as before this change.
    ports = {(p["target"], p.get("host_ip"), p["published"]) for p in svcs["frontend"]["ports"]}
    assert ports == {
        (8080, extra.get("FRONTEND_BIND_ADDRESS"), extra.get("FRONTEND_PORT", "80")),
        (8082, "127.0.0.1", extra.get("CADDY_PROXY_PORT", "18082")),
    }, ports
    assert _env(svcs["frontend"])["TRINITY_PROXY_GATEWAY"] == f"{prefix}.1"

    # The backend's rendered environment, through the real loader.
    benv = _env(svcs["backend"])
    _assert_launcher(_command(svcs["backend"]), f"{name} rendered backend")
    config = ci.load_config(benv)
    assert str(config.frontend) == addr("frontend")
    assert str(config.cloudflared) == addr("cloudflared")
    assert str(config.proxy_subnet) == ipam["subnet"]
    for untrusted in (ipam["gateway"], addr("backend"), f"{prefix}.130", "172.28.0.9"):
        assert not config.is_trusted(ipaddress.ip_address(untrusted)), untrusted
    assert [str(n) for n in config.operator] == (
        [f"{extra['TRUSTED_PROXIES']}/32"] if "TRUSTED_PROXIES" in extra else [])


@_needs_compose
def test_dev_compose_grants_no_forwarding_trust_and_keeps_its_topology():
    cfg = _render("docker-compose.yml")
    names = {n.get("name") for n in cfg["networks"].values()}
    assert "trinity-proxy-network" not in names
    benv = _env(cfg["services"]["backend"])
    for knob in ("TRINITY_BUNDLED_PROXY_TRUST", "TRUSTED_PROXIES"):
        assert knob not in benv, knob
    assert ci.load_config(benv) == ci.ClientIPConfig()
    assert list(cfg["services"]["frontend"]["networks"]) == ["trinity-agent"]


def test_prod_and_hosted_wire_the_same_knobs_raw():
    """The four-file rule, raw: every new knob has a default (rendering with
    `--env-file /dev/null` must work) and is documented in .env.example."""
    example = (_ROOT / ".env.example").read_text()
    for knob in ("TRINITY_PROXY_PREFIX", "TRUSTED_PROXIES", "FRONTEND_BIND_ADDRESS", "CADDY_PROXY_PORT"):
        assert re.search(rf"^#?\s*{knob}=", example, re.M), f".env.example does not document {knob}"
        for name in _PROD_LIKE:
            text = (_ROOT / name).read_text()
            uses = re.findall(rf"\$\{{{knob}([^}}]*)\}}", text)
            assert uses, f"{name} does not wire {knob}"
            # `:-default`, or `:+...` (empty means "omit the host IP").
            assert all(u.startswith((":-", ":+")) for u in uses), f"{name}: {knob} without a default: {uses}"
    dev = (_ROOT / "docker-compose.yml").read_text()
    assert "TRINITY_BUNDLED_PROXY_TRUST" not in dev and "trinity-proxy" not in dev
