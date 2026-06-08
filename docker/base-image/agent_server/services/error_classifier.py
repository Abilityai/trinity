"""Error classification and metadata recovery for Claude Code execution.

Extracted from `claude_code.py` per #122 (issue split). Three concerns live
here:

1. **Pattern detection** — ``_is_rate_limit_message``, ``_is_model_access_error``,
   ``_is_auth_failure_message`` decide what an error string is "about".

2. **Exit classification** — ``_diagnose_exit_failure`` and
   ``_classify_signal_exit`` translate a non-zero subprocess exit into a
   user-facing HTTP status + detail. Signal kills (SIGKILL/SIGTERM/SIGINT)
   must be classified BEFORE the auth-fallback heuristic gets a chance to
   misread "zero tokens processed" as an expired token (Issue #516).

3. **Empty-result recovery** — ``_recover_metadata_from_raw_messages`` and
   ``_classify_empty_result`` handle the stdout-pipe-race case where
   ``return_code == 0`` but the result line never reached the reader thread.
   The recovery prefers per-API-call usage on assistant messages over
   cumulative ``result.usage`` totals; the comment block in
   ``stream_parser.process_stream_line`` documents the invariant.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ..utils.credential_sanitizer import sanitize_dict, sanitize_text

if TYPE_CHECKING:
    from ..models import ExecutionMetadata

logger = logging.getLogger(__name__)


def _is_rate_limit_message(text: str) -> bool:
    """Check if a message indicates a subscription usage/rate limit error."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "out of extra usage",
        "out of usage",
        "usage limit",
        "rate limit",
        "rate_limit",
        "resets ",  # "resets 1am (America/New_York)"
        "exceeded your",
        "quota exceeded",
    ])


def _is_model_access_error(text: str) -> bool:
    """Check if a message indicates a model access/subscription tier error."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "model is not available",
        "not available on your subscription",
        "don't have access to",
        "model not found",
        "invalid model",
        "model access",
        "cannot access",
        "not supported by your plan",
    ])


def _is_auth_failure_message(text: str) -> bool:
    """Check if a message indicates an authentication/token failure.

    These patterns indicate the subscription token is expired, revoked,
    or otherwise invalid. When detected during execution, we should
    abort immediately rather than waiting for the full timeout.

    Issue #285: Expired tokens can cause Claude Code to hang instead of
    failing fast. Real-time detection in stderr allows early abort.

    The patterns also cover diagnostic messages this module emits itself
    (``_diagnose_exit_failure``'s "No authentication configured..." +
    "Subscription token may be expired" strings) so the classification
    pipeline in headless_executor / claude_code stays consistent whether
    the auth signal came from claude's stderr/stdout or from our own
    fallback diagnostic.
    """
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "subscription token may be expired",
        "token may be expired",
        "token expired",
        "token revoked",
        "invalid token",
        "authentication failed",
        "auth failed",
        "setup-token",  # "Generate a new one with 'claude setup-token'"
        "oauth token",
        "unauthorized",
        "invalid credentials",
        "credentials expired",
        # Patterns matching this module's own _diagnose_exit_failure output
        # so the auth pipeline classifies its own diagnostic strings.
        "no authentication configured",
        "set anthropic_api_key",
    ])


def _format_rate_limit_error(metadata: "ExecutionMetadata") -> str:
    """Format a clear, actionable rate limit error message."""
    base_msg = metadata.error_message or "Subscription usage limit reached"
    return (
        f"Subscription usage limit: {base_msg}. "
        f"To resolve: (1) wait for the usage reset, "
        f"(2) set an ANTHROPIC_API_KEY on this agent for pay-per-use billing, "
        f"or (3) assign a different subscription token in Settings → Subscriptions."
    )


def _diagnose_exit_failure(return_code: int, metadata: Optional["ExecutionMetadata"] = None) -> str:
    """Diagnose common Claude Code exit failures when stderr is empty."""
    # Check for rate limit detected during stream parsing
    if metadata and metadata.error_type == "rate_limit":
        return _format_rate_limit_error(metadata)

    # Check for billing errors (e.g., credit balance too low)
    if metadata and metadata.error_type == "billing_error":
        error_msg = metadata.error_message or "Billing error"
        return (
            f"{error_msg}. "
            f"To resolve: (1) add credits to your Anthropic account at console.anthropic.com, "
            f"or (2) assign a subscription token with available usage in Settings → Subscriptions."
        )

    # Check for model access errors detected during stream parsing
    if metadata and metadata.error_message and _is_model_access_error(metadata.error_message):
        return (
            f"Model access error: {metadata.error_message}. "
            f"The agent's configured model may not be available with the current subscription. "
            f"Try using a different model (sonnet, opus) or check subscription settings."
        )

    # Check for missing credentials
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_oauth_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    if not has_api_key and not has_oauth_token:
        return "No authentication configured. Set ANTHROPIC_API_KEY or assign a subscription token."
    if not has_api_key and has_oauth_token:
        # Issue #81: This error message was misleading when the actual issue was
        # model incompatibility. Now that we default to 'sonnet' for headless tasks,
        # this message is more likely to be accurate.
        # #904: stop declaring "token expired" as the diagnosis when we have
        # zero evidence — exit > 0 with no stderr happens on cgroup OOM kills
        # whose signal classification didn't fire (e.g. claude wrapping the
        # SIGKILL as a clean exit 1) just as often as on real token expiry.
        # The old wording fed SUB-003's substring matcher and triggered a
        # futile auto-switch on every OOM. Avoid any phrase that
        # `_is_auth_failure_message` matches (no "token expired",
        # "setup-token", "oauth token", etc.) so the result can't loop back
        # into the auth-503 path when used as `error_preview` in
        # `headless_executor.py`.
        return (
            f"Process failed with exit code {return_code} and no diagnostic output. "
            f"Most likely causes: OOM kill (raise the agent's memory limit), "
            f"schedule timeout (extend timeout_seconds), or container restart. "
            f"Check the agent container logs."
        )

    # Exit code hints
    hints = {
        1: "General error. Check agent logs for details.",
        2: "Misuse of command or invalid arguments.",
        126: "Claude Code command found but not executable.",
        127: "Claude Code command not found. Base image may need rebuilding.",
        137: "Process killed (SIGKILL). Likely out of memory — check agent resource limits.",
        139: "Segmentation fault.",
        143: "Process terminated (SIGTERM).",
    }
    return hints.get(return_code, f"Process exited with code {return_code}. Check agent container logs.")


# Signals that indicate external termination of the claude subprocess
# (timeout SIGKILL, OOM-kill, parent SIGTERM, operator cancel).
# Python subprocess returns these as negative numbers; shell wrappers
# may surface them as 128 + signum (130, 137, 143).
_SIGNAL_EXIT_NAMES = {
    2: "SIGINT",
    9: "SIGKILL",
    15: "SIGTERM",
}
_SHELL_ENCODED_SIGNAL_EXITS = {130, 137, 143}


def _classify_signal_exit(
    return_code: int,
    metadata: Optional["ExecutionMetadata"] = None,
) -> Optional[Tuple[int, str]]:
    """Classify a non-zero subprocess exit as an external signal kill.

    Issue #516: When claude is killed by SIGKILL/SIGTERM/SIGINT (schedule
    timeout, OOM-kill, parent cancel), the subprocess never emits its final
    `result` message and `process.wait()` returns a negative or 128+N exit
    code. Without this classification, the downstream auth-fallback heuristic
    misreads "zero tokens processed" as an expired subscription token and
    surfaces a confusing "Generate a new one with claude setup-token" error
    on every cron tick — masking the real cause (timeout/OOM/cancel).

    Mirrors the #361 max-turns special-case pattern: classify *before* the
    auth heuristics get a chance to misclassify.

    Returns ``(status_code, detail)`` for signal exits, or ``None`` if the
    return code is not a recognized signal termination (caller proceeds
    with normal error classification).
    """
    if return_code < 0:
        signum = -return_code
    elif return_code in _SHELL_ENCODED_SIGNAL_EXITS:
        signum = return_code - 128
    else:
        return None

    sig_name = _SIGNAL_EXIT_NAMES.get(signum, f"signal {signum}")
    tool_count = metadata.tool_count if metadata else 0
    num_turns = metadata.num_turns if (metadata and metadata.num_turns) else 0
    # #929: agent cap is now the schedule ceiling (write-time validation on
    # the backend), so the SIGKILL cause set is bounded: schedule timeout,
    # OOM, or operator cancel. Drop the misleading "schedule/agent" disjunction
    # — the agent cap never silently truncates a schedule under Approach A.
    detail = (
        f"Execution terminated by {sig_name} after {tool_count} tool calls "
        f"/ {num_turns} turns (exit code {return_code}). "
        f"Likely cause: schedule timeout exceeded, OOM kill, or operator cancel. "
        f"To allow longer runs raise the schedule's timeout_seconds "
        f"(bounded by the agent's execution_timeout_seconds cap); "
        f"for OOM raise the agent memory limit; otherwise split the skill into smaller steps."
    )
    return (504, detail)


def _recover_metadata_from_raw_messages(
    metadata: Optional["ExecutionMetadata"],
    raw_messages: Optional[List[Dict]],
) -> bool:
    """Back-fill ``metadata`` from a ``{"type": "result"}`` entry in
    ``raw_messages`` when ``process_stream_line`` failed to populate it.

    Issue #630: even when the reader thread successfully appends the result
    line to ``raw_messages``, ``process_stream_line`` may not run for that
    line if the reader is interrupted between the append and the parse
    (registry publish raising, permission-validation re-raise, any other
    in-loop exception). In that case ``metadata.cost_usd`` /
    ``duration_ms`` stay ``None`` even though Claude completed cleanly and
    the final stats are sitting in ``raw_messages[-1]``.

    This recovery pass scans ``raw_messages`` from the end (the result line
    is always last) and copies the fields ``process_stream_line`` would
    have set: ``cost_usd``, ``duration_ms``, ``num_turns``, and
    ``contextWindow`` from ``modelUsage``. ``error_type`` / response text
    are not back-filled — those drive control flow and would change
    behaviour beyond this defensive recovery.

    Token accounting (#122 finding 3): per-API-call usage on the LATEST
    assistant message is the authoritative source — ``result.usage`` and
    ``modelUsage.inputTokens`` are CUMULATIVE across every internal API
    call this turn made (a tool-using turn with 18 iterations has
    cache_read in result.usage = 18 × per-call cache_read = 1M+ tokens),
    so overwriting metadata.* with those values would break the
    context-window-pressure metric. We walk backward looking for an
    assistant message with a ``usage`` block and use that; only when none
    is found (raw_messages contains only init + result, or the reader
    exited before any assistant message arrived) do we fall back to
    ``result.usage`` so callers get *some* token signal rather than zero.

    Returns ``True`` if recovery populated metadata, ``False`` otherwise.
    Safe to call when metadata is already populated — short-circuits.
    """
    if metadata is None or not raw_messages:
        return False
    if metadata.cost_usd is not None or metadata.duration_ms is not None:
        return False

    for msg in reversed(raw_messages):
        if not isinstance(msg, dict) or msg.get("type") != "result":
            continue

        cost = msg.get("total_cost_usd")
        dur = msg.get("duration_ms")
        if cost is None and dur is None:
            return False  # malformed result entry — nothing to recover

        metadata.cost_usd = cost
        metadata.duration_ms = dur
        metadata.num_turns = msg.get("num_turns")

        # Prefer per-call usage on the latest assistant message — see
        # docstring for the cumulative-vs-per-call invariant.
        per_call_usage_found = False
        for prior in reversed(raw_messages):
            if not isinstance(prior, dict) or prior.get("type") != "assistant":
                continue
            asst_msg = prior.get("message")
            if not isinstance(asst_msg, dict):
                continue
            usage = asst_msg.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            metadata.input_tokens = usage.get("input_tokens", 0) or 0
            metadata.output_tokens = usage.get("output_tokens", 0) or 0
            metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0
            metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
            per_call_usage_found = True
            break

        if not per_call_usage_found:
            # Fallback path: no assistant message had usage data, so the
            # cumulative result.usage is the only token signal we have.
            usage = msg.get("usage", {}) if isinstance(msg.get("usage"), dict) else {}
            if usage:
                metadata.input_tokens = (
                    usage.get("input_tokens", metadata.input_tokens) or metadata.input_tokens
                )
                metadata.output_tokens = (
                    usage.get("output_tokens", metadata.output_tokens) or metadata.output_tokens
                )
                metadata.cache_creation_tokens = (
                    usage.get("cache_creation_input_tokens", metadata.cache_creation_tokens)
                    or metadata.cache_creation_tokens
                )
                metadata.cache_read_tokens = (
                    usage.get("cache_read_input_tokens", metadata.cache_read_tokens)
                    or metadata.cache_read_tokens
                )

        # contextWindow always comes from modelUsage (it's per-model
        # capacity, not per-call usage). Only fall back to cumulative
        # token totals from modelUsage when we had no per-call data.
        model_usage = msg.get("modelUsage", {})
        if isinstance(model_usage, dict):
            for _, model_data in model_usage.items():
                if not isinstance(model_data, dict):
                    continue
                if "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                if not per_call_usage_found:
                    model_in = model_data.get("inputTokens")
                    if isinstance(model_in, int) and model_in > metadata.input_tokens:
                        metadata.input_tokens = model_in
                    model_out = model_data.get("outputTokens")
                    if isinstance(model_out, int) and model_out > metadata.output_tokens:
                        metadata.output_tokens = model_out
                break  # first model wins, mirrors process_stream_line

        return True

    return False


def _classify_empty_result(
    metadata: Optional["ExecutionMetadata"] = None,
    raw_message_count: int = 0,
    raw_messages: Optional[List[Dict]] = None,
    parse_failure_count: int = 0,
    parse_failure_sample: Optional[str] = None,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Classify a clean (return_code == 0) exit that produced no result message.

    Issue #520: When the claude subprocess exits 0 but the final
    ``{"type":"result"}`` JSON line was dropped before the reader thread
    captured it (typical cause: an MCP tool / child subprocess inherited
    stdout, kept the pipe open past claude's exit, the reader leaked,
    pgroup unwind closed the pipe, the result line went with it), the
    metadata fields populated *only* by the result message — ``cost_usd``
    and ``duration_ms`` — stay ``None``. Returning HTTP 200 here would
    have agent-server log "completed successfully" while backend silently
    reaps the execution as an orphan minutes later, masking the real
    failure with misleading diagnostics.

    Sibling of ``_classify_signal_exit`` (issue #516): both classify
    "subprocess plumbing dropped the result" cases that the success path
    would otherwise mishandle. The two-field check (``cost_usd`` AND
    ``duration_ms`` both ``None``) is conservative — single-field
    nullability could be a Claude format quirk; both-None is a strong
    signal that the terminal ``result`` message never arrived.

    When the result line is lost, metadata.tool_count / num_turns are also
    None (populated only by that line). Derive honest counts from
    raw_messages when available so the 502 detail is accurate. (#531)

    Issue #630: before classifying, attempt
    ``_recover_metadata_from_raw_messages`` — covers the case where the
    result line *was* parsed and appended to raw_messages but
    process_stream_line failed to run for it (reader-thread exit between
    append and parse). When recovery succeeds, metadata is populated and
    the function falls through to the success path.

    Issue #640: ``parse_failure_count`` / ``parse_failure_sample`` come from
    the stdout reader's tally of lines that ``json.loads`` rejected. A
    non-zero count is the strongest signal we have that the result was lost
    to wire interleaving rather than to the reader thread leaking past
    claude's exit. The detail string surfaces both the count and the first
    failed line (sanitized + length-capped) so the next debug session has
    a concrete trace instead of just a generic "child held stdout" guess.

    Returns ``(status_code, detail_dict)`` for empty-result exits, or
    ``None`` if metadata looks well-formed (caller proceeds with the
    normal response-building path).

    Issue #678: the detail is a **dict** (was a string until 2026-05-11),
    carrying partial metadata + raw_messages_count + parse_failure_count
    so the backend HTTPError handler can salvage cost/context/model_name
    onto the failure row instead of writing all-null telemetry. Both
    ``detail["message"]`` and ``detail["metadata"]`` are sanitized via
    ``sanitize_text`` / ``sanitize_dict`` because
    ``ExecutionMetadata.error_message`` is populated directly from
    Claude's output text (`stream_parser.py`) and can leak tokens.
    """
    if metadata is None:
        return None
    if metadata.cost_usd is not None or metadata.duration_ms is not None:
        return None

    if _recover_metadata_from_raw_messages(metadata, raw_messages):
        logger.warning(
            "[Headless Task] Recovered result metadata from raw_messages "
            "(stream parser missed the result line; cost=%s duration=%sms turns=%s)",
            metadata.cost_usd, metadata.duration_ms, metadata.num_turns,
        )
        return None

    # tool_count is accumulated per-message during parsing, so it's reliable
    # even when the result line is lost. num_turns is populated only by the
    # result line — fall back to counting assistant messages in raw_messages
    # when it's None. (#531)
    tool_count = metadata.tool_count or 0
    if metadata.num_turns is not None:
        num_turns = metadata.num_turns
    elif raw_messages:
        num_turns = sum(1 for m in raw_messages if m.get("type") == "assistant")
    else:
        num_turns = 0

    # Issue #640: summarise what raw_messages we did capture — when the
    # result is lost, the message-type histogram tells operators whether the
    # reader caught most of the stream (e.g. assistant-heavy → likely lost
    # only the trailing result line) or stopped near the start (e.g. corrupt
    # init line → reader bailed early on a different failure).
    if raw_messages:
        type_counts = Counter(m.get("type", "?") for m in raw_messages)
        type_summary = ",".join(f"{t}={c}" for t, c in type_counts.most_common(6))
    else:
        type_summary = "<none>"

    message = (
        f"Execution completed without a result message after {tool_count} tool calls "
        f"/ {num_turns} turns (raw_messages={raw_message_count} types={type_summary}, "
        f"parse_failures={parse_failure_count}). "
        f"Likely cause: a tool or child subprocess inherited stdout and prevented "
        f"the claude reader thread from capturing the final result block. "
        f"Check agent-server logs for 'Reader thread(s) stuck after process exit', "
        f"'Orphan pipe-writer SIGKILL' (#618), or 'I/O operation on closed file' "
        f"near this execution. "
        f"This is a transient infrastructure failure; retry the task."
    )
    if parse_failure_count and parse_failure_sample:
        message += f" First malformed stdout line: {parse_failure_sample!r}"

    # #678: structured body so the backend HTTPError handler can salvage
    # partial telemetry onto the failure row. Sanitize before returning —
    # `metadata.error_message` is populated directly from Claude output
    # in stream_parser.py and can carry leaked credentials.
    partial_metadata: Dict[str, Any] = {}
    if metadata is not None:
        partial_metadata = sanitize_dict(metadata.model_dump())

    body: Dict[str, Any] = {
        "message": sanitize_text(message),
        "metadata": partial_metadata,
        "raw_message_count": raw_message_count,
        "parse_failure_count": parse_failure_count,
        "recovery_attempted": True,
    }
    return (502, body)
