"""Two log lines that must not carry secret-derived detail (trinity-enterprise#1117).

* Boot (`database._ensure_admin_user`): a weak ADMIN_PASSWORD gets a warning in
  the container log. It used to list each failed rule, which reveals the
  password's length ("currently 10"), which character classes it lacks, and
  whether it is on the public common-password list -- for a common password
  that narrows it to a handful of guesses. The warning must now be the same
  text whatever the password is.
* Fork-to-own (`fork_to_own.fork_template_to_own_repo`): a failed template
  clone logs git's output. `_run_git` already scrubs the read token from it,
  and the log line now uses that output directly instead of scrubbing again.
  The logged text must be exactly the once-scrubbed git output, with no token
  in any of the shapes git prints it (characterization: identical before and
  after, by design).
"""
from __future__ import annotations

import base64
import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Boot warning for a weak ADMIN_PASSWORD
# ---------------------------------------------------------------------------

def _mkdb():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, "
                "password_hash TEXT, role TEXT, created_at TEXT, updated_at TEXT)")
    cur.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)")
    conn.commit()
    return conn, cur


def _boot_output(monkeypatch, capsys, password):
    import database
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    conn, cur = _mkdb()
    capsys.readouterr()
    database._ensure_admin_user(cur, conn)
    return capsys.readouterr().out


# Each fails a different set of rules, at a different length.
_WEAK = ["trinity123", "short", "alllowercaseletters", "ALLUPPER-1234567"]


class TestBootWarning:
    def test_warning_is_identical_for_every_weak_password(self, monkeypatch, capsys):
        outputs = {pw: _boot_output(monkeypatch, capsys, pw) for pw in _WEAK}
        first = outputs[_WEAK[0]]
        assert "WARNING: ADMIN_PASSWORD does not meet complexity requirements" in first
        for pw, out in outputs.items():
            assert out == first, f"boot output depends on the password ({pw!r}):\n{out}"

    def test_warning_names_no_length_or_failed_rule(self, monkeypatch, capsys):
        out = _boot_output(monkeypatch, capsys, "trinity123")
        assert "currently" not in out
        assert "10" not in out
        assert "too common" not in out
        assert "Must contain" not in out

    def test_strong_password_gets_no_warning(self, monkeypatch, capsys):
        out = _boot_output(monkeypatch, capsys, "Str0ng-Passw0rd!x")
        assert "WARNING" not in out


# ---------------------------------------------------------------------------
# Fork-to-own: the clone-failure log line
# ---------------------------------------------------------------------------

_READ_PAT = "ghp_testReadTokenNotReal"  # synthetic, deliberately not token-shaped
_B64 = base64.b64encode(f"x-access-token:{_READ_PAT}".encode()).decode()


class _RepoInfo:
    def __init__(self, exists, default_branch="main"):
        self.exists = exists
        self.default_branch = default_branch
        self.private = True


class _FakeGH:
    """Template exists (tip 'tipsha'); destination exists and is empty."""

    def __init__(self, pat):
        self.pat = pat

    async def validate_token(self):
        return True, "alice"

    async def check_repo_exists(self, owner, name):
        return _RepoInfo(True)

    async def get_branch_sha(self, repo, branch):
        return "tipsha"

    async def list_branches(self, repo, limit=10):
        return []


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """A `git` on PATH that fails like a clone with the token in its output:
    raw PAT in a URL, the extraHeader env value git was given, and a curl-style
    Authorization line. It records exactly what it printed."""
    printed = tmp_path / "printed.txt"
    script = tmp_path / "bin" / "git"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        "{\n"
        f"  echo \"fatal: could not read Password for 'https://x-access-token:{_READ_PAT}@github.com'\"\n"
        "  echo \"trace: http.extraHeader=$GIT_CONFIG_VALUE_0\"\n"
        f"  echo \"> Authorization: basic {_B64}\"\n"
        "} | tee '" + str(printed) + "'\n"
        "exit 128\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    return printed


def _fork_module():
    ds = sys.modules.get("services.docker_service")
    if ds is not None and not hasattr(ds, "list_all_agents_fast"):
        ds.list_all_agents_fast = lambda *a, **kw: []
    import services.agent_service.fork_to_own as f2o
    return f2o


class TestForkCloneFailureLog:
    @pytest.mark.asyncio
    async def test_logged_output_is_the_once_scrubbed_git_output(self, fake_git, monkeypatch, caplog):
        f2o = _fork_module()
        monkeypatch.setattr(f2o, "GitHubService", _FakeGH)
        from fastapi import HTTPException

        with caplog.at_level(logging.ERROR, logger=f2o.logger.name):
            with pytest.raises(HTTPException) as exc:
                await f2o.fork_template_to_own_repo(
                    "Abilityai/cornelius", "alice/brain", "ghp_user", _READ_PAT, True)
        assert exc.value.detail["code"] == "FORK_TEMPLATE_CLONE_FAILED"

        raw = fake_git.read_text()
        assert _READ_PAT in raw and _B64 in raw  # git really printed the token
        records = [r for r in caplog.records if "template clone failed" in r.getMessage()]
        assert len(records) == 1
        logged = records[0].getMessage()
        expected = f2o.scrub_secret(raw, _READ_PAT)
        assert logged == f"fork-to-own: template clone failed for Abilityai/cornelius: {expected}"
        assert _READ_PAT not in logged and _B64 not in logged

    @pytest.mark.parametrize("text", [
        f"fatal: could not read Password for 'https://{_READ_PAT}@github.com'",
        f"trace: http.extraHeader=Authorization: basic {_B64}",
        f"remote: https://x-access-token:{_READ_PAT}@github.com/o/r.git {_READ_PAT}",
        "fatal: Remote branch main not found in upstream origin",
        "",
    ])
    def test_scrub_is_idempotent_on_git_output(self, text):
        """Why dropping the second scrub changes nothing: scrubbing twice equals
        scrubbing once for a token without `*` (GitHub tokens never have one)."""
        f2o = _fork_module()
        once = f2o.scrub_secret(text, _READ_PAT)
        assert f2o.scrub_secret(once, _READ_PAT) == once
        assert _READ_PAT not in once and _B64 not in once
