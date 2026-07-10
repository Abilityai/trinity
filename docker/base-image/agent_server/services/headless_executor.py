"""Headless task execution path for Claude Code.

Extracted from `claude_code.py` per #122 (issue split). Owns the
``execute_headless_task`` orchestrator plus three focused helpers
(setup, subprocess run, finalise) and the ``HeadlessRunContext`` dataclass
that carries state between them.

The orchestrator retains terminate authority on the outer ``asyncio.wait_for``
timeout — helpers mutate ``ctx`` but never own the process handle for cleanup
purposes. This preserves the L1877–1893 behaviour from the original
monolithic implementation: when the outer timeout fires (the safety net
above the inner ``process.wait`` budget), the orchestrator is the one that
calls ``ctx.terminate()`` and raises HTTP 504.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import resolve_context_window
from ..state import agent_state
from ..utils.credential_sanitizer import sanitize_dict, sanitize_subprocess_line, sanitize_text
from ._runtime_config import _DEFAULT_MAX_TURNS_TASK, _load_guardrails
from .error_classifier import (
    _classify_empty_result,
    _classify_signal_exit,
    _diagnose_exit_failure,
    _format_rate_limit_error,
    _is_auth_failure_message,
    _is_rate_limit_message,
)
from .jsonl_recovery import (
    _extract_compact_events_from_jsonl,
    _recover_metadata_from_jsonl,
    _recover_response_from_jsonl,
)
from .process_registry import get_process_registry
from .stream_parser import process_stream_line
from .subprocess_lifecycle import (
    _capture_pgid,
    _drain_bounded,
    _safe_close_pipes,
    _terminate_process_group,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME

logger = logging.getLogger(__name__)

# Issue #678 (JSONL persistence Option B): headless tasks above this
# timeout threshold automatically get JSONL persistence enabled so the
# stdout-race recovery code can fire. Short tasks remain disk-cheap.
# 600s = 10 min; tunable if disk pressure shows up. Threshold matches
# the cost/pain inflection: short fan-out is cheap to re-run, long
# deliverables aren't.
_JSONL_PERSIST_THRESHOLD_S = 600

# #970: cadence for the bounded subprocess wait, and the no-progress ceiling
# for a single tool_use that never receives a tool_result. Claude Code has
# NO per-MCP-tool timeout, so a hung stdio tools/call would otherwise wedge
# `process.wait` for the full execution budget (the ticket's 2h false-
# timeout). The stall limit is intentionally generous — "tool running" is
# not "tool stalled" — so only a genuinely wedged call trips it.
_WAIT_POLL_S = 2.0

# #1369: default per-tool stall ceiling (was a flat hard-coded 300s, which
# killed legitimately long internal MCP calls). Operator-configurable via
# AGENT_TOOL_STALL_LIMIT_S; <=0 disables the watchdog (the execution_timeout
# budget stays the backstop). The watchdog is also scoped to opaque MCP
# (``mcp__*``) tools only — built-ins (Bash/Read/Task/sub-agents) return on
# their own and are already bounded by execution_timeout_seconds, so a long
# (progressing) built-in must never be killed here. A genuinely hung built-in
# or a hang *inside* a Task sub-agent (which surfaces as an open ``Task`` in
# the parent log) now falls to the execution_timeout backstop rather than this
# 300s kill — the accepted interim tradeoff (see #1369).
_STALL_LIMIT_DEFAULT_S = 1800.0


def _resolve_stall_limit_s() -> float:
    """Resolved per-run stall ceiling (seconds).

    Read fresh per run (not a module constant) so it honors the container env
    and is monkeypatchable in tests. ``AGENT_TOOL_STALL_LIMIT_S`` overrides the
    default; a missing/blank value uses the default; a non-numeric value falls
    back to the default with a warning (a typo must not brick the executor).
    A value ``<= 0`` means *disabled* and is handled at the call site — it is
    NOT passed into ``_open_tool_exceeding`` (where ``> 0`` would fire instantly).
    Non-finite values (``inf``/``nan``) parse as floats but would silently make
    the watchdog inert/disabled, so they fall back to the default too — only a
    deliberate ``<= 0`` disables.
    """
    raw = os.environ.get("AGENT_TOOL_STALL_LIMIT_S")
    if raw is None or not raw.strip():
        return _STALL_LIMIT_DEFAULT_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = math.nan
    if not math.isfinite(val):
        logger.warning(
            "[Headless Task] Invalid AGENT_TOOL_STALL_LIMIT_S=%r — using default %.0fs",
            raw, _STALL_LIMIT_DEFAULT_S,
        )
        return _STALL_LIMIT_DEFAULT_S
    return val


def _open_tool_exceeding(ctx: HeadlessRunContext, limit_s: float) -> Optional[str]:
    """Name of an ``mcp__*`` tool_use open (no tool_result) for > ``limit_s``, else None.

    #970 stall-watchdog signal. Reuses the stream parser's existing
    bookkeeping — ``tool_start_times`` (set on every tool_use) and the
    ``execution_log`` tool_result entries that close them. A hung stdio
    MCP ``tools/call`` never emits a tool_result, so its tool_use stays
    open: the only externally-visible signal that claude is wedged.

    #1369: only ``mcp__*`` tools are considered. Built-ins return on their
    own and are bounded by execution_timeout, so they are exempt here. The
    caller must only invoke this with ``limit_s > 0`` (a ``<= 0`` limit would
    match every open tool).
    """
    log = list(ctx.execution_log)  # snapshot — reader thread mutates concurrently
    completed = {e.id for e in log if e.type == "tool_result"}
    now = datetime.now()
    for e in log:
        if e.type == "tool_use" and e.id not in completed:
            # Scope to opaque MCP stdio calls; None-safe (a tool_use with no
            # name must not crash the poll loop).
            if not (e.tool or "").startswith("mcp__"):
                continue
            started = ctx.tool_start_times.get(e.id)
            if started and (now - started).total_seconds() > limit_s:
                return e.tool
    return None


def _attempt_empty_result_recovery(
    metadata: ExecutionMetadata,
    raw_messages: List[Dict],
    response_parts: List[str],
    parse_failure_count: int,
    parse_failure_sample: Optional[str],
    task_start_iso: Optional[str],
    session_id_fallback: Optional[str] = None,
) -> Optional[Tuple[int, Dict]]:
    """Shared empty-result recovery used by both async and sync execution paths.

    Issue #678: when ``return_code == 0`` but the trailing ``result``
    line was lost (stdout reader race), this helper:

    1. Classifies the empty result and gives up early if metadata
       already looks complete (caller proceeds to success path).
    2. Tries metadata recovery from the on-disk JSONL — back-fills
       ``cost_usd``, ``duration_ms``, ``num_turns``, ``model_name``,
       per-call token usage on the metadata in place.
    3. Tries text recovery from the JSONL when ``response_parts`` is
       empty. Sets ``recovered_from_jsonl=True`` on success.

    ``session_id_fallback`` is the UUID we passed to ``claude --session-id``
    (captured in ``HeadlessRunContext.claude_session_uuid``). When the
    reader race fires before any stdout arrives, ``metadata.session_id``
    stays unset; recovery would silently no-op without a way to locate
    the JSONL on disk. The fallback closes that gap.

    Returns:
      - ``None`` when metadata was already complete OR when text was
        recovered (caller continues to success path with populated
        metadata).
      - ``(502, dict_body)`` when the result is genuinely lost and no
        text could be recovered. Caller raises HTTPException with this.
    """
    empty_result = _classify_empty_result(
        metadata,
        raw_message_count=len(raw_messages),
        raw_messages=raw_messages,
        parse_failure_count=parse_failure_count,
        parse_failure_sample=parse_failure_sample,
    )
    if empty_result is None:
        return None

    # Resolve the JSONL filename UUID. metadata.session_id wins (it's the
    # one Claude actually echoed back); fall back to the UUID we passed
    # on the command line when the race wedged the reader before init.
    effective_session_id = metadata.session_id or session_id_fallback or None

    # Step 1: try to back-fill metadata from the JSONL. Mutates in place.
    _recover_metadata_from_jsonl(
        effective_session_id,
        since_iso=task_start_iso,
        metadata=metadata,
    )

    # Step 2: text recovery branches.
    if response_parts:
        logger.warning(
            f"[Recovery] Result event lost (stdout pipe race) but "
            f"response_parts has {sum(len(p) for p in response_parts)} chars "
            f"of assistant content across {len(response_parts)} blocks — "
            f"recovering as soft success. raw_messages={len(raw_messages)} "
            f"cost_recovered={metadata.cost_usd}"
        )
        return None

    recovered_text = _recover_response_from_jsonl(effective_session_id)
    if recovered_text:
        logger.warning(
            f"[Recovery] Stdout race lost the response "
            f"(raw_messages={len(raw_messages)}, no text in stream), but "
            f"recovered {len(recovered_text)} chars from JSONL "
            f"(session_id={effective_session_id}) — surfacing as soft success. "
            f"cost_recovered={metadata.cost_usd}"
        )
        response_parts.append(recovered_text)
        metadata.recovered_from_jsonl = True
        return None

    # Hard failure: return the structured 502 body. The classifier already
    # built it with sanitized partial metadata.
    status_code, body = empty_result
    # Refresh the metadata snapshot in the body — we may have populated
    # cost/duration/model_name above via JSONL metadata recovery.
    # sanitize_dict is idempotent over the recovered fields.
    body["metadata"] = sanitize_dict(metadata.model_dump())
    return (status_code, body)


def _timeout_504_detail(
    ctx: "HeadlessRunContext",
    message: str,
    termination_reason: str,
    stalled_tool: Optional[str] = None,
) -> Dict:
    """Structured 504 body for an agent-side timeout kill (#1094, #1201).

    Carries the telemetry accumulated before the kill (cost / context / tool
    calls) under ``metadata`` — the same sanitized
    ``ExecutionMetadata.model_dump()`` shape the #678 empty-result body uses —
    so the backend's HTTPError salvage (`task_execution_service.py` HTTPError
    branch) persists cost/context onto the timed-out FAILED row instead of
    writing it bare. ``termination_reason`` ("max_duration" | "stall_no_output")
    and the timeout-failure semantics are unchanged.
    """
    detail: Dict = {
        "message": message,
        "termination_reason": termination_reason,
        "metadata": sanitize_dict(ctx.metadata.model_dump()),
    }
    if stalled_tool is not None:
        detail["stalled_tool"] = stalled_tool
    return detail


@dataclass
class HeadlessRunContext:
    """State carrier for the headless execution lifecycle.

    Owned by ``execute_headless_task``; populated by ``_setup_headless_command``
    and progressively filled in by ``_run_headless_subprocess`` and
    ``_finalize_headless_result``. The orchestrator retains terminate
    authority — ``terminate()`` is the outer-timeout escape hatch the
    orchestrator calls when the outer ``asyncio.wait_for`` fires before the
    inner ``process.wait`` budget bounded the subprocess.
    """

    # Setup outputs
    cmd: List[str]
    task_session_id: str
    task_start_iso: str
    effective_timeout: int
    images: Optional[List[Dict]]
    prompt: str
    # #678: UUID we passed to `claude --session-id`. Used as the JSONL
    # filename fallback when the reader race fires before any stdout
    # arrives, leaving `metadata.session_id` unset. Empty string when
    # the run resumed an existing session (we didn't generate a UUID).
    claude_session_uuid: str = ""

    # Run state (populated by _run_headless_subprocess)
    process: Optional[subprocess.Popen] = None
    process_pgid: Optional[int] = None
    return_code: Optional[int] = None
    verbose_output_lines: List[str] = field(default_factory=list)
    parse_failure_count: int = 0
    parse_failure_sample: Optional[str] = None
    auth_abort_event: threading.Event = field(default_factory=threading.Event)
    auth_abort_reason: List[str] = field(default_factory=list)
    permission_mode_validated: bool = False
    result_seen: threading.Event = field(default_factory=threading.Event)  # #970: claude emitted {"type":"result"}
    stdout_exc: List[BaseException] = field(default_factory=list)
    # #1094: which termination path fired, so the terminal 504 carries a
    # distinct reason instead of always claiming the max-duration timeout.
    #   "stall_no_output" — an mcp__ tool produced no result for >stall_limit_s
    #   "max_duration"    — the effective_timeout budget was genuinely exhausted
    termination_reason: Optional[str] = None
    stalled_tool: Optional[str] = None
    # #1369: the per-run resolved stall ceiling actually applied, so the
    # terminal-504 wording reports the configured value (not a stale constant).
    stall_limit_s: float = _STALL_LIMIT_DEFAULT_S
    # #1025 (salvaged from #980): True when the drain returned anything other
    # than "completed" (budget_exceeded / errored / leaked) — a reader thread
    # may still be alive and mutating the shared buffers below, so
    # _finalize_headless_result snapshots its read fields before reading them.
    reader_may_be_live: bool = False

    # Shared mutable buffers (populated by stream_parser via process_stream_line)
    response_parts: List[str] = field(default_factory=list)
    execution_log: List[ExecutionLogEntry] = field(default_factory=list)
    metadata: ExecutionMetadata = field(default_factory=ExecutionMetadata)
    raw_messages: List[Dict] = field(default_factory=list)
    tool_start_times: Dict[str, datetime] = field(default_factory=dict)

    def terminate(self) -> None:
        """Outer-timeout escape hatch — preserves the original L1877–1893
        cleanup contract. The orchestrator calls this when the outer
        ``asyncio.wait_for`` fires; the inner ``process.wait`` budget should
        already have bounded the subprocess in the common case.

        Concurrency contract (preserved from pre-#122 monolithic code):
        ``run_in_executor(None, ...)`` does NOT cancel the underlying
        synchronous function on ``asyncio.wait_for`` timeout — the original
        executor thread for ``_run_headless_subprocess`` may still be running
        (inside ``process.wait``, draining, or mid-readline) when this method
        is invoked from a fresh executor thread. Two threads will then
        concurrently mutate the same ``Popen`` object's pipe FDs. The
        ``_drain_bounded`` daemon-thread budget (Issue #728) bounds the
        resulting deadlock window; eliminating it would require replacing
        ``run_in_executor(None, ...)`` with a cancellation-aware primitive
        (e.g. anyio task group), which is out of scope for #122 and would
        affect every subprocess call site in the agent server.

        The ``self.process is not None`` guard makes this method safe even
        when outer-timeout fires before ``Popen`` has returned — a slight
        improvement over the original closure-captured ``process`` variable.
        """
        if self.process is not None:
            _terminate_process_group(
                self.process, graceful_timeout=2,
                pgid=self.process_pgid, execution_tag=self.task_session_id,
            )
            _safe_close_pipes(self.process)


def _setup_headless_command(
    prompt: str,
    model: Optional[str],
    allowed_tools: Optional[List[str]],
    system_prompt: Optional[str],
    timeout_seconds: int,
    max_turns: Optional[int],
    execution_id: Optional[str],
    resume_session_id: Optional[str],
    persist_session: bool,
    images: Optional[List[Dict]],
) -> HeadlessRunContext:
    """Build the claude CLI command and initialise the run context.

    Replicates the original L1527–1631 setup: model defaults, command
    assembly (--resume / --no-session-persistence / --session-id /
    --mcp-config / --model / --allowedTools / --disallowedTools /
    --input-format / --append-system-prompt / --max-turns), and unique
    task-session-id generation. Pure — no subprocess spawn, no DI.
    """
    # Safety-net fallback: backend always resolves model before calling the agent
    # (#831), so this branch should only fire for direct agent-server calls.
    if model is None:
        model = "claude-sonnet-4-6"
        logger.debug("[Headless Task] No model specified, defaulting to 'claude-sonnet-4-6'")

    # Build command - NO --continue flag (stateless) unless resuming
    cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

    # Add --resume if resuming a previous session (EXEC-023)
    # #678: track the JSONL filename UUID so recovery can fall back to it
    # when metadata.session_id is unset. For --resume, the JSONL is named
    # after resume_session_id; for new sessions, we generate the UUID below.
    claude_session_uuid = ""
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
        claude_session_uuid = resume_session_id
        logger.info(f"[Headless Task] Resuming session: {resume_session_id}")
    else:
        # Session isolation: prevent headless tasks from writing session files
        # that could collide with interactive /api/chat sessions or other tasks.
        # --no-session-persistence avoids shared state in ~/.claude/projects/
        # --session-id ensures unique namespace per task execution.
        #
        # Session tab opt-in (persist_session=True): the caller wants the
        # JSONL written so the next turn can --resume it. We still pass a
        # unique --session-id so cold turns don't collide on disk.
        #
        # Issue #678 (JSONL persistence Option B): also auto-persist for
        # long-running headless tasks (timeout > 10 min) so the JSONL
        # recovery code can fire when the stdout reader thread wedges.
        # Short fan-out / utility tasks stay disk-cheap; long deliverable
        # tasks (the real telemetry-loss pain) get a recovery surface.
        # The retention sweep in session_cleanup_service.py reaps these
        # JSONLs after 24h so disk cost stays bounded.
        effective_persist = persist_session or (timeout_seconds > _JSONL_PERSIST_THRESHOLD_S)
        if persist_session is False and timeout_seconds > _JSONL_PERSIST_THRESHOLD_S:
            logger.info(
                f"event=jsonl_persistence_auto_enabled timeout_seconds={timeout_seconds} "
                f"threshold={_JSONL_PERSIST_THRESHOLD_S}"
            )
        if not effective_persist:
            cmd.append("--no-session-persistence")
        # Claude Code requires --session-id to be a valid UUID.
        # execution_id is a base64url token (not a UUID), so always generate one.
        # #678: capture into a variable so JSONL recovery can fall back to
        # this UUID when the reader race fires before metadata.session_id
        # is populated by Claude's init message.
        claude_session_uuid = str(uuid.uuid4())
        cmd.extend(["--session-id", claude_session_uuid])

    # Add MCP config if .mcp.json exists (for agent-to-agent collaboration via Trinity MCP)
    mcp_config_path = Path.home() / ".mcp.json"
    if mcp_config_path.exists():
        cmd.extend(["--mcp-config", str(mcp_config_path)])

    # Add model selection if specified
    if model:
        cmd.extend(["--model", model])
        logger.info(f"[Headless Task] Using model: {model}")

    # Add allowed tools restriction if specified
    if allowed_tools:
        tools_str = ",".join(allowed_tools)
        cmd.extend(["--allowedTools", tools_str])
        logger.info(f"[Headless Task] Restricting tools to: {tools_str}")

    # GUARD-003: merge disallowed tools from guardrails config.
    guardrails = _load_guardrails()
    disallowed_tools = guardrails.get("disallowed_tools") or []
    if disallowed_tools:
        cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])
        logger.info(f"[Headless Task] Guardrails disallow tools: {disallowed_tools}")

    # #562: when images are present, use stream-json stdin format so images
    # are delivered as proper vision content blocks, not base64 text strings.
    if images:
        cmd.extend(["--input-format", "stream-json"])
        logger.info(f"[Headless Task] {len(images)} image(s) — switching to stream-json input")

    # Add system prompt if specified
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
        logger.info(f"[Headless Task] Appending system prompt ({len(system_prompt)} chars)")

    # GUARD-003: max-turns limit for runaway prevention.
    # Caller value wins; otherwise fall back to guardrails config, then hardcoded default.
    effective_max_turns = max_turns
    if effective_max_turns is None:
        effective_max_turns = int(guardrails.get("max_turns_task") or _DEFAULT_MAX_TURNS_TASK)
    cmd.extend(["--max-turns", str(effective_max_turns)])
    logger.info(f"[Headless Task] Limiting to {effective_max_turns} agentic turns")

    # Use provided execution_id if available (enables termination tracking from backend)
    task_session_id = execution_id or str(uuid.uuid4())

    # Anchor for scoping post-turn JSONL extracts (compact events) to
    # this turn only — the JSONL accumulates across the resumed
    # session's lifetime, so we filter records by timestamp >= start.
    task_start_iso = datetime.utcnow().isoformat() + "Z"

    logger.info(f"[Headless Task] Starting task {task_session_id}: {' '.join(cmd[:5])}...")

    return HeadlessRunContext(
        cmd=cmd,
        task_session_id=task_session_id,
        task_start_iso=task_start_iso,
        effective_timeout=timeout_seconds,
        images=images,
        prompt=prompt,
        claude_session_uuid=claude_session_uuid,
        # #1521: seed the fallback context window from the model catalog. The
        # stream parser overwrites it from the runtime's modelUsage.contextWindow
        # when present; this catalog value is the fallback for the salvage /
        # empty-result paths (headless never otherwise sets context_window, so it
        # would default to a flat 200K).
        metadata=ExecutionMetadata(context_window=resolve_context_window(model)),
    )


def _run_headless_subprocess(ctx: HeadlessRunContext) -> None:
    """Spawn claude, run the readers, wait for exit, drain.

    Runs in a thread pool via ``run_in_executor``. Mutates ``ctx`` —
    populates ``process``, ``process_pgid``, ``return_code``,
    ``verbose_output_lines``, ``parse_failure_*``, ``auth_abort_*``,
    ``stdout_exc``, ``permission_mode_validated``, and the shared parsing
    buffers (``response_parts``, ``execution_log``, ``metadata``,
    ``raw_messages``).

    Replicates the original L1633–1865 sequence: Popen with
    ``start_new_session=True``, capture pgid, register with the process
    registry, define the stderr/stdout reader closures (auth-abort
    detection, permission-mode validation, per-line stdout parse), write
    stdin (vision-aware stream-json or plain text), bounded
    ``process.wait``, drain readers, re-raise any captured stdout
    exception. Raises ``subprocess.TimeoutExpired`` on inner timeout —
    the caller converts to HTTP 504.
    """
    # Issue #407: start_new_session=True puts claude (and any hooks it
    # spawns) into their own process group so we can reap the whole tree
    # on exit/timeout. Without this, hook grandchildren can outlive
    # claude, keep pipe FDs open, and wedge readline() forever.
    # Issue #817: TRINITY_EXECUTION_ID env var is inherited by every
    # descendant across fork/exec/setsid/double-fork. Cleanup uses it
    # to identify and kill orphans that escape both the pgid sweep
    # and the FD-based pipe-writer sweep.
    process = subprocess.Popen(
        ctx.cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
        start_new_session=True,
        env={**os.environ, EXECUTION_TAG_NAME: ctx.task_session_id},
    )
    ctx.process = process
    # Issue #407: capture pgid now — after wait() reaps the parent,
    # the pid is gone and we lose the ability to signal the group.
    ctx.process_pgid = _capture_pgid(process)

    # Register process for potential termination
    registry = get_process_registry()
    registry.register(ctx.task_session_id, process, metadata={
        "type": "task",
        "message_preview": ctx.prompt[:100],
        "pgid": ctx.process_pgid,
    })

    def read_stderr() -> None:
        """Read stderr line by line; scan for auth failures."""
        try:
            for line in iter(process.stderr.readline, ''):
                if not line:
                    break
                stripped = line.rstrip('\n')
                ctx.verbose_output_lines.append(stripped)

                # Issue #285: detect auth failures in real time
                if _is_auth_failure_message(stripped):
                    logger.warning(
                        f"[Headless Task] Auth failure detected in stderr: {stripped[:200]}"
                    )
                    ctx.auth_abort_reason.append(stripped)
                    ctx.auth_abort_event.set()
                    # Kill the whole process group so stdout's readline()
                    # gets EOF and we unwind cleanly (Issue #407).
                    try:
                        _terminate_process_group(process, graceful_timeout=2, pgid=ctx.process_pgid, execution_tag=ctx.task_session_id)
                    except Exception as kill_err:
                        logger.error(
                            f"[Headless Task] Failed to kill process on auth abort: {kill_err}"
                        )
                    break
        except Exception as e:
            logger.error(f"[Headless Task] Error reading stderr: {e}")

    def read_stdout() -> None:
        """Read stdout (stream-json); parse and publish log entries."""
        try:
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                # Issue #285: stderr thread detected auth failure
                if ctx.auth_abort_event.is_set():
                    logger.info("[Headless Task] Stdout loop exiting due to auth abort")
                    break

                # Issue #630: each line is processed inside a per-line
                # try/except so a single failure (publish_log_entry
                # raising, process_stream_line tripping on weird input,
                # any non-RuntimeError exception) does not kill the
                # reader. If the reader exits early the result line
                # later in the stream is lost and the execution is
                # misclassified as "completed without a result message".
                # RuntimeError (permission-mode failure) is intentional
                # — keep the existing fast-fail behaviour.
                try:
                    try:
                        raw_msg = json.loads(line.strip())
                    except json.JSONDecodeError:
                        # Issue #640: track parse failures (see chat-path comment).
                        ctx.parse_failure_count += 1
                        if ctx.parse_failure_sample is None:
                            sample = sanitize_subprocess_line(line).rstrip("\n")
                            if len(sample) > 300:
                                sample = sample[:299] + "…"
                            ctx.parse_failure_sample = sample
                        raw_msg = None

                    if isinstance(raw_msg, dict):
                        # SECURITY: Sanitize credentials from output before storing
                        raw_msg = sanitize_dict(raw_msg)
                        ctx.raw_messages.append(raw_msg)
                        # Publish to live streaming subscribers — isolate
                        # so subscriber-side breakage cannot back-pressure
                        # the reader.
                        try:
                            registry.publish_log_entry(ctx.task_session_id, raw_msg)
                        except Exception as pub_err:  # noqa: BLE001
                            logger.warning(
                                f"[Headless Task] publish_log_entry failed (continuing): {pub_err}"
                            )

                        # Validate permissionMode on init message (first message from Claude Code).
                        # If permission bypass isn't active, kill immediately instead of timing out
                        # after hours with zero work completed (all tool calls silently denied).
                        # Claude Code emits {"type": "system", "subtype": "init", ...} — see Appendix B
                        # of docs/planning/SESSION_TAB_2026-04.md.
                        if (
                            raw_msg.get("type") == "system"
                            and raw_msg.get("subtype") == "init"
                            and not ctx.permission_mode_validated
                        ):
                            perm_mode = raw_msg.get("permissionMode", "unknown")
                            if perm_mode == "bypassPermissions":
                                ctx.permission_mode_validated = True
                                logger.info(f"[Headless Task] Permission mode confirmed: {perm_mode}")
                            else:
                                logger.error(
                                    f"[Headless Task] CRITICAL: Permission bypass not active! "
                                    f"permissionMode={perm_mode} (expected bypassPermissions). "
                                    f"Killing process tree to prevent silent timeout. "
                                    f"Task: {ctx.task_session_id}"
                                )
                                _terminate_process_group(process, graceful_timeout=2, pgid=ctx.process_pgid, execution_tag=ctx.task_session_id)
                                raise RuntimeError(
                                    f"Permission bypass failed: permissionMode={perm_mode}. "
                                    f"This may be caused by a stale Claude Code session process "
                                    f"or project settings overriding the CLI flag. "
                                    f"Try restarting the agent container."
                                )

                    # SECURITY: Sanitize the line before processing
                    sanitized_line = sanitize_subprocess_line(line)
                    # Process each line for metadata/tool tracking
                    process_stream_line(
                        sanitized_line,
                        ctx.execution_log,
                        ctx.metadata,
                        ctx.tool_start_times,
                        ctx.response_parts,
                    )

                    # #970 early-completion: the result line is the definitive
                    # end of a `claude --print` turn. Signal it AFTER parsing so
                    # metadata/response_parts are populated; the wait loop can
                    # then finalize even if the process lingers in teardown.
                    if isinstance(raw_msg, dict) and raw_msg.get("type") == "result":
                        ctx.result_seen.set()
                except RuntimeError:
                    raise  # Re-raise permission-mode failures
                except Exception as line_err:  # noqa: BLE001
                    logger.warning(
                        f"[Headless Task] Per-line stdout processing error (continuing): {line_err}"
                    )
        except RuntimeError:
            raise  # Re-raise permission mode failures
        except Exception as e:
            logger.error(f"[Headless Task] Error reading stdout: {e}")

    def _run_stdout() -> None:
        try:
            read_stdout()
        except BaseException as e:  # noqa: BLE001 — captured for main thread re-raise
            ctx.stdout_exc.append(e)
            # Wake the main thread's process.wait() by killing the group
            try:
                _terminate_process_group(process, graceful_timeout=2, pgid=ctx.process_pgid, execution_tag=ctx.task_session_id)
            except Exception:
                pass

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread = threading.Thread(target=_run_stdout, daemon=True)
    stderr_thread.start()
    stdout_thread.start()

    # Build and write stdin payload. For vision tasks use stream-json
    # format so images arrive as proper content blocks (#562).
    if ctx.images:
        content_blocks: List[Dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            }
            for img in ctx.images
        ]
        content_blocks.append({"type": "text", "text": ctx.prompt})
        stdin_payload = (
            json.dumps({"type": "user", "message": {"role": "user", "content": content_blocks}})
            + "\n"
        )
    else:
        stdin_payload = ctx.prompt

    process.stdin.write(stdin_payload)
    process.stdin.close()

    # Bounded, polling wait. Three exits beyond the overall budget (#970):
    #   (a) early-completion — claude emitted its {"type":"result"} (the turn
    #       is definitively over) but the process lingers in teardown (e.g. a
    #       stdio MCP child holding the pipe). Finalize with the captured
    #       result instead of burning the budget. The result IS success, so
    #       set return_code=0 — genuine errors were already classified into
    #       metadata.error_type from the stream and are surfaced in finalize.
    #   (b) stall watchdog — an open mcp__ tool_use with no tool_result for
    #       >stall_limit (AGENT_TOOL_STALL_LIMIT_S). Claude Code has no per-MCP-
    #       tool timeout, so a hung tools/call would otherwise wedge until the
    #       full budget. Scoped to mcp__* and disable-able (#1369).
    #   (c) effective_timeout budget — unchanged backstop.
    deadline = time.monotonic() + ctx.effective_timeout
    # #1369: resolve the stall ceiling once per run. <=0 disables the per-tool
    # watchdog (the deadline backstop still applies). Disable is gated HERE, not
    # by passing 0 down — _open_tool_exceeding's `> limit_s` would otherwise
    # match every open tool and fire instantly.
    stall_limit = _resolve_stall_limit_s()
    ctx.stall_limit_s = stall_limit
    stall_enabled = stall_limit > 0
    stalled_tool: Optional[str] = None
    try:
        while True:
            try:
                ctx.return_code = process.wait(timeout=_WAIT_POLL_S)
                break
            except subprocess.TimeoutExpired:
                if ctx.result_seen.is_set():
                    logger.warning(
                        f"[Headless Task] Task {ctx.task_session_id} produced a result "
                        f"but did not exit — finalizing early, terminating teardown"
                    )
                    _terminate_process_group(process, graceful_timeout=2, pgid=ctx.process_pgid, execution_tag=ctx.task_session_id)
                    ctx.return_code = 0
                    break
                if stall_enabled:
                    stalled_tool = _open_tool_exceeding(ctx, stall_limit)
                if stalled_tool or time.monotonic() >= deadline:
                    raise
    except subprocess.TimeoutExpired:
        # #1094: record which path fired so the terminal 504 (built in the
        # orchestrator) can carry a distinct reason instead of the generic
        # max-duration label.
        if stalled_tool:
            ctx.termination_reason = "stall_no_output"
            ctx.stalled_tool = stalled_tool
        else:
            ctx.termination_reason = "max_duration"
        reason = (
            f"tool '{stalled_tool}' stalled with no result for >{stall_limit:.0f}s"
            if stalled_tool
            else f"timed out after {ctx.effective_timeout}s"
        )
        logger.error(
            f"[Headless Task] Task {ctx.task_session_id} {reason} — killing process group"
        )
        _terminate_process_group(process, graceful_timeout=5, pgid=ctx.process_pgid, execution_tag=ctx.task_session_id)
        # Bound the post-kill drain, then re-raise. The orchestrator converts
        # TimeoutExpired to HTTP 504 WITHOUT calling _finalize_headless_result,
        # so there is no finalize read to protect here — we intentionally do
        # not set reader_may_be_live on this path (#1025).
        _drain_bounded(process, stdout_thread, stderr_thread,
                       grace=3, pgid=ctx.process_pgid,
                       execution_tag=ctx.task_session_id)
        raise

    # Subprocess exited. Drain readers — if a hook grandchild still
    # holds a pipe, the helper will close the pipe FDs so the
    # reader threads can exit.
    drain_outcome = _drain_bounded(process, stdout_thread, stderr_thread,
                                   grace=5, pgid=ctx.process_pgid,
                                   execution_tag=ctx.task_session_id)
    # Anything but "completed" means a reader thread may still be alive and
    # mutating the shared buffers finalize reads (#1025).
    ctx.reader_may_be_live = drain_outcome != "completed"

    # Re-raise permission-mode failure captured by stdout thread
    if ctx.stdout_exc:
        raise ctx.stdout_exc[0]


# #1025: number of times _snapshot_for_finalize retries the metadata deep-copy
# when it loses the race with a still-mutating leaked reader. The mutation
# window is a single attribute set, so the first retry almost always wins.
_SNAPSHOT_RETRY_ATTEMPTS = 3


def _freeze_event(event: threading.Event) -> threading.Event:
    """Capture an Event's set/clear state into a fresh, immutable-by-the-reader
    Event (#1025). The leaked reader holds the ORIGINAL event; the snapshot's
    copy reflects the verdict as of snapshot time and can't be flipped later."""
    frozen = threading.Event()
    if event.is_set():
        frozen.set()
    return frozen


def _snapshot_for_finalize(ctx: HeadlessRunContext) -> HeadlessRunContext:
    """Copy the finalize-read fields of ``ctx`` onto a fresh context (#1025/D19).

    Called whenever the drain left a reader thread that may still be alive
    (``reader_may_be_live``), where that reader can mutate ``ctx`` concurrently.
    Every field finalize reads is captured: the four buffers (``list(...)`` of a
    list never raises on a concurrent append), ``metadata`` (deep-copied), and
    the auth-abort signal (``auth_abort_event`` frozen via :func:`_freeze_event`
    + ``auth_abort_reason`` list-copied) — the stderr reader is exactly the
    thread that can leak, and finalize reads ``auth_abort_event.is_set()`` /
    ``auth_abort_reason[0]``, so leaving them aliased would let a late auth match
    flip a success to a spurious 503.

    ``metadata.model_copy(deep=True)`` iterates the pydantic model's ``__dict__``
    / ``__pydantic_fields_set__`` and CAN raise ``RuntimeError("... changed size
    during iteration")`` if the reader sets a not-yet-set field mid-copy (most
    relevant on the ``errored`` outcome, where the reader isn't necessarily
    wedged in ``readline``). The window is a single attribute set, so a bounded
    retry almost always succeeds on the next attempt; if every attempt loses the
    race we fall back to the live ``ctx`` (the pre-#1025 behaviour — no worse
    than not snapshotting).
    """
    last_exc: Optional[RuntimeError] = None
    for _ in range(_SNAPSHOT_RETRY_ATTEMPTS):
        try:
            return replace(
                ctx,
                raw_messages=list(ctx.raw_messages),
                response_parts=list(ctx.response_parts),
                execution_log=list(ctx.execution_log),
                verbose_output_lines=list(ctx.verbose_output_lines),
                metadata=ctx.metadata.model_copy(deep=True),
                auth_abort_event=_freeze_event(ctx.auth_abort_event),
                auth_abort_reason=list(ctx.auth_abort_reason),
            )
        except RuntimeError as exc:
            # "dictionary/set changed size during iteration" — the leaked
            # reader mutated metadata mid-deep-copy. Retry.
            last_exc = exc
    logger.warning(
        "[Headless Task] Run-context snapshot lost the race with a leaked "
        "reader %s times for task %s (%s) — finalizing against the live "
        "context as a last resort. Issue #1025.",
        _SNAPSHOT_RETRY_ATTEMPTS, ctx.task_session_id, last_exc,
    )
    return ctx


def _finalize_headless_result(
    ctx: HeadlessRunContext,
) -> Tuple[str, List[Dict], ExecutionMetadata, str]:
    """Translate completed run state into the public return tuple.

    Replicates the original L1910–2117 sequence: sanitize verbose stderr,
    classify rate-limit / max-turns / auth-abort / signal-exit / generic
    error / empty-result / JSONL recovery, build response text, augment
    metadata with authoritative compact_events from the JSONL, return
    ``(response_text, raw_messages, metadata, final_session_id)``.

    Raises ``HTTPException`` for any unrecoverable failure path (the
    orchestrator's outer ``except HTTPException: raise`` chain re-surfaces
    them).
    """
    # #1025 (D19): when the drain left a reader thread that may still be alive
    # (budget_exceeded / errored / leaked), it can keep mutating the shared
    # buffers below. Snapshot EVERY field finalize reads onto a fresh context so
    # iteration can't tear and a late append by the leaked reader can't be
    # half-read. ``parse_failure_count`` / ``parse_failure_sample`` are an int +
    # str (atomic reads, no snapshot). The leaked reader keeps mutating the
    # ORIGINAL ctx (which it captured by closure); rebinding the local ``ctx``
    # to the snapshot means the rest of finalize — including
    # _attempt_empty_result_recovery's in-place mutations — operates entirely
    # on the isolated copy. Clean-drain runs skip this and keep the zero-copy
    # fast path. The snapshot itself is retry-guarded against the same race
    # (see _snapshot_for_finalize).
    if ctx.reader_may_be_live:
        logger.warning(
            "[Headless Task] Drain left a possibly-live reader for task %s — "
            "finalizing against a snapshot of the run context (the reader may "
            "still be mutating the live buffers). Issue #1025.",
            ctx.task_session_id,
        )
        ctx = _snapshot_for_finalize(ctx)

    # Build verbose transcript from stderr (the human-readable execution log)
    # SECURITY: Sanitize stderr output
    sanitized_lines = [sanitize_text(line) for line in ctx.verbose_output_lines]
    verbose_transcript = "\n".join(sanitized_lines)

    # Check for rate limit detected during stream parsing (takes priority)
    if ctx.metadata.error_type == "rate_limit":
        error_detail = _format_rate_limit_error(ctx.metadata)
        logger.error(f"[Headless Task] Rate limit: {error_detail}")
        raise HTTPException(
            status_code=429,
            detail=error_detail
        )

    # Issue #361: Check for max_turns termination (before auth checks to prevent misclassification)
    if ctx.metadata.error_type == "max_turns":
        error_msg = ctx.metadata.error_message or f"Task stopped after {ctx.metadata.num_turns} turns"
        logger.warning(f"[Headless Task] Max turns reached: {error_msg}")
        raise HTTPException(
            status_code=422,
            detail=f"Task exceeded turn limit: {error_msg}. Consider increasing max_turns_task in guardrails or breaking into smaller subtasks."
        )

    # Stream parser sets ``metadata.error_type = "authentication_failed"`` from
    # the assistant message's ``error`` field (e.g. claude emits "Not logged in
    # · Please run /login"). This is the authoritative auth signal — surface
    # it before the heuristic fallback below so the 503 detail carries the
    # actual error message rather than the generic "no output" wording.
    if ctx.metadata.error_type == "authentication_failed":
        auth_msg = sanitize_text(ctx.metadata.error_message or "Authentication failed")
        logger.error(f"[Headless Task] Auth failure (stream-parser signal): {auth_msg[:200]}")
        raise HTTPException(
            status_code=503,
            detail=f"Authentication failure: {auth_msg[:300]}. Check subscription token or API key configuration."
        )

    # Issue #285: Check for auth failure detected in stderr
    # Return 503 (Service Unavailable) so backend can classify as AUTH error
    if ctx.auth_abort_event.is_set():
        auth_msg = ctx.auth_abort_reason[0] if ctx.auth_abort_reason else "Authentication failure detected"
        # SECURITY: Sanitize before logging/returning (auth_abort_reason is captured pre-sanitization)
        auth_msg = sanitize_text(auth_msg)
        logger.error(f"[Headless Task] Auth abort: {auth_msg}")
        raise HTTPException(
            status_code=503,
            detail=f"Authentication failure: {auth_msg[:300]}. Check subscription token or API key configuration."
        )

    # Check for errors
    if ctx.return_code != 0:
        # Issue #516: Signal terminations (timeout SIGKILL, OOM, parent SIGTERM,
        # operator cancel) must be classified before the auth heuristics, which
        # would otherwise misread "zero tokens processed" as an expired token.
        # Same shape as the #361 max-turns special-case above. The #61 path
        # (backend-driven terminate_execution_on_agent → process_registry's
        # SIGINT→SIGKILL) makes this the common case for any timeout.
        signal_exit = _classify_signal_exit(ctx.return_code, ctx.metadata)
        if signal_exit is not None:
            status_code, detail = signal_exit
            logger.warning(f"[Headless Task] {detail}")
            raise HTTPException(status_code=status_code, detail=detail)

        error_preview = verbose_transcript[:500] if verbose_transcript else ""
        if not error_preview:
            # Try to provide a meaningful fallback based on common failure patterns
            error_preview = _diagnose_exit_failure(ctx.return_code, ctx.metadata)

        # Issue #285: Check for auth failure patterns in stderr (fallback for cases
        # where real-time detection didn't trigger, e.g., pattern in later lines)
        if _is_auth_failure_message(error_preview) or _is_auth_failure_message(verbose_transcript):
            logger.error(f"[Headless Task] Auth failure (fallback detection): {error_preview[:200]}")
            raise HTTPException(
                status_code=503,
                detail=f"Authentication failure: {error_preview[:300]}. Check subscription token or API key configuration."
            )

        # Issue #285: Heuristic fallback — if exit code > 0 AND zero tokens processed,
        # likely an auth failure even if we didn't see the exact pattern.
        # Issue #516: require return_code > 0 — signal exits are handled above and
        # would otherwise produce a false positive here (zero tokens after kill).
        # #904: do NOT phrase this as an authentication issue. The confirmed-auth
        # path (`_is_auth_failure_message` branch above) already fired if there
        # was a real signal; reaching this code means exit > 0 with no
        # recognized auth indicator AND no signal classification — the cause
        # is genuinely unknown (OOM surfacing as exit 1, broken pipe from the
        # orphan killer, real auth, etc.). Naming "authentication" in the
        # detail caused SUB-003's substring matcher to fire a futile
        # auto-switch on every OOM and burn the 2h skip-list slot.
        if ctx.return_code > 0 and ctx.metadata.input_tokens == 0 and ctx.metadata.output_tokens == 0:
            logger.warning(
                f"[Headless Task] Zero tokens processed with exit code {ctx.return_code}. "
                f"Stderr: {error_preview[:200]}"
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Execution failed with no output (exit code {ctx.return_code}): "
                    f"{error_preview[:300]}"
                ),
            )

        # Also check if stderr contains a rate limit message
        if _is_rate_limit_message(error_preview) or _is_rate_limit_message(verbose_transcript):
            raise HTTPException(
                status_code=429,
                detail=f"Subscription usage limit: {error_preview[:300]}"
            )
        logger.error(f"[Headless Task] Task {ctx.task_session_id} failed (exit {ctx.return_code}): {error_preview}")
        raise HTTPException(
            status_code=500,
            detail=f"Task execution failed (exit code {ctx.return_code}): {error_preview[:300]}"
        )

    # Issue #520 + Session-tab pipe race recovery: clean exit
    # (return_code == 0) but the final `result` JSON line never reached
    # the reader thread — typically because a child subprocess inherited
    # stdout. metadata.cost_usd / duration_ms stay None.
    #
    # Recovery: response_parts is appended to as each assistant message's
    # text content arrives (independent of the result event). If we have
    # accumulated text, the assistant DID complete its response — only
    # the closing-stats line was lost. Synthesize a soft success so the
    # user gets their reply instead of a 502 + retry-recommended error.
    # cost/duration stay None in this branch (we don't know what they
    # were); the backend records the execution as success with null
    # cost rather than a misleading FAILED.
    #
    # Hard failure path stays as-is for the truly empty case (no
    # assistant text accumulated → nothing to recover).
    #
    # Issue #640 / #678: shared recovery pipeline. Tries JSONL metadata
    # back-fill first (rescues cost/context/model_name even when text
    # recovery succeeds), then text recovery from response_parts or
    # JSONL, then raises a structured 502 dict body for the backend to
    # salvage telemetry from.
    hard_failure = _attempt_empty_result_recovery(
        metadata=ctx.metadata,
        raw_messages=ctx.raw_messages,
        response_parts=ctx.response_parts,
        parse_failure_count=ctx.parse_failure_count,
        parse_failure_sample=ctx.parse_failure_sample,
        task_start_iso=ctx.task_start_iso,
        session_id_fallback=ctx.claude_session_uuid or None,
    )
    if hard_failure is not None:
        status_code, body = hard_failure
        logger.error(f"[Headless Task] {body['message']}")
        raise HTTPException(status_code=status_code, detail=body)

    # Build final response text
    response_text = "\n".join(ctx.response_parts) if ctx.response_parts else ""
    # SECURITY: Sanitize credentials from response text
    response_text = sanitize_text(response_text)

    if not response_text:
        # #160: `context: fork` skills do their work in a sub-context whose
        # output never reaches the parent stream. The parent claude exits
        # cleanly with a populated result line (cost_usd / duration_ms set
        # — that's why `_classify_empty_result` above returned None and we
        # got here), but `response_parts` is empty because no assistant
        # text was emitted to the parent's stdout. Pre-#160 we 500'd here,
        # which silently failed every scheduled invocation of any fork
        # skill (the issue reported 8 consecutive daily failures).
        #
        # When the parent reports completion cleanly, trust it: synthesize
        # a short placeholder reply so the caller gets a 200 instead of an
        # opaque "Task returned empty response" error. Real plumbing
        # failures (lost result line, dropped pipe, etc.) are already
        # handled above by `_classify_empty_result` and never reach here.
        if ctx.return_code == 0 and ctx.metadata.cost_usd is not None:
            logger.info(
                "[Headless Task] Task %s exited cleanly with no assistant "
                "text in the parent stream (cost=$%s, duration=%sms) — "
                "likely a `context: fork` skill. Returning placeholder "
                "response.",
                ctx.task_session_id,
                ctx.metadata.cost_usd,
                ctx.metadata.duration_ms,
            )
            response_text = (
                "(Task completed with no direct output — "
                "skill may use `context: fork`.)"
            )
        else:
            raise HTTPException(
                status_code=500,
                detail="Task returned empty response"
            )

    # Count unique tools used
    tool_use_count = len([e for e in ctx.execution_log if e.type == "tool_use"])
    ctx.metadata.tool_count = tool_use_count
    ctx.metadata.execution_id = ctx.task_session_id  # Track execution_id in metadata

    # Use session_id from Claude if available, otherwise use our generated one
    final_session_id = ctx.metadata.session_id or ctx.task_session_id

    # Authoritative compact_events from the JSONL on disk.
    # Claude Code's stdout stream-json fires the compact_boundary
    # event without the compactMetadata envelope, so the parser
    # branch only learns "a compact happened" but loses the
    # pre/post/duration fields. The JSONL has the canonical shape;
    # read it after the turn completes and override whatever
    # stdout captured. Filtered to records emitted at or after
    # task_start_iso to scope to this turn's compacts only.
    jsonl_compacts = _extract_compact_events_from_jsonl(
        final_session_id, since_iso=ctx.task_start_iso
    )
    if jsonl_compacts:
        ctx.metadata.compact_events = jsonl_compacts
        for ev in jsonl_compacts:
            logger.info(
                f"event=session_auto_compact "
                f"claude_session_id={final_session_id} "
                f"trigger={ev.trigger} "
                f"pre_tokens={ev.pre_tokens} "
                f"post_tokens={ev.post_tokens} "
                f"duration_ms={ev.duration_ms}"
            )

    # Log warning if raw_messages is empty (transcript won't be available in UI)
    if len(ctx.raw_messages) == 0:
        logger.warning(f"[Headless Task] Task {final_session_id} completed but raw_messages is empty - execution transcript will be unavailable")
    else:
        logger.info(f"[Headless Task] Task {final_session_id} completed: cost=${ctx.metadata.cost_usd}, duration={ctx.metadata.duration_ms}ms, tools={ctx.metadata.tool_count}, raw_messages={len(ctx.raw_messages)}, parse_failures={ctx.parse_failure_count}")
    # Issue #640: same as chat path — surface parse failures even on
    # success because they may indicate silently dropped content.
    if ctx.parse_failure_count:
        logger.warning(
            f"[Headless Task] {ctx.parse_failure_count} stdout line(s) failed JSON parse "
            f"(first sample: {ctx.parse_failure_sample!r}). Likely cause: stdio MCP child "
            f"interleaved with claude on the agent-server pipe. "
            f"task_id={final_session_id}"
        )

    # Return raw_messages as the execution log (full JSON transcript from Claude Code)
    # Contains: init, assistant (thinking/tool_use), user (tool_result), result
    return response_text, ctx.raw_messages, ctx.metadata, final_session_id


async def execute_headless_task(
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
) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute Claude Code in headless mode for parallel task execution.

    Unlike execute_claude_code(), this function:
    - Does NOT acquire execution lock (parallel allowed)
    - Does NOT use --continue flag (stateless, no conversation context) by default
    - Each call is independent and can run concurrently
    - Can resume previous sessions via resume_session_id (EXEC-023)
    - Can persist the session JSONL via persist_session=True (Session tab)

    Args:
        prompt: The task to execute
        model: Optional model override (sonnet, opus, haiku). Defaults to "sonnet" if not
               specified to ensure compatibility with all subscription types (Issue #81).
        allowed_tools: Optional list of allowed tools (restricts available tools)
        system_prompt: Optional additional system prompt
        timeout_seconds: Execution timeout (default 5 minutes)
        max_turns: Maximum agentic turns for runaway prevention (None = unlimited)
        execution_id: Optional execution ID to use for process registry (enables termination tracking)
        resume_session_id: Optional Claude Code session ID to resume (EXEC-023)
        persist_session: If True, omit ``--no-session-persistence`` so the
            JSONL is written to ``~/.claude/projects/...`` and a future
            ``--resume`` can reattach. Default False keeps headless tasks
            stateless for all existing callers.

    Returns: (response_text, execution_log, metadata, session_id)
    """
    if not agent_state.claude_code_available:
        raise HTTPException(
            status_code=503,
            detail="Claude Code is not available in this container"
        )

    try:
        # Note: Claude Code will use whatever authentication is available:
        # 1. OAuth session from /login (Claude Pro/Max subscription) - stored in ~/.claude.json
        # 2. ANTHROPIC_API_KEY environment variable (API billing)
        # We don't require ANTHROPIC_API_KEY since users may be logged in with their subscription.
        ctx = _setup_headless_command(
            prompt=prompt,
            model=model,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            execution_id=execution_id,
            resume_session_id=resume_session_id,
            persist_session=persist_session,
            images=images,
        )

        registry = get_process_registry()

        # Run with timeout using asyncio. The inner function already bounds
        # its wait on the subprocess; the outer wait_for is a safety net
        # with a small grace period for drain/cleanup.
        loop = asyncio.get_event_loop()
        try:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _run_headless_subprocess, ctx),
                    timeout=ctx.effective_timeout + 60
                )
            except asyncio.TimeoutError:
                # Inner machinery should have raised first; safety net.
                logger.error(
                    f"[Headless Task] Outer timeout on task {ctx.task_session_id} "
                    f"— killing process group as last resort"
                )
                # ctx.terminate() does up to 4s of process.wait() (SIGTERM grace + SIGKILL grace);
                # off-load to the executor so the event loop stays responsive while we tear down.
                await loop.run_in_executor(None, ctx.terminate)
                # #1094: same semantic cause as the inner max-duration branch —
                # keep the structured detail symmetric so consumers filtering on
                # termination_reason see budget timeouts from either path.
                raise HTTPException(
                    status_code=504,
                    detail=_timeout_504_detail(
                        ctx,
                        f"Task execution timed out after {ctx.effective_timeout} seconds",
                        "max_duration",
                    ),
                )
            except subprocess.TimeoutExpired:
                # Inner process.wait() bounded out; tree has already been killed.
                # #1094: two distinct causes reach here — the per-tool no-output
                # stall watchdog and genuine max-duration budget exhaustion.
                # Stamp a reason-specific 504 instead of always claiming the
                # max-duration timeout (which misled operators into bumping the
                # execution timeout — the wrong knob for a 300s stall-kill).
                if ctx.termination_reason == "stall_no_output":
                    logger.error(
                        f"[Headless Task] Task {ctx.task_session_id} killed by stall "
                        f"watchdog (tool '{ctx.stalled_tool}' silent >{ctx.stall_limit_s:.0f}s)"
                    )
                    raise HTTPException(
                        status_code=504,
                        detail=_timeout_504_detail(
                            ctx,
                            (
                                f"Killed: tool '{ctx.stalled_tool}' produced no output "
                                f"for {ctx.stall_limit_s:.0f}s (stall watchdog)"
                            ),
                            "stall_no_output",
                            stalled_tool=ctx.stalled_tool,
                        ),
                    )
                logger.error(f"[Headless Task] Task {ctx.task_session_id} timed out after {ctx.effective_timeout}s")
                raise HTTPException(
                    status_code=504,
                    detail=_timeout_504_detail(
                        ctx,
                        f"Task execution timed out after {ctx.effective_timeout} seconds",
                        "max_duration",
                    ),
                )
            except RuntimeError as e:
                # Permission mode validation failure — fast-fail with actionable error
                if "Permission bypass failed" in str(e):
                    raise HTTPException(
                        status_code=503,
                        detail=str(e)
                    )
                raise

            return _finalize_headless_result(ctx)
        finally:
            # Always unregister process when done
            registry.unregister(ctx.task_session_id)

    except HTTPException:
        raise
    except (BrokenPipeError, ConnectionResetError) as pipe_err:
        # Subprocess stdin pipe closed before write completed — typically the
        # child Claude process exited early (auth abort, permission-mode kill,
        # or upstream cancellation). NOT a server-side fault; logging at ERROR
        # spams operators with misleading [Errno 32] noise (#474).
        #
        # 502 (not 503): SUB-003 (task_execution_service.py:628) interprets 503
        # from agent endpoints as auth-class failure and auto-switches the
        # subscription. 502 ("Bad Gateway to Claude subprocess") is semantically
        # correct and collision-free with the auto-switch path.
        logger.info(
            f"[Headless Task] Subprocess pipe closed before write completed: {pipe_err}. "
            f"This typically means the child Claude process exited early (auth abort, "
            f"permission validation kill, or upstream cancellation)."
        )
        raise HTTPException(
            status_code=502,
            detail="Agent subprocess closed before task could complete",
        )
    except Exception as e:
        logger.error(f"[Headless Task] Execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Task execution error: {str(e)}")
