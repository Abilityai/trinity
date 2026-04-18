"""
S5 — Operator-readable conflict classifier (#386).

Pure-function tests for `classify_conflict(stderr, ahead, behind, common_ancestor_sha)`
in src/backend/services/git_service.py. The classifier takes a stderr string
plus numeric ahead/behind counts and returns a `ConflictClass` enum value used
by the GitConflictModal to render operator-readable copy.

Stderr samples are the real payloads captured in /tmp/trinity-repro/; we store
trimmed copies in tests/git-sync/fixtures/ so the tests are reproducible even
if the repro scripts are re-run.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Load git_service.py directly via importlib so the test does NOT pull in
# services/__init__.py (which imports docker, database, fastapi, etc.).
# classify_conflict is a pure function — the test should exercise it without
# needing the full backend stack to be importable.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GIT_SERVICE_PATH = REPO_ROOT / "src" / "backend" / "services" / "git_service.py"


def _load_git_service():
    # Stub the modules git_service.py imports at top-level so the file loads.
    # These are only used by the async sync/pull helpers, not by classify_conflict.
    if "database" not in sys.modules:
        db_stub = types.ModuleType("database")
        db_stub.db = None
        db_stub.AgentGitConfig = object
        db_stub.GitSyncResult = object
        sys.modules["database"] = db_stub
    if "services" not in sys.modules:
        sys.modules["services"] = types.ModuleType("services")
    if "services.docker_service" not in sys.modules:
        docker_stub = types.ModuleType("services.docker_service")
        docker_stub.get_agent_container = lambda *a, **k: None
        docker_stub.execute_command_in_container = lambda *a, **k: None
        sys.modules["services.docker_service"] = docker_stub

    spec = importlib.util.spec_from_file_location(
        "git_service_under_test", GIT_SERVICE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gs = _load_git_service()
ConflictClass = _gs.ConflictClass
classify_conflict = _gs.classify_conflict

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_working_branch_external_write_from_cannot_lock_ref() -> None:
    """`remote: error: cannot lock ref` is the P5 silent-clobber signature.

    Two agents racing into the same working branch: the second force-push loses
    because the ref moved between when git wrote the expected sha and when the
    server tried to apply it. This is NOT parallel history — it's an external
    write on the working branch itself.
    """
    stderr = _load("p5_clobber_stderr.txt")
    assert classify_conflict(stderr, ahead=1, behind=0) == ConflictClass.WORKING_BRANCH_EXTERNAL_WRITE


def test_parallel_history_from_rebase_failure() -> None:
    """`Rebasing (N/M) ... could not apply <sha>` is the P2 parallel-history trap.

    Main was rewritten under the agent; `git pull --rebase origin main` fails
    because the common ancestor sits before the divergence and the agent's
    commits cannot replay on top of the rewritten main.
    """
    stderr = _load("p2_parallel_history_stderr.txt")
    assert classify_conflict(stderr, ahead=2, behind=1) == ConflictClass.PARALLEL_HISTORY


def test_auth_failure_from_authentication_failed() -> None:
    stderr = (
        "remote: Invalid username or password.\n"
        "fatal: Authentication failed for 'https://example.com/foo/bar.git/'\n"
    )
    assert classify_conflict(stderr, ahead=0, behind=0) == ConflictClass.AUTH_FAILURE


def test_auth_failure_from_missing_username_prompt() -> None:
    stderr = (
        "fatal: could not read Username for 'https://example.com': "
        "terminal prompts disabled\n"
    )
    assert classify_conflict(stderr, ahead=0, behind=0) == ConflictClass.AUTH_FAILURE


def test_uncommitted_local_from_would_be_overwritten() -> None:
    stderr = (
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\tworkspace.txt\n"
        "Please commit your changes or stash them before you merge.\n"
        "Aborting\n"
    )
    assert classify_conflict(stderr, ahead=0, behind=1) == ConflictClass.UNCOMMITTED_LOCAL


def test_ahead_only_clean_state() -> None:
    """Clean sync-ready state: ahead>0, behind=0, stderr empty."""
    assert classify_conflict("", ahead=3, behind=0) == ConflictClass.AHEAD_ONLY


def test_behind_only_clean_state() -> None:
    """Need to pull: behind>0, ahead=0, stderr empty."""
    assert classify_conflict("", ahead=0, behind=2) == ConflictClass.BEHIND_ONLY


def test_unknown_for_unrecognized_stderr() -> None:
    stderr = "some completely unrelated error from a future git version\n"
    assert classify_conflict(stderr, ahead=0, behind=0) == ConflictClass.UNKNOWN


def test_returns_enum_instance_not_string() -> None:
    """Contract: return type is the enum itself so callers get IDE help."""
    result = classify_conflict("", ahead=0, behind=0)
    assert isinstance(result, ConflictClass)


def test_enum_has_expected_members() -> None:
    """The spec lists seven members; guard against accidental renames."""
    expected = {
        "AHEAD_ONLY",
        "BEHIND_ONLY",
        "PARALLEL_HISTORY",
        "UNCOMMITTED_LOCAL",
        "AUTH_FAILURE",
        "WORKING_BRANCH_EXTERNAL_WRITE",
        "UNKNOWN",
    }
    assert {m.name for m in ConflictClass} == expected


def test_common_ancestor_sha_is_optional() -> None:
    """The function signature allows `common_ancestor_sha=None` (default)."""
    # Should not raise with or without the kwarg.
    classify_conflict("", ahead=0, behind=0, common_ancestor_sha=None)
    classify_conflict("", ahead=0, behind=0, common_ancestor_sha="deadbeef")
