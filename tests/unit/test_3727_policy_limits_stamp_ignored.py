"""Regression tests for #3727 - Claude Code's policy-limits stamp file must not
reach an agent's repository.

Claude Code writes `.claude/policy-limits.json.stamp.json` next to
`.claude/policy-limits.json`. The managed block listed only the exact
`.claude/policy-limits.json`, so the auto-sync's `git add -A` staged the stamp
(a hash of the agent's auth identity plus a timestamp) on an own-repo agent's
first sync.

Both tests run the shipped builders against a real temporary git repo, the
same way `test_2036_claude_settings_leak.py` does:

1. a fresh agent: after the canonical merge, `git add -A` stages neither file;
2. an agent that already committed the stamp before this fix: the Push-time
   merge + `rm --cached` sweep (what `_migrate_workspace_gitignore` runs)
   untracks it, keeps it on disk, and reports it in the sweep's `removed`.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock


_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


POLICY_LIMITS = ".claude/policy-limits.json"
STAMP = ".claude/policy-limits.json.stamp.json"
# Agent source next to them that must keep syncing.
KEEP = ".claude/skills/research/SKILL.md"

_STAMP_BODY = (
    '{"v":1,"identity":"' + "0" * 64 + '","kind":"token",'
    '"sha":"sha256:' + "0" * 64 + '","confirmed_at":1760000000000,'
    '"hipaa_seen":[]}'
)

_STUBBED_MODULE_NAMES = (
    "docker", "docker.errors", "docker.types",
    "redis", "redis.asyncio",
    "database",
    "services.docker_service",
)


def _load_gitignore(monkeypatch):
    """Import the gitignore module with heavy dependencies stubbed, through
    `monkeypatch` so teardown restores the real modules
    (`tests/lint_sys_modules.py`)."""
    for name in _STUBBED_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, name, Mock())
    sys.modules["database"].db = Mock()
    sys.modules["database"].AgentGitConfig = Mock
    sys.modules["database"].GitSyncResult = Mock
    for key in list(sys.modules):
        if key.startswith("services.git_service"):
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.git_service.gitignore as gs
    return gs


def _git(repo: Path):
    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            check=True, timeout=10,
        )
    return run


def _init_repo(repo: Path):
    git = _git(repo)
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    return git


def _write(repo: Path, rel: str, content: str = "x"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _run_shell(command: str, repo: Path) -> str:
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"command failed in {repo}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def test_add_all_does_not_stage_policy_limits_stamp(tmp_path, monkeypatch):
    """A fresh agent: the canonical merge, then the auto-sync's `git add -A`."""
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _write(tmp_path, POLICY_LIMITS, '{"restrictions":{}}')
    _write(tmp_path, STAMP, _STAMP_BODY)
    _write(tmp_path, KEEP)

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    git("add", "-A")
    staged = set(git("diff", "--cached", "--name-only").stdout.splitlines())

    assert STAMP not in staged, f"{STAMP} staged by `git add -A`: {sorted(staged)}"
    assert POLICY_LIMITS not in staged
    assert KEEP in staged, "agent source next to the stamp stopped syncing"


def test_push_sweep_untracks_an_already_committed_stamp(tmp_path, monkeypatch):
    """An agent that committed the stamp before the fix: the Push-time merge +
    sweep untrack it (the next push deletes it from the remote), leave the file
    on disk for Claude Code, and say so in the sweep report."""
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _write(tmp_path, POLICY_LIMITS, '{"restrictions":{}}')
    _write(tmp_path, STAMP, _STAMP_BODY)
    _write(tmp_path, KEEP)
    # The pre-fix state: the managed block the real merge wrote before this
    # line existed, then the first auto-sync committing the stamp.
    pre_fix = tuple(p for p in gs._GITIGNORE_PATTERNS if p != STAMP)
    with monkeypatch.context() as m:
        m.setattr(gs, "_GITIGNORE_PATTERNS", pre_fix)
        m.setattr(
            gs, "_GITIGNORE_MANAGED_LINES",
            tuple(p for p in gs._GITIGNORE_MANAGED_LINES if p != STAMP),
        )
        _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    assert STAMP not in (tmp_path / ".gitignore").read_text()
    git("add", "-A")
    git("commit", "-q", "-m", "first auto-sync")
    assert STAMP in set(git("ls-files").stdout.splitlines()), "fixture did not track the stamp"

    merge_out = _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    sweep_out = _run_shell(gs._build_rm_cached_ignored_command(str(tmp_path)), tmp_path)

    tracked = set(git("ls-files").stdout.splitlines())
    assert STAMP not in tracked, "stamp still tracked after the Push-time sweep"
    assert KEEP in tracked, "sweep untracked legitimate agent source"
    assert (tmp_path / STAMP).read_text() == _STAMP_BODY, "sweep touched the working-tree file"

    report = gs.gitignore_sweep._parse_gitignore_sweep(merge_out, sweep_out)
    assert STAMP in report.removed, f"sweep did not report the untracked stamp: {report}"

    # The next auto-sync's `git add -A` keeps it out of the index, so its
    # commit carries the stamp's DELETION, not a fresh copy.
    git("add", "-A")
    assert STAMP not in set(git("ls-files").stdout.splitlines()), (
        "the next `git add -A` re-staged the stamp"
    )
    status = dict(
        line.split("\t", 1)[::-1]
        for line in git("diff", "--cached", "--name-status").stdout.splitlines()
    )
    assert status.get(STAMP) == "D", f"stamp is not staged as a deletion: {status}"
