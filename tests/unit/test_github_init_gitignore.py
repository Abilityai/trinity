"""
Regression tests for #458 — `initialize_git_in_container` .gitignore handling.

The bug: the old code ran `cat > .gitignore <<EOF ...` which clobbered any
workspace-supplied `.gitignore` (so `/trinity:onboard`'s ignore rules were
lost) and listed only shell/cache entries (so `.env` and `.mcp.json`, which
`inject_credentials` writes, were never ignored and got committed on the
initial sync). The fix replaces that truncate-and-write with an
append-if-missing merge that preserves existing rules and adds the three
patterns the reporter named.

These tests exercise the actual bash script returned by
`_build_gitignore_merge_command` against a temp directory, which is honest:
the only difference from production is the host filesystem vs. the agent
container.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def _load_git_service():
    """Import git_service with heavy dependencies mocked out."""
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


def _run_merge(tmp_path: Path) -> str:
    """Run the real merge command (produced by `_build_gitignore_merge_command`)
    against ``tmp_path`` and return the resulting `.gitignore` contents.
    """
    gs = _load_git_service()
    # The production helper hardcodes the path that the agent container
    # passes in; for tests we just point it at the temp dir.
    cmd = gs._build_gitignore_merge_command(str(tmp_path))
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, (
        f"merge command failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return (tmp_path / ".gitignore").read_text()


def test_preserves_preexisting_gitignore(tmp_path):
    """A workspace `.gitignore` with user rules must survive the merge.

    Regression guard for the primary #458 bug: the old `cat > .gitignore`
    path clobbered anything `/trinity:onboard` (or the user) had written.
    """
    preexisting = "# user rules\nnode_modules/\nbuild/\n*.log\n"
    (tmp_path / ".gitignore").write_text(preexisting)

    content = _run_merge(tmp_path)

    # Every user line still present, verbatim.
    for line in ("# user rules", "node_modules/", "build/", "*.log"):
        assert line in content.splitlines(), (
            f"user rule {line!r} lost — got:\n{content}"
        )
    # And the three credential patterns the reporter named are now covered.
    for p in (".env", ".env.*", ".mcp.json"):
        assert p in content.splitlines(), (
            f"credential pattern {p!r} missing after merge — got:\n{content}"
        )


def test_fresh_agent_ignores_env_and_mcp_json(tmp_path):
    """With no pre-existing `.gitignore`, the merge must produce one that
    ignores `.env`, `.env.*`, and `.mcp.json` — the files `inject_credentials`
    writes and that #458 observed leaking on the initial commit.
    """
    assert not (tmp_path / ".gitignore").exists()

    content = _run_merge(tmp_path)
    lines = content.splitlines()

    for p in (".env", ".env.*", ".mcp.json"):
        assert p in lines, (
            f"pattern {p!r} not in .gitignore after fresh merge — got:\n{content}"
        )


# ---------------------------------------------------------------------------
# #462 regression coverage — the platform-injected `.gitignore` must cover the
# full canonical list and existing agents must migrate on the next Push.
# ---------------------------------------------------------------------------


def test_full_documented_exclusion_list_present(tmp_path):
    """Every entry in `_GITIGNORE_PATTERNS` must end up in `.gitignore`
    after a fresh merge. Guards against accidental constant truncation.
    """
    gs = _load_git_service()
    content = _run_merge(tmp_path)
    lines = content.splitlines()

    for pattern in gs._GITIGNORE_PATTERNS:
        assert pattern in lines, (
            f"pattern {pattern!r} from _GITIGNORE_PATTERNS missing — got:\n"
            f"{content}"
        )


def test_idempotent_double_run(tmp_path):
    """Running the merge twice must not duplicate any line. The append guard
    is `grep -qxF` per pattern; a regression here would mean every Push
    grows the file by one full copy of the canonical list.
    """
    _run_merge(tmp_path)
    second = _run_merge(tmp_path)
    lines = second.splitlines()

    # Each pattern must appear exactly once.
    gs = _load_git_service()
    for pattern in gs._GITIGNORE_PATTERNS:
        count = lines.count(pattern)
        assert count == 1, (
            f"pattern {pattern!r} appears {count} times after double run — "
            f"merge is not idempotent. Full file:\n{second}"
        )


def test_doc_and_constant_in_sync():
    """The `.gitignore` code block in `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
    must contain every entry in `_GITIGNORE_PATTERNS`.

    The Python constant is the source of truth. The doc is hand-written and
    drifts; this test catches drift in CI before the doc gets out of date
    again. A new entry in `_GITIGNORE_PATTERNS` requires a matching update
    to the doc block (see `### 5. .gitignore (Required)` section).
    """
    import re
    gs = _load_git_service()

    doc_path = _project_root / "docs" / "TRINITY_COMPATIBLE_AGENT_GUIDE.md"
    doc = doc_path.read_text()

    # Find the first ```gitignore ... ``` fenced block in the file.
    match = re.search(r"```gitignore\n(.*?)\n```", doc, flags=re.DOTALL)
    assert match, (
        f"no ```gitignore``` code block found in {doc_path} — did the "
        "section get renamed or the fence language change?"
    )
    doc_lines = set(match.group(1).splitlines())

    missing = [p for p in gs._GITIGNORE_PATTERNS if p not in doc_lines]
    assert not missing, (
        f"_GITIGNORE_PATTERNS entries missing from doc block: {missing}. "
        f"Update the gitignore code block in {doc_path.name} to match "
        "git_service.py — they are intentionally kept in sync."
    )


def test_rm_cached_for_newly_ignored_files(tmp_path):
    """A file that was committed BEFORE its ignore rule existed must be
    untracked (`git rm --cached`) by the migration helper, but its working
    tree copy must remain on disk. Acceptance criterion #462.5: the fix
    only helps existing agents if previously-tracked runtime files are
    removed from the index too.
    """
    gs = _load_git_service()

    # Fresh repo with deterministic identity so commits don't fail under CI.
    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")

    # Force-track files that the new patterns ignore. Use `-f` to bypass any
    # ignore rule (simulating an old agent that committed these before the
    # fix landed).
    cache_file = tmp_path / ".cache" / "leaked"
    session_file = tmp_path / ".claude" / "sessions" / "leaked"
    keeper = tmp_path / "agent" / "skill.md"
    for f, content in [
        (cache_file, "old cache"),
        (session_file, "old session"),
        (keeper, "real value"),
    ]:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    git("add", "-f", str(cache_file), str(session_file), str(keeper))
    git("commit", "-q", "-m", "seed runtime files")

    # Sanity: all three files start out tracked.
    tracked_before = set(git("ls-files").stdout.splitlines())
    assert ".cache/leaked" in tracked_before
    assert ".claude/sessions/leaked" in tracked_before
    assert "agent/skill.md" in tracked_before

    # Run the real migration: gitignore merge + rm-cached for ignored files.
    for build in (
        gs._build_gitignore_merge_command,
        gs._build_rm_cached_ignored_command,
    ):
        result = subprocess.run(
            build(str(tmp_path)),
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"command failed: {build.__name__}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    tracked_after = set(git("ls-files").stdout.splitlines())

    # The runtime files must be untracked now.
    assert ".cache/leaked" not in tracked_after, (
        f".cache/leaked still tracked after migration; tracked={tracked_after}"
    )
    assert ".claude/sessions/leaked" not in tracked_after, (
        f".claude/sessions/leaked still tracked; tracked={tracked_after}"
    )
    # The agent-value file must remain tracked.
    assert "agent/skill.md" in tracked_after, (
        f"agent/skill.md was wrongly untracked; tracked={tracked_after}"
    )

    # Working-tree files MUST still exist on disk — the migration only
    # touches the index, never the user's files.
    assert cache_file.exists(), "rm --cached must not delete the on-disk file"
    assert session_file.exists(), "rm --cached must not delete the on-disk file"


def test_rm_cached_exempts_brain_orb_hooks(tmp_path):
    """Template-committed Brain Orb hooks survive the gitignore migration
    (trinity-enterprise#76). The fleet merge appends `.trinity/`, which makes
    the committed `.trinity/brain-orb/*` hooks ignored-but-tracked; the
    rm-cached sweep must exempt exactly that directory — untracking the hooks
    would commit+push their deletion, so a later re-clone boots hookless.
    Sibling `.trinity/` files must STILL be swept (the exemption is surgical).
    """
    gs = _load_git_service()

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=check,
            timeout=10,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")

    hook = tmp_path / ".trinity" / "brain-orb" / "action"
    setup = tmp_path / ".trinity" / "setup.sh"
    leaked_state = tmp_path / ".trinity" / "runtime-state.yaml"
    for f, content in [(hook, "#!/usr/bin/env python3\n"),
                       (setup, "#!/bin/bash\n"),
                       (leaked_state, "x: 1\n")]:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    # Template shape: parent star-ignored, setup.sh + hook dir re-included (a
    # dir-form `.trinity/` exclusion can't be re-included under, so templates
    # use `*`).
    (tmp_path / ".gitignore").write_text(
        ".trinity/*\n!.trinity/setup.sh\n!.trinity/brain-orb/\n")
    git("add", str(hook), str(setup))
    git("add", "-f", str(leaked_state))  # simulate an old agent that committed state
    git("commit", "-q", "-m", "template ships brain-orb hooks")

    tracked_before = set(git("ls-files").stdout.splitlines())
    assert ".trinity/brain-orb/action" in tracked_before
    assert ".trinity/setup.sh" in tracked_before
    assert ".trinity/runtime-state.yaml" in tracked_before

    # Run the real migration: fleet merge (appends `.trinity/`) + rm-cached.
    for build in (
        gs._build_gitignore_merge_command,
        gs._build_rm_cached_ignored_command,
    ):
        result = subprocess.run(
            build(str(tmp_path)),
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"command failed: {build.__name__}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )

    tracked_after = set(git("ls-files").stdout.splitlines())
    assert ".trinity/brain-orb/action" in tracked_after, (
        f"brain-orb hook was untracked by the sweep; tracked={tracked_after}"
    )
    assert ".trinity/setup.sh" in tracked_after, (
        f"setup.sh was untracked by the sweep (caught live in the #76 e2e Push); "
        f"tracked={tracked_after}"
    )
    assert ".trinity/runtime-state.yaml" not in tracked_after, (
        f"sibling .trinity/ state must still be swept; tracked={tracked_after}"
    )
    assert hook.exists() and setup.exists() and leaked_state.exists()
