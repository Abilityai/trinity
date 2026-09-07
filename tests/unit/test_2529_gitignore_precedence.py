"""Canonical `.gitignore` precedence and honest sweep reporting (#2529).

Both writers of an agent's `.gitignore` appended, and git is last-match-wins,
so the canonical `_GITIGNORE_PATTERNS` block — appended to the END of the file
on every Push — silently overrode every `!negation` the agent wrote above it,
and `_build_rm_cached_ignored_command` then `git rm --cached`'d the files those
negations were protecting. Two confirmed field instances (2026-07-30 internal
fleet agent, causing commit `47efd80`; corbin 2026-09-02, hand-restored with
the comment *"Negation must stay LAST in this file."*).

`.env.example` is the headline casualty: compat check F-004 requires it and
`credential_requirements_service` reads it, so an agent that ships one loses it
on its first Push and then fails its own compatibility contract.

These tests exercise the REAL builders against REAL git repositories — the
defect lives in git's own last-match-wins semantics, which a mocked container
cannot express. Modelled on `test_2070_trinity_authored_paths.py`'s harness.

TWO CONSECUTIVE Pushes are the load-bearing shape: the first Push on a
pre-canonical repo appends the block and the file is *already* gone by the
second, so a single-Push assertion passes even with the bug live.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _gs():
    try:
        import services.git_service as gs
    except Exception:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return gs


def _git(cwd: Path, *args: str) -> str:
    env = dict(_ENV, HOME=str(cwd))
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _run(cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run a builder's command the way the backend does — docker-py splits a
    string command with shlex, so the test does too."""
    env = dict(_ENV, HOME=str(cwd))
    return subprocess.run(
        shlex.split(cmd), cwd=cwd, env=env, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path, files: dict[str, str], gitignore: str) -> Path:
    home = tmp_path / "developer"
    home.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    (home / ".gitignore").write_text(gitignore)
    _git(home, "init", "-q", ".")
    _git(home, "config", "user.email", "t@t")
    _git(home, "config", "user.name", "t")
    _git(home, "add", "-A", "-f")
    _git(home, "commit", "-qm", "init")
    return home


def _push(home: Path) -> set[str]:
    """The two commands a backend Push runs, in order. Returns tracked paths."""
    gs = _gs()
    for build in (gs._build_gitignore_merge_command, gs._build_rm_cached_ignored_command):
        out = _run(build(str(home)), home)
        assert out.returncode == 0, f"command failed: {out.stderr[:400]}"
    return set(_git(home, "ls-files").split())


# ---------------------------------------------------------------------------
# The reported bug (AC-5 / AC-6)
# ---------------------------------------------------------------------------

def test_env_example_survives_two_consecutive_pushes(tmp_path):
    """AC-5. `.env.example` is negated by the agent AND followed by an unrelated
    rule, so the win cannot come from merely being the file's last line."""
    home = _make_repo(
        tmp_path,
        {".env.example": "API_KEY=\n", "CLAUDE.md": "agent\n"},
        "!.env.example\nmy-scratch/\n",
    )
    assert ".env.example" in _push(home), "untracked by the FIRST Push"
    assert ".env.example" in _push(home), (
        ".env.example was untracked by the SECOND Push — the canonical block "
        "appended below the agent's negation and reversed it (#2529)"
    )


def test_claude_settings_json_negation_survives_a_push(tmp_path):
    """AC-6. #2036's rationale offered `!.claude/settings.json` as the escape
    hatch; the hatch has to survive the next append."""
    home = _make_repo(
        tmp_path,
        {".claude/settings.json": "{}\n", "CLAUDE.md": "agent\n"},
        "!.claude/settings.json\nmy-scratch/\n",
    )
    assert ".claude/settings.json" in _push(home)
    assert ".claude/settings.json" in _push(home), (
        "the #2036 negation hatch did not survive a second Push (#2529)"
    )
