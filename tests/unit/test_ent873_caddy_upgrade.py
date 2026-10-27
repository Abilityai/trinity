"""trinity-enterprise#873 / #3047 — a provisioned host's Caddy switches to the
PROXY-protocol listener by itself, and only when that listener is really there.

On a Caddy-fronted install (DigitalOcean 1-Click, AWS, Vultr) Caddy states each
client's address in a PROXY protocol header on the web container's protected
listener, because Docker's loopback port publishing does not carry it. Existing installs must get
that from the upgrade they already run — `start.sh --hosted` — with no extra
step, and must never be pointed at a listener their running web image lacks.

The orchestration is EXECUTED: the real start.sh runs in a scratch directory
with its absolute host paths redirected and the host's commands (docker,
systemctl, caddy, curl, id, uname, ...) replaced by recording stubs. The
listener probe is the real `probe-proxy-listener.sh`, talking to a real socket
this test opens — so "the listener answered" is an observed byte exchange, not
a flag.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")]

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _ROOT / "scripts" / "deploy"
_START = _DEPLOY / "start.sh"
_PROBE = _DEPLOY / "probe-proxy-listener.sh"
_REAL_FLOCK = shutil.which("flock")
_BASH = shutil.which("bash")

PROXY_V2_TCP4_LOOPBACK = (
    b"\r\n\r\n\x00\r\nQUIT\n"          # signature
    b"\x21\x11\x00\x0c"                  # v2 PROXY, TCP/IPv4, 12 address bytes
    b"\x7f\x00\x00\x01\x7f\x00\x00\x01"  # 127.0.0.1 -> 127.0.0.1
    b"\xc0\x00\x00\x50"                  # 49152 -> 80
)
PROBE_REQUEST = (b"GET / HTTP/1.1\r\nHost: localhost\r\nUser-Agent: trinity-proxy-probe\r\n"
                 b"Connection: close\r\n\r\n")


# ---------------------------------------------------------------------------
# A real listener the probe talks to
# ---------------------------------------------------------------------------

class Listener:
    """One-port TCP server. `mode`: "proxy-200" answers 200 only after a valid
    PROXY v2 header; "plain-http" behaves like an ordinary HTTP listener
    (answers 400 to anything that is not a request line); "silent" accepts and
    never answers."""

    def __init__(self, mode: str):
        self.mode, self.received = mode, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while b"\r\n\r\n" not in data[28:] and len(data) < 4096:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                self.received.append(data)
                if self.mode == "silent":
                    time.sleep(6)
                    continue
                ok = self.mode == "proxy-200" and data.startswith(PROXY_V2_TCP4_LOOPBACK)
                status = b"200 OK" if ok else b"400 Bad Request"
                try:
                    conn.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def listener():
    made = []

    def make(mode):
        made.append(Listener(mode))
        return made[-1]

    yield make
    for x in made:
        x.close()


def _probe(port, timeout="2"):
    return subprocess.run([_BASH, str(_PROBE), str(port)], capture_output=True, text=True,
                          env={**os.environ, "PROBE_TIMEOUT": timeout}, timeout=30)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------

def test_probe_sends_exact_proxy_v2_framing_then_one_request(listener):
    srv = listener("proxy-200")
    assert _probe(srv.port).returncode == 0
    assert srv.received == [PROXY_V2_TCP4_LOOPBACK + PROBE_REQUEST]


def test_an_ordinary_http_listener_fails_the_probe(listener):
    srv = listener("plain-http")
    assert _probe(srv.port).returncode != 0


def test_nothing_listening_fails_the_probe():
    assert _probe(_free_port()).returncode != 0


def test_a_listener_that_never_answers_is_bounded(listener):
    srv = listener("silent")
    started = time.monotonic()
    assert _probe(srv.port, timeout="1").returncode != 0
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("bad", ["", "abc", "0", "70000", "80;id"])
def test_the_probe_refuses_anything_but_a_port_number(bad):
    r = _probe(bad)
    assert r.returncode == 2, (bad, r.stderr)


# ---------------------------------------------------------------------------
# The installer, executed
# ---------------------------------------------------------------------------

_FAKE_DOCKER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as f:
    f.write("docker " + " ".join(args) + "\n")
nets = [l.split("|") for l in os.environ.get("NETWORKS", "").splitlines() if l.strip()]
if args[:1] == ["port"]:
    out = os.environ.get("DOCKER_PORT_OUT", "")
    if out:
        print(out)
        sys.exit(0)
    sys.exit(1)
if args[:2] == ["network", "ls"]:
    for n in nets:
        print(n[0])
    sys.exit(0)
if args[:2] == ["network", "inspect"]:
    rest = args[2:]
    fmt = None
    if rest[:1] == ["-f"]:
        fmt, rest = rest[1], rest[2:]
    by = {n[0]: n for n in nets}
    if any(r not in by for r in rest):
        sys.exit(1)
    for r in rest:
        name, subnet, gw, rng = (by[r] + ["", "", ""])[:4]
        if fmt and "|{{.Subnet}}" in fmt:
            print(f"{name}|{subnet}|{gw}|{rng}")
        elif fmt:
            print(subnet + " ")
    sys.exit(0)
if args[:1] == ["run"]:
    print("0")
    sys.exit(0)
if args[:2] == ["volume", "inspect"]:
    sys.exit(1)
if args[:1] == ["images"]:
    sys.exit(0)
sys.exit(0)
'''

_STUBS = {
    "curl": r'''#!/bin/bash
echo "curl $*" >> "$CALL_LOG"
for a in "$@"; do case "$a" in
  http://169.254.169.254/*) printf '%s' "${METADATA_IP:-203.0.113.10}"; exit 0 ;;
  http://localhost:8000/health) [ "${HEALTH_OK:-1}" = 1 ]; exit $? ;;
  http://127.0.0.1:*/) [ "${WEB_OK:-1}" = 1 ]; exit $? ;;
esac; done
exit 0
''',
    "systemctl": r'''#!/bin/bash
echo "systemctl $*" >> "$CALL_LOG"
case "$1" in
  cat) [ "${CADDY_UNIT:-1}" = 1 ]; exit $? ;;
  is-active) exit 0 ;;
esac
exit 0
''',
    "caddy": '#!/bin/bash\necho "caddy $*" >> "$CALL_LOG"\nexit "${CADDY_VALIDATE_RC:-0}"\n',
    "id": '#!/bin/bash\n[ "$1" = -u ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n',
    "uname": '#!/bin/bash\n[ "$1" = -s ] && { echo Linux; exit 0; }\nexec /usr/bin/uname "$@"\n',
    "stat": '#!/bin/bash\necho 1000\n',
    "chown": '#!/bin/bash\necho "chown $*" >> "$CALL_LOG"\n',
    "sudo": '#!/bin/bash\n"$@"\n',
    "getent": '#!/bin/bash\necho docker:x:999:\n',
    "sleep": '#!/bin/bash\n:\n',
    # Time moves 30s per read so the bounded waits end at once.
    "date": r'''#!/bin/bash
if [ "$1" = +%s ]; then
  f="$TMPROOT/clock"; t=$(cat "$f" 2>/dev/null || echo 1700000000); t=$((t + 30)); echo $t > "$f"; echo $t; exit 0
fi
exec /bin/date "$@"
''',
}


def _flock_stub() -> str:
    if _REAL_FLOCK:
        return f'#!/bin/bash\necho "flock $*" >> "$CALL_LOG"\nexec {_REAL_FLOCK} "$@"\n'
    return '#!/bin/bash\necho "flock $*" >> "$CALL_LOG"\nexit 0\n'


_DOTENV = """ADMIN_PASSWORD=Adm1n!Passw0rd-873
REDIS_PASSWORD=r1
REDIS_BACKEND_PASSWORD=r2
POSTGRES_PASSWORD=p1
DATABASE_URL=
TRINITY_IMAGE_TAG=v1.0.0
FRONTEND_PORT=8081
TRINITY_INSTALL_SOURCE=do-marketplace
FRONTEND_URL=https://203.0.113.10
DOCKER_GID=999
"""


class Run:
    def __init__(self, proc, root):
        self.proc, self.root = proc, root

    @property
    def calls(self) -> str:
        p = self.root / "calls.log"
        return p.read_text() if p.exists() else ""

    @property
    def caddyfile(self) -> str:
        p = self.root / "etc-caddy" / "Caddyfile"
        return p.read_text() if p.exists() else ""

    @property
    def dotenv(self) -> str:
        return (self.root / ".env").read_text()


def _scratch(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    deploy = root / "scripts" / "deploy"
    deploy.mkdir(parents=True)
    text = _START.read_text()
    for real, fake in (("/etc/trinity", root / "etc-trinity"), ("/etc/caddy", root / "etc-caddy"),
                       ("/run/trinity-provision.lock", root / "provision.lock")):
        text = text.replace(real, str(fake))
    (deploy / "start.sh").write_text(text)
    shutil.copy(_DEPLOY / "env-file.sh", deploy / "env-file.sh")
    shutil.copy(_PROBE, deploy / "probe-proxy-listener.sh")
    for f in deploy.iterdir():
        f.chmod(0o755)
    (root / "etc-caddy").mkdir()
    (root / ".env").write_text(_DOTENV)
    (root / ".env.example").write_text("")
    (root / "trinity-data").mkdir()
    return root


def _install(tmp_path, *args, managed=True, prior_caddyfile="previous\n", dotenv_extra="",
             env=None, root=None) -> Run:
    root = root or _scratch(tmp_path)
    state = root / "etc-trinity"
    if managed:
        state.mkdir(exist_ok=True)
        (state / "public-ip").write_text("203.0.113.10\n")
    if prior_caddyfile is not None and not (root / "etc-caddy" / "Caddyfile").exists():
        (root / "etc-caddy" / "Caddyfile").write_text(prior_caddyfile)
    if dotenv_extra:
        with open(root / ".env", "a") as f:
            f.write(dotenv_extra)
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(_FAKE_DOCKER)
    for name, body in {**_STUBS, "flock": _flock_stub()}.items():
        (bindir / name).write_text(body)
    for f in bindir.iterdir():
        f.chmod(0o755)
    full_env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(root),
        "CALL_LOG": str(root / "calls.log"),
        "TMPROOT": str(root),
    }
    full_env.update(env or {})
    proc = subprocess.run([_BASH, str(root / "scripts" / "deploy" / "start.sh"), *args],
                          cwd=root, capture_output=True, text=True, env=full_env, timeout=120)
    return Run(proc, root)


def _protected(caddyfile: str, port: int) -> bool:
    return (caddyfile.count(f"reverse_proxy 127.0.0.1:{port} {{") == 2
            and caddyfile.count("proxy_protocol v2") == 2 and "8081" not in caddyfile)


def test_a_healthy_managed_upgrade_switches_caddy_with_no_manual_step(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--hosted", "--unattended",
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.proc.returncode == 0, r.proc.stdout[-3000:] + r.proc.stderr[-3000:]
    assert _protected(r.caddyfile, srv.port), r.caddyfile
    assert "Reconfiguring Caddy for this release" in r.proc.stdout
    calls = r.calls
    # Rendered only after the stack came up and answered, and Caddy restarted once.
    assert calls.index("docker compose -f docker-compose.hosted.yml up -d") < calls.index("caddy validate")
    assert calls.index("curl -fsS -m 3 http://localhost:8000/health") < calls.index("caddy validate")
    assert calls.count("systemctl restart caddy") == 1
    # The shared provisioning lock was taken before any image pull or compose call.
    assert "flock" in calls
    first_flock = calls.index("flock")
    for later in ("docker pull", "docker compose"):
        assert first_flock < calls.index(later), later
    # The ordinary listener moved to loopback before the stack was replaced.
    assert re.search(r"^FRONTEND_BIND_ADDRESS=127\.0\.0\.1$", r.dotenv, re.M)


def test_an_identical_rerun_does_not_restart_caddy(tmp_path, listener):
    srv = listener("proxy-200")
    env = {"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"}
    first = _install(tmp_path, "--hosted", "--unattended", env=env)
    assert first.proc.returncode == 0
    (first.root / "calls.log").unlink()
    again = _install(tmp_path, "--hosted", "--unattended", env=env, root=first.root)
    assert again.proc.returncode == 0
    assert "caddy validate" in again.calls and "systemctl restart caddy" not in again.calls


def test_an_older_web_image_keeps_the_ordinary_listener_and_says_so(tmp_path):
    """No 8082 publication on the running container (an older image): the
    upgrade still finishes, Caddy is pointed at the ordinary listener, and
    the warning is printed."""
    r = _install(tmp_path, "--hosted", "--unattended", env={"DOCKER_PORT_OUT": ""})
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "reverse_proxy 127.0.0.1:8081 {" in r.caddyfile
    assert "proxy_protocol" not in r.caddyfile
    assert "NOT yet passed through" in r.proc.stderr


def test_a_published_port_that_does_not_speak_proxy_protocol_is_not_selected(tmp_path, listener):
    srv = listener("plain-http")
    r = _install(tmp_path, "--hosted", "--unattended",
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.proc.returncode == 0
    assert "proxy_protocol" not in r.caddyfile and "reverse_proxy 127.0.0.1:8081 {" in r.caddyfile


def test_the_shell_port_wins_over_dotenv_as_compose_resolves_it(tmp_path, listener):
    srv = listener("proxy-200")
    decoy = _free_port()
    r = _install(tmp_path, "--hosted", "--unattended", dotenv_extra=f"CADDY_PROXY_PORT={decoy}\n",
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert _protected(r.caddyfile, srv.port), r.caddyfile
    # And with no shell value, .env's is the one probed.
    root = _scratch(tmp_path / "second")
    r2 = _install(tmp_path, "--hosted", "--unattended", root=root,
                  dotenv_extra=f"CADDY_PROXY_PORT={srv.port}\n",
                  env={"DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert _protected(r2.caddyfile, srv.port), r2.caddyfile


def test_an_unhealthy_stack_leaves_caddy_alone(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--hosted", "--unattended",
                 env={"HEALTH_OK": "0", "CADDY_PROXY_PORT": str(srv.port),
                      "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.caddyfile == "previous\n"
    assert "caddy validate" not in r.calls
    assert "Caddy left as it was" in r.proc.stderr


def test_a_web_container_that_never_answers_leaves_caddy_alone(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--hosted", "--unattended",
                 env={"WEB_OK": "0", "CADDY_PROXY_PORT": str(srv.port),
                      "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.caddyfile == "previous\n" and "caddy validate" not in r.calls


def test_a_failed_validation_keeps_the_running_config(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--hosted", "--unattended",
                 env={"CADDY_VALIDATE_RC": "1", "CADDY_PROXY_PORT": str(srv.port),
                      "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.proc.returncode == 0
    assert r.caddyfile == "previous\n"
    assert "systemctl restart caddy" not in r.calls
    assert "previous configuration is still serving" in r.proc.stderr


@pytest.mark.parametrize("missing", ["public-ip", "caddy-unit"])
def test_a_host_trinity_did_not_provision_gets_no_caddy_and_no_lock(tmp_path, listener, missing):
    srv = listener("proxy-200")
    env = {"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"}
    if missing == "caddy-unit":
        env["CADDY_UNIT"] = "0"
    r = _install(tmp_path, "--hosted", "--unattended", managed=(missing != "public-ip"), env=env)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert r.caddyfile == "previous\n"
    assert "caddy validate" not in r.calls and "flock" not in r.calls
    assert "FRONTEND_BIND_ADDRESS=127.0.0.1" not in r.dotenv


def test_an_operators_explicit_bind_address_is_kept(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--hosted", "--unattended", dotenv_extra="FRONTEND_BIND_ADDRESS=10.0.0.4\n",
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.proc.returncode == 0
    assert "FRONTEND_BIND_ADDRESS=127.0.0.1" not in r.dotenv


def test_a_development_run_never_renders_caddy(tmp_path, listener):
    srv = listener("proxy-200")
    r = _install(tmp_path, "--unattended",
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    assert r.caddyfile == "previous\n" and "caddy validate" not in r.calls


def test_an_ip_refresh_against_an_older_web_image_keeps_the_ordinary_listener(tmp_path):
    """The refresh timer runs the WORKING-TREE start.sh against whatever web
    image is running. A checkout ahead of that image must not point Caddy at a
    listener the container does not have: the probe fails, Caddy stays put."""
    root = _scratch(tmp_path)
    (root / "etc-trinity").mkdir()
    (root / "etc-trinity" / "public-ip").write_text("198.51.100.1\n")
    r = _install(tmp_path, "--provision", "--cloud", "digitalocean", "--refresh-ip", "--hosted",
                 "--unattended", root=root, prior_caddyfile=None, managed=False,
                 env={"DOCKER_PORT_OUT": "", "METADATA_IP": "203.0.113.10"})
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "reverse_proxy 127.0.0.1:8081 {" in r.caddyfile and "proxy_protocol" not in r.caddyfile
    assert "https://203.0.113.10 {" in r.caddyfile
    assert "up -d --no-deps --no-build --pull never backend" in r.calls
    assert (root / "etc-trinity" / "public-ip").read_text().strip() == "203.0.113.10"


def test_an_ip_refresh_with_a_live_listener_keeps_proxy_protocol(tmp_path, listener):
    srv = listener("proxy-200")
    root = _scratch(tmp_path)
    (root / "etc-trinity").mkdir()
    (root / "etc-trinity" / "public-ip").write_text("198.51.100.1\n")
    r = _install(tmp_path, "--provision", "--cloud", "digitalocean", "--refresh-ip", "--hosted",
                 "--unattended", root=root, prior_caddyfile=None, managed=False,
                 env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}",
                      "METADATA_IP": "203.0.113.10"})
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert _protected(r.caddyfile, srv.port), r.caddyfile


# ---------------------------------------------------------------------------
# The ingress subnet preflight
# ---------------------------------------------------------------------------

def test_a_conflicting_network_stops_the_upgrade_before_anything_changes(tmp_path):
    r = _install(tmp_path, "--hosted", "--unattended", managed=False,
                 env={"NETWORKS": "corp-vpn|172.30.0.0/16|172.30.0.1|"})
    assert r.proc.returncode == 1
    assert "corp-vpn" in r.proc.stderr and "172.30.0.0/16" in r.proc.stderr
    assert "TRINITY_PROXY_PREFIX" in r.proc.stderr
    assert "docker compose -f docker-compose.hosted.yml up" not in r.calls
    assert "docker compose -f docker-compose.hosted.yml pull" not in r.calls


def test_the_network_this_install_owns_is_accepted_on_a_rerun(tmp_path):
    r = _install(tmp_path, "--hosted", "--unattended", managed=False, env={"NETWORKS": "\n".join([
        "trinity-proxy-network|172.30.0.0/24|172.30.0.1|172.30.0.128/25",
        "trinity-agent-network|172.28.0.0/16|172.28.0.1|",
        "bridge|172.17.0.0/16|172.17.0.1|",
    ])})
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "docker compose -f docker-compose.hosted.yml up -d" in r.calls


@pytest.mark.parametrize("line", [
    "trinity-proxy-network|172.30.0.0/24|172.30.0.254|172.30.0.128/25",   # other gateway
    "trinity-proxy-network|172.30.9.0/24|172.30.9.1|172.30.9.128/25",     # other prefix
])
def test_an_owned_network_with_a_different_address_plan_is_refused(tmp_path, line):
    r = _install(tmp_path, "--hosted", "--unattended", managed=False, env={"NETWORKS": line})
    assert r.proc.returncode == 1
    assert "trinity-proxy-network" in r.proc.stderr
    assert "docker compose -f docker-compose.hosted.yml up" not in r.calls


def test_a_non_default_prefix_is_checked_instead(tmp_path):
    r = _install(tmp_path, "--hosted", "--unattended", managed=False,
                 dotenv_extra="TRINITY_PROXY_PREFIX=10.77.3\n",
                 env={"NETWORKS": "lab|10.77.0.0/16|10.77.0.1|"})
    assert r.proc.returncode == 1 and "10.77.3.0/24" in r.proc.stderr


# ---------------------------------------------------------------------------
# The shared lock (real flock: Linux, util-linux)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_REAL_FLOCK is None, reason="flock (util-linux) required")
def test_a_managed_upgrade_waits_for_a_running_provision_or_refresh(tmp_path, listener):
    srv = listener("proxy-200")
    root = _scratch(tmp_path)
    holder = subprocess.Popen([_REAL_FLOCK, str(root / "provision.lock"), "sleep", "3"])
    time.sleep(0.5)
    try:
        r = _install(tmp_path, "--hosted", "--unattended", root=root,
                     env={"CADDY_PROXY_PORT": str(srv.port), "DOCKER_PORT_OUT": f"127.0.0.1:{srv.port}"})
    finally:
        holder.kill()
        holder.wait()
    assert r.proc.returncode == 0
    assert "Waiting for another provisioning run to finish" in r.proc.stdout
    assert _protected(r.caddyfile, srv.port)


@pytest.mark.skipif(_REAL_FLOCK is None, reason="flock (util-linux) required")
def test_a_refresh_during_an_upgrade_exits_without_touching_anything(tmp_path):
    """The IP-refresh timer and an upgrade take the same lock; the refresh
    never queues and never writes while the upgrade holds it."""
    root = _scratch(tmp_path)
    (root / "etc-trinity").mkdir()
    (root / "etc-trinity" / "public-ip").write_text("198.51.100.1\n")
    (root / "etc-caddy" / "Caddyfile").write_text("previous\n")
    holder = subprocess.Popen([_REAL_FLOCK, str(root / "provision.lock"), "sleep", "20"])
    time.sleep(0.5)
    try:
        r = _install(tmp_path, "--provision", "--cloud", "digitalocean", "--refresh-ip", "--hosted",
                     "--unattended", root=root, managed=False, env={"METADATA_IP": "203.0.113.10"})
    finally:
        holder.kill()
        holder.wait()
    assert r.proc.returncode == 0, r.proc.stderr
    # It got as far as the lock (the address DID change) and stopped there.
    assert "flock -n 9" in r.calls
    assert r.caddyfile == "previous\n"
    assert "docker compose" not in r.calls and "systemctl restart" not in r.calls
    assert (root / "etc-trinity" / "public-ip").read_text() == "198.51.100.1\n"


def test_the_lock_is_taken_before_any_env_or_compose_mutation_in_source_order():
    """Static companion to the executed ordering: the managed-host block that
    takes the lock sits before every `.env` writer the install runs and before
    the first compose call."""
    body = _START.read_text()
    take = body.index('if [ "$HOSTED" = "1" ] && provision_managed_caddy; then')
    for later in ("\nensure_admin_password\n", "\nensure_redis_passwords\n", "\nensure_hosted_database\n",
                  'echo "Starting services..."'):
        assert take < body.index(later), later
