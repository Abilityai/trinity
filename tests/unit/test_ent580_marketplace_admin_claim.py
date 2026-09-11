"""trinity-enterprise#580 — a marketplace droplet's admin is created in the browser.

A one-click droplet used to generate an admin password at first boot and print
it in the MOTD, so every operator had to open a terminal to learn how to log in.
Now first boot provisions NO admin: the first person to open the instance gets
/setup and creates it. The operator-supplied path (a user-data password) is
unchanged: pre-provisioned, wizard closed, MOTD says "the password you supplied".

Both paths are pinned here, layer by layer, because each layer can undo the
other two on its own:

* **start.sh** (`ensure_admin_password`) — shared with EVERY server install. It
  leaves ADMIN_PASSWORD blank only under ADMIN_PASSWORD_SOURCE=browser and
  persists that marker, because it is also the documented UPDATE path: a re-run
  that generated a password would have the backend re-sync it over the one the
  operator chose in the browser. Without the marker it behaves exactly as before.
* **compose** — hosted only: `${ADMIN_PASSWORD?}` (unset still refuses to render,
  an explicit blank renders) plus `ADMIN_PASSWORD_SOURCE=${…:-unset}`. Prod keeps
  `:?` — nothing that claims in the browser runs it.
* **the /setup backstop** — blank + `ADMIN_PASSWORD_SOURCE=unset` (a hand-run
  hosted compose) is refused; absent (dev) and `browser` are not.
* **the MOTD** — prints the URL to claim, never a password.
* **the backend** — the blank-env branch already existed; the only new logic is
  the /setup backstop above. What is pinned is the end-to-end contract: blank → no admin, flag false, /setup
  provisions, and a reboot never re-syncs over the browser-set password; set →
  admin at boot, flag true, /setup refuses.
* **J01** — the journey catalog names both variants.

Import isolation: `database` / `routers.setup` are imported lazily (the #2381
convention — see test_2381_setup_fail_closed.py).
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_ENV_FILE_SH = _ROOT / "scripts" / "deploy" / "env-file.sh"
_FIRSTBOOT = _ROOT / "packer/digitalocean/files/opt/trinity-firstboot/firstboot.sh"
_MOTD = _ROOT / "packer/digitalocean/files/etc/update-motd.d/99-trinity"
# Hosted only: the marketplace (and `start.sh --hosted`) runs it; nothing that
# claims in the browser runs prod, which keeps `:?` (pinned below).
_COMPOSES = ("docker-compose.hosted.yml",)

_BASH = shutil.which("bash") is not None
_SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


# ---------------------------------------------------------------------------
# start.sh — the installer every server install shares
# ---------------------------------------------------------------------------

def _extract(func: str) -> str:
    """One top-level function out of start.sh, verbatim (the test_2390 idiom)."""
    m = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", _START.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"start.sh no longer defines a top-level `{func}()`"
    return m.group(0)


def _ensure_admin_password(tmp_path: Path, dotenv: str | None, env: dict[str, str]):
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    script = "\n".join([
        "set -e",
        f". '{_ENV_FILE_SH}'",
        _extract("env_value"),
        'UNATTENDED="${UNATTENDED:-0}"',
        'GENERATED_ADMIN_PASSWORD=""',
        "ADMIN_IN_BROWSER=0",
        _extract("ensure_admin_password"),
        "ensure_admin_password",
        'printf "GENERATED=%s\\nIN_BROWSER=%s\\n" "$GENERATED_ADMIN_PASSWORD" "$ADMIN_IN_BROWSER"',
    ])
    r = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": _SYS_PATH, **env},
    )
    vals = dict(l.split("=", 1) for l in r.stdout.splitlines() if l.startswith(("GENERATED=", "IN_BROWSER=")))
    dot = (tmp_path / ".env").read_text() if (tmp_path / ".env").exists() else ""
    return r, vals, dot


def _lines(dot: str, key: str) -> list[str]:
    return [l for l in dot.splitlines() if l.startswith(f"{key}=")]


_TEMPLATE = "ADMIN_USERNAME=admin\nADMIN_PASSWORD=\n"  # what .env.example ships


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_first_boot_claim_leaves_the_password_blank_and_persists_why(tmp_path):
    """The claimable boot: firstboot exports the marker and passes --unattended,
    which on any other install means "generate one"."""
    r, vals, dot = _ensure_admin_password(
        tmp_path, _TEMPLATE, {"ADMIN_PASSWORD_SOURCE": "browser", "UNATTENDED": "1"}
    )
    assert r.returncode == 0, r.stderr
    assert vals == {"GENERATED": "", "IN_BROWSER": "1"}
    assert _lines(dot, "ADMIN_PASSWORD") == ["ADMIN_PASSWORD="], (
        "the password must stay EXPLICITLY blank — compose's `${ADMIN_PASSWORD?}` "
        "refuses an unset one"
    )
    assert _lines(dot, "ADMIN_PASSWORD_SOURCE") == ["ADMIN_PASSWORD_SOURCE=browser"]


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_claim_writes_an_explicit_blank_when_the_env_has_no_line(tmp_path):
    r, vals, dot = _ensure_admin_password(
        tmp_path, "ADMIN_USERNAME=admin\n", {"ADMIN_PASSWORD_SOURCE": "browser"}
    )
    assert r.returncode == 0, r.stderr
    assert _lines(dot, "ADMIN_PASSWORD") == ["ADMIN_PASSWORD="]


@pytest.mark.skipif(not _BASH, reason="bash required")
@pytest.mark.parametrize("unattended", ["0", "1"])
def test_a_later_update_run_leaves_a_claimed_instance_alone(tmp_path, unattended):
    """`start.sh --hosted` is the documented update, with or without
    --unattended. Generating here would put a password in .env that the backend
    re-syncs over the browser-set one on the next boot; refusing would block the
    update. The marker in .env — not the environment — is what it reads."""
    before = "ADMIN_USERNAME=admin\nADMIN_PASSWORD=\nADMIN_PASSWORD_SOURCE=browser\n"
    r, vals, dot = _ensure_admin_password(tmp_path, before, {"UNATTENDED": unattended})
    assert r.returncode == 0, r.stderr
    assert vals == {"GENERATED": "", "IN_BROWSER": "1"}
    assert dot == before, "an already-claimed .env must be left byte-for-byte alone"


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_a_supplied_password_wins_over_the_marker(tmp_path):
    """The pre-provisioned path, and the recovery path for a forgotten browser
    password: a real ADMIN_PASSWORD in .env is used, never blanked."""
    before = "ADMIN_PASSWORD=Supplied-Pass-2026!\n"
    r, vals, dot = _ensure_admin_password(
        tmp_path, before, {"ADMIN_PASSWORD_SOURCE": "browser", "UNATTENDED": "1"}
    )
    assert r.returncode == 0, r.stderr
    assert vals == {"GENERATED": "", "IN_BROWSER": "0"}
    assert dot == before, "the marker must not be persisted over a supplied password"


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_without_the_marker_unattended_still_generates(tmp_path):
    """Every non-marketplace install: unchanged."""
    r, vals, dot = _ensure_admin_password(tmp_path, _TEMPLATE, {"UNATTENDED": "1"})
    assert r.returncode == 0, r.stderr
    # {16,24}, not {24}: the generator strips `+`/`/` out of 24 base64 chars, so
    # it often yields 22-23. Pre-existing and harmless; not this issue's to change.
    assert re.fullmatch(r"[A-Za-z0-9]{16,24}", vals["GENERATED"]), vals
    assert _lines(dot, "ADMIN_PASSWORD") == [f"ADMIN_PASSWORD={vals['GENERATED']}"]
    assert not _lines(dot, "ADMIN_PASSWORD_SOURCE")


@pytest.mark.skipif(not _BASH, reason="bash required")
@pytest.mark.parametrize("marker", [None, "yes", "BROWSER", ""])
def test_without_the_marker_an_interactive_run_still_refuses(tmp_path, marker):
    """Unchanged, and only the exact value `browser` opts in."""
    env = {} if marker is None else {"ADMIN_PASSWORD_SOURCE": marker}
    r, _, dot = _ensure_admin_password(tmp_path, _TEMPLATE, env)
    assert r.returncode == 1
    assert "ERROR: ADMIN_PASSWORD is blank in .env." in r.stderr
    assert not _lines(dot, "ADMIN_PASSWORD_SOURCE")


def test_start_sh_runs_the_function_and_tells_the_operator_where_to_go() -> None:
    text = _START.read_text()
    assert re.search(r"^ensure_admin_password$", text, re.MULTILINE), (
        "start.sh defines ensure_admin_password but no longer calls it"
    )
    card = text[text.index("── Your next steps"):]
    assert 'elif [ "$ADMIN_IN_BROWSER" = "1" ]' in card, (
        "the next-steps card must not tell a claim install to log in with a "
        "password from .env — there is none"
    )


def test_firstboot_exports_the_key_start_sh_reads() -> None:
    """Two files, one key. A rename on either side makes the claim path refuse
    (interactive) or generate (unattended) — silently, at first boot."""
    assert "export ADMIN_PASSWORD_SOURCE=browser" in _FIRSTBOOT.read_text()
    assert "${ADMIN_PASSWORD_SOURCE:-$(env_value ADMIN_PASSWORD_SOURCE)}" in _extract(
        "ensure_admin_password"
    )


_EMPTY_FORMS = ['ADMIN_PASSWORD=""\n', "ADMIN_PASSWORD=''\n", "ADMIN_PASSWORD= \n"]


@pytest.mark.skipif(not _BASH, reason="bash required")
@pytest.mark.parametrize("line", _EMPTY_FORMS)
def test_quoted_or_space_empty_is_blank_unattended_generates(tmp_path, line):
    """Compose renders these EMPTY; a raw `=.+` grep called them set, so an
    unattended run booted a hosted stack with no password and no marker."""
    r, vals, dot = _ensure_admin_password(tmp_path, "ADMIN_USERNAME=admin\n" + line, {"UNATTENDED": "1"})
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"[A-Za-z0-9]{16,24}", vals["GENERATED"]), vals
    assert _lines(dot, "ADMIN_PASSWORD") == [f"ADMIN_PASSWORD={vals['GENERATED']}"]


@pytest.mark.skipif(not _BASH, reason="bash required")
@pytest.mark.parametrize("line", _EMPTY_FORMS)
def test_quoted_or_space_empty_is_blank_interactive_refuses(tmp_path, line):
    r, _, _ = _ensure_admin_password(tmp_path, line, {})
    assert r.returncode == 1
    assert "ERROR: ADMIN_PASSWORD is blank in .env." in r.stderr


@pytest.mark.skipif(not _BASH, reason="bash required")
@pytest.mark.parametrize("line", _EMPTY_FORMS)
def test_quoted_or_space_empty_with_the_marker_stays_blank(tmp_path, line):
    before = line + "ADMIN_PASSWORD_SOURCE=browser\n"
    r, vals, dot = _ensure_admin_password(tmp_path, before, {"UNATTENDED": "1"})
    assert r.returncode == 0, r.stderr
    assert vals == {"GENERATED": "", "IN_BROWSER": "1"}
    assert dot == before, "a blank line compose already reads as empty is left alone"


# ---------------------------------------------------------------------------
# compose — hosted relaxes, prod does not
# ---------------------------------------------------------------------------

def _env_list(compose: str, service: str) -> list[str]:
    data = yaml.safe_load((_ROOT / compose).read_text())
    return data["services"][service]["environment"]


@pytest.mark.parametrize("compose", _COMPOSES)
@pytest.mark.parametrize(("service", "var"), [("backend", "ADMIN_PASSWORD"), ("mcp-server", "TRINITY_PASSWORD")])
def test_compose_refuses_unset_but_accepts_blank(compose, service, var):
    entry = next(e for e in _env_list(compose, service) if e.startswith(f"{var}="))
    assert entry.startswith(f"{var}=${{ADMIN_PASSWORD?"), (
        f"{compose} {service}: `${{ADMIN_PASSWORD?...}}` is the contract — `:?` "
        f"refuses the browser-claim path, `:-` would let an UNSET password through"
    )


@pytest.mark.parametrize(("service", "var"), [("backend", "ADMIN_PASSWORD"), ("mcp-server", "TRINITY_PASSWORD")])
def test_prod_compose_still_refuses_a_blank_password(service, var):
    entry = next(e for e in _env_list("docker-compose.prod.yml", service) if e.startswith(f"{var}="))
    assert entry.startswith(f"{var}=${{ADMIN_PASSWORD:?"), (
        f"docker-compose.prod.yml {service}: must stay `:?` — source builds run it "
        f"and nothing claims in the browser there"
    )


def test_hosted_backend_forwards_the_claim_marker_defaulting_to_unset():
    """The /setup backstop's input: a hand-run hosted compose with no marker."""
    assert "ADMIN_PASSWORD_SOURCE=${ADMIN_PASSWORD_SOURCE:-unset}" in _env_list(
        "docker-compose.hosted.yml", "backend"
    )


def test_hosted_mcp_server_needs_no_password_by_default():
    """What makes a blank TRINITY_PASSWORD safe: API-key mode is the default."""
    assert "MCP_REQUIRE_API_KEY=${MCP_REQUIRE_API_KEY:-true}" in _env_list(
        "docker-compose.hosted.yml", "mcp-server"
    )


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _render(compose: str, tmp_path: Path, dotenv_text: str):
    """Resolved by Compose itself, not read off the YAML."""
    redis = secrets.token_hex(24)
    base_env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
    dotenv = tmp_path / "claim.env"
    dotenv.write_text(dotenv_text + f"REDIS_PASSWORD={redis}\nREDIS_BACKEND_PASSWORD={redis}\n")
    return subprocess.run(
        ["docker", "compose", "--env-file", str(dotenv), "-f", compose, "config"],
        cwd=_ROOT, capture_output=True, text=True, timeout=120, env=base_env,
    )


@pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")
@pytest.mark.parametrize("compose", _COMPOSES)
def test_compose_render_unset_fails_blank_renders(compose, tmp_path):
    """`.env`'s `KEY=` is what start.sh writes on the claim path."""
    unset = _render(compose, tmp_path, "")
    assert unset.returncode != 0 and "ADMIN_PASSWORD" in unset.stderr, "an unset password must still refuse"

    blank = _render(compose, tmp_path, "ADMIN_PASSWORD=\nADMIN_PASSWORD_SOURCE=browser\n")
    assert blank.returncode == 0, blank.stderr
    services = yaml.safe_load(blank.stdout)["services"]
    assert services["backend"]["environment"]["ADMIN_PASSWORD"] == ""
    assert services["backend"]["environment"]["ADMIN_PASSWORD_SOURCE"] == "browser"
    assert services["mcp-server"]["environment"]["TRINITY_PASSWORD"] == ""

    # Hand-run with no marker: renders, but /setup sees `unset` and refuses.
    no_marker = _render(compose, tmp_path, "ADMIN_PASSWORD=\n")
    assert no_marker.returncode == 0, no_marker.stderr
    assert yaml.safe_load(no_marker.stdout)["services"]["backend"]["environment"]["ADMIN_PASSWORD_SOURCE"] == "unset"


@pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")
def test_prod_compose_render_refuses_a_blank_password(tmp_path):
    blank = _render("docker-compose.prod.yml", tmp_path, "ADMIN_PASSWORD=\nADMIN_PASSWORD_SOURCE=browser\n")
    assert blank.returncode != 0 and "ADMIN_PASSWORD" in blank.stderr, (
        "prod must refuse a blank password even with the marker — only hosted is claimable"
    )


# ---------------------------------------------------------------------------
# The MOTD — the URL to claim, never a password
# ---------------------------------------------------------------------------

def _motd(tmp_path: Path, source: str | None, *, curl_out: str = "", curl_rc: int = 0) -> str:
    state = tmp_path / "state"
    state.mkdir()
    (state / "public-ip").write_text("203.0.113.7\n")
    (state / "tls-status").write_text("ok\n")
    if source is not None:
        (state / "admin-credentials").write_text(f"source={source}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(f"#!/bin/sh\nprintf '%s' '{curl_out}'\nexit {curl_rc}\n")
    fake_curl.chmod(0o755)
    script = _MOTD.read_text().replace("STATE_DIR=/etc/trinity", f"STATE_DIR='{state}'", 1)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{bin_dir}:{_SYS_PATH}"})
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_motd_on_an_unclaimed_droplet_says_open_the_url(tmp_path):
    out = _motd(tmp_path, "browser", curl_out='{"setup_completed":false,"setup_available":true}')
    assert "https://203.0.113.7" in out
    assert "none yet" in out and "create it" in out
    assert "Login:" not in out


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_motd_fails_toward_the_claim_action_when_the_backend_is_down(tmp_path):
    out = _motd(tmp_path, "browser", curl_rc=7)
    assert "none yet" in out


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_motd_after_the_claim_stops_urging_it(tmp_path):
    out = _motd(tmp_path, "browser", curl_out='{"setup_completed":true,"setup_available":true}')
    assert "created in the browser" in out
    assert "none yet" not in out


@pytest.mark.skipif(not _BASH, reason="bash required")
def test_motd_on_the_supplied_password_path_is_unchanged(tmp_path):
    out = _motd(tmp_path, "user-data")
    assert "Login:   admin / the password you supplied in user-data" in out
    assert "none yet" not in out


def test_motd_never_reads_a_password() -> None:
    code = "\n".join(l for l in _MOTD.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert "password=" not in code, "the MOTD must not read (so cannot print) a password"


# ---------------------------------------------------------------------------
# The backend — both paths end to end on a real SQLite file
# ---------------------------------------------------------------------------

def _database():
    import database as m
    return m


def _setup():
    import routers.setup as m
    return m


def _mkdb(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, "
        "password_hash TEXT, role TEXT, email TEXT, created_at TEXT, updated_at TEXT)"
    )
    cur.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    return conn, cur


class _SqliteSetupDB:
    """The five calls routers.setup makes, over the same file the boot path wrote."""

    def __init__(self, conn):
        self.conn = conn

    def get_user_by_username(self, username):
        row = self.conn.execute("SELECT username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        return None if row is None else {"username": row[0], "password": row[1]}

    def get_setting_value(self, key, default=None):
        row = self.conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else row[0]

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO system_settings VALUES (?, ?, 'now')", (key, value))
        self.conn.commit()

    def update_user_password(self, username, hashed):
        if not self.conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hashed, username)).rowcount:
            self.conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')", (username, hashed))
        self.conn.commit()

    def update_user(self, username, updates):
        self.conn.execute("UPDATE users SET email = ? WHERE username = ?", (updates["email"], username))
        self.conn.commit()


def _boot(conn, cur, monkeypatch, admin_password: str):
    """The two #2381 boot steps, in init_database's order, under a given env."""
    monkeypatch.setenv("ADMIN_PASSWORD", admin_password)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    db = _database()
    db._ensure_admin_user(cur, conn)
    db._mark_setup_completed_if_provisioned(cur, conn)


def _claim(conn, monkeypatch, password: str = "Claimed-In-Browser-26!"):
    setup = _setup()
    monkeypatch.setattr(setup, "db", _SqliteSetupDB(conn))
    monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
    data = setup.SetAdminPasswordRequest(password=password, confirm_password=password, email="op@example.com")
    return asyncio.run(setup.set_admin_password(data, None, BackgroundTasks()))


def test_blank_password_boot_is_claimable_and_the_claim_survives_a_reboot(tmp_path, monkeypatch):
    conn, cur = _mkdb(tmp_path)
    _boot(conn, cur, monkeypatch, "")

    ops = _SqliteSetupDB(conn)
    assert ops.get_user_by_username("admin") is None, "a blank env must not provision an admin"
    assert ops.get_setting_value("setup_completed", "false") != "true", "the wizard must be open"

    assert _claim(conn, monkeypatch)["success"] is True
    claimed = ops.get_user_by_username("admin")["password"]
    assert claimed.startswith("$2"), "the browser-chosen password is stored bcrypt-hashed"
    assert ops.get_setting_value("setup_completed") == "true"

    # Reboot: the env is still blank (ADMIN_PASSWORD_SOURCE=browser keeps it so).
    _boot(conn, cur, monkeypatch, "")
    assert ops.get_user_by_username("admin")["password"] == claimed, (
        "a blank env must never re-sync over the password set in the browser"
    )
    assert ops.get_setting_value("setup_completed") == "true"

    # And the door the claim closed stays closed.
    with pytest.raises(HTTPException) as exc:
        _claim(conn, monkeypatch, "Squatter-Password-99!")
    assert exc.value.status_code == 403
    assert ops.get_user_by_username("admin")["password"] == claimed


def test_supplied_password_boot_closes_the_wizard(tmp_path, monkeypatch):
    conn, cur = _mkdb(tmp_path)
    _boot(conn, cur, monkeypatch, "Supplied-Pass-2026!")

    ops = _SqliteSetupDB(conn)
    provisioned = ops.get_user_by_username("admin")["password"]
    assert provisioned.startswith("$2")
    assert ops.get_setting_value("setup_completed") == "true"

    with pytest.raises(HTTPException) as exc:
        _claim(conn, monkeypatch)
    assert exc.value.status_code == 403
    assert ops.get_user_by_username("admin")["password"] == provisioned


def _marker(monkeypatch, source):
    if source is None:
        monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
    else:
        monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", source)


@pytest.mark.parametrize("source", ["browser", None], ids=["marketplace", "absent-dev-compose"])
def test_blank_password_is_claimable_with_the_marker_or_without_the_variable(tmp_path, monkeypatch, source):
    conn, cur = _mkdb(tmp_path)
    _boot(conn, cur, monkeypatch, "")
    _marker(monkeypatch, source)
    assert _claim(conn, monkeypatch)["success"] is True


def test_blank_password_on_a_hand_run_hosted_compose_is_refused(tmp_path, monkeypatch):
    """Hosted renders `ADMIN_PASSWORD_SOURCE=unset` when nothing opted in: the
    instance is not the marketplace, so its first visitor must not own it."""
    conn, cur = _mkdb(tmp_path)
    _boot(conn, cur, monkeypatch, "")
    _marker(monkeypatch, "unset")
    monkeypatch.setattr(_setup(), "hash_password", lambda p: pytest.fail("hashed before refusing"))
    with pytest.raises(HTTPException) as exc:
        _claim(conn, monkeypatch)
    assert exc.value.status_code == 403
    assert "ADMIN_PASSWORD" in exc.value.detail and "start.sh --hosted" in exc.value.detail
    ops = _SqliteSetupDB(conn)
    assert ops.get_user_by_username("admin") is None
    assert ops.get_setting_value("setup_completed", "false") != "true"


def test_existing_admin_refusal_still_comes_first(tmp_path, monkeypatch):
    """#2381's check is unchanged and runs before the backstop."""
    conn, cur = _mkdb(tmp_path)
    _boot(conn, cur, monkeypatch, "Supplied-Pass-2026!")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    _marker(monkeypatch, "unset")
    with pytest.raises(HTTPException) as exc:
        _claim(conn, monkeypatch)
    assert exc.value.status_code == 403
    assert "already has an administrator account" in exc.value.detail


# ---------------------------------------------------------------------------
# J01 — the cold-install journey names both shapes
# ---------------------------------------------------------------------------

def test_j01_declares_both_marketplace_variants():
    catalog = yaml.safe_load((_ROOT / "tests/journeys/catalog.yaml").read_text())
    j01 = next(j for j in catalog["journeys"] if j["id"] == "J01")
    assert "digitalocean" in j01["lanes"]
    assert {v["name"] for v in j01.get("variants") or []} == {"claimable", "pre-provisioned"}
    rendered = (_ROOT / "docs/testing/JOURNEYS.md").read_text()
    assert "**J01 · claimable**" in rendered and "**J01 · pre-provisioned**" in rendered
