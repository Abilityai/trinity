"""Fire-and-forget result callback — agent side (#1083).

When the backend dispatches an async-eligible turn (``async_result=true``) AND
this agent runs the Claude runtime, ``/api/task`` accepts with **202** and runs
the turn in a *detached* task. On completion the typed terminal is:

  1. persisted to ``~/.trinity/pending-results/<execution_id>.json`` (so a
     backend deploy / agent restart mid-callback doesn't turn completed work
     into a phantom ``LEASE_EXPIRED``), then
  2. POSTed to the backend's result-callback endpoint, retried with capped
     exponential backoff **up to the slot-lease deadline** (dispatch +
     ``timeout + SLOT_TTL_BUFFER``), then
  3. deleted from disk on a 2xx (or a permanent 4xx reject).

A **startup sweep** re-sends any envelope left on disk by a crash/restart — a
late SUCCESS can still overwrite a reaper's phantom ``LEASE_EXPIRED`` via the
backend CAS (SUCCESS wins over a non-cancelled terminal).

Authenticated with the agent's own ``TRINITY_MCP_API_KEY`` (Option B,
least-privilege), mirroring ``heartbeat.py``. Everything is best-effort: a
failure to report never crashes the agent — the backend's lease reaper is the
final backstop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

import httpx
from fastapi import HTTPException

from ..state import agent_state
from .process_registry import get_process_registry

logger = logging.getLogger(__name__)

# Mirrors the backend slot_service.SLOT_TTL_BUFFER — the grace window between the
# agent timeout and the slot-lease TTL the reaper enforces.
_SLOT_TTL_BUFFER_SECONDS = 300
_PENDING_DIR = Path(os.path.expanduser("~/.trinity/pending-results"))
_POST_TIMEOUT = 15.0          # per-attempt HTTP timeout
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0           # cap backoff between retries
_SWEEP_DEADLINE_SECONDS = 180.0  # bounded best-effort window for the startup sweep

# #1085 startup-sweep spread. After a backend restart ~N agents all run the
# resend sweep at once; jittering the first POST (and each envelope) smears that
# t≈0 thundering herd over a window instead of a synchronized spike.
_SWEEP_INITIAL_JITTER_SECONDS = 60.0
_SWEEP_PER_ENVELOPE_JITTER_SECONDS = 5.0


def _jittered_backoff(prev_sleep: float) -> float:
    """Decorrelated-jitter backoff (AWS pattern), capped at ``_BACKOFF_CAP``.

    ``sleep ∈ [base, max(base, prev) * 3]`` — self-paces (grows with the previous
    sleep) AND spreads (uniform), so a fleet of retrying agents desynchronises.
    Unlike pure exponential (lockstep, every agent on the same schedule) or full
    jitter (``uniform(0, cap)``, which discards the floor and can hot-loop)."""
    return min(_BACKOFF_CAP, random.uniform(_BACKOFF_BASE, max(_BACKOFF_BASE, prev_sleep) * 3))


def _parse_retry_after(value: object) -> float:
    """Best-effort parse of a ``Retry-After`` header (integer seconds form) to a
    float. The backend only ever sends integer seconds (e.g. the #1085 governor
    503). Returns 0.0 on absence / a non-integer (HTTP-date) value — the jittered
    backoff then applies unchanged."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(str(value).strip())))
    except (TypeError, ValueError):
        return 0.0

# Permanent rejects — retrying won't help (auth/ownership/marker/size/replay).
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 422})

# Strong refs to detached run+report tasks. asyncio holds only a WEAK ref to a
# bare create_task result, so without this the GC could collect the task
# mid-flight and the turn would never report back (the lease reaper would then
# FAIL completed work). The done-callback discards so the set never grows.
_inflight: "Set[asyncio.Task[Any]]" = set()


def is_claude_runtime() -> bool:
    """v1 async dispatch is Claude-runtime only (decision 5): the typed
    terminal-reason envelope is Claude-specific."""
    return agent_state.agent_runtime in ("claude-code", "claude")


def _callbacks_configured() -> bool:
    return bool(os.getenv("TRINITY_BACKEND_URL") and os.getenv("TRINITY_MCP_API_KEY"))


# Defense-in-depth (#1083): execution_id is backend-generated — a urlsafe token
# (``secrets.token_urlsafe``) or a UUID — and is used both to build a filesystem
# path under _PENDING_DIR and the backend callback URL. Belt: validate it against
# that charset at the async-dispatch entry point (try_spawn_async) so a
# malformed/hostile value never reaches the path build or the callback URL. (The
# ``temp-…`` fallback id carries a ``.`` but only exists when execution_id is
# absent, in which case try_spawn_async already falls back to sync.)
# Suspenders: the #950 normpath+startswith containment guard, co-located with the
# write/unlink sinks in _persist/_delete below (CodeQL only honours a CWE-022
# barrier in the same function as the sink, not across a callee's return).
_SAFE_EXECUTION_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _is_safe_execution_id(execution_id: Any) -> bool:
    return bool(isinstance(execution_id, str) and _SAFE_EXECUTION_ID.match(execution_id))


def _persist(execution_id: str, record: Dict) -> None:
    """Atomically write the pending record (tmp + rename) so a crash mid-write
    never leaves a half-JSON file the sweep would choke on.

    The #950 path-containment guard is inlined here, co-located with the
    write/replace sink, so a hostile execution_id is provably confined to
    _PENDING_DIR (CWE-022). Best-effort: a containment failure is swallowed.
    """
    try:
        _PENDING_DIR.mkdir(parents=True, exist_ok=True)
        base = os.path.normpath(str(_PENDING_DIR))
        dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
        if dest != base and not dest.startswith(base + os.sep):
            raise ValueError(f"pending-result path escapes {base}: {execution_id!r}")
        tmp = f"{dest}.tmp"
        Path(tmp).write_text(json.dumps(record))
        Path(tmp).replace(dest)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.debug("[#1083] could not persist pending result %s", execution_id, exc_info=True)


def _delete(execution_id: str) -> None:
    """Remove a pending record. The #950 containment guard is inlined here,
    co-located with the unlink sink, so a hostile id can't unlink outside
    _PENDING_DIR (CWE-022). Best-effort: a containment failure is a no-op."""
    try:
        base = os.path.normpath(str(_PENDING_DIR))
        dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
        if dest != base and not dest.startswith(base + os.sep):
            return
        Path(dest).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("[#1083] could not delete pending result %s", execution_id, exc_info=True)


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------
def _success_envelope(response_text, raw_messages, metadata, session_id) -> Dict:
    return {
        "status": "success",
        "response": response_text,
        "execution_log": raw_messages,
        "metadata": metadata.model_dump() if hasattr(metadata, "model_dump") else (metadata or {}),
        "session_id": session_id,
        "terminal_reason": "completed",
    }


# status_code → (error_code, terminal_reason). Only "auth" feeds the backend
# dispatch breaker (D10); the rest are informational. Mirrors the sync-path
# classification (503 → AUTH; everything else non-AUTH).
#
# #1085: 429 now carries error_code "billing" (the enum existed but was never
# set). A fleet-wide Claude-API 429 storm is a shared cause the re-delivery
# governor must catch alongside AUTH — without this, a rate-limit terminal would
# stay error_code=None and never reach the distinct-agent detector. The
# terminal_reason stays "rate_limit" so the cancel-relabel guard
# (`_is_auth_or_rate`) and the backend's mirror still treat it as auth/rate (must
# never be reclassified as a clean cancellation).
_STATUS_MAP = {
    503: ("auth", "auth"),
    504: ("timeout", "max_duration"),
    502: (None, "empty_result"),
    429: ("billing", "rate_limit"),
    422: (None, "max_turns"),
    500: (None, "error"),
}


def _envelope_from_http_exception(exc: HTTPException) -> Dict:
    """Build a typed FAILED envelope from the headless executor's HTTPException.

    The 502 empty-result path already carries a structured dict body with
    ``metadata`` (#678); other paths carry a string detail. Full metadata on the
    504/503 paths is the #1201 coordination (T8, P2 fast-follow) — until then a
    non-502 async failure writes a null-cost/context row (pre-#678 behaviour).
    """
    detail = exc.detail
    metadata: Dict = {}
    if isinstance(detail, dict):
        error_msg = detail.get("message") or json.dumps(detail)[:500]
        if isinstance(detail.get("metadata"), dict):
            metadata = detail["metadata"]
    else:
        error_msg = str(detail)[:500]

    error_code, terminal_reason = _STATUS_MAP.get(exc.status_code, (None, "error"))
    return {
        "status": "failed",
        "error": error_msg,
        "error_code": error_code,
        "terminal_reason": terminal_reason,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# #679 cancel relabel
# ---------------------------------------------------------------------------
def _cancelled_override(envelope: Dict) -> Dict:
    """Relabel a terminal envelope as a user cancel (#679).

    Keeps whatever response/metadata/session_id/execution_log the turn produced
    (Claude emits a graceful final message on SIGINT) and drops ``error_code`` so
    the backend doesn't treat the cancel as an agent failure. The backend maps
    ``status:"cancelled"`` → ``CANCELLED`` (never a billable success)."""
    overridden = dict(envelope)
    overridden["status"] = "cancelled"
    overridden["terminal_reason"] = "cancelled"
    overridden["error_code"] = None
    return overridden


def _is_auth_or_rate(envelope: Dict) -> bool:
    """Issue 6 / C6: an auth (503) or rate-limit (429) terminal must NOT be
    relabelled cancelled even if the execution was terminated — the backend's
    AUTH dispatch breaker / SUB-003 still need to record it on the async path."""
    return (
        envelope.get("error_code") == "auth"
        or envelope.get("terminal_reason") in ("auth", "rate_limit")
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
async def _deliver(
    execution_id: str,
    agent_name: str,
    envelope: Dict,
    backend_url: str,
    mcp_key: str,
    deadline_monotonic: float,
) -> bool:
    """POST the envelope, retrying with capped backoff until the lease deadline.

    Returns True on a 2xx (delivered) OR a permanent 4xx reject (no point
    retrying — the row is gone / already terminal / the request is malformed).
    Returns False only when the deadline passed without a definitive response.
    """
    url = f"{backend_url}/api/agents/{agent_name}/executions/{execution_id}/result"
    headers = {"Authorization": f"Bearer {mcp_key}"}
    prev_sleep = _BACKOFF_BASE
    async with httpx.AsyncClient(timeout=_POST_TIMEOUT) as client:
        while True:
            retry_after_floor = 0.0
            try:
                resp = await client.post(url, json=envelope, headers=headers)
                if resp.status_code < 300:
                    logger.info("[#1083] result for %s delivered (%s)", execution_id, resp.status_code)
                    return True
                if resp.status_code in _PERMANENT_STATUSES:
                    # Permanent: marker missing (409), not owned (404), replay
                    # rejected, oversized (413), bad auth (403). Stop retrying.
                    logger.warning(
                        "[#1083] result for %s permanently rejected (%s) — giving up",
                        execution_id, resp.status_code,
                    )
                    return True
                # Transient (incl. the #1085 governor 503 throttle/pause). Honor a
                # Retry-After hint as a floor on the jittered backoff so a
                # throttled agent actually waits the hinted time instead of
                # re-storming on its own short schedule.
                retry_after_floor = _parse_retry_after(resp.headers.get("Retry-After"))
                logger.warning(
                    "[#1083] result for %s got %s — will retry", execution_id, resp.status_code
                )
            except Exception:  # noqa: BLE001 — transport error → retry until deadline
                logger.debug("[#1083] result POST for %s failed", execution_id, exc_info=True)

            now = time.monotonic()
            if now >= deadline_monotonic:
                logger.warning(
                    "[#1083] result for %s not delivered before lease deadline — "
                    "leaving persisted for startup re-send (reaper is the backstop)",
                    execution_id,
                )
                return False
            # #1085: decorrelated jitter (not lockstep exponential) so a fleet of
            # retrying agents desynchronises, with a server Retry-After as a floor.
            backoff = max(_jittered_backoff(prev_sleep), retry_after_floor)
            prev_sleep = min(_BACKOFF_CAP, backoff)
            # Never sleep past the deadline.
            backoff = min(backoff, max(0.0, deadline_monotonic - now))
            await asyncio.sleep(backoff)


async def _run_and_report(request, backend_url: str, mcp_key: str, dispatch_monotonic: float) -> None:
    """Run the headless turn, build + persist the typed terminal, deliver it."""
    from ..services.runtime_adapter import get_runtime

    execution_id = request.execution_id
    agent_name = agent_state.agent_name
    runtime = get_runtime()

    agent_state.record_task_start()
    try:
        response_text, raw_messages, metadata, session_id = await runtime.execute_headless(
            prompt=request.message,
            model=request.model,
            allowed_tools=request.allowed_tools,
            system_prompt=request.system_prompt,
            timeout_seconds=request.timeout_seconds or 900,
            max_turns=request.max_turns,
            execution_id=execution_id,
            resume_session_id=request.resume_session_id,
            persist_session=bool(request.persist_session),
            images=request.images,
        )
        envelope = _success_envelope(response_text, raw_messages, metadata, session_id)
        finish_success: Optional[bool] = True
    except HTTPException as exc:
        finish_success = False
        envelope = _envelope_from_http_exception(exc)
    except BaseException as exc:  # noqa: BLE001 — any failure must still report a terminal
        finish_success = False
        envelope = {
            "status": "failed",
            "error": str(exc)[:500] or type(exc).__name__,
            "error_code": None,
            "terminal_reason": "error",
            "metadata": {},
        }

    # #679: a graceful exit-0 OR a SIGKILL→504 for a turn the operator cancelled
    # must be relabelled `cancelled` so the backend writes CANCELLED — never a
    # billable success, never a breaker-counting failure. Auth/rate envelopes are
    # excluded (C6) so the AUTH dispatch breaker + SUB-003 still record on async.
    if (
        execution_id
        and get_process_registry().was_terminated(execution_id)
        and not _is_auth_or_rate(envelope)
    ):
        envelope = _cancelled_override(envelope)
        # #679 (F4) parity with the sync path: a cancel is NEUTRAL for the
        # consecutive_failures health counter — never trips or resets the
        # dispatch breaker (#526). Recorded after the relabel decision so the
        # graceful-cancel (was True) and the 504/502-cancel (was False) agree.
        finish_success = None

    # Recorded once, after the cancel decision, so the health counter matches the
    # final terminal label.
    agent_state.record_task_finish(success=finish_success)

    _persist(execution_id, {"agent_name": agent_name, "envelope": envelope})

    # Deadline = dispatch + (agent timeout + buffer) = the slot-lease TTL window.
    lease_seconds = float(request.timeout_seconds or 900) + _SLOT_TTL_BUFFER_SECONDS
    deadline = dispatch_monotonic + lease_seconds
    delivered = await _deliver(execution_id, agent_name, envelope, backend_url, mcp_key, deadline)
    if delivered:
        _delete(execution_id)


def try_spawn_async(request) -> bool:
    """Spawn the detached run+report for an async dispatch. Returns True (caller
    answers 202) only when eligible: async requested, Claude runtime, an
    execution_id to key the callback, and callback creds present. Otherwise
    returns False and the caller runs the turn synchronously (the non-202
    fallback that keeps mixed image versions + non-Claude runtimes working)."""
    if not getattr(request, "async_result", False):
        return False
    if not is_claude_runtime():
        logger.info("[#1083] async_result requested but runtime is %s — running sync",
                    agent_state.agent_runtime)
        return False
    if not request.execution_id:
        logger.info("[#1083] async_result requested without execution_id — running sync")
        return False
    if not _is_safe_execution_id(request.execution_id):
        # Defense-in-depth: a malformed/hostile execution_id must never reach the
        # pending-results path build or the callback URL. Fall back to sync.
        logger.warning(
            "[#1083] async_result requested with unsafe execution_id %r — running sync",
            request.execution_id,
        )
        return False
    if not _callbacks_configured():
        logger.info("[#1083] async_result requested but callback creds absent — running sync")
        return False

    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    dispatch_monotonic = time.monotonic()
    task = asyncio.create_task(
        _run_and_report(request, backend_url, mcp_key, dispatch_monotonic)
    )
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    logger.info("[#1083] accepted async task %s (202); reporting via callback", request.execution_id)
    return True


# ---------------------------------------------------------------------------
# Startup sweep — re-send envelopes left by a crash / restart
# ---------------------------------------------------------------------------
async def resend_pending_results() -> None:
    """Best-effort: re-POST every persisted envelope once. A turn that completed
    but whose callback never landed (backend deploy, agent restart) is delivered
    here — a late SUCCESS can still correct a reaper's LEASE_EXPIRED via CAS."""
    if not _callbacks_configured() or not _PENDING_DIR.exists():
        return
    # #1085 A2 (highest-value change): one-shot initial jitter so ~N agents
    # restarting together smear the t≈0 resend burst over a minute instead of a
    # synchronized spike on the backend's callback endpoint.
    await asyncio.sleep(random.uniform(0, _SWEEP_INITIAL_JITTER_SECONDS))
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    fallback_agent = agent_state.agent_name
    try:
        pending = sorted(_PENDING_DIR.glob("*.json"))
    except Exception:  # noqa: BLE001
        return
    for path in pending:
        # #1085 A2: small per-envelope jitter so one agent's many envelopes don't
        # fire back-to-back in a tight loop.
        await asyncio.sleep(random.uniform(0, _SWEEP_PER_ENVELOPE_JITTER_SECONDS))
        execution_id = path.stem
        try:
            record = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 — corrupt/partial file: drop it
            logger.debug("[#1083] dropping unreadable pending result %s", execution_id, exc_info=True)
            _delete(execution_id)
            continue
        envelope = record.get("envelope")
        if not isinstance(envelope, dict):
            _delete(execution_id)
            continue
        agent_name = record.get("agent_name") or fallback_agent
        deadline = time.monotonic() + _SWEEP_DEADLINE_SECONDS
        delivered = await _deliver(execution_id, agent_name, envelope, backend_url, mcp_key, deadline)
        if delivered:
            _delete(execution_id)


def schedule_pending_result_resend(app) -> None:
    """Attach a startup handler that re-sends leftover pending results. Gated on
    callback creds, mirroring schedule_heartbeat."""
    if not _callbacks_configured():
        return

    @app.on_event("startup")
    async def _resend_on_startup() -> None:
        try:
            await resend_pending_results()
        except Exception:  # noqa: BLE001 — never block startup
            logger.debug("[#1083] startup pending-result resend failed", exc_info=True)
