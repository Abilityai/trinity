"""OpenAI Codex CLI execution service (#1187).

Implements the :class:`AgentRuntime` interface for OpenAI's Codex CLI, the third
Trinity agent runtime alongside Claude Code and Gemini.

Built **independently** on the existing per-runtime primitives (process
registry, concurrency-safe orphan drain, activity tracking, credential
sanitizer) rather than on a shared subprocess helper — see #1187 decision 4.
That keeps Codex from inheriting Gemini's blanket ``kill_cgroup_orphans()``
(which SIGKILLs sibling executions in the same cgroup); Codex uses the
concurrency-safe ``_drain_bounded`` path that preserves other in-flight work.

Safety parity with the Claude path (#1187 decision 8, Phase C):
  * **System prompt / identity** — the backend's effective ``system_prompt``
    is prepended to every turn (Codex ``exec`` has no ``--append-system-prompt``);
    persistent identity comes from ``AGENTS.md`` (startup copies ``CLAUDE.md``).
  * **Read-only mode** — when ``~/.trinity/read-only-config.json`` is enabled,
    Codex runs with ``--sandbox read-only`` (the Claude hook can't apply here).
  * **Guardrails** — read-only is honored via the sandbox; ``disallowed_tools``
    that have no Codex equivalent are SURFACED in the logs, never silently
    dropped.
  * **Credential sanitization** — every stdout line, the final response, and
    stderr pass through ``utils.credential_sanitizer`` exactly as the Claude /
    headless paths do.

Codex specifics:
  * Non-interactive: ``codex exec [PROMPT]``; ``--json`` emits a JSONL event
    stream; ``-o/--output-last-message FILE`` is the durable result record
    (#548/#333) — read-then-delete in ``finally``.
  * Continuity: ``codex exec resume <thread_id>`` replays a prior thread.
  * No native cost — derived from ``turn.completed.usage`` token counts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import CODEX_CONTEXT_WINDOW, resolve_context_window
from ..state import agent_state
from ..utils.credential_sanitizer import (
    sanitize_dict,
    sanitize_subprocess_line,
    sanitize_text,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import _DEFAULT_EXECUTION_TIMEOUT_SEC, _load_guardrails
from .activity_tracking import complete_tool_execution, start_tool_execution
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .subprocess_lifecycle import (
    _capture_pgid,
    _drain_bounded,
    _safe_close_pipes,
    _terminate_process_group,
)

logger = logging.getLogger(__name__)

# One long-lived reader-thread worker (mirrors claude_code.py / gemini_runtime.py).
# A fresh ThreadPoolExecutor per call relies on CPython's non-deterministic
# weakref cleanup of the worker thread under load (#333 hardening).
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-subproc")

# GPT-5 context window (input). Cosmetic — drives the context gauge only.
# Single-sourced from the shared model catalog (#1521); imported above.

# Codex / GPT-5 pricing per 1K tokens (USD). Codex reports no cost; we derive
# it from token counts. ``cached`` is the discounted rate for cached input
# tokens. Bump deliberately when OpenAI pricing changes (#1137-style).
CODEX_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-5.1-codex": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
    "gpt-5.1-codex-max": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
    "gpt-5-codex": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
    "gpt-5.1": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
    "gpt-5": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
    "gpt-5-mini": {"input": 0.00025, "cached": 0.000025, "output": 0.002},
    "gpt-5-nano": {"input": 0.00005, "cached": 0.000005, "output": 0.0004},
    # Unknown / future model → GPT-5 standard pricing.
    "default": {"input": 0.00125, "cached": 0.000125, "output": 0.01},
}


def _resolve_pricing(model: Optional[str]) -> Dict[str, float]:
    """Pricing for ``model`` — exact key first, then longest matching prefix,
    then the ``default`` fallback (never KeyErrors)."""
    if not model:
        return CODEX_PRICING["default"]
    key = model.lower()
    if key in CODEX_PRICING:
        return CODEX_PRICING[key]
    # Longest-prefix match so "gpt-5.1-codex-2025-xx" resolves to the codex rate.
    candidates = [k for k in CODEX_PRICING if k != "default" and key.startswith(k)]
    if candidates:
        return CODEX_PRICING[max(candidates, key=len)]
    return CODEX_PRICING["default"]


def calculate_codex_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    model: Optional[str] = None,
) -> float:
    """Estimated USD cost for a Codex turn.

    ``reasoning_output_tokens`` is a SUBSET of ``output_tokens`` — bill
    ``output_tokens`` once, never ``output_tokens + reasoning_output_tokens``.
    Cached input tokens bill at the cheaper cached rate; only the uncached
    remainder bills at the full input rate.
    """
    pricing = _resolve_pricing(model)
    uncached_input = max(0, input_tokens - cached_input_tokens)
    cached = max(0, cached_input_tokens)
    input_cost = (uncached_input / 1000) * pricing["input"] + (
        cached / 1000
    ) * pricing["cached"]
    output_cost = (output_tokens / 1000) * pricing["output"]
    return round(input_cost + output_cost, 6)


# ---------------------------------------------------------------------------
# Credentials, sandbox, CODEX_HOME (parity wiring — #1187 Phase C/T4)
# ---------------------------------------------------------------------------

_API_KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")
_AGENT_HOME = "/home/developer"
_READ_ONLY_CONFIG = Path(_AGENT_HOME) / ".trinity" / "read-only-config.json"


def _parse_env_value(raw_value: str) -> str:
    """Extract a value from a ``.env`` ``KEY=VALUE`` right-hand side.

    Handles the shapes a human SSH-editing ``.env`` would produce that Trinity's
    own plain ``KEY=VALUE`` writer never emits: a quoted value (the quotes are
    stripped and an interior ``#`` is kept), and an unquoted value with a
    trailing ``# inline comment`` (dropped at the first whitespace-``#``).
    """
    value = raw_value.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment].rstrip()
    return value


def _load_openai_api_key() -> Optional[str]:
    """Resolve the OpenAI/Codex API key.

    The per-agent ``.env`` (CRED-002) is copied to ``/home/developer/.env`` by
    startup.sh but is NOT exported into the agent-server process — so unlike the
    Claude/Gemini key (a container env var), the Codex key must be read from the
    process env (if present) OR parsed out of ``.env`` (the cold-start path the
    outside-voice review flagged). Accepts either OPENAI_API_KEY or CODEX_API_KEY.
    """
    for var in _API_KEY_VARS:
        value = os.environ.get(var)
        if value:
            return value
    env_path = Path(_AGENT_HOME) / ".env"
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Tolerate `export KEY=VALUE` (a hand-edited .env), not just KEY=VALUE.
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            if key.strip() in _API_KEY_VARS:
                cleaned = _parse_env_value(value)
                if cleaned:
                    return cleaned
    except (IOError, OSError):
        pass
    return None


def _codex_home() -> str:
    """Non-workspace home for Codex state + the ``-o`` result file.

    Codex defaults ``CODEX_HOME`` to ``~/.codex`` — inside the git-tracked agent
    repo, which would dirty auto-sync. Relocate it under ``$TMPDIR`` (the
    disk-backed ``/home/developer/.tmp`` scratch dir, #1098) which startup.sh
    gitignores for Codex agents.
    """
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        return explicit
    tmpdir = os.environ.get("TMPDIR") or os.path.join(_AGENT_HOME, ".tmp")
    return os.path.join(tmpdir, "codex")


def _ensure_codex_home() -> str:
    home = _codex_home()
    try:
        os.makedirs(home, exist_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("[Codex] could not create CODEX_HOME %s: %s", home, exc)
    return home


def _is_read_only() -> bool:
    """True when the backend has put this agent in read-only mode.

    The signal is the same JSON file the Claude read-only *hook* consumes
    (``~/.trinity/read-only-config.json`` → ``enabled``). Codex can't run Claude
    hooks, so we read the file directly and translate it to ``--sandbox
    read-only`` (a sandbox-native, non-cooperative enforcement).

    An absent file ⇒ not read-only (the normal writable-agent state — silent).
    A present-but-unreadable/corrupt file fails OPEN **and logs**, matching the
    reference hook (``read-only-guard.py`` logs ``read_only_config_load_error``
    and allows). Diverging one runtime to fail-closed would make read-only
    enforcement inconsistent across runtimes (CSO #1187 finding 3); if the
    platform wants fail-closed, change both loaders together in a dedicated
    issue.
    """
    try:
        raw = _READ_ONLY_CONFIG.read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "[Codex] read-only config unreadable (%s); treating as not read-only", exc
        )
        return False
    try:
        return bool(json.loads(raw).get("enabled"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "[Codex] read-only config malformed (%s); treating as not read-only", exc
        )
        return False


def _resolve_sandbox_mode() -> str:
    """Map Trinity's mode to a Codex ``--sandbox`` value.

    Normal (writable) agents run with ``danger-full-access``, which DISABLES
    Codex's own bubblewrap sandbox. ``workspace-write``/``read-only`` both invoke
    ``bwrap`` to create a user namespace, which the hardened Trinity container
    forbids (``bwrap: No permissions to create a new namespace``) — so any
    in-sandbox mode blocks EVERY shell tool. The Trinity container is already the
    security boundary (``cap_drop ALL`` + AppArmor + ``no-new-privileges``),
    exactly the posture Claude and Gemini run under (no internal sandbox), so
    dropping Codex's redundant inner sandbox weakens nothing.

    Read-only mode is the deliberate exception: it keeps ``--sandbox read-only``
    (sandbox-native write protection) as the interim enforcement. A fail-closed
    read-only enforcement story for Codex is a fast-follow.
    """
    return "read-only" if _is_read_only() else "danger-full-access"


def _surface_unmapped_guardrails(allowed_tools: Optional[List[str]]) -> None:
    """Honor what maps to Codex's control surface; SURFACE (never silently
    drop) the rest (#1187 decision 8 + the unresolved-decision caveat).

    Read-only is enforced via the sandbox. Claude ``disallowed_tools`` names
    (Bash, Write, Edit, WebSearch, …) have no 1:1 Codex ``exec`` CLI toggle in
    the MVP, so we log them at WARNING for operator visibility rather than
    pretending they're enforced.
    """
    guardrails = _load_guardrails()
    disallowed = guardrails.get("disallowed_tools") or []
    if disallowed:
        logger.warning(
            "[Codex] guardrails disallow %s — Codex exec has no per-tool CLI "
            "toggle in the MVP; only read-only (sandbox) and network access are "
            "enforced. Tracking finer-grained Codex tool gating as a fast-follow.",
            disallowed,
        )
    if allowed_tools:
        logger.info(
            "[Codex] allowed_tools=%s requested; Codex exec runs its full tool "
            "set under the sandbox (no allowlist CLI flag in the MVP).",
            allowed_tools,
        )


def _compose_prompt(system_prompt: Optional[str], prompt: str) -> str:
    """Codex ``exec`` has no system-prompt flag, so the effective platform
    prompt (platform instructions + execution context + caller prompt, always
    sent by the backend) is prepended to the user message. Persistent identity
    additionally comes from AGENTS.md."""
    if system_prompt:
        return f"{system_prompt}\n\n---\n\n{prompt}"
    return prompt


def _ensure_within(base: str, path: str) -> str:
    """Resolve ``path`` and confirm it stays within ``base``; raise otherwise.

    Defense-in-depth at the filesystem sink. The result filename is already
    reduced to a safe token by ``_safe_result_token`` + a fixed ``-last.txt``
    suffix, so this never trips in practice — but anchoring the containment
    check at the ``open``/``unlink`` sink keeps the safety property local to the
    operation that actually touches the filesystem (and is the barrier static
    analysis recognizes)."""
    base_real = os.path.realpath(base)
    target = os.path.realpath(path)
    if target != base_real and not target.startswith(base_real + os.sep):
        raise ValueError(f"result path escapes codex_home: {path!r}")
    return target


def _read_and_consume_result_file(path: str, base: str) -> Optional[str]:
    """Read the ``-o`` durable result file. Deletion is the caller's ``finally``
    (read-then-delete, happy + error path — #1187 decision 5). ``base`` anchors
    the sink-side containment guard (see ``_ensure_within``)."""
    try:
        with open(_ensure_within(base, path), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except ValueError:
        # Containment guard tripped — must never happen in practice; surface it
        # rather than masking a genuine path bug as a benign missing file.
        logger.warning("[Codex] refusing to read result file outside codex_home: %r", path)
        return None
    except (IOError, OSError):
        return None


def _safe_unlink(path: str, base: str) -> None:
    try:
        os.unlink(_ensure_within(base, path))
    except ValueError:
        logger.warning("[Codex] refusing to unlink result file outside codex_home: %r", path)
    except OSError:
        pass


def _safe_result_token(execution_id: str) -> str:
    """Filesystem-safe token for the ``-o`` result filename. ``execution_id`` is
    system-generated today (uuid4 fallback / backend urlsafe token), but never
    build a path from it unguarded: reduce it to a basename and a conservative
    charset so a '/' or '..' can't escape CODEX_HOME (defense-in-depth — CSO
    #1187 finding 2)."""
    token = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(execution_id))
    return token or "codex"


def _resolve_returned_session_id(metadata: ExecutionMetadata) -> Optional[str]:
    """The thread id to cache for chat continuity (review I4).

    Codex emits ``thread.started`` on every ``exec``; if it somehow didn't,
    return ``None`` so the next turn degrades to a fresh run — NOT a fabricated
    id (e.g. the random ``execution_id``), which would make the next
    ``codex exec resume <id>`` fail hard and repeat every turn.
    """
    return metadata.session_id


def _finalize_codex_response(
    result_file_text: Optional[str], response_parts: List[str]
) -> str:
    """The ``-o`` file is the authoritative response; JSONL ``agent_message``
    parts are the fallback when the file is missing/empty (#1187 decision 5)."""
    if result_file_text and result_file_text.strip():
        return result_file_text.strip()
    return "\n".join(response_parts).strip()


# ---------------------------------------------------------------------------
# JSONL event parsing
# ---------------------------------------------------------------------------

# item.type values that represent tool/command activity (vs. agent_message /
# reasoning / todo_list). Confirmed against codex exec_events.rs ThreadItemDetails.
_CODEX_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
}

_CODEX_TOOL_DISPLAY = {
    "command_execution": "Shell",
    "file_change": "FileChange",
    "mcp_tool_call": "McpTool",
    "web_search": "WebSearch",
}


@dataclass
class _CodexParseState:
    """Mutable accumulators threaded through per-event parsing."""

    execution_log: List[ExecutionLogEntry]
    metadata: ExecutionMetadata
    response_parts: List[str]
    model: Optional[str] = None
    seen_tool_ids: set = field(default_factory=set)


def _tool_display_name(item: dict, item_type: str) -> str:
    if item_type == "mcp_tool_call":
        tool = item.get("tool") or item.get("name")
        server = item.get("server")
        if tool:
            return f"{server}.{tool}" if server else str(tool)
    return _CODEX_TOOL_DISPLAY.get(item_type, item_type)


def _tool_input(item: dict, item_type: str) -> dict:
    if item_type == "command_execution":
        return {"command": item.get("command")}
    if item_type == "web_search":
        return {"query": item.get("query")}
    if item_type == "file_change":
        return {"changes": item.get("changes")}
    if item_type == "mcp_tool_call":
        return {"arguments": item.get("arguments")}
    return {}


def _tool_output(item: dict, item_type: str) -> str:
    for key in ("aggregated_output", "output", "result", "stdout"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _record_tool_use(state: _CodexParseState, tool_id: str, item: dict, item_type: str) -> None:
    if tool_id in state.seen_tool_ids:
        return
    state.seen_tool_ids.add(tool_id)
    name = _tool_display_name(item, item_type)
    tool_input = _tool_input(item, item_type)
    state.execution_log.append(
        ExecutionLogEntry(
            id=tool_id,
            type="tool_use",
            tool=name,
            input=tool_input,
            timestamp=datetime.now().isoformat(),
        )
    )
    try:
        start_tool_execution(tool_id, name, tool_input)
    except Exception:  # noqa: BLE001 - activity tracking is best-effort
        logger.debug("[Codex] start_tool_execution failed for %s", tool_id, exc_info=True)


def _record_tool_result(state: _CodexParseState, tool_id: str, item: dict, item_type: str) -> None:
    name = _tool_display_name(item, item_type)
    output = _tool_output(item, item_type)
    status = item.get("status")
    exit_code = item.get("exit_code")
    is_error = status == "failed" or (isinstance(exit_code, int) and exit_code != 0)
    state.execution_log.append(
        ExecutionLogEntry(
            id=tool_id,
            type="tool_result",
            tool=name,
            output=output or None,
            success=not is_error,
            timestamp=datetime.now().isoformat(),
        )
    )
    try:
        complete_tool_execution(tool_id, not is_error, output)
    except Exception:  # noqa: BLE001
        logger.debug("[Codex] complete_tool_execution failed for %s", tool_id, exc_info=True)


def _process_codex_event(event: dict, state: _CodexParseState) -> None:
    """Update ``state`` from one parsed Codex JSONL event. Tolerant of unknown
    event/item types and missing fields — the ``-o`` file is authoritative for
    the response, so a best-effort parser here only affects tokens, tool
    activity, and error classification."""
    event_type = event.get("type")

    if event_type == "thread.started":
        state.metadata.session_id = event.get("thread_id") or state.metadata.session_id

    elif event_type == "turn.completed":
        usage = event.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        # reasoning_output_tokens is a subset of output_tokens — do NOT add it.
        state.metadata.input_tokens = input_tokens
        state.metadata.output_tokens = output_tokens
        state.metadata.cache_read_tokens = cached
        state.metadata.cost_usd = calculate_codex_cost(
            input_tokens, cached, output_tokens, state.model
        )

    elif event_type == "turn.failed":
        error = event.get("error") or {}
        state.metadata.error_type = "turn_failed"
        state.metadata.error_message = (
            error.get("message") if isinstance(error, dict) else str(error)
        ) or "Codex turn failed"

    elif event_type == "error":
        state.metadata.error_type = "error"
        state.metadata.error_message = event.get("message") or "Codex error"

    elif event_type in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        item_type = item.get("type") or (item.get("details") or {}).get("type")
        if not item_type:
            return
        item_id = item.get("id") or str(uuid.uuid4())

        if item_type == "agent_message":
            if event_type == "item.completed":
                text = item.get("text") or item.get("message") or ""
                if text:
                    state.response_parts.append(text)
        elif item_type in _CODEX_TOOL_ITEM_TYPES:
            if event_type == "item.started":
                _record_tool_use(state, item_id, item, item_type)
            elif event_type == "item.completed":
                _record_tool_use(state, item_id, item, item_type)  # no-op if seen
                _record_tool_result(state, item_id, item, item_type)
        elif item_type == "error":
            state.metadata.error_type = "error"
            state.metadata.error_message = (
                item.get("message") or state.metadata.error_message or "Codex item error"
            )


def parse_codex_jsonl(
    lines: List[str], model: Optional[str] = None
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
    """Parse a full Codex ``--json`` line stream (unit-test entrypoint).

    Returns ``(response_text, execution_log, metadata, raw_messages)`` where
    ``response_text`` is the JSONL-assembled fallback (the live path overrides
    it with the ``-o`` file)."""
    metadata = ExecutionMetadata()
    metadata.context_window = CODEX_CONTEXT_WINDOW
    state = _CodexParseState(execution_log=[], metadata=metadata, response_parts=[], model=model)
    raw_messages: List[Dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            raw_messages.append(event)
            _process_codex_event(event, state)
    metadata.tool_count = len([e for e in state.execution_log if e.type == "tool_use"])
    response_text = "\n".join(state.response_parts).strip()
    return response_text, state.execution_log, metadata, raw_messages


# ---------------------------------------------------------------------------
# Error classification (return-code path)
# ---------------------------------------------------------------------------

# AUTH detection is anchored, not bare-substring. Bare "401"/"api key" are
# over-broad — a non-auth failure whose output merely contains "401" (e.g. an
# upstream MCP/tool returning 401) must NOT be read as an auth failure, because
# 503 is the backend's AUTH signal and the dispatch breaker counts AUTH only
# (#1187 decision 3, review I1). Each pattern names an actual auth condition and
# uses word boundaries so it won't fire on an incidental token.
_AUTH_PATTERNS = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|incorrect|missing|no)[ _]api[ _]key\b", re.IGNORECASE),
    re.compile(r"\bnot\s+authenticated\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s+(?:failed|error)\b", re.IGNORECASE),
)
_RATE_MARKERS = ("429", "rate limit", "rate_limit", "quota", "too many requests")


def _classify_codex_failure(
    return_code: int, stderr: str, metadata: ExecutionMetadata
) -> Tuple[int, str]:
    """Map a non-zero Codex exit (+ stderr + parsed error) to an HTTP status.

    auth → 503, rate-limit → 429, everything else → 500 (runtime-unavailable).
    Crucially a generic runtime failure is 500, NOT 503 — 503 is the backend's
    AUTH signal and the dispatch breaker counts AUTH only (#1187 decision 3)."""
    haystack = " ".join(
        s for s in (stderr or "", metadata.error_message or "") if s
    )
    haystack_lower = haystack.lower()
    if any(marker in haystack_lower for marker in _RATE_MARKERS):
        return 429, f"Codex rate limit: {(stderr or metadata.error_message or '')[:300]}"
    if any(pattern.search(haystack) for pattern in _AUTH_PATTERNS):
        return 503, (
            f"Codex authentication failure: {(stderr or metadata.error_message or '')[:300]}. "
            "Check OPENAI_API_KEY."
        )
    detail = stderr.strip() or metadata.error_message or "see agent logs"
    return 500, f"Codex execution failed (exit code {return_code}): {detail[:300]}"


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class CodexRuntime(AgentRuntime):
    """OpenAI Codex CLI implementation of AgentRuntime."""

    def __init__(self) -> None:
        # Codex thread id for the interactive chat session (continuity). The
        # singleton instance persists across /api/chat calls in a container.
        self._chat_thread_id: Optional[str] = None

    # -- capability declaration (#1187 Phase G) --------------------------------
    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            chat_continuity=True,        # codex exec resume <thread_id>
            session_tab_resume=False,    # MVP: Session tab stays Claude/Gemini
            mcp_support=True,            # codex mcp add
            cost_reporting="estimated",  # no native cost → derived from tokens
        )

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        return "gpt-5.1-codex"

    def get_context_window(self, model: Optional[str] = None) -> int:
        """Fallback context window for Codex/GPT-5 models (#1521).

        Resolves via the shared catalog; falls back to the default Codex model id
        so a None model still resolves to Codex's 272K window.
        """
        return resolve_context_window(model or self.get_default_model())

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """Delegate to the shared Codex MCP configuration in trinity_mcp.py."""
        from .trinity_mcp import _configure_codex_mcp_servers

        return _configure_codex_mcp_servers(mcp_servers)

    # -- command construction --------------------------------------------------
    def _build_codex_command(
        self,
        *,
        model: Optional[str],
        sandbox_mode: str,
        result_file: str,
        agent_home: str,
        resume_thread_id: Optional[str],
    ) -> List[str]:
        cmd = ["codex", "exec"]
        # Exec-level flags belong to `codex exec`, NOT to the `resume`
        # sub-subcommand. In codex 0.139.0, `exec resume [OPTIONS] [SESSION_ID]
        # [PROMPT]` has a NARROWER option set and rejects -C/--sandbox/--json/-o
        # ("error: unexpected argument '-C' found", exit 2 — breaks every
        # turn-2+ continuity call). So they MUST be emitted BEFORE `resume`.
        cmd += [
            "--json",
            "--skip-git-repo-check",
            "-C",
            agent_home,
            "--sandbox",
            sandbox_mode,
            "-o",
            result_file,
        ]
        # Normal mode is `danger-full-access` (no inner sandbox; the Trinity
        # container is the boundary — see _resolve_sandbox_mode), which already
        # permits network access, so no `sandbox_workspace_write.network_access`
        # override is needed. Read-only stays `read-only`. We no longer emit
        # `workspace-write` at all.
        if model:
            cmd += ["-m", model]
        # Continuity: `codex exec <flags> resume <thread_id>` replays a prior
        # thread. Emitted AFTER the exec-level flags above (narrower arg set).
        if resume_thread_id:
            cmd += ["resume", resume_thread_id]
        # End-of-options separator (review I3): the caller appends the prompt as
        # the next (positional) token — for a resume it is resume's PROMPT arg —
        # so a prompt starting with "-"/"--" can never be reparsed as a flag
        # (worst case weakening the sandbox).
        cmd.append("--")
        return cmd

    # -- core subprocess execution (stubbed in unit tests) ---------------------
    async def _execute_codex(
        self,
        *,
        prompt: str,
        model: Optional[str],
        system_prompt: Optional[str],
        resume_thread_id: Optional[str],
        timeout_seconds: int,
        allowed_tools: Optional[List[str]],
        execution_id: Optional[str],
        concurrent_reader: bool = False,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], Optional[str]]:
        execution_id = execution_id or str(uuid.uuid4())

        api_key = _load_openai_api_key()
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "OpenAI API key not configured in agent container. Inject "
                    "OPENAI_API_KEY via credentials."
                ),
            )

        codex_home = _ensure_codex_home()
        result_file = os.path.join(codex_home, f"{_safe_result_token(execution_id)}-last.txt")
        sandbox_mode = _resolve_sandbox_mode()
        _surface_unmapped_guardrails(allowed_tools)
        composed_prompt = _compose_prompt(system_prompt, prompt)

        cmd = self._build_codex_command(
            model=model,
            sandbox_mode=sandbox_mode,
            result_file=result_file,
            agent_home=_AGENT_HOME,
            resume_thread_id=resume_thread_id,
        )
        cmd.append(composed_prompt)

        env = {
            **os.environ,
            EXECUTION_TAG_NAME: execution_id,
            "CODEX_HOME": codex_home,
            # Inject under both names — the ecosystem standard is OPENAI_API_KEY;
            # some Codex builds also read CODEX_API_KEY. Defensive (verified in
            # /verify-local).
            "OPENAI_API_KEY": api_key,
            "CODEX_API_KEY": api_key,
        }

        metadata = ExecutionMetadata()
        metadata.context_window = self.get_context_window(model)
        metadata.execution_id = execution_id
        execution_log: List[ExecutionLogEntry] = []
        raw_messages: List[Dict] = []
        response_parts: List[str] = []
        state = _CodexParseState(
            execution_log=execution_log,
            metadata=metadata,
            response_parts=response_parts,
            model=model,
        )
        stderr_lines: List[str] = []

        registry = get_process_registry()
        logger.info(
            "[Codex] exec sandbox=%s resume=%s model=%s execution_id=%s",
            sandbox_mode, bool(resume_thread_id), model or "(default)", execution_id,
        )

        # stdin=DEVNULL: the prompt is a positional arg, so Codex must not block
        # waiting on stdin. start_new_session=True isolates the process group so
        # cleanup signals only Codex's descendants, never sibling executions.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        process_pgid = _capture_pgid(process)
        registry.register(
            execution_id, process, metadata={"type": "codex", "pgid": process_pgid}
        )

        import threading

        def read_stdout() -> None:
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    try:
                        sanitized = sanitize_subprocess_line(line)
                        try:
                            event = json.loads(sanitized.strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            event = sanitize_dict(event)
                            raw_messages.append(event)
                            try:
                                registry.publish_log_entry(execution_id, event)
                            except Exception as pub_err:  # noqa: BLE001
                                logger.debug(
                                    "[Codex] publish_log_entry failed (continuing): %s",
                                    pub_err,
                                )
                            _process_codex_event(event, state)
                    except Exception as line_err:  # noqa: BLE001
                        logger.debug(
                            "[Codex] per-line processing error (continuing): %s",
                            line_err,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("[Codex] error reading stdout: %s", exc)

        def read_stderr() -> None:
            try:
                for line in iter(process.stderr.readline, ""):
                    if not line:
                        break
                    stderr_lines.append(line)
            except Exception as exc:  # noqa: BLE001
                logger.error("[Codex] error reading stderr: %s", exc)

        def read_subprocess_output() -> Tuple[str, int]:
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(
                    "[Codex] execution %s timed out after %ss — killing group",
                    execution_id, timeout_seconds,
                )
                _terminate_process_group(
                    process, graceful_timeout=5, pgid=process_pgid,
                    execution_tag=execution_id,
                )
                _drain_bounded(
                    process, stdout_thread, stderr_thread, grace=3,
                    pgid=process_pgid, execution_tag=execution_id,
                )
                raise
            _drain_bounded(
                process, stdout_thread, stderr_thread, grace=5,
                pgid=process_pgid, execution_tag=execution_id,
            )
            stderr = "".join(stderr_lines)
            return (sanitize_text(stderr) if stderr else stderr), return_code

        # The lock-serialized chat path uses the bounded single-worker executor;
        # the concurrent /api/task path uses the loop's default executor so
        # parallel task readers don't serialize behind one worker (review I2,
        # parity with Claude's headless path). None → default executor.
        reader_executor = None if concurrent_reader else _executor

        loop = asyncio.get_event_loop()
        try:
            try:
                stderr_output, return_code = await asyncio.wait_for(
                    loop.run_in_executor(reader_executor, read_subprocess_output),
                    timeout=timeout_seconds + 60,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[Codex] outer timeout on %s — killing group as last resort",
                    execution_id,
                )
                await loop.run_in_executor(
                    None,
                    lambda: _terminate_process_group(
                        process, graceful_timeout=2, pgid=process_pgid,
                        execution_tag=execution_id,
                    ),
                )
                await loop.run_in_executor(None, _safe_close_pipes, process)
                raise HTTPException(
                    status_code=504,
                    detail=f"Codex execution timed out after {timeout_seconds} seconds",
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(
                    status_code=504,
                    detail=f"Codex execution timed out after {timeout_seconds} seconds",
                )

            if return_code != 0:
                status_code, detail = _classify_codex_failure(
                    return_code, stderr_output, metadata
                )
                # NOTE: no metadata.status write here — this path raises
                # HTTPException and the local metadata is discarded, so the
                # backend reads the failure from the HTTP status, not metadata.
                logger.error("[Codex] %s", detail)
                raise HTTPException(status_code=status_code, detail=detail)

            # -o file is authoritative; JSONL parts are the fallback.
            result_text = _read_and_consume_result_file(result_file, codex_home)
            response_text = _finalize_codex_response(result_text, response_parts)
            response_text = sanitize_text(response_text)

            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count
            if not response_text:
                response_text = (
                    "(Task completed)" if tool_use_count else "(No response from Codex)"
                )
            metadata.status = "success"
            session_id = _resolve_returned_session_id(metadata)
            logger.info(
                "[Codex] done execution_id=%s cost=$%s tokens=%s/%s tools=%s",
                execution_id, metadata.cost_usd, metadata.input_tokens,
                metadata.output_tokens, metadata.tool_count,
            )
            return response_text, execution_log, metadata, raw_messages, session_id
        finally:
            # Read-then-delete in finally — happy + error path (#1187 decision 5).
            _safe_unlink(result_file, codex_home)
            registry.unregister(execution_id)

    # -- public interface ------------------------------------------------------
    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Codex CLI is not available in this container",
            )

        resume_thread_id: Optional[str] = None
        if continue_session and agent_state.session_started and self._chat_thread_id:
            resume_thread_id = self._chat_thread_id
        else:
            agent_state.session_started = True
            self._chat_thread_id = None

        guardrails = _load_guardrails()
        timeout_seconds = int(
            guardrails.get("execution_timeout_sec") or _DEFAULT_EXECUTION_TIMEOUT_SEC
        )

        try:
            response, log, metadata, raw, session_id = await self._execute_codex(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                resume_thread_id=resume_thread_id,
                timeout_seconds=timeout_seconds,
                allowed_tools=None,
                execution_id=execution_id,
                concurrent_reader=False,  # chat is lock-serialized → bounded reader
            )
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            logger.info("[Codex] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before the chat could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[Codex] execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Execution error: {exc}")

        # Track thread id for the next continue_session turn.
        if session_id:
            self._chat_thread_id = session_id
            agent_state.session_started = True

        # Update session rollups (mirrors the Gemini path).
        if metadata.cost_usd:
            agent_state.session_total_cost += metadata.cost_usd
        agent_state.session_total_output_tokens += metadata.output_tokens
        if metadata.input_tokens > agent_state.session_context_tokens:
            agent_state.session_context_tokens = metadata.input_tokens
        agent_state.session_context_window = metadata.context_window
        return response, log, metadata, raw

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, Optional[str]]:
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Codex CLI is not available in this container",
            )
        if images:
            logger.warning("[Codex] images are not supported in the MVP — ignoring")
        if max_turns is not None:
            logger.info(
                "[Codex] max_turns=%s requested; Codex exec has no turn cap CLI "
                "flag — relying on the %ss wall-clock timeout.",
                max_turns, timeout_seconds,
            )

        try:
            response, log, metadata, raw, session_id = await self._execute_codex(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                resume_thread_id=resume_session_id,
                timeout_seconds=timeout_seconds,
                allowed_tools=allowed_tools,
                execution_id=execution_id,
                concurrent_reader=True,  # /api/task runs concurrently → default reader
            )
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            # 502 (not 503) so the SUB-003 auth-switch isn't tripped by an early
            # child exit — parity with the Claude/Gemini headless paths (#474).
            logger.info("[Codex] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before task could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[Codex] task execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Task execution error: {exc}")

        return response, log, metadata, session_id


# Global Codex runtime instance (singleton, mirrors claude/gemini).
_codex_runtime: Optional[CodexRuntime] = None


def get_codex_runtime() -> CodexRuntime:
    global _codex_runtime
    if _codex_runtime is None:
        _codex_runtime = CodexRuntime()
    return _codex_runtime
