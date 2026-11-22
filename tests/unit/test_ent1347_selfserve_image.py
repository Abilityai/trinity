"""Per-commit self-serve snapshot for DigitalOcean (abilityai/trinity-enterprise#1347).

A second image variant beside the Marketplace 1-Click: one commit in, one snapshot
out, and first boot writes only what is unique to the instance. Requirement:
docs/memory/requirements/infrastructure.md §8.14 (SSI-001..006).

Every script here is EXECUTED against a scratch filesystem with stubbed `docker`
and `start.sh`. A grep would pass a script that never runs. The template checks
at the end are pins on top of those: their consumer is Packer, which CI does not
install. `packer validate` runs when it is on PATH.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DO = _ROOT / "packer" / "digitalocean"
_SS = _DO / "selfserve"
_FIRSTBOOT = _SS / "firstboot.sh"
_CHECKOUT = _SS / "10-checkout.sh"
_FINISH = _SS / "80-finish.sh"
_ASSERT = _SS / "95-assert-clean.sh"
_TEMPLATE = _DO / "trinity-selfserve.pkr.hcl"
_MARKETPLACE = _DO / "trinity.pkr.hcl"
_AWS = _ROOT / "packer" / "aws" / "trinity.pkr.hcl"
_PROVISION = _DO / "scripts" / "01-provision.sh"
_BUILD = _DO / "scripts" / "selfserve-build.sh"
_BOOT_CHECK = _DO / "scripts" / "selfserve-boot-check.sh"

_BASE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": _BASE_PATH,
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


# --------------------------------------------------------------------------- #
# First boot (SSI-003, SSI-004)
# --------------------------------------------------------------------------- #

class _Box:
    """A scratch instance: /etc/trinity, /opt/trinity and stubbed tools."""

    def __init__(self, tmp: Path, *, enterprise: bool = True):
        self.tmp = tmp
        self.state = tmp / "etc-trinity"
        self.trinity = tmp / "opt-trinity"
        self.bin = tmp / "bin"
        self.calls = tmp / "calls"
        self.state.mkdir()
        self.trinity.mkdir()
        self.calls.mkdir()
        (self.state / "baked-image-tag").write_text("sha-abc1234\n")
        (self.trinity / ".env.example").write_text(
            "ADMIN_PASSWORD=\nPUBLIC_CHAT_URL=\nFRONTEND_URL=\nTRINITY_DATA_PATH=\n"
        )
        # start.sh stub: records its argv, the .env it was handed and the
        # environment variables a user-data password could arrive through.
        _exe(self.trinity / "scripts" / "deploy" / "start.sh", f"""#!/bin/bash
printf '%s\\n' "$@" > "{self.calls}/start.args"
cp .env "{self.calls}/start.env"
printf 'ADMIN_PASSWORD=%s\\n' "${{ADMIN_PASSWORD-<unset>}}" > "{self.calls}/start.environ"
ls -A trinity-data 2>/dev/null > "{self.calls}/start.data" || true
""")
        _exe(self.trinity / "scripts" / "deploy" / "docker-firewall.sh", "#!/bin/bash\nexit 0\n")
        _exe(self.bin / "docker", f"""#!/bin/bash
printf '%s\\n' "$*" >> "{self.calls}/docker"
exit 0
""")
        _exe(self.bin / "sleep", "#!/bin/bash\nexit 0\n")
        if enterprise:
            init = self.trinity / "src" / "backend" / "enterprise" / "backend" / "__init__.py"
            init.parent.mkdir(parents=True)
            init.write_text("")

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_FIRSTBOOT)],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{self.bin}:{_BASE_PATH}",
                "TRINITY_STATE_DIR": str(self.state),
                "TRINITY_DIR": str(self.trinity),
                "TRINITY_FIRSTBOOT_LOG": str(self.tmp / "firstboot.log"),
            },
        )

    def env_at_start(self) -> dict[str, str]:
        out = {}
        for line in (self.calls / "start.env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def started(self) -> bool:
        return (self.calls / "start.args").exists()


def test_first_boot_configures_claim_mode_and_the_public_name_before_the_first_start(tmp_path):
    box = _Box(tmp_path)
    (box.state / "public-name").write_text("Box-42.Example.COM\n")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr

    env = box.env_at_start()
    assert env["ADMIN_PASSWORD"] == ""
    assert env["ADMIN_PASSWORD_SOURCE"] == "instance-id"
    assert env["PUBLIC_CHAT_URL"] == "https://box-42.example.com"
    assert env["MCP_EXTERNAL_ACCESS"] == "true"
    assert env["TRINITY_IMAGE_TAG"] == "sha-abc1234"
    assert oct((box.trinity / ".env").stat().st_mode & 0o777) == "0o600"

    args = (box.calls / "start.args").read_text().split()
    assert args[:2] == ["--provision", "--cloud"] and args[2] == "digitalocean"
    for flag in ("--site-only", "--hosted", "--unattended"):
        assert flag in args
    assert args[args.index("--provenance") + 1] == "script"


def test_first_boot_mounts_the_enterprise_module_when_the_snapshot_carries_it(tmp_path):
    box = _Box(tmp_path, enterprise=True)
    (box.state / "public-name").write_text("box.example.com")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    docker = (box.calls / "docker").read_text()
    assert "-f docker-compose.hosted.yml -f docker-compose.prod.enterprise.yml up -d --no-deps --pull never backend" in docker
    assert "exec trinity-backend test -f /app/enterprise/backend/__init__.py" in docker


def test_first_boot_without_the_enterprise_module_leaves_the_backend_alone(tmp_path):
    box = _Box(tmp_path, enterprise=False)
    (box.state / "public-name").write_text("box.example.com")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    docker = box.calls / "docker"
    assert not docker.exists() or "prod.enterprise" not in docker.read_text()


def test_first_boot_discards_a_claim_and_a_password_planted_through_user_data(tmp_path):
    """SSI-004: the claim secret never comes from user-data, which every process
    on the droplet can read back from the metadata service."""
    box = _Box(tmp_path)
    (box.state / "public-name").write_text("box.example.com")
    (box.state / "admin-password").write_text("hunter2hunter2")
    data = box.trinity / "trinity-data"
    data.mkdir()
    (data / "setup-claim").write_text("email-bound:" + "a" * 64 + "\n")
    (data / "setup-claim.tmp").write_text("x")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.calls / "start.data").read_text().strip() == ""
    assert not (box.state / "admin-password").exists()
    assert (box.calls / "start.environ").read_text().strip() == "ADMIN_PASSWORD=<unset>"
    assert box.env_at_start()["ADMIN_PASSWORD"] == ""


def test_first_boot_honours_a_data_path_set_in_env_example(tmp_path):
    box = _Box(tmp_path)
    elsewhere = tmp_path / "data-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "setup-claim").write_text("planted")
    (box.trinity / ".env.example").write_text(f"ADMIN_PASSWORD=\nTRINITY_DATA_PATH={elsewhere}\n")
    (box.state / "public-name").write_text("box.example.com")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (elsewhere / "setup-claim").exists()


@pytest.mark.parametrize("name", [
    None,                       # no user-data at all
    "",                         # empty file
    "localhost",                # one label: no on-demand site matches it
    "203.0.113.7",              # an IP is not a name
    "a.b.c.d.e.f.g",            # seven labels: deeper than the Caddyfile's wildcards
    "bad_name.example.com",     # underscore
    "-lead.example.com",        # label starting with a hyphen
    "sp ace.example.com",       # whitespace inside
    "x" * 64 + ".example.com",  # label over 63
])
def test_first_boot_refuses_to_start_without_a_valid_public_name(tmp_path, name):
    box = _Box(tmp_path)
    if name is not None:
        (box.state / "public-name").write_text(name)
    r = box.run()
    assert r.returncode != 0
    assert not box.started(), "Trinity started with no usable public name"
    assert "public name" in (r.stdout + r.stderr).lower()


def test_first_boot_runs_clean_twice(tmp_path):
    """Support re-runs it by hand; a second run must not duplicate keys."""
    box = _Box(tmp_path)
    (box.state / "public-name").write_text("box.example.com")
    assert box.run().returncode == 0
    assert box.run().returncode == 0
    keys = [l.split("=", 1)[0] for l in (box.trinity / ".env").read_text().splitlines() if "=" in l]
    assert len(keys) == len(set(keys)), keys


# --------------------------------------------------------------------------- #
# Build checkout (SSI-001)
# --------------------------------------------------------------------------- #

TOKEN = "tok-SECRET-0123456789abcdef"


@pytest.fixture
def upstream(tmp_path):
    """A platform repo whose commit pins an enterprise repo, both served over file://."""
    ent = tmp_path / "enterprise"
    ent.mkdir()
    _git(ent, "init", "-q", "-b", "main")
    (ent / "backend").mkdir()
    (ent / "backend" / "__init__.py").write_text("# module\n")
    _git(ent, "add", "-A")
    _git(ent, "commit", "-q", "-m", "e1")
    pin = _git(ent, "rev-parse", "HEAD")
    # A later enterprise commit: the checkout must take the PIN, not the tip.
    (ent / "later.txt").write_text("later\n")
    _git(ent, "add", "-A")
    _git(ent, "commit", "-q", "-m", "e2")
    _git(ent, "config", "uploadpack.allowAnySHA1InWant", "true")

    plat = tmp_path / "platform"
    plat.mkdir()
    _git(plat, "init", "-q", "-b", "main")
    (plat / ".gitmodules").write_text(
        f'[submodule "src/backend/enterprise"]\n\tpath = src/backend/enterprise\n'
        f"\turl = file://{ent}\n\tupdate = none\n"
    )
    (plat / "README").write_text("platform\n")
    _git(plat, "add", "-A")
    _git(plat, "update-index", "--add", "--cacheinfo", f"160000,{pin},src/backend/enterprise")
    _git(plat, "commit", "-q", "-m", "p1")
    commit = _git(plat, "rev-parse", "HEAD")
    (plat / "README").write_text("newer\n")
    _git(plat, "commit", "-q", "-am", "p2")
    _git(plat, "config", "uploadpack.allowAnySHA1InWant", "true")
    return {"platform": plat, "enterprise": ent, "commit": commit, "pin": pin}


def _checkout(tmp_path, upstream, commit=None, token=TOKEN):
    dest = tmp_path / "opt-trinity"
    env = {
        "PATH": _BASE_PATH,
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "TRINITY_COMMIT": commit or upstream["commit"],
        "TRINITY_ENTERPRISE_TOKEN": token,
        "TRINITY_CHECKOUT_DIR": str(dest),
        "TRINITY_REPO_URL": f"file://{upstream['platform']}",
    }
    r = subprocess.run(["bash", str(_CHECKOUT)], capture_output=True, text=True, env=env)
    return r, dest


def test_checkout_is_the_commit_with_the_enterprise_module_at_its_pin(tmp_path, upstream):
    r, dest = _checkout(tmp_path, upstream)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(dest, "rev-parse", "HEAD") == upstream["commit"]
    ent = dest / "src" / "backend" / "enterprise"
    assert _git(ent, "rev-parse", "HEAD") == upstream["pin"]
    assert (ent / "backend" / "__init__.py").is_file()
    assert not (ent / "later.txt").exists(), "took the enterprise tip, not the pin"


def test_checkout_leaves_no_trace_of_the_token(tmp_path, upstream):
    r, dest = _checkout(tmp_path, upstream)
    assert r.returncode == 0, r.stdout + r.stderr
    import base64
    needles = [TOKEN.encode(), base64.b64encode(f"x-access-token:{TOKEN}".encode())]
    for p in [*dest.rglob("*"), *tmp_path.glob(".git*")]:
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            for n in needles:
                assert n not in data, f"token written to {p}"
    assert TOKEN not in r.stdout + r.stderr


@pytest.mark.parametrize("which", ["short", "unknown"])
def test_checkout_refuses_a_short_or_unknown_commit(tmp_path, upstream, which):
    commit = upstream["commit"][:7] if which == "short" else "f" * 40
    r, _ = _checkout(tmp_path, upstream, commit=commit)
    assert r.returncode != 0


def test_checkout_refuses_without_a_token(tmp_path, upstream):
    r, _ = _checkout(tmp_path, upstream, token="")
    assert r.returncode != 0
    assert "TRINITY_ENTERPRISE_TOKEN" in r.stderr


@pytest.mark.parametrize("given,expected", [
    ("git@github.com:Owner/repo.git", "https://github.com/Owner/repo.git"),
    ("git@github.com:Owner/repo", "https://github.com/Owner/repo.git"),
    ("https://github.com/Owner/repo.git", "https://github.com/Owner/repo.git"),
    ("ssh://git@github.com/Owner/repo.git", "https://github.com/Owner/repo.git"),
])
def test_the_submodule_url_is_fetched_over_https(given, expected):
    r = subprocess.run(
        ["bash", "-c", f'source "{_CHECKOUT}"; https_url "$1"', "_", given],
        capture_output=True, text=True, env={"PATH": _BASE_PATH},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


# --------------------------------------------------------------------------- #
# Snapshot clean check (SSI-002)
# --------------------------------------------------------------------------- #

class _Snapshot:
    """A scratch root laid out like a finished self-serve build."""

    def __init__(self, tmp: Path, upstream: dict):
        self.root = tmp / "root"
        self.bin = tmp / "bin"
        r, dest = _checkout(tmp, upstream)
        assert r.returncode == 0, r.stderr
        self.trinity = self.root / "opt" / "trinity"
        self.trinity.parent.mkdir(parents=True)
        shutil.move(str(dest), str(self.trinity))
        etc = self.root / "etc"
        (etc / "trinity").mkdir(parents=True)
        (etc / "trinity" / "baked-commit").write_text(upstream["commit"] + "\n")
        (etc / "trinity" / "baked-enterprise-pin").write_text(upstream["pin"] + "\n")
        (etc / "trinity" / "baked-image-tag").write_text(f"sha-{upstream['commit'][:7]}\n")
        (etc / "ssh").mkdir()
        (etc / "machine-id").write_text("")
        (self.root / "root").mkdir()
        self.state = tmp / "docker-state"
        self.state.mkdir()
        # docker stub: volumes/containers listed from files; images present
        # unless named in `missing-images`.
        _exe(self.bin / "docker", f"""#!/bin/bash
S="{self.state}"
case "$1 $2" in
  "volume ls") cat "$S/volumes" 2>/dev/null; exit 0 ;;
  "ps -aq")    cat "$S/containers" 2>/dev/null; exit 0 ;;
  "image inspect") grep -qxF "$3" "$S/missing-images" 2>/dev/null && exit 1; exit 0 ;;
esac
exit 0
""")

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_ASSERT)],
            capture_output=True, text=True,
            env={"PATH": f"{self.bin}:{_BASE_PATH}", "SNAPSHOT_ROOT": str(self.root),
                 "HOME": str(self.root / "root"), "GIT_CONFIG_NOSYSTEM": "1"},
        )


def test_a_clean_snapshot_passes(tmp_path, upstream):
    snap = _Snapshot(tmp_path, upstream)
    r = snap.run()
    assert r.returncode == 0, r.stdout + r.stderr


def _dirty(snap: _Snapshot, what: str, upstream: dict) -> str:
    t = snap.trinity
    if what == "host-key":
        (snap.root / "etc" / "ssh" / "ssh_host_ed25519_key").write_text("k")
        return "host key"
    if what == "machine-id":
        (snap.root / "etc" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        return "machine-id"
    if what == "dbus-machine-id":
        p = snap.root / "var" / "lib" / "dbus" / "machine-id"
        p.parent.mkdir(parents=True)
        p.write_text("0123456789abcdef0123456789abcdef\n")
        return "machine-id"
    if what == "env":
        (t / ".env").write_text("SECRET_KEY=abc\n")
        return ".env"
    if what == "data":
        (t / "trinity-data").mkdir()
        (t / "trinity-data" / "trinity.db").write_text("")
        return "data"
    if what == "claim":
        (snap.root / "etc" / "trinity" / "setup-claim").write_text("x")
        return "setup-claim"
    if what == "volume":
        (snap.state / "volumes").write_text("trinity_postgres-data\n")
        return "volume"
    if what == "container":
        (snap.state / "containers").write_text("abc123\n")
        return "container"
    if what == "extraheader":
        with open(t / ".git" / "config", "a") as f:
            f.write('[http]\n\textraHeader = Authorization: Basic eDp5\n')
        return "credential"
    if what == "url-credential":
        _git(t / "src" / "backend" / "enterprise", "remote", "set-url", "origin",
             "https://x-access-token:abc@github.com/o/r.git")
        return "credential"
    if what == "git-credentials":
        (snap.root / "root" / ".git-credentials").write_text("https://u:p@github.com\n")
        return "credential"
    if what == "pin":
        (snap.root / "etc" / "trinity" / "baked-enterprise-pin").write_text("f" * 40 + "\n")
        return "pin"
    if what == "no-enterprise":
        shutil.rmtree(t / "src" / "backend" / "enterprise")
        return "enterprise"
    if what == "image":
        (snap.state / "missing-images").write_text(
            f"ghcr.io/abilityai/trinity-backend:sha-{upstream['commit'][:7]}\n")
        return "image"
    raise AssertionError(what)


@pytest.mark.parametrize("what", [
    "host-key", "machine-id", "dbus-machine-id", "env", "data", "claim", "volume",
    "container", "extraheader", "url-credential", "git-credentials", "pin",
    "no-enterprise", "image",
])
def test_a_dirty_snapshot_fails_the_build_naming_what_it_found(tmp_path, upstream, what):
    snap = _Snapshot(tmp_path, upstream)
    word = _dirty(snap, what, upstream)
    r = snap.run()
    assert r.returncode != 0, f"{what} passed the clean check"
    assert word in (r.stdout + r.stderr), r.stdout + r.stderr


# --------------------------------------------------------------------------- #
# Marketplace unchanged + template wiring
# --------------------------------------------------------------------------- #

def _provision_checkout_block() -> str:
    text = _PROVISION.read_text()
    start = text.index("# --- checkout ---")
    end = text.index("# --- end checkout ---")
    return text[start:end]


def test_marketplace_provision_still_clones_the_tag(tmp_path):
    bin_ = tmp_path / "bin"
    _exe(bin_ / "git", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{tmp_path}/git"\n')
    script = "set -euo pipefail\nTRINITY_IMAGE_TAG=v9.9.9\n" + _provision_checkout_block()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{bin_}:{_BASE_PATH}"})
    assert r.returncode == 0, r.stderr
    assert "clone --depth 1 --branch v9.9.9 https://github.com/abilityai/trinity.git /opt/trinity" in (tmp_path / "git").read_text()


def test_provision_with_a_prepared_checkout_does_not_clone(tmp_path):
    bin_ = tmp_path / "bin"
    _exe(bin_ / "git", f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{tmp_path}/git"\n')
    script = ("set -euo pipefail\nTRINITY_IMAGE_TAG=sha-abc1234\nTRINITY_CHECKOUT_READY=1\n"
              + _provision_checkout_block())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{bin_}:{_BASE_PATH}"})
    assert not (tmp_path / "git").exists(), "cloned over the prepared checkout"
    # No /opt/trinity/.git on a test machine: the guard must refuse, not proceed.
    if not Path("/opt/trinity/.git").exists():
        assert r.returncode != 0


def test_marketplace_templates_never_set_the_prepared_checkout_switch():
    for t in (_MARKETPLACE, _AWS):
        assert "TRINITY_CHECKOUT_READY" not in t.read_text(), t
    assert "selfserve" not in _MARKETPLACE.read_text()


def test_selfserve_template_orders_its_steps():
    t = _TEMPLATE.read_text()
    order = [
        "cloud-init status --wait",
        "selfserve/10-checkout.sh",
        "scripts/01-provision.sh",
        "selfserve/80-finish.sh",
        "scripts/90-cleanup-and-check.sh",
        "selfserve/95-assert-clean.sh",
    ]
    idx = [t.index(s) for s in order]
    assert idx == sorted(idx), dict(zip(order, idx))
    assert "TRINITY_CHECKOUT_READY=1" in t
    import re
    assert re.search(r'image_tag\s*=\s*"sha-\$\{substr\(var\.commit, 0, 7\)\}"', t)


def test_scripts_parse():
    for p in (_FIRSTBOOT, _CHECKOUT, _FINISH, _ASSERT, _BUILD, _BOOT_CHECK, _PROVISION):
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{p.name}: {r.stderr}"


def test_build_refuses_a_bad_commit_before_touching_the_cloud(tmp_path):
    r = subprocess.run(["bash", str(_BUILD), "not-a-sha"], capture_output=True, text=True,
                       env={"PATH": _BASE_PATH, "HOME": str(tmp_path)})
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_boot_check_refuses_bad_arguments(tmp_path):
    r = subprocess.run(["bash", str(_BOOT_CHECK), "abc", "zz"], capture_output=True, text=True,
                       env={"PATH": _BASE_PATH, "HOME": str(tmp_path)})
    assert r.returncode == 2


@pytest.mark.skipif(shutil.which("packer") is None, reason="packer not installed")
def test_packer_validates_the_selfserve_template():
    env = {**os.environ, "PKR_VAR_do_token": "x", "PKR_VAR_enterprise_git_token": "x",
           "PKR_VAR_commit": "0123456789abcdef0123456789abcdef01234567"}
    r = subprocess.run(["packer", "validate", "-syntax-only", _TEMPLATE.name],
                       cwd=_DO, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
