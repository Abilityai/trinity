"""abilityai/trinity-enterprise#1101 — the MCP server's raw port and its packaging.

A fresh install publishes the MCP container port on loopback only
(`MCP_BIND_ADDRESS=127.0.0.1` from `.env.example` and `provision_site`); an
upgraded `.env` without the key keeps publishing on every interface, exactly as
before. The backend reads the same key (honest advertised URL / status) and the
`MCP_EXTERNAL_ACCESS` opt-in, so both are wired into every compose file's
backend environment — a key a container never receives is inert (the
#1039/#1056/#1707 packaging-gap class). On prod/hosted the MCP server takes a
reserved ingress address and cloudflared pins its names to it, so a tunnel rule
aimed straight at the container arrives from the ingress network (off-box)
rather than over the agent network.

What runs:
- the compose port and address strings are INTERPOLATED (a small model of
  compose's `${VAR}`, `${VAR:-x}`, `${VAR:+x}`, `${VAR:?x}`) with the variable
  set and unset, and parsed the way compose parses a short port mapping; when
  the Compose CLI is present the real `docker compose config` render is checked
  too;
- `provision_site` is executed (extracted from start.sh, as the provisioning
  tests do);
- the real start.sh `--hosted` upgrade is executed with the recording stubs of
  test_ent873_caddy_upgrade.py, and its printed summary is read;
- the real verify-platform.sh is executed in a scratch tree with stubbed
  docker/curl.
"""
from __future__ import annotations

import importlib.util
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

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")]

_ROOT = Path(__file__).resolve().parents[2]
_ALL = ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml")
_PROD_LIKE = ("docker-compose.prod.yml", "docker-compose.hosted.yml")
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_ENV_FILE_SH = _ROOT / "scripts" / "deploy" / "env-file.sh"
_VERIFY = _ROOT / "scripts" / "deploy" / "verify-platform.sh"
_SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_SETTINGS_POINTER = "Settings → MCP Keys"


# ---------------------------------------------------------------------------
# A model of compose interpolation, enough for the strings these files use
# ---------------------------------------------------------------------------

def _interp(text: str, env: dict) -> str:
    out, i = [], 0
    while i < len(text):
        if text.startswith("${", i):
            depth, j = 1, i + 2
            while depth:
                if text.startswith("${", j):
                    depth, j = depth + 1, j + 2
                    continue
                depth -= text[j] == "}"
                j += 1
            body = text[i + 2:j - 1]
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:[-+?])(.*))?$", body, re.S)
            name, op, word = m.group(1), m.group(2), m.group(3)
            val = env.get(name, "")
            if op == ":-":
                val = val or _interp(word, env)
            elif op == ":+":
                val = _interp(word, env) if val else ""
            elif op == ":?":
                assert val, f"{name} is required: {word}"
            out.append(val)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _port(short: str) -> dict:
    """Compose's short port syntax: `[host_ip:]published:target`."""
    parts = short.split(":")
    if len(parts) == 2:
        return {"published": parts[0], "target": int(parts[1])}
    assert len(parts) == 3, short
    return {"host_ip": parts[0], "published": parts[1], "target": int(parts[2])}


def _services(name: str) -> dict:
    return yaml.safe_load((_ROOT / name).read_text())["services"]


def _env_list(svc: dict) -> list:
    env = svc.get("environment") or []
    return env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]


def _mcp_8080(name: str, env: dict) -> list:
    ports = [_port(_interp(p, env)) for p in _services(name)["mcp-server"]["ports"]]
    return [p for p in ports if p["target"] == 8080]


# ---------------------------------------------------------------------------
# The raw port: loopback when set, every interface when unset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _ALL)
def test_unset_bind_address_publishes_on_every_interface_as_before(name):
    ports = _mcp_8080(name, {})
    assert ports == [{"published": "8080", "target": 8080}], ports


@pytest.mark.parametrize("name", _ALL)
@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.4"])
def test_a_set_bind_address_publishes_on_that_address_only(name, addr):
    ports = _mcp_8080(name, {"MCP_BIND_ADDRESS": addr})
    assert ports == [{"host_ip": addr, "published": "8080", "target": 8080}], ports


# ---------------------------------------------------------------------------
# prod / hosted: a reserved ingress address, and the tunnel pinned to it
# ---------------------------------------------------------------------------

def _hosts(svc: dict) -> dict:
    hosts = svc.get("extra_hosts") or []
    if isinstance(hosts, dict):
        return dict(hosts)
    return {h.replace("=", ":", 1).split(":", 1)[0]: h.replace("=", ":", 1).split(":", 1)[1]
            for h in hosts}


@pytest.mark.parametrize("name", _PROD_LIKE)
@pytest.mark.parametrize("env", [{}, {"TRINITY_PROXY_PREFIX": "172.31.40"}], ids=["default", "moved"])
def test_mcp_server_holds_a_reserved_ingress_address_the_tunnel_pins(name, env):
    svcs = _services(name)
    prefix = env.get("TRINITY_PROXY_PREFIX", "172.30.0")
    mcp_net = svcs["mcp-server"]["networks"]["trinity-proxy"]
    mcp_addr = _interp(mcp_net["ipv4_address"], env)
    assert mcp_addr == f"{prefix}.5"

    pool = ipaddress.ip_network(f"{prefix}.128/25")
    assert ipaddress.ip_address(mcp_addr) not in pool
    reserved = {_interp(svcs[s]["networks"]["trinity-proxy"]["ipv4_address"], env)
                for s in ("frontend", "cloudflared", "backend")}
    assert mcp_addr not in reserved, reserved

    hosts = {h: _interp(a, env) for h, a in _hosts(svcs["cloudflared"]).items()}
    assert hosts == {"backend": f"{prefix}.4", "trinity-backend": f"{prefix}.4",
                     "mcp-server": mcp_addr, "trinity-mcp-server": mcp_addr}, hosts


# ---------------------------------------------------------------------------
# The four-file rule for both keys, with a live consumer for each
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", _ALL)
@pytest.mark.parametrize("knob", ["MCP_BIND_ADDRESS", "MCP_EXTERNAL_ACCESS"])
def test_the_backend_receives_the_key_in_every_compose_file(name, knob):
    backend = _env_list(_services(name)["backend"])
    lines = [e for e in backend if e.split("=", 1)[0] == knob]
    assert lines == [f"{knob}=${{{knob}:-}}"], lines
    # Renders without a value (`--env-file /dev/null`) and carries one when set.
    assert _interp(lines[0], {}) == f"{knob}="
    assert _interp(lines[0], {knob: "x"}) == f"{knob}=x"


@pytest.mark.parametrize("knob", ["MCP_BIND_ADDRESS", "MCP_EXTERNAL_ACCESS"])
def test_the_backend_actually_reads_the_key(knob):
    readers = [p for p in (_ROOT / "src" / "backend").rglob("*.py")
               if "enterprise" not in p.parts
               and re.search(rf"""getenv\(\s*(?:["']{knob}["']|MCP_EXTERNAL_ACCESS_ENV)""", p.read_text())]
    assert readers, f"nothing in src/backend reads {knob}: the compose wiring would be inert"


def _dotenv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def test_env_example_ships_loopback_and_a_commented_opt_in():
    text = (_ROOT / ".env.example").read_text()
    values = _dotenv(text)
    assert values.get("MCP_BIND_ADDRESS") == "127.0.0.1"
    # Commented: a copied .env must never read `source=env` by accident (ent#671).
    assert "MCP_EXTERNAL_ACCESS" not in values
    assert re.search(r"^# MCP_EXTERNAL_ACCESS=true$", text, re.M)
    # A fresh install that copies .env.example therefore renders loopback.
    for name in _ALL:
        assert _mcp_8080(name, values) == [
            {"host_ip": "127.0.0.1", "published": "8080", "target": 8080}], name


# ---------------------------------------------------------------------------
# Rendered by the real Compose CLI, where it is available
# ---------------------------------------------------------------------------

def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")
@pytest.mark.parametrize("name", _ALL)
@pytest.mark.parametrize("bind", [None, "127.0.0.1"])
def test_rendered_mcp_ports_and_backend_env(name, bind):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
    env.update({"ADMIN_PASSWORD": secrets.token_urlsafe(18) + "Aa1!",
                "REDIS_PASSWORD": secrets.token_hex(24), "REDIS_BACKEND_PASSWORD": secrets.token_hex(24),
                "POSTGRES_PASSWORD": secrets.token_hex(24), "TUNNEL_TOKEN": "placeholder"})
    if bind:
        env["MCP_BIND_ADDRESS"] = bind
    proc = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f", name, "--profile", "tunnel",
                           "config", "--format", "json"], cwd=_ROOT, capture_output=True, text=True,
                          timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    svcs = json.loads(proc.stdout)["services"]
    ports = [p for p in svcs["mcp-server"]["ports"] if p["target"] == 8080]
    assert [p.get("host_ip") for p in ports] == [bind]
    benv = svcs["backend"]["environment"]
    assert benv.get("MCP_BIND_ADDRESS", "") == (bind or "")
    assert "MCP_EXTERNAL_ACCESS" in benv


# ---------------------------------------------------------------------------
# provision_site writes the key (executed)
# ---------------------------------------------------------------------------

def _extract(func: str) -> str:
    m = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", _START.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"start.sh no longer defines a top-level `{func}()`"
    return m.group(0)


def test_provision_site_writes_the_loopback_bind(tmp_path):
    (tmp_path / ".env.example").write_text("")
    script = "\n".join([
        "set -e",
        f". '{_ENV_FILE_SH}'",
        _extract("env_value"),
        _extract("provision_site").replace("/etc/trinity", str(tmp_path / "etc-trinity")),
        # Everything provision_site delegates to is out of scope here.
        *(f"{f}() {{ :; }}" for f in ("provision_default_admin_source", "provision_setup_claim",
                                      "provision_apply_ip", "provision_refresh_units")),
        "provision_default_provenance() { echo test; }",
        "provision_public_ip() { echo 203.0.113.20; }",
        "provision_site",
    ])
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
                       env={"PATH": _SYS_PATH})
    assert r.returncode == 0, r.stderr
    values = _dotenv((tmp_path / ".env").read_text())
    assert values.get("MCP_BIND_ADDRESS") == "127.0.0.1"
    assert values.get("FRONTEND_BIND_ADDRESS") == "127.0.0.1"


# ---------------------------------------------------------------------------
# A `--hosted` upgrade: the key is never moved, and the summary is honest
# ---------------------------------------------------------------------------

def _caddy_harness():
    spec = importlib.util.spec_from_file_location(
        "_ent873_caddy_upgrade_harness", Path(__file__).with_name("test_ent873_caddy_upgrade.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mcp_summary(stdout: str) -> str:
    block = stdout[stdout.index("Access points:"):]
    block = block[:block.index("── Your next steps")]
    return "\n".join(line for line in block.splitlines() if "MCP" in line or line.startswith("    "))


def test_a_hosted_upgrade_without_the_key_leaves_it_unset_and_says_so(tmp_path):
    h = _caddy_harness()
    r = h._install(tmp_path, "--hosted", "--unattended", env={"DOCKER_PORT_OUT": ""})
    assert r.proc.returncode == 0, r.proc.stdout[-2000:] + r.proc.stderr[-2000:]
    assert "MCP_BIND_ADDRESS" not in r.dotenv
    summary = _mcp_summary(r.proc.stdout)
    assert "http://localhost:8080/mcp" in summary, summary
    assert _SETTINGS_POINTER in summary, summary
    assert "127.0.0.1 only" not in summary, summary


def test_a_loopback_bind_points_at_the_web_door_not_the_raw_port(tmp_path):
    h = _caddy_harness()
    r = h._install(tmp_path, "--hosted", "--unattended", dotenv_extra="MCP_BIND_ADDRESS=127.0.0.1\n",
                   env={"DOCKER_PORT_OUT": ""})
    assert r.proc.returncode == 0, r.proc.stdout[-2000:] + r.proc.stderr[-2000:]
    assert _dotenv(r.dotenv).get("MCP_BIND_ADDRESS") == "127.0.0.1"
    summary = _mcp_summary(r.proc.stdout)
    # _DOTENV sets FRONTEND_PORT=8081, so the Web UI is http://localhost:8081.
    assert ("http://localhost:8081/mcp once an admin turns on External MCP access "
            "(Settings → MCP Keys); the container port is published on 127.0.0.1 only") in summary, summary
    assert ":8080/mcp" not in summary, summary


# ---------------------------------------------------------------------------
# verify-platform.sh (executed): same wording, still probes localhost:8080
# ---------------------------------------------------------------------------

def _verify(tmp_path: Path, dotenv: str):
    root = tmp_path / "install"
    (root / "scripts" / "deploy").mkdir(parents=True)
    shutil.copy(_VERIFY, root / "scripts" / "deploy" / "verify-platform.sh")
    (root / ".env").write_text(dotenv)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(r'''#!/bin/bash
case "$1" in
  ps) for a in "$@"; do case "$a" in name=^/*) n=${a#name=^/}; n=${n%\$}; echo "$n";; esac; done ;;
  images) echo trinity-agent-base:latest ;;
esac
exit 0
''')
    (bindir / "curl").write_text('#!/bin/bash\necho "curl $*" >> "$CALL_LOG"\necho "<html>trinity ok healthy</html>"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    calls = tmp_path / "calls.log"
    r = subprocess.run(["bash", str(root / "scripts" / "deploy" / "verify-platform.sh")], capture_output=True,
                       text=True, env={"PATH": f"{bindir}:{_SYS_PATH}", "CALL_LOG": str(calls)}, timeout=60)
    return r, calls.read_text() if calls.exists() else ""


def test_verify_platform_loopback_wording(tmp_path):
    r, calls = _verify(tmp_path, "FRONTEND_PORT=8081\nMCP_BIND_ADDRESS=127.0.0.1\n")
    assert r.returncode == 0, r.stdout[-2000:]
    assert ("http://localhost:8081/mcp once an admin turns on External MCP access "
            "(Settings → MCP Keys); the container port is published on 127.0.0.1 only") in r.stdout, r.stdout
    assert "http://localhost:8080/health" in calls


def test_verify_platform_every_interface_wording(tmp_path):
    r, calls = _verify(tmp_path, "FRONTEND_PORT=8081\n")
    assert r.returncode == 0, r.stdout[-2000:]
    summary = r.stdout[r.stdout.index("Access points:"):]
    assert "http://localhost:8080/mcp" in summary and _SETTINGS_POINTER in summary, summary
    assert "http://localhost:8080/health" in calls
