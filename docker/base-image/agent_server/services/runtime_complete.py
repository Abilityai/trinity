"""One-shot completion on the agent's own runtime (trinity-enterprise#1027).

The backend's retrieval memory extracts and reconciles facts with a small
model call. On a subscription-only install there is no platform key, and
the token the install does have belongs to Claude Code — so the call runs
HERE, inside the agent container, through the agent's own CLI, and the
agent's subscription (or its own ``.env`` key) pays for remembering what it
did. Nothing of the agent's context is in play: no tools, no MCP servers,
no project ``CLAUDE.md`` or ``.claude/settings.json`` hooks (an empty scratch
cwd), one model turn, the prompt on stdin.

The subprocess is a first-class citizen of this container: registered with
the process registry (so the orphan sweep leaves it alone and a terminate
request can reach it), tagged with ``EXECUTION_TAG_NAME`` like every other
spawn, killed as a process group on timeout, and its output credential-
sanitised before it leaves. Concurrency is bounded here (``_SEM``), never
serialised behind the chat lock — a memory extraction must not wait for a
twenty-minute turn.

Only the Claude runtime implements this today. ``AgentRuntime.complete``'s
default raises ``RuntimeUnsupported`` so Gemini / Codex answer ``503
unsupported`` honestly: neither CLI has a clean "answer with no tools" one-shot
without running the real binary to find out.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from ..config import WORKSPACE_DIR
from ..utils.credential_sanitizer import sanitize_text
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import _load_guardrails, merged_disallowed_tools
from .error_classifier import _is_auth_failure_message, _is_rate_limit_message
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .subprocess_lifecycle import _capture_pgid, _terminate_process_group

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
SYSTEM_PROMPT = "Answer only with what is asked. Use no tools."
# Claude Code's built-in tools — every one denied so the answer is one turn of
# text. The platform denials and the operator's guardrail list are merged in.
BUILTIN_TOOLS = (
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "Task", "TodoWrite", "Agent",
)
_NO_MCP = '{"mcpServers":{}}'
_MAX_CONCURRENT = 2
_LOG_PREVIEW = 120

_SEM = asyncio.Semaphore(_MAX_CONCURRENT)


class CompleteError(Exception):
    """A classified failure of one completion: ``status`` is the HTTP status
    the route answers with, ``code`` the stable reason."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def build_claude_complete_argv(model: Optional[str], guardrails: Optional[dict] = None) -> list:
    """``claude --print`` for one tool-less turn: JSON envelope, one turn, the
    minimal system prompt REPLACED (not appended), no MCP servers, every tool
    denied. The prompt is never an argument (stdin)."""
    denied = list(merged_disallowed_tools(guardrails if guardrails is not None else _load_guardrails()))
    for tool in BUILTIN_TOOLS:
        if tool not in denied:
            denied.append(tool)
    return [
        "claude", "--print", "--output-format", "json",
        "--model", model or DEFAULT_MODEL,
        "--max-turns", "1",
        "--system-prompt", SYSTEM_PROMPT,
        "--strict-mcp-config", "--mcp-config", _NO_MCP,
        "--disallowedTools", ",".join(denied),
        "--no-session-persistence",
    ]


def _scratch_root() -> Path:
    return Path(os.environ.get("TMPDIR") or (WORKSPACE_DIR / ".tmp")) / "complete"


def _classify(stdout: str, stderr: str, returncode: int) -> CompleteError:
    text = f"{stderr}\n{stdout}"
    if _is_rate_limit_message(text):
        return CompleteError(429, "rate_limited", "the runtime's subscription is at its usage limit")
    if _is_auth_failure_message(text):
        return CompleteError(502, "auth_failure", "the runtime's credential was refused")
    preview = sanitize_text((stderr or stdout or "").strip())[:_LOG_PREVIEW]
    return CompleteError(502, "runtime_error", f"the runtime exited {returncode}: {preview}" if preview
                         else f"the runtime exited {returncode} with no output")


def _run_claude(prompt: str, *, model: Optional[str], timeout_seconds: int, execution_id: str) -> str:
    """Blocking: spawn, register, write the prompt, wait (bounded), classify."""
    cmd = build_claude_complete_argv(model)
    root = _scratch_root()
    cwd = root / uuid.uuid4().hex
    cwd.mkdir(parents=True, exist_ok=False)
    registry = get_process_registry()
    process = None
    pgid = None
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=str(cwd),
            env=build_execution_env({EXECUTION_TAG_NAME: execution_id}),
        )
        pgid = _capture_pgid(process)
        registry.register(execution_id, process, metadata={
            "type": "complete",
            "message_preview": prompt[:_LOG_PREVIEW],
            "pgid": pgid,
        })
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, 0, pgid=pgid, execution_tag=execution_id)
            raise CompleteError(504, "timeout", f"the runtime did not answer within {timeout_seconds}s") from None
        stdout, stderr = stdout or "", stderr or ""
        if process.returncode != 0:
            raise _classify(stdout, stderr, process.returncode)
        body = stdout.strip()
        if not body:
            raise _classify(stdout, stderr, 0)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict) and "result" in data:
            result = str(data.get("result") or "")
            if data.get("is_error"):
                raise _classify(result, stderr, 0)
            body = result.strip()
        if not body:
            raise _classify("", stderr, 0)
        return sanitize_text(body)
    finally:
        if process is not None:
            try:
                registry.unregister(execution_id)
            except Exception:  # noqa: BLE001 — bookkeeping never masks the result
                logger.debug("[complete] unregister failed for %s", execution_id)
        shutil.rmtree(cwd, ignore_errors=True)


async def complete_claude(prompt: str, *, model: Optional[str], timeout_seconds: int,
                          execution_id: Optional[str] = None) -> str:
    """The Claude runtime's ``complete``: one bounded subprocess off the loop."""
    run_id = execution_id or f"complete-{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    async with _SEM:
        text = await asyncio.to_thread(_run_claude, prompt, model=model, timeout_seconds=timeout_seconds,
                                       execution_id=run_id)
    logger.info("[complete] %s answered in %.1fs (%d chars)", run_id, time.monotonic() - started, len(text))
    return text
