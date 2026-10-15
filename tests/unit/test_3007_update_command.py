"""`scripts/deploy/update.sh` — one update command with preflight, published-URL
verification and rollback (#3007).

The issue names four failures from a real upgrade, and each one is pinned here by
EXECUTING the script, not by reading it:

  1. the documented entry point recreated an entitled source-built backend
     without ``docker-compose.prod.enterprise.yml`` → every compose call of an
     update on a ``prod-enterprise`` host must carry the overlay, and a host
     whose label lacks it while the enterprise tree is present is put back on it;
  2. ``FRONTEND_PORT`` turned from inert to live and moved the published port →
     a published-port change is refused BEFORE any image is built or container
     recreated, and the checkout is put back;
  3. every probe was localhost/container-side → success requires the PUBLISHED
     URL to serve HTML, a hashed asset and the API through the same origin;
  4. a missing ``CREDENTIAL_ENCRYPTION_KEY`` was discovered at boot → preflight
     refuses before anything is touched.

Harness: a real throwaway upstream git repository with two release tags (and an
enterprise gitlink pointing into a second real repository), cloned as "the
instance"; ``docker`` and ``curl`` are stubs on PATH that log every call and
answer from fixtures. No daemon, no network. The SQLite backup helper runs
against a real SQLite file, and the CLI locator is a stdlib module loaded by path
(the unit job does not install click).
"""
from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        shutil.which("bash") is None or shutil.which("git") is None,
        reason="bash and git are required to exercise update.sh",
    ),
]

_REPO = Path(__file__).resolve().parents[2]
_UPDATE = _REPO / "scripts" / "deploy" / "update.sh"
_DB_HELPER = _REPO / "scripts" / "deploy" / "update-db.py"
_ENV_FILE_SH = _REPO / "scripts" / "deploy" / "env-file.sh"
_HOST_UPDATE = _REPO / "src" / "cli" / "trinity_cli" / "host_update.py"

_PROD = "docker-compose.prod.yml"
_ENT = "docker-compose.prod.enterprise.yml"

_GOOD_HTML = (
    '<!DOCTYPE html><html lang="en"><head>'
    '<script type="module" crossorigin src="/assets/index-Bx7fQ2aZ.js"></script>'
    '<link rel="stylesheet" href="/assets/index-C9d0eF1g.css"></head>'
    '<body><div id="app"></div></body></html>'
)

# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

_DOCKER_STUB = r"""#!/bin/bash
# Logs every call, answers from STUB_* env fixtures.
printf 'docker %s\n' "$*" >> "$CALLS"
args="$*"
case "$1" in
  info) exit "${STUB_DOCKER_INFO_RC:-0}" ;;
  volume) exit 1 ;;
  logs) printf '%s\n' "${STUB_BOOT_LOG:-}"; exit 0 ;;
  exec)
    cat >/dev/null
    if [ "${@: -1}" = "schema" ]; then
      # The schema position the live backend reports; tests change the file
      # between the update and the rollback to simulate a migration.
      if [ -n "${STUB_SCHEMA_FILE:-}" ] && [ -f "$STUB_SCHEMA_FILE" ]; then cat "$STUB_SCHEMA_FILE"
      else echo '{"status": "ok", "backend": "sqlite", "position": "aaaa1111"}'; fi
      exit "${STUB_SCHEMA_RC:-0}"
    fi
    default_backup='{"status": "ok", "path": "/data/backups/pre-migration-20261015-120000.db"}'
    printf '%s\n' "${STUB_BACKUP_JSON:-$default_backup}"
    exit "${STUB_BACKUP_RC:-0}"
    ;;
  inspect)
    case "$args" in
      *config_files*)
        [ -n "${STUB_CONFIG_FILES:-}" ] || exit 1
        printf '%s\n' "$STUB_CONFIG_FILES"; exit 0 ;;
      *PortBindings*)
        last="${@: -1}"
        var="STUB_PORTS_$(printf '%s' "$last" | tr -c 'A-Za-z0-9\n' '_')"
        printf '%s\n' "${!var:-}"; exit 0 ;;
      *StartedAt*) echo "2026-10-15T12:00:00Z"; exit 0 ;;
    esac
    exit 0 ;;
  compose)
    case "$args" in
      *"config --format json"*)
        default_json='{"services": {}}'
        printf '%s\n' "${STUB_COMPOSE_JSON:-$default_json}"; exit "${STUB_COMPOSE_JSON_RC:-0}" ;;
      *"config -q"*) exit "${STUB_COMPOSE_CONFIG_RC:-0}" ;;
      *" build"*) exit "${STUB_BUILD_RC:-0}" ;;
      *" up"*) exit "${STUB_UP_RC:-0}" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

_CURL_STUB = r"""#!/bin/bash
# Answers a URL from $STUB_WEB/<key>; <key>.fail -> exit 22 (curl -f on >=400).
printf 'curl %s\n' "$*" >> "$CALLS"
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2; continue ;;
    -m|--max-time|-w) shift 2; continue ;;
    http*) url="$1" ;;
  esac
  shift
done
key=$(printf '%s' "$url" | tr -c 'A-Za-z0-9\n' '_')
if [ -f "$STUB_WEB/$key.fail" ] || [ ! -f "$STUB_WEB/$key" ]; then exit 22; fi
if [ -n "$out" ]; then cat "$STUB_WEB/$key" > "$out"; else cat "$STUB_WEB/$key"; fi
exit 0
"""


def _start_sh_helpers() -> str:
    text = (_REPO / "scripts" / "deploy" / "start.sh").read_text()
    out = []
    for fn in ("env_value", "compose_project_name"):
        m = re.search(rf"^{fn}\(\) \{{\n.*?^\}}$", text, re.MULTILINE | re.DOTALL)
        assert m, f"start.sh no longer defines `{fn}()`, which update.sh loads from it"
        out.append(m.group(0))
    return "\n".join(out) + "\n"


def _key(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", url)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


class Instance:
    """An upstream with tags v1.0.0 and v1.1.0, cloned and checked out at v1.0.0."""

    def __init__(self, tmp: Path, *, enterprise: bool, base_image_change: bool = False):
        self.tmp = tmp
        self.upstream = tmp / "upstream"
        self.ent_upstream = tmp / "ent-upstream"
        self.root = tmp / "instance"
        self.bin = tmp / "bin"
        self.web = tmp / "web"
        self.calls = tmp / "calls.log"
        self.state = tmp / "state"
        for d in (self.upstream, self.bin, self.web, self.state):
            d.mkdir()
        self.calls.write_text("")

        # Enterprise repo with two commits; v1.0.0 pins the first, v1.1.0 the second.
        self.ent_pins: list[str] = []
        if enterprise:
            self.ent_upstream.mkdir()
            _git(self.ent_upstream, "init", "-q")
            (self.ent_upstream / "backend").mkdir()
            for n in (1, 2):
                (self.ent_upstream / "backend" / "__init__.py").write_text(f"# ent {n}\n")
                _git(self.ent_upstream, "add", "-A")
                _git(self.ent_upstream, "commit", "-qm", f"ent {n}")
                self.ent_pins.append(_git(self.ent_upstream, "rev-parse", "HEAD"))

        up = self.upstream
        _git(up, "init", "-q")
        (up / "scripts" / "deploy").mkdir(parents=True)
        shutil.copy(_UPDATE, up / "scripts" / "deploy" / "update.sh")
        shutil.copy(_DB_HELPER, up / "scripts" / "deploy" / "update-db.py")
        shutil.copy(_ENV_FILE_SH, up / "scripts" / "deploy" / "env-file.sh")
        # A stub start.sh (the hosted delegate) and build-base-image.sh that only
        # log. The start.sh stub carries the REAL env_value/compose_project_name
        # definitions, because update.sh loads its one copy of them from there.
        for name in ("start.sh", "build-base-image.sh"):
            p = up / "scripts" / "deploy" / name
            rc_var = "STUB_" + name.split(".")[0].upper().replace("-", "_") + "_RC"
            helpers = _start_sh_helpers() if name == "start.sh" else ""
            p.write_text(
                "#!/bin/bash\n" + helpers
                + f'printf "{name} %s\\n" "$*" >> "$CALLS"\nexit "${{{rc_var}:-0}}"\n'
            )
            p.chmod(0o755)
        for f in ("docker-compose.yml", _PROD, _ENT, "docker-compose.hosted.yml"):
            (up / f).write_text("services: {}\n")
        (up / "VERSION").write_text("1.0.0\n")
        (up / "docker" / "base-image").mkdir(parents=True)
        (up / "docker" / "base-image" / "Dockerfile").write_text("FROM scratch\n")
        (up / ".gitignore").write_text(".env\ntrinity-data/\n")
        _git(up, "add", "-A")
        if enterprise:
            _git(up, "update-index", "--add", "--cacheinfo", f"160000,{self.ent_pins[0]},src/backend/enterprise")
        _git(up, "commit", "-qm", "v1.0.0")
        _git(up, "tag", "v1.0.0")
        (up / "VERSION").write_text("1.1.0\n")
        if base_image_change:
            (up / "docker" / "base-image" / "Dockerfile").write_text("FROM scratch\n# changed\n")
        _git(up, "add", "-A")
        if enterprise:
            # `add -A` staged the gitlink as deleted (no working dir) — re-add it.
            _git(up, "update-index", "--add", "--cacheinfo", f"160000,{self.ent_pins[1]},src/backend/enterprise")
        _git(up, "commit", "-qm", "v1.1.0")
        _git(up, "tag", "v1.1.0")
        # A pre-release on top must never be the default target.
        (up / "VERSION").write_text("1.2.0-rc1\n")
        _git(up, "add", "VERSION")
        _git(up, "commit", "-qm", "rc")
        _git(up, "tag", "v1.2.0-rc1")

        _git(tmp, "clone", "-q", str(up), str(self.root))
        _git(self.root, "checkout", "-q", "v1.0.0")
        if enterprise:
            ent_dir = self.root / "src" / "backend" / "enterprise"
            if ent_dir.exists():
                ent_dir.rmdir()
            _git(tmp, "clone", "-q", str(self.ent_upstream), str(ent_dir))
            _git(ent_dir, "checkout", "-q", self.ent_pins[0])

        (self.root / ".env").write_text(
            "CREDENTIAL_ENCRYPTION_KEY=" + "a" * 64 + "\n"
            "FRONTEND_URL=https://trinity.example.com\n"
        )
        (self.root / "trinity-data").mkdir()
        (self.root / "trinity-data" / "trinity.db").write_bytes(b"")

        for name, body in (("docker", _DOCKER_STUB), ("curl", _CURL_STUB)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)

        self.serve("http://localhost:8000/health", '{"status":"healthy"}')
        self.serve_site("https://trinity.example.com")

    # -- fixtures ------------------------------------------------------------
    def serve(self, url: str, body: str) -> None:
        (self.web / _key(url)).write_text(body)

    def fail(self, url: str) -> None:
        (self.web / (_key(url) + ".fail")).write_text("")

    def serve_site(self, base: str, html: str = _GOOD_HTML) -> None:
        self.serve(base + "/", html)
        self.serve(base + "/assets/index-Bx7fQ2aZ.js", "console.log(1)")
        self.serve(base + "/assets/index-C9d0eF1g.css", "body{}")
        self.serve(base + "/api/auth/mode", '{"mode":"email"}')

    def config_files(self, *names: str) -> str:
        return ",".join(str(self.root / n) for n in names)

    # -- run -----------------------------------------------------------------
    def run(self, *args: str, env: dict | None = None, stdin: str = "") -> subprocess.CompletedProcess:
        base_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.tmp),
            "CALLS": str(self.calls),
            "STUB_WEB": str(self.web),
            "TRINITY_UPDATE_STATE_DIR": str(self.state),
            "TRINITY_UPDATE_MIN_FREE_GB": "0",
            "TRINITY_UPDATE_HEALTH_TIMEOUT": "2",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        if shutil.which("python3"):
            base_env["PATH"] += ":" + str(Path(shutil.which("python3")).parent)
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "deploy" / "update.sh"), *args],
            cwd=self.root, capture_output=True, text=True, input=stdin,
            env={**base_env, **(env or {})}, timeout=120,
        )

    # -- observations ----------------------------------------------------------
    def calls_list(self) -> list[str]:
        return [c for c in self.calls.read_text().splitlines() if c]

    def compose_calls(self) -> list[str]:
        return [c for c in self.calls_list() if c.startswith("docker compose")]

    def mutating_calls(self) -> list[str]:
        return [
            c for c in self.calls_list()
            if re.search(r"^docker compose .* (build|up)\b", c)
            or c.startswith("docker exec")
            or c.startswith("start.sh")
            or c.startswith("build-base-image.sh")
        ]

    def version(self) -> str:
        return (self.root / "VERSION").read_text().strip()

    def ent_head(self) -> str:
        return _git(self.root / "src" / "backend" / "enterprise", "rev-parse", "HEAD")


def _ports_json(frontend: str) -> str:
    return json.dumps({"services": {
        "frontend": {"container_name": "trinity-frontend",
                     "ports": [{"mode": "ingress", "target": 8080, "published": frontend, "protocol": "tcp"}]},
        "backend": {"container_name": "trinity-backend",
                    "ports": [{"mode": "ingress", "target": 8000, "published": "8000", "protocol": "tcp"}]},
        "redis": {"container_name": "trinity-redis"},
    }})


_REGISTERED = "INFO boot\nTrinity Enterprise modules registered\n"


def _ent_env(inst: Instance, **extra: str) -> dict:
    return {
        "STUB_CONFIG_FILES": inst.config_files(_PROD, _ENT),
        "STUB_BOOT_LOG": _REGISTERED,
        "STUB_COMPOSE_JSON": _ports_json("80"),
        "STUB_PORTS_trinity_frontend": "80",
        "STUB_PORTS_trinity_backend": "8000",
        **extra,
    }


# --------------------------------------------------------------------------- #
# Failure #1 — the delivery shape is detected and every compose call honours it
# --------------------------------------------------------------------------- #

def test_prod_enterprise_update_keeps_the_overlay_on_every_compose_call(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--yes", env=_ent_env(inst))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    composes = inst.compose_calls()
    assert any(" build" in c for c in composes) and any(" up" in c for c in composes), composes
    for call in composes:
        assert f"-f {_PROD} -f {_ENT}" in call, (
            f"a compose call on a prod-enterprise host dropped the overlay: {call!r} — "
            "this is the #3007 failure #1 (entitled backend recreated OSS-only)"
        )
    # Default target is the newest RELEASE tag, never a pre-release.
    assert inst.version() == "1.1.0"
    # The enterprise pin is read off the deployed ref, not from anywhere else.
    assert inst.ent_head() == inst.ent_pins[1]
    assert "prod-enterprise" in proc.stdout


def test_label_without_overlay_but_enterprise_tree_present_is_put_back_on_it(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--yes", env=_ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "without the enterprise overlay" in proc.stdout + proc.stderr
    for call in inst.compose_calls():
        assert f"-f {_ENT}" in call, call


def test_prod_without_enterprise_tree_uses_prod_file_only(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_BOOT_LOG="INFO boot\n")
    proc = inst.run("--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    composes = inst.compose_calls()
    assert composes
    for call in composes:
        assert f"-f {_PROD}" in call and _ENT not in call, call
        assert "-f docker-compose.yml" not in call, "never stack the dev file under prod (#2528)"


def test_enterprise_label_with_missing_enterprise_tree_is_refused_untouched(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    shutil.rmtree(inst.root / "src" / "backend" / "enterprise")
    (inst.root / "src" / "backend" / "enterprise").mkdir()
    proc = inst.run("--yes", env=_ent_env(inst))
    assert proc.returncode != 0
    assert "enterprise" in proc.stderr.lower()
    assert inst.mutating_calls() == []
    assert inst.version() == "1.0.0"


def test_ambiguous_host_without_backend_container_is_refused(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / "trinity-data" / "trinity.db").unlink()
    proc = inst.run("--yes", env={"STUB_CONFIG_FILES": ""})
    assert proc.returncode != 0
    assert "--shape" in proc.stderr, "an undetectable shape must name the explicit override"
    assert inst.mutating_calls() == []


def test_shape_from_config_files_is_pure(tmp_path):
    cases = {
        "/srv/t/docker-compose.prod.yml,/srv/t/docker-compose.prod.enterprise.yml": "prod-enterprise",
        "/srv/t/docker-compose.prod.yml": "prod",
        "/srv/t/docker-compose.hosted.yml,/srv/t/docker-compose.override.yml": "hosted",
        "/home/u/trinity/docker-compose.yml,/home/u/trinity/docker-compose.override.yml": "dev",
        "": "",
    }
    for files, want in cases.items():
        proc = subprocess.run(
            ["bash", "-c", f'source "{_UPDATE}"; shape_from_config_files "$1"', "_", files],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == want, files


# --------------------------------------------------------------------------- #
# Failure #4 — preflight fails before anything is touched
# --------------------------------------------------------------------------- #

def test_missing_credential_encryption_key_refuses_before_touching_anything(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    (inst.root / ".env").write_text("FRONTEND_URL=https://trinity.example.com\n")
    proc = inst.run("--yes", env=_ent_env(inst))
    assert proc.returncode != 0
    assert "CREDENTIAL_ENCRYPTION_KEY" in proc.stderr
    assert inst.mutating_calls() == [], "preflight must fail before any backup/build/up"
    assert inst.version() == "1.0.0", "preflight must fail before the checkout moves"
    assert inst.ent_head() == inst.ent_pins[0]
    assert not list(inst.state.iterdir()), "no rollback state for an update that never started"


def test_dirty_checkout_is_refused(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / _PROD).write_text("services: {local: edit}\n")
    proc = inst.run("--yes", env=_ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD)))
    assert proc.returncode != 0
    assert _PROD in proc.stderr
    assert inst.mutating_calls() == []


def test_unreachable_enterprise_pin_is_refused_before_the_checkout_moves(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    # The local enterprise checkout cannot reach the pin v1.1.0 records.
    ent_dir = inst.root / "src" / "backend" / "enterprise"
    _git(ent_dir, "remote", "remove", "origin")
    _git(ent_dir, "reset", "-q", "--hard", inst.ent_pins[0])
    for ref in _git(ent_dir, "for-each-ref", "--format=%(refname)").splitlines():
        _git(ent_dir, "update-ref", "-d", ref)
    _git(ent_dir, "reflog", "expire", "--expire=now", "--all")
    _git(ent_dir, "gc", "-q", "--prune=now")
    proc = inst.run("--yes", env=_ent_env(inst))
    assert proc.returncode != 0
    assert inst.ent_pins[1][:12] in proc.stderr
    assert inst.version() == "1.0.0"
    assert inst.mutating_calls() == []


def test_check_mode_reports_shape_and_touches_nothing(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--check", env=_ent_env(inst))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "prod-enterprise" in proc.stdout
    assert "v1.1.0" in proc.stdout
    assert inst.mutating_calls() == []
    assert inst.version() == "1.0.0"


# --------------------------------------------------------------------------- #
# Failure #2 — a published-port change stops the update before containers move
# --------------------------------------------------------------------------- #

def test_published_port_change_is_refused_and_checkout_restored(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--yes", env=_ent_env(inst, STUB_COMPOSE_JSON=_ports_json("3000")))
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "frontend" in out and "80" in out and "3000" in out
    assert not any(re.search(r" (build|up)\b", c) for c in inst.compose_calls()), (
        "no image may be built and no container recreated after a port drift"
    )
    assert inst.version() == "1.0.0", "the checkout must be put back"
    assert inst.ent_head() == inst.ent_pins[0], "the enterprise tree must be put back too"


@pytest.mark.parametrize("how", ["unparseable", "render_fails"])
def test_port_comparison_that_cannot_be_made_fails_closed(tmp_path, how):
    """An unrenderable target must not read as "no port change" — that silent
    pass is exactly how the #3007 502 would ship again."""
    inst = Instance(tmp_path, enterprise=False)
    extra = {"STUB_COMPOSE_JSON": "not json"} if how == "unparseable" else {"STUB_COMPOSE_JSON_RC": "1"}
    proc = inst.run("--yes", env=_ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), **extra))
    assert proc.returncode != 0
    assert "--accept-port-change" in proc.stderr
    assert not any(re.search(r" (build|up)\b", c) for c in inst.compose_calls())
    assert inst.version() == "1.0.0"


def test_published_port_change_proceeds_when_accepted(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--yes", "--accept-port-change", env=_ent_env(inst, STUB_COMPOSE_JSON=_ports_json("3000")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert inst.version() == "1.1.0"


# --------------------------------------------------------------------------- #
# Failure #3 — success is defined by the published URL
# --------------------------------------------------------------------------- #

def test_published_url_502_fails_the_update_and_records_rollback(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    inst.fail("https://trinity.example.com/")
    proc = inst.run("--yes", env=_ent_env(inst))
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "https://trinity.example.com" in out
    assert "--rollback" in out
    states = list(inst.state.iterdir())
    assert len(states) == 1
    prev = _git(inst.upstream, "rev-parse", "v1.0.0^{commit}")
    assert prev in states[0].read_text()


@pytest.mark.parametrize(
    "breakage",
    ["no_html", "no_hashed_asset", "asset_404", "api_down", "api_not_json"],
)
def test_published_url_checks_each_layer(tmp_path, breakage):
    inst = Instance(tmp_path, enterprise=False)
    base = "https://trinity.example.com"
    if breakage == "no_html":
        inst.serve(base + "/", "Bad Gateway")
    elif breakage == "no_hashed_asset":
        inst.serve(base + "/", '<html><body><div id="app"></div></body></html>')
    elif breakage == "asset_404":
        (inst.web / _key(base + "/assets/index-Bx7fQ2aZ.js")).unlink()
    elif breakage == "api_down":
        inst.fail(base + "/api/auth/mode")
    elif breakage == "api_not_json":
        inst.serve(base + "/api/auth/mode", "<html>login</html>")
    proc = inst.run("--verify", env={"STUB_CONFIG_FILES": inst.config_files(_PROD)})
    assert proc.returncode != 0, f"{breakage}: verification passed a broken published URL"
    assert "\u274c" in proc.stderr, f"{breakage}: the failure must be reported, not a silent exit"
    proc_ok = None
    if breakage == "api_down":
        (inst.web / (_key(base + "/api/auth/mode") + ".fail")).unlink()
        proc_ok = inst.run("--verify", env={"STUB_CONFIG_FILES": inst.config_files(_PROD)})
        assert proc_ok.returncode == 0, proc_ok.stdout + proc_ok.stderr


def test_verify_without_frontend_url_uses_local_port_and_says_so(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / ".env").write_text("CREDENTIAL_ENCRYPTION_KEY=" + "a" * 64 + "\nFRONTEND_PORT=3000\n")
    inst.serve_site("http://localhost:3000")
    proc = inst.run("--verify", env={"STUB_CONFIG_FILES": inst.config_files(_PROD)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "http://localhost:3000" in proc.stdout
    assert "--url" in proc.stdout, "an unpublished check must say how to verify the real entry point"


def test_enterprise_registration_missing_from_boot_log_fails(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    proc = inst.run("--yes", env=_ent_env(inst, STUB_BOOT_LOG="INFO boot\nTrinity Enterprise registration FAILED\n"))
    assert proc.returncode != 0
    assert "OSS-only" in proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# Backup, base image, rollback, hosted
# --------------------------------------------------------------------------- #

def test_failed_backup_stops_before_the_checkout_moves(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD),
                   STUB_BACKUP_JSON='{"status": "failed", "error": "disk full"}', STUB_BACKUP_RC="1")
    proc = inst.run("--yes", env=env)
    assert proc.returncode != 0
    assert "--skip-backup" in proc.stderr
    assert inst.version() == "1.0.0"
    assert not any(re.search(r" (build|up)\b", c) for c in inst.compose_calls())


def test_skip_backup_never_takes_a_backup(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_BOOT_LOG="")
    proc = inst.run("--yes", "--skip-backup", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(c.startswith("docker exec") and c.endswith(" backup") for c in inst.calls_list())


def test_base_image_rebuilt_only_when_the_range_touched_it(tmp_path):
    (tmp_path / "a").mkdir()
    untouched = Instance(tmp_path / "a", enterprise=False)
    env = _ent_env(untouched, STUB_CONFIG_FILES=untouched.config_files(_PROD), STUB_BOOT_LOG="")
    assert untouched.run("--yes", env=env).returncode == 0
    assert not any(c.startswith("build-base-image.sh") for c in untouched.calls_list())

    (tmp_path / "b").mkdir()
    touched = Instance(tmp_path / "b", enterprise=False, base_image_change=True)
    env = _ent_env(touched, STUB_CONFIG_FILES=touched.config_files(_PROD), STUB_BOOT_LOG="")
    proc = touched.run("--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(c.startswith("build-base-image.sh") for c in touched.calls_list())


def test_rollback_returns_code_and_enterprise_pin_to_the_recorded_version(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    assert inst.run("--yes", env=_ent_env(inst)).returncode == 0
    assert inst.version() == "1.1.0"
    inst.calls.write_text("")
    proc = inst.run("--rollback", "--yes", env=_ent_env(inst))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert inst.version() == "1.0.0"
    assert inst.ent_head() == inst.ent_pins[0]
    composes = inst.compose_calls()
    assert any(" up" in c for c in composes)
    for call in composes:
        assert f"-f {_ENT}" in call, call
    assert "database" in proc.stdout.lower(), "rollback must say the database is not restored automatically"


def test_already_current_is_a_no_op(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    _git(inst.root, "checkout", "-q", "v1.1.0")
    proc = inst.run("--yes", env=_ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "already" in proc.stdout.lower()
    assert inst.mutating_calls() == []


def test_hosted_update_pins_the_tag_checks_out_the_tree_and_delegates(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / ".env").write_text(
        "CREDENTIAL_ENCRYPTION_KEY=" + "a" * 64 + "\nTRINITY_IMAGE_TAG=v1.0.0\n"
        "FRONTEND_URL=https://trinity.example.com\n"
    )
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files("docker-compose.hosted.yml"), STUB_BOOT_LOG="")
    proc = inst.run("--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "TRINITY_IMAGE_TAG=v1.1.0" in (inst.root / ".env").read_text()
    assert inst.version() == "1.1.0", "the compose file must match the images (#2901)"
    assert any(c.startswith("start.sh --hosted --unattended") for c in inst.calls_list())


def test_hosted_refuses_a_branch_target(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / ".env").write_text("CREDENTIAL_ENCRYPTION_KEY=" + "a" * 64 + "\nTRINITY_IMAGE_TAG=v1.0.0\n")
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files("docker-compose.hosted.yml"))
    proc = inst.run("--yes", "--to", "main", env=env)
    assert proc.returncode != 0
    assert "release tag" in proc.stderr
    assert inst.mutating_calls() == []
    assert "TRINITY_IMAGE_TAG=v1.0.0" in (inst.root / ".env").read_text()


def test_hosted_delegate_failure_restores_tag_and_checkout(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    (inst.root / ".env").write_text(
        "CREDENTIAL_ENCRYPTION_KEY=" + "a" * 64 + "\nTRINITY_IMAGE_TAG=v1.0.0\n"
    )
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files("docker-compose.hosted.yml"), STUB_START_RC="1")
    proc = inst.run("--yes", env=env)
    assert proc.returncode != 0
    assert "TRINITY_IMAGE_TAG=v1.0.0" in (inst.root / ".env").read_text()


# --------------------------------------------------------------------------- #
# The in-container backup helper (stdlib only)
# --------------------------------------------------------------------------- #

def test_backup_helper_takes_a_verified_online_copy_under_a_pruned_name(tmp_path):
    db = tmp_path / "trinity.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER)")
    conn.execute("INSERT INTO users VALUES (1)")
    conn.commit()
    proc = subprocess.run(
        [sys.executable, "-I", "-", "backup"],
        input=_DB_HELPER.read_text(), capture_output=True, text=True,
        env={"TRINITY_DB_PATH": str(db), "PATH": os.environ.get("PATH", "")},
    )
    conn.close()
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["status"] == "ok"
    artifact = Path(result["path"])
    assert artifact.parent == tmp_path / "backups"
    assert artifact.exists()
    # The name must fall inside the #2216 prune patterns, or it grows forever.
    sys.path.insert(0, str(_REPO / "src" / "backend"))
    try:
        spec = importlib.util.spec_from_file_location("_bp", _REPO / "src/backend/db/backup_primitives.py")
        patterns = None
        if spec and spec.loader:
            try:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                patterns = mod.ARTIFACT_PATTERNS
            except Exception:
                patterns = None
        patterns = patterns or ("pre-migration-*.db",)
    finally:
        sys.path.pop(0)
    assert any(fnmatch.fnmatch(artifact.name, p) for p in patterns), artifact.name
    copy = sqlite3.connect(artifact)
    assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    copy.close()
    assert not list((tmp_path / "backups").glob("*.tmp*")), "no tmp residue"


def test_backup_helper_fails_loudly_on_a_missing_database(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-I", "-", "backup"], input=_DB_HELPER.read_text(),
        capture_output=True, text=True,
        env={"TRINITY_DB_PATH": str(tmp_path / "nope.db"), "PATH": os.environ.get("PATH", "")},
    )
    assert proc.returncode != 0
    assert json.loads(proc.stdout.strip().splitlines()[-1])["status"] == "failed"
    assert not (tmp_path / "nope.db").exists(), "the helper must never create an empty database"


# --------------------------------------------------------------------------- #
# `trinity update` — the CLI front door
# --------------------------------------------------------------------------- #

def _host_update():
    spec = importlib.util.spec_from_file_location("_host_update", _HOST_UPDATE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_locates_the_checkout_script_and_passes_arguments_through(tmp_path):
    mod = _host_update()
    (tmp_path / "scripts" / "deploy").mkdir(parents=True)
    script = tmp_path / "scripts" / "deploy" / "update.sh"
    script.write_text("#!/bin/bash\n")
    assert mod.find_update_script(str(tmp_path), env={}) == script
    assert mod.build_command(script, ["--check", "--to", "v1.1.0"]) == [
        "bash", str(script), "--check", "--to", "v1.1.0",
    ]
    # TRINITY_HOME is the fallback when --dir is not given.
    assert mod.find_update_script(None, env={"TRINITY_HOME": str(tmp_path)}) == script


def test_cli_refuses_a_directory_that_is_not_a_checkout(tmp_path):
    mod = _host_update()
    with pytest.raises(mod.NotACheckout) as exc:
        mod.find_update_script(str(tmp_path), env={})
    assert "scripts/deploy/update.sh" in str(exc.value)


# --------------------------------------------------------------------------- #
# Rollback across a migration — code alone is refused when the schema moved
# --------------------------------------------------------------------------- #

def _schema(inst: Instance, position: str | None) -> Path:
    f = inst.tmp / "schema.json"
    if position is None:
        f.write_text('{"status": "failed", "error": "backend down"}')
    else:
        f.write_text(json.dumps({"status": "ok", "backend": "sqlite", "position": position}))
    return f


def _updated(inst: Instance, schema_file: Path) -> dict:
    env = _ent_env(inst, STUB_SCHEMA_FILE=str(schema_file))
    proc = inst.run("--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert inst.version() == "1.1.0"
    inst.calls.write_text("")
    return env


def test_update_records_the_schema_position(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    _updated(inst, _schema(inst, "before01"))
    state = next(inst.state.iterdir()).read_text()
    assert "SCHEMA_BEFORE=before01" in state


def test_rollback_with_unchanged_schema_rolls_the_code_back(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    f = _schema(inst, "same0001")
    env = _updated(inst, f)
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert inst.version() == "1.0.0"
    assert "not touched" in proc.stdout


def test_rollback_after_a_migration_refuses_and_prints_restore_steps(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    f = _schema(inst, "before01")
    env = _updated(inst, f)
    _schema(inst, "after002")  # the update ran a migration
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode != 0
    err = proc.stderr
    assert "before01" in err and "after002" in err
    assert "/data/backups/pre-migration-20261015-120000.db" in err, "the backup must be named"
    assert f"docker compose -f {_PROD} -f {_ENT} stop backend scheduler" in err
    assert "cp /data/backups/pre-migration-20261015-120000.db /data/trinity.db" in err
    assert "--rollback --db-restored" in err
    # Code alone was NOT rolled back, and nothing was rebuilt or restarted.
    assert inst.version() == "1.1.0"
    assert inst.ent_head() == inst.ent_pins[1]
    assert not any(re.search(r" (build|up|stop)\b", c) for c in inst.compose_calls())


def test_rollback_refuses_when_the_schema_cannot_be_read(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    f = _schema(inst, "before01")
    env = _updated(inst, f)
    _schema(inst, None)
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode != 0
    assert "cannot be read" in proc.stderr
    assert inst.version() == "1.1.0"


def test_rollback_refuses_when_no_position_was_recorded(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    f = _schema(inst, None)  # unreadable at update time
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_SCHEMA_FILE=str(f), STUB_BOOT_LOG="")
    proc = inst.run("--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--db-restored" in proc.stderr, "the update must warn that rollback will need a restore"
    _schema(inst, "now00001")
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode != 0
    assert "No schema position was recorded" in proc.stderr
    assert inst.version() == "1.1.0"


def test_postgres_rollback_prints_pg_restore_of_the_dump(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    f = _schema(inst, "before01")
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_SCHEMA_FILE=str(f), STUB_BOOT_LOG="",
                   STUB_BACKUP_JSON='{"status": "ok", "path": "/data/backups/pre-update-20261015-120000.dump"}')
    assert inst.run("--yes", env=env).returncode == 0
    _schema(inst, "after002")
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode != 0
    assert "pg_restore --clean --if-exists --no-owner" in proc.stderr
    assert "/data/backups/pre-update-20261015-120000.dump" in proc.stderr


def test_rollback_with_db_restored_skips_the_schema_check(tmp_path):
    inst = Instance(tmp_path, enterprise=True)
    f = _schema(inst, "before01")
    env = _updated(inst, f)
    _schema(inst, None)  # the backend is stopped for the restore — unreadable is expected
    proc = inst.run("--rollback", "--db-restored", "--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert inst.version() == "1.0.0"
    assert inst.ent_head() == inst.ent_pins[0]
    assert not any(c.startswith("docker exec") for c in inst.calls_list())


def test_rebuild_base_forces_the_agent_base_image(tmp_path):
    inst = Instance(tmp_path, enterprise=False)
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_BOOT_LOG="")
    proc = inst.run("--yes", "--rebuild-base", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(c.startswith("build-base-image.sh") for c in inst.calls_list())


# --------------------------------------------------------------------------- #
# update-db.py: schema position + the fresh PostgreSQL dump
# --------------------------------------------------------------------------- #

def _helper(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-", *args], input=_DB_HELPER.read_text(),
                          capture_output=True, text=True, env=env)


def _last_json(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_sqlite_schema_position_moves_exactly_when_a_migration_is_recorded(tmp_path):
    db = tmp_path / "trinity.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES ('m1', 't')")
    conn.execute("CREATE TABLE agents (name TEXT)")
    conn.commit()
    env = {"TRINITY_DB_PATH": str(db), "PATH": os.environ.get("PATH", "")}
    first = _last_json(_helper(["schema"], env))
    conn.execute("INSERT INTO agents VALUES ('a')")  # ordinary writes do not move it
    conn.commit()
    same = _last_json(_helper(["schema"], env))
    conn.execute("INSERT INTO schema_migrations VALUES ('m2', 't')")
    conn.commit()
    moved = _last_json(_helper(["schema"], env))
    conn.execute("CREATE TABLE enterprise_schema_migrations (name TEXT, applied_at TEXT)")
    conn.execute("INSERT INTO enterprise_schema_migrations VALUES ('e1', 't')")
    conn.commit()
    ent = _last_json(_helper(["schema"], env))
    conn.close()
    assert first["status"] == "ok" and first["oss"] == 1
    assert same["position"] == first["position"]
    assert moved["position"] != first["position"]
    assert ent["position"] != moved["position"] and ent["enterprise"] == 1


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    return d


def _pg_env(tmp_path: Path, fakebin: Path) -> dict:
    return {
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(_REPO / "src" / "backend"),
        "DATABASE_URL": "postgresql+psycopg://trinity:s3cret@db:5432/trinity",
        "TRINITY_DB_PATH": str(tmp_path / "data" / "trinity.db"),
        "REDIS_URL": "redis://backend:pw@localhost:6379/0",
        "CALLS": str(tmp_path / "pg_calls.log"),
        "HOME": str(tmp_path),
    }


_PGDUMP = r"""printf 'argv %s\n' "$*" >> "$CALLS"; printf 'pgpassword %s\n' "$PGPASSWORD" >> "$CALLS"
while [ $# -gt 0 ]; do [ "$1" = "-f" ] && out="$2"; shift; done
printf 'PGDMPfake' > "$out"
"""


def test_postgres_backup_takes_a_fresh_dump_under_a_pruned_name(tmp_path):
    pytest.importorskip("sqlalchemy")
    fakebin = _fake_bin(tmp_path, "pg_dump", _PGDUMP)
    proc = _helper(["backup"], _pg_env(tmp_path, fakebin))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = _last_json(proc)
    artifact = Path(result["path"])
    assert artifact.parent == tmp_path / "data" / "backups"
    assert artifact.name.startswith("pre-update-") and artifact.suffix == ".dump"
    assert artifact.read_bytes().startswith(b"PGDMP")
    sys.path.insert(0, str(_REPO / "src" / "backend"))
    try:
        from db import backup_primitives as bp  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert any(fnmatch.fnmatch(artifact.name, p) for p in bp.ARTIFACT_PATTERNS), (
        "the per-update dump must fall under the #2216 prune, or it grows forever"
    )
    calls = (tmp_path / "pg_calls.log").read_text()
    assert "s3cret" not in calls.split("pgpassword")[0], "the password must never reach argv"
    assert "pgpassword s3cret" in calls
    assert "+psycopg" not in calls, "libpq rejects the SQLAlchemy driver suffix"


def test_postgres_backup_rejects_output_without_pgdmp_magic(tmp_path):
    pytest.importorskip("sqlalchemy")
    fakebin = _fake_bin(tmp_path, "pg_dump", 'while [ $# -gt 0 ]; do [ "$1" = "-f" ] && out="$2"; shift; done\necho junk > "$out"\n')
    proc = _helper(["backup"], _pg_env(tmp_path, fakebin))
    assert proc.returncode != 0
    assert _last_json(proc)["status"] == "failed"
    assert not list((tmp_path / "data" / "backups").glob("pre-update-*")), "no artifact, no tmp residue"


def test_postgres_schema_position_reads_alembic_and_enterprise(tmp_path):
    pytest.importorskip("sqlalchemy")
    psql = r"""case "$*" in
  *alembic_version*) printf '%s\n' "${HEADS:-0050_x}" ;;
  *to_regclass*) echo t ;;
  *enterprise_schema_migrations*) printf '%s\n' "${ENT:-e1|t}" ;;
esac
"""
    fakebin = _fake_bin(tmp_path, "psql", psql)
    env = _pg_env(tmp_path, fakebin)
    a = _last_json(_helper(["schema"], env))
    b = _last_json(_helper(["schema"], {**env, "HEADS": "0051_y"}))
    c = _last_json(_helper(["schema"], {**env, "ENT": "e1|t\ne2|t"}))
    assert a["status"] == "ok" and a["backend"] == "postgresql" and a["oss"] == "0050_x"
    assert len({a["position"], b["position"], c["position"]}) == 3


def test_rollback_refuses_when_neither_position_is_known(tmp_path):
    """Unknown before AND unknown now must not compare equal ("" == "")."""
    inst = Instance(tmp_path, enterprise=False)
    f = _schema(inst, None)
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_SCHEMA_FILE=str(f), STUB_BOOT_LOG="")
    assert inst.run("--yes", env=env).returncode == 0
    proc = inst.run("--rollback", "--yes", env=env)
    assert proc.returncode != 0
    assert "Refusing to roll back the code alone" in proc.stderr
    assert inst.version() == "1.1.0"


def test_printed_sqlite_restore_command_actually_restores(tmp_path):
    """Run the exact `sh -c` payload the refusal prints, with /data mapped to a
    temp dir: the backup becomes trinity.db, the live DB is kept aside, and the
    WAL/SHM sidecars (a corruption hazard beside a restored file) are gone."""
    (tmp_path / "i").mkdir()
    inst = Instance(tmp_path / "i", enterprise=False)
    f = _schema(inst, "before01")
    env = _ent_env(inst, STUB_CONFIG_FILES=inst.config_files(_PROD), STUB_SCHEMA_FILE=str(f), STUB_BOOT_LOG="")
    assert inst.run("--yes", env=env).returncode == 0
    _schema(inst, "after002")
    err = inst.run("--rollback", "--yes", env=env).stderr
    payload = re.search(r"^\s*'(cp /data/trinity\.db .*)'$", err, re.MULTILINE).group(1)

    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    (data / "trinity.db").write_text("live")
    (data / "trinity.db-wal").write_text("wal")
    (data / "trinity.db-shm").write_text("shm")
    (data / "backups" / "pre-migration-20261015-120000.db").write_text("backup")
    proc = subprocess.run(["sh", "-c", payload.replace("/data", str(data))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert (data / "trinity.db").read_text() == "backup"
    assert (data / "trinity.db.before-rollback").read_text() == "live"
    assert not (data / "trinity.db-wal").exists() and not (data / "trinity.db-shm").exists()
