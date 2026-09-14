"""Reading an agent's response: what did that execution actually mean? (#2314)

Carved out of ``task_execution_service`` — the #2 code-health hotspot (2,305
lines, CC 214) and the only top-3 one with no covering refactor issue until
#2314.

WHY THIS SEAM FIRST. The issue is explicit that a big-bang split mid-#1081 is
the wrong shape, so this is the carve that migration cannot collide with: every
function here is a PURE reader of an agent's HTTP response or of an execution's
own numbers. Nothing here touches the DB, the capacity manager, the breakers,
Redis, or the dispatch path #1081 is rewriting. A dependency analysis over the
original module confirmed the whole group needs only ``typing``, ``httpx`` and
the two constants below — no import back into the service, so the module is a
LEAF and the circular-import count the dashboard reports stays at 0.

WHAT LIVES HERE. One question, asked six ways:

* ``_compute_context_used``      — how full was the context window
* ``_is_reader_race_signature``  — is this "success with nothing to show" the
                                   #678 stdout reader race, and therefore worth
                                   one silent retry
* ``detect_unresolved_slash_command`` — did the agent answer by quoting a slash
                                   command back at us (#1410)
* ``_extract_agent_error``       — what did a 502/504 body really say, and what
                                   telemetry can be salvaged from it (#1853)
* ``classify_switch_failure``    — is this failure the subscription's fault
                                   (SUB-003 / #792)
* ``_salvage_attempt_cost``      — what did the attempt cost before it failed

NOT MOVED, deliberately: ``_alert_skill_not_found`` reads like a sibling of
``detect_unresolved_slash_command`` but writes to the operator queue through the
#1677 bounded-alert path. It is an EFFECT, not a classification, and pulling it
here would import the queue into a leaf module and make this the seventh thing
in a module whose value is that it is one thing.

IMPORT COMPATIBILITY. ``task_execution_service`` re-exports every name below, so
existing importers — ``chat_execution_service`` and six test modules — keep
working unchanged. That is what makes this a pure refactor: no call site moved,
no behaviour touched.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

def _compute_context_used(metadata: dict) -> Optional[int]:
    """Derive context-window pressure (tokens used) from an
    ``ExecutionMetadata.model_dump()`` dict.

    Mirrors the success-path logic at the original call site: cache_read
    + cache_creation is the stable signal (monotonic across a resumed
    session), fall back to input_tokens when caching isn't engaged.
    Returns None when no token signal is present.

    Shared between the success path and the #678 HTTPError salvage so
    both compute context_used the same way.
    """
    if not metadata:
        return None
    cache_read = metadata.get("cache_read_tokens") or 0
    cache_create = metadata.get("cache_creation_tokens") or 0
    if cache_read + cache_create > 0:
        return cache_read + cache_create
    input_tokens = metadata.get("input_tokens") or 0
    return input_tokens if input_tokens > 0 else None


# Conservative gating: only retry when the original turn was cheap and
# the agent-server's classifier marked it as a reader-race (not a real
# claude failure). num_turns < 5 keeps a 24-min execution like the
# original #678 from being silently re-burned.
_AUTO_RETRY_MAX_TURNS = 5


def _is_reader_race_signature(detail) -> bool:
    """True when a 502 detail body matches the stdout reader-race
    signature and the original turn was cheap enough to retry.

    The structured body comes from
    ``error_classifier._classify_empty_result`` (Issue #678):

        {
            "message": "Execution completed without a result message ...",
            "metadata": {...},
            "raw_message_count": N,
            "parse_failure_count": N,
            "recovery_attempted": True,
        }

    Gating: raw_message_count == 0 (reader thread emitted nothing —
    distinct from a partial stream), num_turns < 5 (cheap to retry),
    parse_failure_count == 0 (no wire corruption).
    """
    if not isinstance(detail, dict):
        return False
    if not detail.get("recovery_attempted"):
        return False
    if detail.get("raw_message_count", 0) != 0:
        return False
    if detail.get("parse_failure_count", 0) != 0:
        return False
    meta = detail.get("metadata") or {}
    num_turns = meta.get("num_turns") or 0
    if num_turns >= _AUTO_RETRY_MAX_TURNS:
        return False
    msg = (detail.get("message") or "").lower()
    return "result message" in msg


# The agent runtime (Claude Code) answers a slash-command that doesn't resolve
# to an installed skill with a normal, successful assistant turn — e.g.
# "Unknown command: /generate". Anchored at the start of the (stripped)
# response so an agent that merely mentions the phrase mid-prose is never
# matched.
_UNRESOLVED_COMMAND_RE = re.compile(
    r"^\s*unknown (?:slash )?command:\s*(/\S+)", re.IGNORECASE
)


def detect_unresolved_slash_command(
    message: Optional[str], response: Optional[str]
) -> Optional[str]:
    """Return the offending command when *message* was a slash-command
    invocation and *response* is the runtime's unresolved-command reply (#1410).

    A scheduled/triggered ``/foo`` whose skill is absent from the container
    comes back as a successful $0 turn ("Unknown command: /foo"), so the
    execution would otherwise record as SUCCESS and blend into legitimate
    skipped/$0 runs — masking a dead agent function indefinitely. Detection is
    gated on the SENT message being a slash-command so a normal turn that
    happens to echo the phrase is never misclassified. Returns ``None`` when it
    is not this specific shape.
    """
    if not message or not response:
        return None
    if not message.lstrip().startswith("/"):
        return None
    match = _UNRESOLVED_COMMAND_RE.match(response)
    return match.group(1) if match else None


def _extract_agent_error(
    response: Optional[httpx.Response], fallback: str
) -> tuple[str, dict, Any]:
    """Pull a human error string, any #678 structured ``metadata``, and the
    #1853 ``execution_log`` transcript from an agent error-response body. Shared
    by the pre-raise switch path (#792) and the ``except httpx.HTTPError``
    handler so both read the body identically.

    ``execution_log`` is the raw stream-json transcript list the agent's
    structured 502/504 body now carries (``_execution_error_502_detail`` /
    ``_timeout_504_detail``); ``None`` for a bare-string body (old image) — the
    graceful mixed-fleet degrade."""
    error_msg = fallback
    partial_metadata: dict = {}
    execution_log: Any = None
    if response is None:
        return error_msg, partial_metadata, execution_log
    try:
        error_data = response.json()
        detail = error_data.get("detail")
        if isinstance(detail, dict):
            # #678 structured body
            error_msg = detail.get("message") or str(detail)
            if isinstance(detail.get("metadata"), dict):
                partial_metadata = detail["metadata"]
            # #1853: the full stream-json transcript, salvaged onto the FAILED row.
            execution_log = detail.get("execution_log")
        elif "detail" in error_data:
            error_msg = error_data["detail"]
    except Exception:
        if response.text:
            error_msg = response.text[:500]
    return error_msg, partial_metadata, execution_log


def classify_switch_failure(response: httpx.Response) -> Optional[str]:
    """Map an agent response to a SUB-003 switch ``failure_kind``, or None.

    Mirrors the trigger surface SUB-003 uses in the ``except httpx.HTTPError``
    handler so the #792 contract — "any switch-success retries" — is actually
    true (not just 429/503 by status code):

        429                          -> "rate_limit"
        503 / 401 / 403 / 402        -> "auth"
        other 4xx/5xx whose body trips ``is_auth_failure`` -> "auth"
        anything else (incl. 2xx)    -> None
    """
    # Imported here (not at module scope) to match the except-handler import and
    # keep the test patch target `subscription_auto_switch.is_auth_failure` live.
    from services.subscription_auto_switch import is_auth_failure

    code = response.status_code
    if code == 429:
        return "rate_limit"
    if code in (503, 401, 403, 402):
        return "auth"
    if code >= 400:
        error_msg, _, _ = _extract_agent_error(response, "")
        if is_auth_failure(error_msg):
            return "auth"
    return None


def _salvage_attempt_cost(partial_metadata: dict) -> float:
    """Best-effort first-attempt cost from a salvaged ``metadata`` dict, for the
    #678 R2 ``previous_attempt_cost`` rollup. A mid-run 429/auth can carry
    nonzero cost — "≈$0" is not a contract (#792 review, Codex #3)."""
    raw = partial_metadata.get("cost_usd")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return 0.0
