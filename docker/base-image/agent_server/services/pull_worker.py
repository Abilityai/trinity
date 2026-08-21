"""Agent-side pull worker pool (#946 / #1081 Phase 2).

The **consumer** half of the pull / work-stealing coordination model
(``TARGET_ARCHITECTURE.md`` §"Coordination Model",
``MESSAGE_ENVELOPE_SCHEMA.md`` §3). Instead of the backend PUSHing a turn to
``/api/task``, an opted-in agent runs a bounded pool of workers that PULL:

  1. ``GET  /api/internal/next-task`` — atomically claim the oldest queued task
     for this agent (§3.1 claim response, or §3.2 empty).
  2. run the claimed turn through the runtime (``execute_headless``), then
  3. ``POST /api/internal/tasks/{execution_id}/result`` — report the typed
     terminal (§3.3) carrying the ``claim_token`` from the claim response, so the
     backend's compare-and-set terminal write (#1082) can dedup a re-delivery.

**Default OFF (the #1 safety property).** The pool starts ONLY when
``TRINITY_PULL_MODE`` is truthy for this agent (allowlist-injected by the backend,
see ``services/agent_service/pull_mode.py``). With the flag unset — every existing
agent — ``schedule_pull_workers`` registers no startup handler, no worker loop
runs, and the push path (``/api/task`` / ``/api/chat``) is byte-for-byte
unchanged.

**Invariant #5 mirror.** The HTTP/backoff/envelope machinery is modelled on
``result_callback.py`` (#1083) and ``heartbeat.py`` (#307): sleeps-first-style
loop, decorrelated-jitter backoff honoring a server ``Retry-After`` floor,
best-effort delivery to a lease deadline, and swallow-all so a backend blip never
kills a worker. The backend lease reaper is the final backstop for a terminal
that can't be delivered before the lease expires (re-delivery reuses the same
``execution_id``).

**Auth (least-privilege, #307/#1159).** Exactly like ``result_callback``/
``heartbeat``, the worker authenticates with the agent's OWN scoped MCP key
(``Authorization: Bearer ${TRINITY_MCP_API_KEY}``) — the key the platform already
injects into every agent. The two pull seams (``/api/internal/next-task`` +
``/api/internal/tasks/{id}/result``) accept that scoped key (validated via
``authorize_heartbeat``, an agent may act only on itself) as an alternate to the
internal secret, so no master secret is ever placed in an agent container.
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
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

from ..state import agent_state

logger = logging.getLogger(__name__)

# Mirrors the backend slot_service.SLOT_TTL_BUFFER — grace between the turn
# timeout and the slot-lease TTL the reaper enforces (result_callback.py parity).
_SLOT_TTL_BUFFER_SECONDS = 300
_DEFAULT_TURN_TIMEOUT = 900          # headless default, mirrors /api/task
_DEFAULT_POOL_SIZE = 3               # matches agent_ownership.max_parallel_tasks default
_MAX_POOL_SIZE = 32                  # matches the #506 fleet ceiling range

_POLL_TIMEOUT = 15.0                 # per-attempt HTTP timeout (claim + result POST)
_BACKOFF_BASE = 1.0
_IDLE_BACKOFF_CAP = 15.0             # between polls when the queue is empty
_ERROR_BACKOFF_CAP = 30.0           # after a transport error (backend down)
_RESULT_BACKOFF_CAP = 60.0          # between result-POST retries

# Permanent result-POST rejects — retrying won't help (bad token/exec/body/auth).
# Mirrors result_callback._PERMANENT_STATUSES; here 409 = wrong/stale claim_token,
# 404 = unknown execution, 422 = malformed body.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 422})

# ---------------------------------------------------------------------------
# B6 fix (#1081): pre-delivery persistence of a completed pull terminal.
#
# A turn that COMPLETED (its §3.3 body built, its cost already spent) but hasn't
# 2xx'd its result POST yet used to be lost on container shutdown/deploy — the
# worker task was merely cancelled, so the row stayed `running` and the backend
# lease reaper re-ran the whole turn from scratch (wasting the spent work), up to
# poison-park. Fix: persist the body to disk BEFORE delivering and re-send any
# leftover on startup — the same durability pattern result_callback.py (#1083)
# uses for the fire-and-forget callback path. A SEPARATE dir from that module's
# ~/.trinity/pending-results so the two startup sweeps never claim each other's
# files. The backend CAS dedups a re-delivery via the claim_token carried in the
# persisted body, so a resend can't double-apply.
_PENDING_PULL_DIR = Path(os.path.expanduser("~/.trinity/pending-pull-results"))
_SWEEP_DEADLINE_SECONDS = 180.0        # bounded best-effort window for the startup resend
_SWEEP_INITIAL_JITTER_SECONDS = 60.0   # #1085: smear the t≈0 resend burst across a fleet restart
_SWEEP_PER_ENVELOPE_JITTER_SECONDS = 5.0
_SHUTDOWN_DRAIN_SECONDS = 5.0          # bounded settle for in-flight workers before teardown

# execution_id is backend-generated (urlsafe token / UUID) and used to build a
# filesystem path under _PENDING_PULL_DIR; validate it against that charset
# before persisting so a hostile value can never escape the dir (mirrors
# result_callback._SAFE_EXECUTION_ID).
_SAFE_EXECUTION_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _is_safe_execution_id(execution_id: Any) -> bool:
    return bool(isinstance(execution_id, str) and _SAFE_EXECUTION_ID.match(execution_id))


def _persist_pull_result(execution_id: str, record: Dict[str, Any]) -> None:
    """Atomically write the pending pull terminal (tmp + rename) so a crash
    mid-write never leaves a half-JSON file the resend would choke on. The
    path-containment guard is inlined here, co-located with the write/replace
    sink, so a hostile execution_id is provably confined to _PENDING_PULL_DIR
    (CWE-022; CodeQL only honours a barrier in the same function as the sink).
    Best-effort: any failure is swallowed — persistence must never crash a turn.
    Mirrors ``result_callback._persist``."""
    try:
        _PENDING_PULL_DIR.mkdir(parents=True, exist_ok=True)
        base = os.path.normpath(str(_PENDING_PULL_DIR))
        dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
        if dest != base and not dest.startswith(base + os.sep):
            raise ValueError(f"pending pull-result path escapes {base}: {execution_id!r}")
        tmp = f"{dest}.tmp"
        Path(tmp).write_text(json.dumps(record))
        Path(tmp).replace(dest)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        logger.debug("[#1081] could not persist pending pull result %s", execution_id, exc_info=True)


def _delete_pull_result(execution_id: str) -> None:
    """Remove a delivered pending pull terminal. Containment guard inlined at the
    unlink sink so a hostile id can't unlink outside _PENDING_PULL_DIR (CWE-022).
    Best-effort: a containment failure is a no-op. Mirrors ``result_callback._delete``."""
    try:
        base = os.path.normpath(str(_PENDING_PULL_DIR))
        dest = os.path.normpath(os.path.join(base, f"{execution_id}.json"))
        if dest != base and not dest.startswith(base + os.sep):
            return
        Path(dest).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("[#1081] could not delete pending pull result %s", execution_id, exc_info=True)


def _jittered_backoff(prev_sleep: float, cap: float) -> float:
    """Decorrelated-jitter backoff (AWS pattern), capped at ``cap``.

    ``sleep ∈ [base, max(base, prev) * 3]`` — self-paces AND spreads, so a pool
    (and a fleet) of pollers desynchronises rather than storming in lockstep.
    Duplicated from ``result_callback._jittered_backoff`` per Invariant #5's
    note that mirrored *policy* is vendored but utility math is not (the backend
    never inspects the worker's backoff)."""
    return min(cap, random.uniform(_BACKOFF_BASE, max(_BACKOFF_BASE, prev_sleep) * 3))


def _parse_retry_after(value: object) -> float:
    """Best-effort parse of an integer-seconds ``Retry-After`` (the only form the
    backend #1085 governor 503 emits) to a float floor. 0.0 on absence/non-int."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(str(value).strip())))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Config gate — the load-bearing default-OFF property
# ---------------------------------------------------------------------------
def _pull_mode_enabled() -> bool:
    """Per-agent PULL_MODE flag. Default OFF: unset/false ⇒ no pool, push path
    untouched. Injected as ``TRINITY_PULL_MODE=true`` only for allowlisted pilot
    agents (backend ``services/agent_service/pull_mode.py``)."""
    return os.getenv("TRINITY_PULL_MODE", "false").strip().lower() == "true"


def _worker_creds_present() -> bool:
    return bool(os.getenv("TRINITY_BACKEND_URL") and os.getenv("TRINITY_MCP_API_KEY"))


def _pool_size() -> int:
    """Pool bound = the agent's ``max_parallel_tasks`` (injected as
    ``TRINITY_MAX_PARALLEL_TASKS``). Clamped to [1, 32]; falls back to the
    default on an unset/garbage value so the pool can never be size 0 or huge."""
    raw = os.getenv("TRINITY_MAX_PARALLEL_TASKS")
    try:
        n = int(raw) if raw else _DEFAULT_POOL_SIZE
    except (TypeError, ValueError):
        n = _DEFAULT_POOL_SIZE
    return max(1, min(n, _MAX_POOL_SIZE))


def _turn_timeout() -> int:
    raw = os.getenv("TRINITY_PULL_TURN_TIMEOUT")
    try:
        n = int(raw) if raw else _DEFAULT_TURN_TIMEOUT
    except (TypeError, ValueError):
        n = _DEFAULT_TURN_TIMEOUT
    return max(1, n)


def _resolve_turn_timeout(overrides: Dict[str, Any]) -> int:
    """The claimed row's own turn budget when the envelope carries one, else the
    pool default (#2317).

    The backend records a concrete ``timeout_seconds`` on every queued row
    (``backlog_service.enqueue`` — the caller's override, or the agent's
    ``execution_timeout_seconds``) and the PUSH path enforces it. Before #2317 the
    claim envelope never carried it and this pool ran every pulled turn to
    ``TRINITY_PULL_TURN_TIMEOUT`` instead, so a row asking for 60s got 900s.
    Fail-soft: a missing / non-numeric / non-positive value falls back to the
    pool default, never raises."""
    raw = overrides.get("timeout_seconds")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _turn_timeout()
    return n if n > 0 else _turn_timeout()


# ---------------------------------------------------------------------------
# Result body (§3.3 reply payload) construction
# ---------------------------------------------------------------------------
# status_code → typed pull error_code (MESSAGE_ENVELOPE_SCHEMA §4; lowercase, the
# code-enum string values). Mirrors result_callback._STATUS_MAP, translated to
# the pull taxonomy: unmapped/empty-result/generic failures fold to agent_error
# so a `failed` terminal always carries a code (§2.4 "required when failed").
_PULL_STATUS_MAP = {
    503: "auth",
    504: "timeout",
    502: "agent_error",
    429: "billing",
    422: "max_turns",
    500: "agent_error",
}


def _success_result_body(
    claim_token: Optional[str], response_text: str, raw_messages, metadata, session_id
) -> Dict[str, Any]:
    md = metadata.model_dump() if hasattr(metadata, "model_dump") else (metadata or {})
    return {
        "claim_token": claim_token,
        "status": "success",
        "content": response_text,
        "error_code": None,
        "cost": md.get("cost_usd"),
        "tokens": md.get("output_tokens"),
        "session_id": session_id,
        "execution_log": raw_messages,
        "metadata": md,
    }


def _failed_result_body_from_http(claim_token: Optional[str], exc: HTTPException) -> Dict[str, Any]:
    """Typed FAILED §3.3 body from the headless executor's HTTPException. The 502
    empty-result path carries a structured dict body with ``metadata`` (#678);
    other paths carry a string detail."""
    detail = exc.detail
    metadata: Dict[str, Any] = {}
    if isinstance(detail, dict):
        error_msg = detail.get("message") or str(detail)[:500]
        if isinstance(detail.get("metadata"), dict):
            metadata = detail["metadata"]
    else:
        error_msg = str(detail)[:500]
    return {
        "claim_token": claim_token,
        "status": "failed",
        "content": error_msg,
        "error_code": _PULL_STATUS_MAP.get(exc.status_code, "agent_error"),
        "cost": None,
        "tokens": None,
        "session_id": None,
        "execution_log": None,
        "metadata": metadata,
    }


def _failed_result_body(claim_token: Optional[str], error: str) -> Dict[str, Any]:
    return {
        "claim_token": claim_token,
        "status": "failed",
        "content": (error or "error")[:500],
        "error_code": "agent_error",
        "cost": None,
        "tokens": None,
        "session_id": None,
        "execution_log": None,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Claim (GET /api/internal/next-task) — §3.1 / §3.2
# ---------------------------------------------------------------------------
async def _claim_once(
    client: httpx.AsyncClient, backend_url: str, mcp_key: str, agent_name: str, worker_id: str
) -> Optional[Dict[str, Any]]:
    """Short-poll one claim. Returns the §3.1 claim dict, or None for an empty
    queue (§3.2 ``{envelope: null}`` or 204) / any non-200 (treated as empty →
    the caller backs off). Raises only on a transport error (caller backs off)."""
    url = f"{backend_url}/api/internal/next-task"
    resp = await client.get(
        url,
        params={"agent_name": agent_name, "worker_id": worker_id},
        headers={"Authorization": f"Bearer {mcp_key}"},
    )
    if resp.status_code == 204:
        return None
    if resp.status_code >= 300:
        logger.debug("[#1081] next-task for %s got %s", worker_id, resp.status_code)
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — malformed body → treat as empty
        return None
    if not isinstance(data, dict) or not data.get("envelope"):
        return None  # §3.2 empty claim
    return data


# ---------------------------------------------------------------------------
# Result (POST /api/internal/tasks/{id}/result) — §3.3 / §3.4
# ---------------------------------------------------------------------------
async def _deliver_result(
    client: httpx.AsyncClient,
    execution_id: str,
    body: Dict[str, Any],
    backend_url: str,
    mcp_key: str,
    deadline_monotonic: float,
) -> bool:
    """POST the §3.3 terminal, retrying with capped decorrelated-jitter backoff
    until the lease deadline. Returns True on a 2xx (delivered) OR a permanent
    4xx (no point retrying); False only when the deadline passed — the backend
    lease reaper then re-queues (same ``execution_id``), so no work is lost.
    Mirrors ``result_callback._deliver``."""
    url = f"{backend_url}/api/internal/tasks/{execution_id}/result"
    headers = {"Authorization": f"Bearer {mcp_key}"}
    prev_sleep = _BACKOFF_BASE
    while True:
        retry_after_floor = 0.0
        try:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code < 300:
                logger.info("[#1081] result for %s delivered (%s)", execution_id, resp.status_code)
                return True
            if resp.status_code in _PERMANENT_STATUSES:
                logger.warning(
                    "[#1081] result for %s permanently rejected (%s) — giving up",
                    execution_id, resp.status_code,
                )
                return True
            # Transient (incl. the #1085 governor 503). Honor Retry-After as a floor.
            retry_after_floor = _parse_retry_after(resp.headers.get("Retry-After"))
            logger.warning("[#1081] result for %s got %s — will retry", execution_id, resp.status_code)
        except Exception:  # noqa: BLE001 — transport error → retry until deadline
            logger.debug("[#1081] result POST for %s failed", execution_id, exc_info=True)

        now = time.monotonic()
        if now >= deadline_monotonic:
            logger.warning(
                "[#1081] result for %s not delivered before lease deadline — "
                "backend lease reaper is the backstop", execution_id,
            )
            return False
        backoff = max(_jittered_backoff(prev_sleep, _RESULT_BACKOFF_CAP), retry_after_floor)
        prev_sleep = min(_RESULT_BACKOFF_CAP, backoff)
        backoff = min(backoff, max(0.0, deadline_monotonic - now))  # never sleep past deadline
        await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# Claim → run → report
# ---------------------------------------------------------------------------
async def _run_and_report(
    claim: Dict[str, Any],
    client: httpx.AsyncClient,
    backend_url: str,
    mcp_key: str,
    agent_name: str,
) -> None:
    """Run a claimed turn through the runtime, then deliver the typed terminal.

    Session handling (DESIGN CHOICE — confirm): the #946 pilot is agent→agent
    stateless (envelope ``payload.session_id`` null ⇒ cold headless turn). When a
    ``session_id`` IS present the worker resumes it (``resume_session_id`` +
    ``persist_session``), matching the SESSION_TAB ``--resume`` convention — the
    minimal reversible reading of the schema's "presence ⇒ persist/resume"."""
    from ..services.runtime_adapter import get_runtime

    execution_id = claim.get("execution_id")
    claim_token = claim.get("claim_token")
    envelope = claim.get("envelope") or {}
    payload = envelope.get("payload") or {}
    message = payload.get("message") or ""
    session_id = payload.get("session_id")
    overrides = payload.get("task_overrides") or {}
    turn_timeout = _resolve_turn_timeout(overrides)

    agent_state.record_task_start()
    try:
        response_text, raw_messages, metadata, ran_session_id = await get_runtime().execute_headless(
            prompt=message,
            model=overrides.get("model"),
            allowed_tools=overrides.get("allowed_tools"),
            system_prompt=overrides.get("system_prompt"),
            timeout_seconds=turn_timeout,
            max_turns=overrides.get("max_turns"),
            execution_id=execution_id,
            resume_session_id=session_id,
            persist_session=bool(session_id),
        )
        body = _success_result_body(claim_token, response_text, raw_messages, metadata, ran_session_id)
        finish_success: Optional[bool] = True
    except asyncio.CancelledError:
        # Shutdown mid-turn — record the finish, let cancellation propagate.
        agent_state.record_task_finish(success=False)
        raise
    except HTTPException as exc:
        finish_success = False
        body = _failed_result_body_from_http(claim_token, exc)
    except Exception as exc:  # noqa: BLE001 — any failure must still report a terminal
        finish_success = False
        body = _failed_result_body(claim_token, str(exc) or type(exc).__name__)

    agent_state.record_task_finish(success=finish_success)

    # B6 fix (#1081): persist the completed terminal to disk BEFORE attempting
    # delivery, so a shutdown/deploy mid-POST doesn't drop already-spent work
    # (the row would stay `running` → the lease reaper re-runs the turn from
    # scratch). If a CancelledError arrives inside _deliver_result below, it
    # propagates before _delete_pull_result is reached, so the file survives and
    # the startup resend delivers it. Gated on a safe execution_id so a hostile
    # value can't escape _PENDING_PULL_DIR; an unsafe/absent id skips persistence
    # (unchanged pre-B6 behaviour — never a path build from untrusted input).
    persisted = _is_safe_execution_id(execution_id)
    if persisted:
        _persist_pull_result(execution_id, {"execution_id": execution_id, "body": body})

    # Deadline = now + (turn timeout + buffer) = the slot-lease TTL window.
    # #2317: keyed to the POOL's budget, not the row's — honouring a short
    # per-task timeout must not also shrink the result-delivery retry window
    # (the backend lease derives from the agent's execution_timeout, not the row's).
    deadline = time.monotonic() + max(turn_timeout, _turn_timeout()) + _SLOT_TTL_BUFFER_SECONDS
    delivered = await _deliver_result(client, execution_id, body, backend_url, mcp_key, deadline)
    if delivered and persisted:
        _delete_pull_result(execution_id)


# ---------------------------------------------------------------------------
# Worker loop + pool wiring
# ---------------------------------------------------------------------------
async def run_worker(worker_id: str, backend_url: str, mcp_key: str, agent_name: str) -> None:
    """One worker: claim → (run + report) → immediately re-claim; back off with
    capped jitter when the queue is empty or the backend is unreachable. Swallows
    every non-cancellation exception so a blip never kills the worker (mirrors
    heartbeat). Because a worker awaits its turn INLINE, a pool of N workers can
    hold at most N concurrent turns — the pool size IS the concurrency bound."""
    logger.info("[#1081] pull worker %s started", worker_id)
    idle = _BACKOFF_BASE
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT) as client:
        while True:
            try:
                claim = await _claim_once(client, backend_url, mcp_key, agent_name, worker_id)
                if claim is not None:
                    idle = _BACKOFF_BASE
                    await _run_and_report(claim, client, backend_url, mcp_key, agent_name)
                    continue  # drain fast under load
                idle = _jittered_backoff(idle, _IDLE_BACKOFF_CAP)
                await asyncio.sleep(idle)
            except asyncio.CancelledError:
                logger.info("[#1081] pull worker %s stopping", worker_id)
                raise
            except Exception:  # noqa: BLE001 — never let a blip kill the worker
                logger.debug("[#1081] pull worker %s loop error", worker_id, exc_info=True)
                idle = _jittered_backoff(idle, _ERROR_BACKOFF_CAP)
                await asyncio.sleep(idle)


def schedule_pull_workers(app) -> None:
    """Attach startup/shutdown handlers that run the pull pool — ONLY when
    PULL_MODE is on for this agent AND the worker creds are present. Default OFF:
    when ``TRINITY_PULL_MODE`` is unset/false this returns immediately, registers
    no startup handler, and no worker ever runs (the provable no-op that keeps
    every existing agent's push path unchanged). Mirrors ``schedule_heartbeat``."""
    if not _pull_mode_enabled():
        logger.info(
            "[#1081] pull mode OFF (TRINITY_PULL_MODE unset/false) — worker pool not started"
        )
        return
    if not _worker_creds_present():
        logger.warning(
            "[#1081] pull mode ON but TRINITY_BACKEND_URL / TRINITY_MCP_API_KEY missing "
            "— worker pool not started"
        )
        return

    tasks_ref: list = []

    @app.on_event("startup")
    async def _start_pull_workers() -> None:
        backend_url = os.getenv("TRINITY_BACKEND_URL")
        mcp_key = os.getenv("TRINITY_MCP_API_KEY")
        agent_name = agent_state.agent_name
        size = _pool_size()
        for i in range(size):
            worker_id = f"{agent_name}#w{i + 1}"
            tasks_ref.append(
                asyncio.create_task(run_worker(worker_id, backend_url, mcp_key, agent_name))
            )
        logger.info("[#1081] pull pool started: %d worker(s) for agent %s", size, agent_name)

    @app.on_event("shutdown")
    async def _stop_pull_workers() -> None:
        for task in tasks_ref:
            task.cancel()
        if not tasks_ref:
            return
        # Bounded drain: let cancellation propagate so each worker unwinds its
        # in-flight turn cleanly (record_task_finish, any persisted terminal is
        # already flushed to disk). The real durability guarantee is the
        # pre-delivery persistence in _run_and_report — a completed body is on
        # disk BEFORE _deliver_result — so this is only a graceful settle, not the
        # safety net. Keep it short; never block shutdown.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks_ref, return_exceptions=True),
                timeout=_SHUTDOWN_DRAIN_SECONDS,
            )
        except Exception:  # noqa: BLE001 — timeout / any error must not block shutdown
            logger.debug("[#1081] pull worker drain did not settle in time", exc_info=True)


# ---------------------------------------------------------------------------
# Startup resend — deliver terminals left on disk by a crash / shutdown (B6)
# ---------------------------------------------------------------------------
async def resend_pending_pull_results() -> None:
    """Best-effort: re-POST every persisted pull terminal once. A turn that
    completed but whose result POST never landed (shutdown/deploy mid-delivery)
    is delivered here — the backend CAS dedups on the ``claim_token`` in the
    persisted body, so a re-delivery can't double-apply. Mirrors
    ``result_callback.resend_pending_results`` (#1083)."""
    if not _worker_creds_present() or not _PENDING_PULL_DIR.exists():
        return
    # #1085 A2: one-shot initial jitter so ~N agents restarting together smear the
    # t≈0 resend burst over a window instead of a synchronized spike on the
    # /api/internal/tasks/{id}/result endpoint.
    await asyncio.sleep(random.uniform(0, _SWEEP_INITIAL_JITTER_SECONDS))
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    try:
        pending = sorted(_PENDING_PULL_DIR.glob("*.json"))
    except Exception:  # noqa: BLE001
        return
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT) as client:
        for path in pending:
            # small per-envelope jitter so one agent's many terminals don't fire
            # back-to-back in a tight loop.
            await asyncio.sleep(random.uniform(0, _SWEEP_PER_ENVELOPE_JITTER_SECONDS))
            execution_id = path.stem
            try:
                record = json.loads(path.read_text())
            except Exception:  # noqa: BLE001 — corrupt/partial file: drop it
                logger.debug("[#1081] dropping unreadable pending pull result %s", execution_id, exc_info=True)
                _delete_pull_result(execution_id)
                continue
            body = record.get("body")
            if not isinstance(body, dict):
                _delete_pull_result(execution_id)
                continue
            deadline = time.monotonic() + _SWEEP_DEADLINE_SECONDS
            delivered = await _deliver_result(client, execution_id, body, backend_url, mcp_key, deadline)
            if delivered:
                _delete_pull_result(execution_id)


def schedule_pending_pull_result_resend(app) -> None:
    """Attach a startup handler that re-sends leftover persisted pull terminals.
    Gated only on worker creds (mirrors ``schedule_pending_result_resend``), NOT
    on ``TRINITY_PULL_MODE``: a completed turn's terminal must still be delivered
    even if the agent's pull flag was flipped off between the crash and the
    restart. For an agent that never ran the pull pool, _PENDING_PULL_DIR simply
    doesn't exist, so ``resend_pending_pull_results`` returns immediately — a
    provable no-op (no worker, no pending files ⇒ nothing to do)."""
    if not _worker_creds_present():
        return

    @app.on_event("startup")
    async def _resend_pull_on_startup() -> None:
        try:
            await resend_pending_pull_results()
        except Exception:  # noqa: BLE001 — never block startup
            logger.debug("[#1081] startup pending pull-result resend failed", exc_info=True)
