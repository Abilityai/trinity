"""
Tests for the .gitignore merge step of `initialize_git_in_container` (#458).

Regression context: the previous implementation `cat > .gitignore <<EOF`
overwrote any workspace `.gitignore` with a narrow shell/cache deny-list
that did NOT include credentials. Agents that had `.env` / `.mcp.json`
injected after deploy therefore leaked those files on the initial commit.

This module pins the fix:

1. A comprehensive credential deny-list is always present in the generated
   `.gitignore`, regardless of what the workspace started with.
2. Pre-existing `.gitignore` rules supplied by the user/skill are preserved.
3. The merge runs for the legacy `/home/developer/workspace` path too —
   the old code skipped it entirely.
4. The merge script itself is idempotent, safe under edge cases (no trailing
   newline, partial overlap), and reachable via the helper we expose.

Module: src/backend/services/git_service.py
"""
import base64
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend_path = str(_project_root / "src" / "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


def _load_git_service():
    """Import git_service with heavy deps mocked (same pattern as
    tests/unit/test_github_init_push.py)."""
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


# ---------------------------------------------------------------------------
# Helper / constant coverage
# ---------------------------------------------------------------------------

def test_default_denylist_covers_acceptance_criteria():
    """Acceptance criteria on #458: these patterns MUST be in the deny-list.

    Adding a new credential pattern to `DEFAULT_GITIGNORE_DENYLIST` is fine;
    removing any of these is not.
    """
    gs = _load_git_service()
    required = {
        ".env", ".env.*", ".mcp.json", "credentials.json",
        "token.json", "*.pem", "*.key",
    }
    missing = required - set(gs.DEFAULT_GITIGNORE_DENYLIST)
    assert not missing, f"deny-list missing required patterns: {missing}"


def test_default_denylist_negates_env_example():
    """`.env.example` is a committable template — deny-list must whitelist it."""
    gs = _load_git_service()
    assert "!.env.example" in gs.DEFAULT_GITIGNORE_DENYLIST


def test_default_denylist_orders_credentials_before_cache():
    """Credentials come first so an operator skimming the file sees the
    important patterns before the shell/cache noise."""
    gs = _load_git_service()
    denylist = gs.DEFAULT_GITIGNORE_DENYLIST
    env_idx = denylist.index(".env")
    bashrc_idx = denylist.index(".bashrc")
    assert env_idx < bashrc_idx, \
        "credentials must appear before shell/cache entries in the deny-list"


# ---------------------------------------------------------------------------
# Behavioral tests: run the generated bash against a real tmp directory
# ---------------------------------------------------------------------------

def _run_merge_command(git_service_module, target_dir: Path) -> None:
    """Execute `_build_gitignore_merge_command` against an actual directory.

    The helper returns a `bash -c '...'` string; we run it with
    `subprocess.run(..., shell=True)` to exercise the exact quoting path
    production hits. Any shell-quoting bug would surface here.
    """
    cmd = git_service_module._build_gitignore_merge_command(str(target_dir))
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, \
        f"merge script failed: stderr={result.stderr!r} stdout={result.stdout!r}"


def test_merge_creates_denylist_when_gitignore_missing(tmp_path):
    """AC #1: .env/.mcp.json/etc. must be ignored even if the workspace has
    no `.gitignore` at all."""
    gs = _load_git_service()
    _run_merge_command(gs, tmp_path)

    gi = (tmp_path / ".gitignore").read_text()
    for required in (
        ".env", ".env.*", ".mcp.json", "credentials.json",
        "token.json", "*.pem", "*.key",
    ):
        assert required in gi, f"{required!r} missing from generated .gitignore"


def test_merge_preserves_existing_user_rules(tmp_path):
    """AC #2: user-supplied rules (via skill or manual edit) must survive."""
    gs = _load_git_service()
    (tmp_path / ".gitignore").write_text(
        "# My rules\nnode_modules/\n*.log\n!important.log\n"
    )
    _run_merge_command(gs, tmp_path)

    gi = (tmp_path / ".gitignore").read_text()
    assert "# My rules" in gi
    assert "node_modules/" in gi
    assert "*.log" in gi
    assert "!important.log" in gi
    # ...alongside the managed deny-list:
    assert ".env" in gi
    assert ".mcp.json" in gi


def test_merge_does_not_duplicate_when_user_already_has_entry(tmp_path):
    """Idempotency: if the user's `.gitignore` already lists `.env`, we don't
    add a second copy."""
    gs = _load_git_service()
    (tmp_path / ".gitignore").write_text(".env\n")
    _run_merge_command(gs, tmp_path)

    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count(".env\n") == 1, \
        f"`.env` duplicated in merged file:\n{gi}"


def test_merge_is_idempotent(tmp_path):
    """Running the merge twice must produce the exact same file."""
    gs = _load_git_service()
    _run_merge_command(gs, tmp_path)
    first = (tmp_path / ".gitignore").read_text()
    _run_merge_command(gs, tmp_path)
    second = (tmp_path / ".gitignore").read_text()
    assert first == second


def test_merge_handles_no_trailing_newline(tmp_path):
    """Edge case: a hand-edited `.gitignore` may lack a trailing newline.
    The merge must insert one before appending the managed block so the
    first existing rule and the managed header don't collide on one line."""
    gs = _load_git_service()
    (tmp_path / ".gitignore").write_bytes(b"node_modules/")  # no \n
    _run_merge_command(gs, tmp_path)

    gi = (tmp_path / ".gitignore").read_text()
    assert "node_modules/\n" in gi, \
        "existing rule must end with a newline after merge"


def test_merge_adds_managed_header_only_once(tmp_path):
    """The 'Trinity-managed credential deny-list' header appears at most once,
    even across multiple merge runs."""
    gs = _load_git_service()
    _run_merge_command(gs, tmp_path)
    _run_merge_command(gs, tmp_path)
    _run_merge_command(gs, tmp_path)

    gi = (tmp_path / ".gitignore").read_text()
    header_count = gi.count(gs._GITIGNORE_MANAGED_HEADER)
    assert header_count == 1, \
        f"managed header appears {header_count} times, expected 1"


def test_command_is_safe_from_shell_injection(tmp_path):
    """The generated command must not break even if patterns contained shell
    metacharacters. Our deny-list has `*.pem`, `!.env.example`, `$`-free
    entries — base64 encoding of both the patterns AND the script body
    guarantees the shell sees only `[A-Za-z0-9+/=]`."""
    gs = _load_git_service()
    cmd = gs._build_gitignore_merge_command(str(tmp_path))

    # Outer form is `bash -c 'echo <b64> | base64 -d | bash'`. The bit between
    # `echo ` and ` |` must be pure base64.
    assert cmd.startswith("bash -c 'echo "), cmd
    b64_segment = cmd.split("echo ", 1)[1].split(" |", 1)[0]
    assert b64_segment and all(
        c.isalnum() or c in "+/=" for c in b64_segment
    ), f"non-base64 chars leaked into command: {b64_segment!r}"

    # And the decoded script must mention the git_dir we passed, nothing else.
    decoded = base64.b64decode(b64_segment).decode("utf-8")
    assert f"cd {tmp_path}" in decoded


# ---------------------------------------------------------------------------
# Call-site integration: initialize_git_in_container must invoke the merge
# on BOTH the /home/developer path AND the legacy /home/developer/workspace path
# ---------------------------------------------------------------------------

class _RecordingExec:
    """Records every command and returns success. Used to assert that the
    .gitignore merge ran against the right git_dir."""

    def __init__(self, workspace_has_content: bool, remote_has_main: bool = False):
        self.workspace_has_content = workspace_has_content
        self.remote_has_main = remote_has_main
        self.calls: list[str] = []

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        self.calls.append(command)

        # Workspace probe
        if "find /home/developer/workspace" in command:
            return {
                "exit_code": 0,
                "output": "1" if self.workspace_has_content else "0",
            }

        # origin/main existence check
        if "git rev-parse --verify origin/main" in command:
            return {
                "exit_code": 0 if self.remote_has_main else 1,
                "output": "",
            }

        # Final verify
        if "git rev-parse --git-dir" in command:
            return {"exit_code": 0, "output": ".git"}

        return {"exit_code": 0, "output": ""}


@pytest.mark.asyncio
async def test_gitignore_merge_runs_for_home_directory():
    """Standard-path agents (/home/developer) must get the deny-list."""
    gs = _load_git_service()
    fake = _RecordingExec(workspace_has_content=False)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert result.success, f"init failed: {result.error}"
    # The merge command is `bash -c 'echo <b64> | base64 -d | bash'`.
    # It must reference /home/developer inside the decoded script.
    merge_calls = [
        c for c in fake.calls
        if c.startswith("bash -c 'echo ") and "base64 -d | bash" in c
    ]
    assert len(merge_calls) == 1, \
        f"expected exactly one gitignore merge call, got: {merge_calls}"

    b64_segment = merge_calls[0].split("echo ", 1)[1].split(" |", 1)[0]
    decoded = base64.b64decode(b64_segment).decode("utf-8")
    assert "cd /home/developer" in decoded
    assert "cd /home/developer/workspace" not in decoded


@pytest.mark.asyncio
async def test_gitignore_merge_runs_for_legacy_workspace_path():
    """Bug fix (#458 AC #3): the legacy /home/developer/workspace branch must
    also get the deny-list. The old code skipped this path entirely."""
    gs = _load_git_service()
    fake = _RecordingExec(workspace_has_content=True)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="legacy-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert result.success, f"init failed: {result.error}"

    merge_calls = [
        c for c in fake.calls
        if c.startswith("bash -c 'echo ") and "base64 -d | bash" in c
    ]
    assert len(merge_calls) == 1, \
        f"BUG: legacy workspace path did not run the gitignore merge. " \
        f"Calls: {fake.calls}"

    b64_segment = merge_calls[0].split("echo ", 1)[1].split(" |", 1)[0]
    decoded = base64.b64decode(b64_segment).decode("utf-8")
    assert "cd /home/developer/workspace" in decoded


@pytest.mark.asyncio
async def test_gitignore_merge_runs_before_git_add():
    """Ordering: the `.gitignore` must exist before `git add .` stages the
    working tree, otherwise injected credentials are already in the index."""
    gs = _load_git_service()
    fake = _RecordingExec(workspace_has_content=False, remote_has_main=False)

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    def first_index(predicate):
        for i, c in enumerate(fake.calls):
            if predicate(c):
                return i
        return -1

    merge_i = first_index(
        lambda c: c.startswith("bash -c 'echo ") and "base64 -d | bash" in c
    )
    add_i = first_index(lambda c: "git add ." in c)

    assert merge_i >= 0, "gitignore merge was never invoked"
    assert add_i >= 0, "git add was never invoked"
    assert merge_i < add_i, \
        f"gitignore merge must run before `git add .` " \
        f"(merge@{merge_i}, add@{add_i})"


@pytest.mark.asyncio
async def test_merge_failure_aborts_initialization():
    """If the merge command itself fails (bad container state, permissions),
    we must abort before committing — we'd rather leave the repo uninitialized
    than commit credentials on a half-configured agent."""
    gs = _load_git_service()

    class _FailingExec:
        def __init__(self):
            self.calls = []

        async def __call__(self, container_name, command, timeout=60):
            self.calls.append(command)
            if "find /home/developer/workspace" in command:
                return {"exit_code": 0, "output": "0"}
            if "bash -c 'echo " in command and "base64 -d | bash" in command:
                return {"exit_code": 1, "output": "permission denied"}
            return {"exit_code": 0, "output": ""}

    fake = _FailingExec()
    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert not result.success
    assert "deny-list" in (result.error or "").lower() \
        or ".gitignore" in (result.error or "").lower(), \
        f"unclear error message: {result.error}"
    # Confirm we did NOT go on to run `git add` after the failure
    assert not any("git add ." in c for c in fake.calls), \
        "init must abort before `git add` when .gitignore write fails"
