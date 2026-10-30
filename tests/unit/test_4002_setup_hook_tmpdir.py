"""#4002 — the setup hook survives the orphan sweep, TMPDIR stays bounded,
and a failed setup script is visible.

Three surfaces:

  * ``orphan_allowlist.resolve_allowlist`` protects the running
    ``bash /home/developer/.trinity/setup.sh`` and its descendant tree
    (apt/pip/npm children) with no user allow file. A long install used to
    outlive the 90 s sweep grace and get SIGKILLed mid-install.
  * ``startup.sh`` empties the default ``TMPDIR`` (``~/.tmp``, on the home
    volume) at boot and records ``~/.trinity/setup-status.json`` after
    ``setup.sh``. Both blocks run here for real under bash, with
    ``/home/developer`` pointed at a temp dir.
  * compatibility check F-014 reads the status file.

Allowlist tests drive a synthetic /proc tree (test_1501 harness).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

from agent_server.utils import orphan_allowlist as oa  # noqa: E402
from services.compatibility.static_checks import run_static  # noqa: E402
from services.compatibility import spec  # noqa: E402

_STARTUP = _BASE_IMAGE / "startup.sh"
_SETUP_CMD = "bash /home/developer/.trinity/setup.sh"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
#   1    init
#   50   agent server (sweep caller)
#   800  ppid 1    setup.sh (exact startup.sh invocation)
#   801  ppid 800  apt-get install ...
#   802  ppid 801  dpkg child
#   600  ppid 1    leaked orphan
def _fake_proc(monkeypatch, cmd_800: str):
    ppid = {1: 0, 50: 1, 800: 1, 801: 800, 802: 801, 600: 1}
    cmd = {
        1: "/app/startup.sh",
        50: "python3 /app/agent-server.py",
        800: cmd_800,
        801: "apt-get install -y ffmpeg",
        802: "/usr/bin/dpkg --unpack x.deb",
        600: "python3 leaked.py",
    }
    monkeypatch.setattr(oa.os, "listdir", lambda p: [str(p_) for p_ in ppid])
    monkeypatch.setattr(oa, "_read_ppid", lambda pid: ppid.get(pid))
    monkeypatch.setattr(oa, "_read_comm", lambda pid: None)
    monkeypatch.setattr(oa, "_read_cmdline", lambda pid: cmd.get(pid))


def test_setup_hook_and_its_children_survive_without_an_allow_file(monkeypatch):
    _fake_proc(monkeypatch, _SETUP_CMD)
    allow = oa.resolve_allowlist(50, cmdline_patterns=[])
    assert {800, 801, 802}.issubset(allow)
    assert 600 not in allow


def test_setup_hook_protection_ignores_the_user_allow_file(monkeypatch, tmp_path):
    """Loaded from the file path (the production default), not passed in."""
    _fake_proc(monkeypatch, _SETUP_CMD)
    monkeypatch.setenv(oa._ALLOWLIST_ENV, str(tmp_path / "absent.allow"))
    allow = oa.resolve_allowlist(50)
    assert {800, 801, 802}.issubset(allow)


@pytest.mark.parametrize("near_miss", [
    "bash /tmp/setup.sh",
    "bash /home/developer/.trinity/setup.sh.bak",
    "bash /home/developer/.trinity/setup.sh --extra",
    "sh /home/developer/.trinity/setup.sh",
])
def test_near_miss_cmdline_is_not_protected(monkeypatch, near_miss):
    _fake_proc(monkeypatch, near_miss)
    allow = oa.resolve_allowlist(50, cmdline_patterns=[])
    assert not {800, 801, 802} & allow


def test_startup_sh_runs_setup_with_the_protected_cmdline():
    """The pattern only works while startup.sh invokes it exactly so."""
    assert re.search(r"^\s*" + re.escape(_SETUP_CMD) + r" ", _STARTUP.read_text(), re.M)
    assert _SETUP_CMD in oa._PLATFORM_TREE_PATTERNS


# ---------------------------------------------------------------------------
# startup.sh blocks, run under bash
# ---------------------------------------------------------------------------
if shutil.which("bash") is None:  # pragma: no cover
    pytest.skip("bash not available", allow_module_level=True)


def _block(header: str) -> str:
    text = _STARTUP.read_text(encoding="utf-8")
    m = re.search(r"^# === " + re.escape(header) + r".*?\n(.*?)^# === ", text, re.S | re.M)
    assert m, f"block {header!r} missing from startup.sh"
    return m.group(1)


def _run(block: str, home: Path, env_extra: dict) -> subprocess.CompletedProcess:
    script = block.replace("/home/developer", str(home))
    env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
    env.update(env_extra)
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc


def _tmpdir_block() -> str:
    return _block("Scratch space")


def test_default_tmpdir_is_emptied(tmp_path):
    scratch = tmp_path / ".tmp"
    (scratch / "pip-build-abc" / "deep").mkdir(parents=True)
    (scratch / "pip-build-abc" / "deep" / "wheel.whl").write_bytes(b"x" * 4096)
    (scratch / ".hidden").write_text("x")
    out = _run(_tmpdir_block(), tmp_path, {}).stdout
    assert scratch.is_dir()
    assert list(scratch.iterdir()) == []
    assert "TMPDIR" in out and "2 entries" in out


def test_default_tmpdir_keeps_the_codex_login(tmp_path):
    # ~/.tmp/codex is the default CODEX_HOME: a ChatGPT-plan `codex login`
    # lives there and must survive a restart.
    scratch = tmp_path / ".tmp"
    (scratch / "codex").mkdir(parents=True)
    (scratch / "codex" / "auth.json").write_text('{"tokens": {}}')
    (scratch / "pip-build-abc").mkdir()
    out = _run(_tmpdir_block(), tmp_path, {}).stdout
    assert (scratch / "codex" / "auth.json").read_text() == '{"tokens": {}}'
    assert not (scratch / "pip-build-abc").exists()
    assert "1 entries" in out


def test_default_tmpdir_with_only_codex_logs_nothing(tmp_path):
    scratch = tmp_path / ".tmp"
    (scratch / "codex").mkdir(parents=True)
    out = _run(_tmpdir_block(), tmp_path, {}).stdout
    assert (scratch / "codex").is_dir()
    assert "cleared" not in out


def test_default_tmpdir_set_explicitly_is_emptied(tmp_path):
    scratch = tmp_path / ".tmp"
    scratch.mkdir()
    (scratch / "leftover").write_text("x")
    _run(_tmpdir_block(), tmp_path, {"TMPDIR": str(scratch)})
    assert list(scratch.iterdir()) == []


def test_custom_tmpdir_is_untouched(tmp_path):
    custom = tmp_path / "operator-scratch"
    custom.mkdir()
    (custom / "keep.dat").write_text("data")
    out = _run(_tmpdir_block(), tmp_path, {"TMPDIR": str(custom)}).stdout
    assert (custom / "keep.dat").read_text() == "data"
    assert "not cleared" in out


def _setup_block() -> str:
    return _block("Persistent Setup Script")


def _write_setup(home: Path, body: str) -> None:
    d = home / ".trinity"
    d.mkdir(parents=True, exist_ok=True)
    (d / "setup.sh").write_text(body)


def _status(home: Path) -> Path:
    return home / ".trinity" / "setup-status.json"


_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def test_setup_success_records_exit_zero(tmp_path):
    _write_setup(tmp_path, "echo installing\nexit 0\n")
    _run(_setup_block(), tmp_path, {})
    st = json.loads(_status(tmp_path).read_text())
    assert st["exit_code"] == 0
    assert _ISO.match(st["started_at"]) and _ISO.match(st["finished_at"])


def test_setup_failure_records_exit_code_and_warns(tmp_path):
    _write_setup(tmp_path, "exit 7\n")
    out = _run(_setup_block(), tmp_path, {}).stdout
    assert json.loads(_status(tmp_path).read_text())["exit_code"] == 7
    assert "Warning: setup.sh had errors" in out


def test_no_setup_script_removes_a_stale_status(tmp_path):
    _status(tmp_path).parent.mkdir(parents=True)
    _status(tmp_path).write_text('{"exit_code": 1}')
    _run(_setup_block(), tmp_path, {})
    assert not _status(tmp_path).exists()


# ---------------------------------------------------------------------------
# Compatibility check F-014
# ---------------------------------------------------------------------------
def _snap(content):
    files = {}
    if content is not None:
        files[".trinity/setup-status.json"] = {"exists": True, "content": content}
    return {"files": files}


def _f014(content):
    return run_static(_snap(content), ["F-014"])["F-014"]


def test_f014_registered():
    assert spec.BY_ID["F-014"].type == "static"


def test_f014_missing_file_skips():
    status, _m, detail = _f014(None)
    assert status == "skipped"
    assert "#4002" in detail["skip_reason"]


def test_f014_exit_zero_passes():
    assert _f014(json.dumps({"exit_code": 0, "started_at": "a", "finished_at": "b"}))[0] == "pass"


def test_f014_nonzero_fails_with_exit_code():
    status, msg, detail = _f014(json.dumps(
        {"exit_code": "3", "started_at": "2026-10-27T10:00:00Z",
         "finished_at": "2026-10-27T10:05:00Z", "evil": "x" * 10}))
    assert status == "fail"
    assert detail["exit_code"] == 3 and "3" in msg
    assert detail["finished_at"] == "2026-10-27T10:05:00Z"
    assert "evil" not in detail


@pytest.mark.parametrize("garbage", ["{not json", "[1,2]", '{"exit_code": "abc"}', '{}',
                                     '{"exit_code": 1e400}'])
def test_f014_garbage_fails(garbage):
    status, msg, _detail = _f014(garbage)
    assert status == "fail"
    # The check's own verdict, not run_static's catch-all for a raised check.
    assert msg == "setup status file is present but unreadable"
