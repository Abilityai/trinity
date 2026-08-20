"""Regression tests for #2036 — the base image's `~/.claude/settings.json`
must never reach the agent's GitHub repo.

## The defect

`docker/base-image/Dockerfile` baked `hooks/claude-settings.json` into
`/home/developer/.claude/settings.json`, registering the platform guardrail
hooks by ABSOLUTE container path (`/opt/trinity/hooks/*.py`). Agent HOME is
also the git repo root and the in-container auto-sync commits with
`git add -A`, so the file was swept into the repo on the next sync.

**ent#345 removed the platform's copy** (the registration is root-owned at
`/etc/claude-code/managed-settings.json`, outside the tree),
which retires the ORIGINAL cause. The ignore rule stays load-bearing for the two
copies that can still exist — a legacy one on a pre-ent#345 volume, and an
agent-authored one — because either still registers paths that do not resolve
outside the container. See `test_baked_settings_registers_absolute_container_paths`
for the restated premise.

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
from unittest.mock import Mock

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


_STUBBED_MODULE_NAMES = (
    "docker", "docker.errors", "docker.types",
    "redis", "redis.asyncio",
    "database",
    "services.docker_service",
)


def _load_git_service(monkeypatch):
    """Import git_service with heavy dependencies stubbed out.

    Stubs the same surface as `test_github_init_gitignore.py`, but through
    `monkeypatch.setitem`/`delitem` rather than a bare `del sys.modules[...]`
    — teardown then restores the real modules automatically, so this file
    cannot leak a stubbed `database`/`docker` into a later test in the same
    worker (`tests/lint_sys_modules.py` enforces this).
    """
    for name in _STUBBED_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, name, Mock())
    sys.modules["database"].db = Mock()
    sys.modules["database"].AgentGitConfig = Mock
    sys.modules["database"].GitSyncResult = Mock

    # Force a fresh import so the stubs above are the ones it binds to.
    for key in list(sys.modules):
        if key.startswith("services.git_service"):
            monkeypatch.delitem(sys.modules, key, raising=False)

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
    """The reason a `.claude/settings.json` may never be committed.

    This test asked to be re-argued if the hooks ever "moved out of the synced
    tree", and ent#345 moved them: the registration now ships to root-owned
    `/etc/claude-code/managed-settings.json`, because the in-tree copy was
    agent-WRITABLE and let a guarded agent switch off its own guardrails.

    **The ignore rule survives the move, for a different reason than it was
    written for.** It no longer protects against the platform baking a file into
    the tree — nothing does that any more. It protects against the two copies that
    can still be there:

    * a **legacy** one on an agent that predates ent#345. `~/.claude/settings.json`
      lives on the durable home volume, so rebuilding the image does not remove it;
      `startup.sh` deletes it only on an exact content match, and an agent that
      never restarts still has it.
    * an **agent-authored** one, which the platform must not delete and cannot
      vouch for.

    Either still registers absolute `/opt/trinity` paths that do not exist outside
    the container, so committing either still bricks an outside clone — the #2036
    damage, unchanged. Hence: rule kept, premise restated.
    """
    baked = _project_root / "docker" / "base-image" / "hooks" / "managed-settings.json"
    assert baked.is_file(), f"hook settings missing at {baked}"

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


def test_dockerfile_no_longer_installs_settings_into_the_repo_root():
    """The move this file's sibling asked to have re-argued, now pinned in place.

    Was: assert the Dockerfile DOES bake into `/home/developer/.claude/settings.json`
    ("pinned so a move is a conscious change"). ent#345 is that conscious change —
    the registration is root-owned at `/etc/claude-code/managed-settings.json` — so
    the pin inverts: a revert would put an agent-writable guardrail registration back
    inside the synced tree, which is both the #2036 leak and the ent#345 hole.

    Instructions only, not comments: the ent#345 block deliberately NAMES the old
    path in prose to record what moved, and a whole-file substring search would pass
    on that documentation — which is exactly how the old assertion passed against
    this change before the rename made it fail.
    """
    dockerfile = (
        _project_root / "docker" / "base-image" / "Dockerfile"
    ).read_text()
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "/home/developer/.claude/settings.json" not in instructions
    assert "/etc/claude-code/managed-settings.json" in instructions


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
def test_pattern_is_canonical(pattern, monkeypatch):
    gs = _load_git_service(monkeypatch)
    assert pattern in gs._GITIGNORE_PATTERNS, (
        f"{pattern} missing from _GITIGNORE_PATTERNS (#2036)"
    )


def test_project_scoped_claude_source_is_not_swallowed(monkeypatch):
    """No rule may exclude `.claude/` wholesale — commands/skills/agents are
    the agent's source and must keep syncing (G-001 in the validation spec)."""
    gs = _load_git_service(monkeypatch)
    for bad in (".claude", ".claude/", ".claude/*"):
        assert bad not in gs._GITIGNORE_PATTERNS


# ---------------------------------------------------------------------------
# 3. Behaviour: a fresh agent never stages the leaking files
# ---------------------------------------------------------------------------

def test_add_all_does_not_stage_leaking_runtime_files(tmp_path, monkeypatch):
    """The in-container auto-sync runs `git add -A`. After the canonical
    `.gitignore` merge, none of the leaking artifacts may be staged — while
    every legitimate `.claude/` source file still is."""
    gs = _load_git_service(monkeypatch)
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

def test_existing_committed_settings_is_untracked_but_kept_on_disk(tmp_path, monkeypatch):
    """The repos that are already broken are the point of the fix.

    `_migrate_workspace_gitignore` must untrack the committed copy so the next
    push deletes it from the remote (unbricking future clones), while leaving
    the working-tree file alone so the RUNNING agent keeps its guardrail hooks.
    """
    gs = _load_git_service(monkeypatch)
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


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """A second Push must be a no-op — `.gitignore` gains no duplicate lines
    and `rm --cached` finds nothing (it runs on every push, #462)."""
    gs = _load_git_service(monkeypatch)
    _init_repo(tmp_path)
    _write(tmp_path, ".claude/settings.json")

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    first = (tmp_path / ".gitignore").read_text()
    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    second = (tmp_path / ".gitignore").read_text()

    assert first == second
    lines = [ln for ln in second.splitlines() if ln.strip()]
    assert len(lines) == len(set(lines)), "duplicate .gitignore entries appended"


def test_agent_can_override_with_a_negation(tmp_path, monkeypatch):
    """The #1596 escape hatch still works: an agent that genuinely needs to
    commit project settings negates the rule in its own `.gitignore`.

    Asserted because the fix removes a file Claude Code also uses for
    PROJECT-level settings — the trade-off is only acceptable if the opt-out
    is real."""
    gs = _load_git_service(monkeypatch)
    git = _init_repo(tmp_path)
    _write(tmp_path, ".claude/settings.json")

    _run_shell(gs._build_gitignore_merge_command(str(tmp_path)), tmp_path)
    with (tmp_path / ".gitignore").open("a") as fh:
        fh.write("!.claude/settings.json\n")

    git("add", "-A")
    staged = set(git("diff", "--cached", "--name-only").stdout.splitlines())
    assert ".claude/settings.json" in staged
