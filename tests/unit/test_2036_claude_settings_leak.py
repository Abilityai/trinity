"""Regression tests for #2036 — the base image's `~/.claude/settings.json`
must never reach the agent's GitHub repo.

## The defect

`docker/base-image/Dockerfile` bakes `hooks/claude-settings.json` into
`/home/developer/.claude/settings.json`, registering the platform guardrail
hooks by ABSOLUTE container path (`/opt/trinity/hooks/*.py`). Agent HOME is
also the git repo root and the in-container auto-sync commits with
`git add -A`, so the file was swept into the repo on the next sync.

The damage lands somewhere else entirely: a clone of that repo made outside
the container has no `/opt/trinity`, so each registered PreToolUse hook exits
**2** — which is exactly Claude Code's "block this tool call" signal. Every
Bash/Edit/Write is refused. Same leak class as #462 / #1596 / #1702, but those
only bloated the repo; this one bricks it.

## What is asserted here

Two properties, deliberately at different levels:

1. **Behavioural, against real `git` and real `bash`** — the shipped
   `_build_gitignore_merge_command` / `_build_rm_cached_ignored_command` are
   executed against a temp repo. A pattern list assertion alone would pass
   against a rule git does not actually apply (`.claude/.last-cleanup` is a
   dotfile inside a dotdir; `.claude/backups/` is a dir rule), so the test
   drives the real thing.
2. **The premise** — that the baked settings file really does carry absolute
   container paths. That is the entire reason the ignore rule must exist; if a
   future change makes the hook paths relative (or moves registration out of
   the synced tree, the issue's suggested longer-term fix), this test fails and
   the reviewer gets to re-decide the rule rather than inherit it.

Both halves matter for the *already-bricked* repos: the fix only helps them if
the migration untracks the committed copy, which is the `rm --cached` case
below.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


# The runtime artifacts observed leaking in a single real sync commit on an
# affected agent. `settings.json` is the one that bricks; the rest are state.
LEAKING_PATHS = (
    ".claude/settings.json",
    ".claude/remote-settings.json",
    ".claude/policy-limits.json",
    ".claude/backups/session-2026-08-01.json",
    ".claude/.last-cleanup",
)

# Must stay committable — these are the agent's actual source, and a rule that
# swallowed them would silently strip an agent's skills on the next push (the
# `.trinity/setup.sh` trap documented in `_build_rm_cached_ignored_command`).
KEEP_PATHS = (
    ".claude/commands/deploy.md",
    ".claude/skills/research/SKILL.md",
    ".claude/agents/reviewer.md",
    "CLAUDE.md",
)


def _load_git_service():
    """Import git_service with heavy dependencies mocked out (mirrors
    `test_github_init_gitignore.py` so both files stub the same surface)."""
    mock_modules = {}
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        mock_modules[mod] = Mock()
    mock_modules["database"].db = Mock()
    mock_modules["database"].AgentGitConfig = Mock
    mock_modules["database"].GitSyncResult = Mock

    with patch.dict("sys.modules", mock_modules):
        for key in list(sys.modules.keys()):
            if key.startswith("services.git_service"):
                del sys.modules[key]
        import services.git_service as gs
    return gs


def _git(repo: Path):
    def run(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
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
    return path


def _run_shell(command: str, repo: Path):
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"command failed in {repo}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result


# ---------------------------------------------------------------------------
# 1. The premise: the baked file is container-only by construction
# ---------------------------------------------------------------------------

def test_baked_settings_registers_absolute_container_paths():
    """The reason `.claude/settings.json` may never be committed.

    If this fails because the hooks became relative or moved out of the synced
    tree, the ignore rule below is no longer load-bearing and should be
    re-argued — not silently kept.
    """
    baked = _project_root / "docker" / "base-image" / "hooks" / "claude-settings.json"
    assert baked.is_file(), f"baked hook settings missing at {baked}"

    settings = json.loads(baked.read_text())
    commands = [
        hook["command"]
        for event_hooks in settings.get("hooks", {}).values()
        for matcher in event_hooks
        for hook in matcher.get("hooks", [])
        if hook.get("type") == "command"
    ]
    assert commands, "baked settings registers no command hooks — premise changed"
    assert any("/opt/trinity/hooks/" in c for c in commands), (
        "baked settings no longer references absolute /opt/trinity paths; the "
        "#2036 ignore rule exists because those paths cannot resolve outside "
        "the container — re-evaluate it."
    )


def test_dockerfile_still_bakes_settings_into_the_repo_root():
    """HOME == repo root is what puts the baked file inside the working tree.

    Pinned so a move (e.g. registering hooks outside `/home/developer`) is a
    conscious change that revisits this fix rather than leaving a dead rule.
    """
    dockerfile = (
        _project_root / "docker" / "base-image" / "Dockerfile"
    ).read_text()
    assert "/home/developer/.claude/settings.json" in dockerfile


# ---------------------------------------------------------------------------
# 2. The pattern list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern",
    [
        ".claude/settings.json",
        ".claude/remote-settings.json",
        ".claude/policy-limits.json",
        ".claude/backups/",
        ".claude/.last-cleanup",
    ],
)
def test_pattern_is_canonical(pattern):
    gs = _load_git_service()
    assert pattern in gs._GITIGNORE_PATTERNS, (
        f"{pattern} missing from _GITIGNORE_PATTERNS (#2036)"
    )


def test_project_scoped_claude_source_is_not_swallowed():
    """No rule may exclude `.claude/` wholesale — commands/skills/agents are
    the agent's source and must keep syncing (G-001 in the validation spec)."""
    gs = _load_git_service()
    for bad in (".claude", ".claude/", ".claude/*"):
        assert bad not in gs._GITIGNORE_PATTERNS


# ---------------------------------------------------------------------------
# 3. Behaviour: a fresh agent never stages the leaking files
# ---------------------------------------------------------------------------

def test_add_all_does_not_stage_leaking_runtime_files(tmp_path):
    """The in-container auto-sync runs `git add -A`. After the canonical
    `.gitignore` merge, none of the leaking artifacts may be staged — while
    every legitimate `.claude/` source file still is."""
    gs = _load_git_service()
    git = _init_repo(tmp_path)

    for rel in LEAKING_PATHS + KEEP_PATHS:
        _write(tmp_path, rel)

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)

    # Exactly what `_run_auto_sync_once` does in the agent container.
    git("add", "-A")
    staged = set(git("diff", "--cached", "--name-only").stdout.splitlines())

    leaked = [p for p in LEAKING_PATHS if p in staged]
    assert not leaked, f"runtime artifacts still staged by `git add -A`: {leaked}"

    missing = [p for p in KEEP_PATHS if p not in staged]
    assert not missing, f"legitimate agent source no longer staged: {missing}"


# ---------------------------------------------------------------------------
# 4. Behaviour: an already-bricked repo heals on the next Push
# ---------------------------------------------------------------------------

def test_existing_committed_settings_is_untracked_but_kept_on_disk(tmp_path):
    """The repos that are already broken are the point of the fix.

    `_migrate_workspace_gitignore` must untrack the committed copy so the next
    push deletes it from the remote (unbricking future clones), while leaving
    the working-tree file alone so the RUNNING agent keeps its guardrail hooks.
    """
    gs = _load_git_service()
    git = _init_repo(tmp_path)

    for rel in LEAKING_PATHS + KEEP_PATHS:
        _write(tmp_path, rel)
    # `-f`: simulate an agent that committed these before the rule existed.
    git("add", "-f", *(LEAKING_PATHS + KEEP_PATHS))
    git("commit", "-q", "-m", "seed leaked runtime state")

    tracked_before = set(git("ls-files").stdout.splitlines())
    for rel in LEAKING_PATHS:
        assert rel in tracked_before, f"fixture did not track {rel}"

    for build in (
        gs._build_gitignore_merge_command,
        gs._build_rm_cached_ignored_command,
    ):
        _run_shell(build(str(tmp_path)), tmp_path)

    tracked_after = set(git("ls-files").stdout.splitlines())

    still_tracked = [p for p in LEAKING_PATHS if p in tracked_after]
    assert not still_tracked, (
        f"still in the index after migration — external clones stay bricked: "
        f"{still_tracked}"
    )

    # The running agent must NOT lose its hooks: index-only removal.
    assert (tmp_path / ".claude" / "settings.json").is_file(), (
        "working-tree settings.json was deleted — the live agent would lose "
        "its guardrail hook registration"
    )

    dropped = [p for p in KEEP_PATHS if p not in tracked_after]
    assert not dropped, f"migration untracked legitimate agent source: {dropped}"


def test_migration_is_idempotent(tmp_path):
    """A second Push must be a no-op — `.gitignore` gains no duplicate lines
    and `rm --cached` finds nothing (it runs on every push, #462)."""
    gs = _load_git_service()
    _init_repo(tmp_path)
    _write(tmp_path, ".claude/settings.json")

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    first = (tmp_path / ".gitignore").read_text()
    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    second = (tmp_path / ".gitignore").read_text()

    assert first == second
    lines = [ln for ln in second.splitlines() if ln.strip()]
    assert len(lines) == len(set(lines)), "duplicate .gitignore entries appended"


def test_agent_can_override_with_a_negation(tmp_path):
    """The #1596 escape hatch still works: an agent that genuinely needs to
    commit project settings negates the rule in its own `.gitignore`.

    Asserted because the fix removes a file Claude Code also uses for
    PROJECT-level settings — the trade-off is only acceptable if the opt-out
    is real."""
    gs = _load_git_service()
    git = _init_repo(tmp_path)
    _write(tmp_path, ".claude/settings.json")

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    with (tmp_path / ".gitignore").open("a") as fh:
        fh.write("!.claude/settings.json\n")

    git("add", "-A")
    staged = set(git("diff", "--cached", "--name-only").stdout.splitlines())
    assert ".claude/settings.json" in staged
