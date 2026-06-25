"""
Auto-fix engine for correctable compatibility checks (#668).

Only the gitignore-related checks are auto-fixable (the spec-declared
`auto_fixable=True` set). Every fix is a read-modify-write of the in-container
`.gitignore`, computed in Python and written back ATOMICALLY (write tmp + mv) via
a base64-injected payload so file content can never break shell quoting.

Concurrency: per-agent Redis lock (`compat_fix:{name}`) serialises the
read-modify-write so two simultaneous fixes can't lose an update. The lock is
best-effort — if Redis is unavailable the fix still proceeds (the append is
idempotent via exact-line matching, and the removal is a no-op once gone).

No auto-commit: the fix edits the working-tree `.gitignore` only; committing and
pushing is the agent's own git-sync job, so the change is "uncommitted until next
sync" (surfaced to the caller).
"""

import base64
import logging
import re
from typing import List, Optional, Tuple

from services.docker_service import execute_command_in_container, get_agent_container
from services.git_service import _detect_git_dir, _GITIGNORE_PATTERNS
from . import spec

logger = logging.getLogger(__name__)

try:
    import redis as _redis_lib
    from config import REDIS_URL as _REDIS_URL
except Exception:  # pragma: no cover - redis/config always present in prod
    _redis_lib = None
    _REDIS_URL = ""

# Specific patterns appended by each per-check append fix.
_ADD_PATTERNS = {
    "S-001": [".env", ".env.*"],
    "S-002": [".mcp.json"],
    "S-004": [".claude/projects/"],
    "S-005": [".trinity/"],
    "S-006": [".claude/statsig/", ".claude/todos/", ".claude/debug/",
              ".claude/sessions/", ".claude/shell-snapshots/"],
    "S-007": ["content/"],
    "S-008": ["*.pem", "*.key", "credentials.json"],
}

# Specific `.claude/*` exclusions G-001 adds after removing the blanket line.
_G001_REPLACEMENTS = [".claude/projects/", ".claude/statsig/", ".claude/todos/",
                      ".claude/debug/", ".claude/sessions/", ".claude/shell-snapshots/"]


class FixError(Exception):
    """Raised for an un-fixable / unknown check id (router maps to 400)."""


class FixBusy(Exception):
    """Raised when another fix is in flight for the same agent (router maps to 409)."""


def _existing_trimmed(lines: List[str]) -> set:
    return {l.strip().rstrip("\r").strip() for l in lines if l.strip()}


def _append_missing(current: str, patterns: List[str]) -> str:
    """Append any patterns whose exact trimmed line isn't already present."""
    lines = current.splitlines()
    present = _existing_trimmed(lines)
    additions = [p for p in patterns if p not in present]
    if not additions:
        return current
    out = current
    if out and not out.endswith("\n"):
        out += "\n"
    out += "\n".join(additions) + "\n"
    return out


def _remove_blanket_claude(current: str) -> str:
    """Remove a wholesale `.claude/` or `.claude` exclusion (line-by-line,
    CRLF-normalized, never substring — `.claude/projects/` must survive)."""
    kept = []
    for raw in current.splitlines():
        trimmed = raw.strip().rstrip("\r").strip()
        if trimmed in (".claude/", ".claude") and not trimmed.startswith("#"):
            continue  # drop the blanket line
        kept.append(raw.rstrip("\r"))
    out = "\n".join(kept)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def _compute_new_gitignore(check_id: str, current: str) -> str:
    """Pure transform: current .gitignore content -> fixed content for check_id."""
    if check_id == "F-003":
        # Ensure a .gitignore exists with the canonical pattern list.
        base = current if current.strip() else ""
        return _append_missing(base, list(_GITIGNORE_PATTERNS))
    if check_id == "G-002":
        return _append_missing(current, list(_GITIGNORE_PATTERNS))
    if check_id == "G-001":
        removed = _remove_blanket_claude(current)
        return _append_missing(removed, _G001_REPLACEMENTS)
    if check_id in _ADD_PATTERNS:
        return _append_missing(current, _ADD_PATTERNS[check_id])
    raise FixError(f"check {check_id} is not auto-fixable")


def _lock(agent_name: str):
    if _redis_lib is None or not _REDIS_URL:
        return None
    try:
        client = _redis_lib.from_url(_REDIS_URL, decode_responses=True)
        if client.set(f"compat_fix:{agent_name}", "1", nx=True, ex=30):
            return client
        raise FixBusy("another fix is in progress for this agent")
    except FixBusy:
        raise
    except Exception as e:  # noqa: BLE001 — redis down → proceed best-effort
        logger.warning("[compatibility] fix lock unavailable for %s: %s", agent_name, e)
        return None


def _unlock(client, agent_name: str):
    if client is None:
        return
    try:
        client.delete(f"compat_fix:{agent_name}")
    except Exception:  # noqa: BLE001
        pass


async def _read_gitignore(agent_name: str, git_dir: str) -> str:
    result = await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=f'bash -c "cat {git_dir}/.gitignore 2>/dev/null || true"',
        timeout=10,
    )
    return result.get("output") or ""


async def _write_gitignore(agent_name: str, git_dir: str, content: str) -> bool:
    """Atomically write .gitignore via base64 (content-injection-safe) + mv."""
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (
        f'bash -c "echo {b64} | base64 -d > {git_dir}/.gitignore.tmp '
        f'&& mv {git_dir}/.gitignore.tmp {git_dir}/.gitignore"'
    )
    result = await execute_command_in_container(
        container_name=f"agent-{agent_name}", command=cmd, timeout=10,
    )
    return result.get("exit_code") == 0


async def apply_fix(agent_name: str, check_id: str) -> Tuple[bool, str]:
    """Apply the auto-fix for check_id. Returns (fixed, message).

    Raises FixError (unknown/non-fixable id), FixBusy (concurrent fix), or
    returns (False, reason) for a no-op / failed write.
    """
    if check_id not in spec.AUTO_FIXABLE_IDS:
        raise FixError(f"check {check_id} is not auto-fixable")

    container = get_agent_container(agent_name)
    if container is None or getattr(container, "status", None) != "running":
        return False, "Agent must be running to apply a fix"

    client = _lock(agent_name)
    try:
        git_dir = await _detect_git_dir(f"agent-{agent_name}")
        current = await _read_gitignore(agent_name, git_dir)
        new_content = _compute_new_gitignore(check_id, current)
        if new_content == current:
            return True, "Already satisfied — no change needed"
        if not await _write_gitignore(agent_name, git_dir, new_content):
            return False, "Failed to write .gitignore"
        # Verify the write landed.
        verify = await _read_gitignore(agent_name, git_dir)
        if verify != new_content:
            return False, "Write verification failed"
        return True, "Fixed .gitignore (uncommitted until the agent's next git sync)"
    finally:
        _unlock(client, agent_name)
