"""
Agent-side liveness heartbeat (RELIABILITY-004 / #307).

Each running agent POSTs a lightweight heartbeat to the backend every ~5s so
the backend can detect a crashed container in <=15s instead of waiting up to
30s for the next monitoring poll. Mirrors the auto_sync.py background-loop
pattern: sleeps-first, infinite loop, swallows *all* exceptions so a backend
blip can never kill the loop (the heartbeat failing silently is by design — the
backend's N-consecutive-miss guard + soft `degraded` downgrade keep a false
positive recoverable).

Authenticated with the agent's own injected ``TRINITY_MCP_API_KEY`` (Option B,
least-privilege) — no master internal secret is injected into agents. The loop
is gated on both ``TRINITY_BACKEND_URL`` and ``TRINITY_MCP_API_KEY`` being
present, so old-image / mis-provisioned agents simply never heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from .state import agent_state

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 5  # seconds
_POST_TIMEOUT = 2  # seconds — keep the beat cheap; never block the agent
_STATUS_PATH = Path("/proc/self/status")

# Module-load baseline for uptime. monotonic() is immune to wall-clock jumps.
_START_MONOTONIC = time.monotonic()


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------
def _parse_vmrss(status_text: str) -> Optional[float]:
    """Extract resident memory (MB) from /proc/self/status text. None on miss."""
    for line in status_text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            # Format: "VmRSS:\t   53124 kB"
            if len(parts) >= 2:
                try:
                    return int(parts[1]) / 1024.0
                except (ValueError, IndexError):
                    return None
            return None
    return None


def _read_memory_mb() -> Optional[float]:
    """Resident memory in MB via /proc/self/status (psutil is not installed)."""
    try:
        return _parse_vmrss(_STATUS_PATH.read_text())
    except Exception:  # noqa: BLE001 — non-Linux / unreadable -> just omit it
        logger.debug("heartbeat: could not read VmRSS", exc_info=True)
        return None


def _list_running() -> List:
    """Indirection so the active-execution count is unit-testable."""
    from .services.process_registry import get_process_registry
    return get_process_registry().list_running()


def _count_active_executions() -> int:
    try:
        return len(_list_running())
    except Exception:  # noqa: BLE001 — registry hiccup -> report 0, never crash
        logger.debug("heartbeat: active-execution count failed", exc_info=True)
        return 0


# trinity-enterprise#620: the per-execution activity the Work card shows.
# Bounded here AND re-validated by the backend model; the backend never trusts
# these counts. 120 chars matches the Work read's title bound.
ACTIVITY_MAX_EXECUTIONS = 20
ACTIVITY_SUMMARY_MAX = 120


def _executions_activity() -> list:
    """One entry per RUNNING execution: `{execution_id, tool, summary, since}`.

    `tool` is the display name (`Read`, `Bash`, `mcp:trinity`, `Task:explore`)
    and `summary` the bounded human input summary the activity tracker
    already builds (`get_input_summary`: `.../a/b`, `"pattern"`, `cmd[:50]…`).
    `tool: None` means the run is between tools — the card says "Thinking".
    The list is INTERSECTED with the process registry, so a finished run drops
    out on the next beat by construction (never a stale line, AC #4), and the
    tracker's slot for a run the registry no longer knows is pruned here.
    """
    try:
        running = _list_running()
    except Exception:  # noqa: BLE001
        return []
    try:
        from .services.activity_tracking import execution_activity, forget_execution
        from .state import agent_state
        known = set(agent_state.session_activity.get("by_execution", {}).keys())
    except Exception:  # noqa: BLE001
        return []
    # Prune against EVERY running id, not the capped slice: a 21st concurrent
    # execution is still alive, and forgetting its slot here would reset it to
    # "Thinking" on every beat for as long as the fleet stays that busy.
    live_ids = [e.get("execution_id") for e in running if isinstance(e.get("execution_id"), str) and e.get("execution_id")]
    out = []
    for entry in running[:ACTIVITY_MAX_EXECUTIONS]:
        eid = entry.get("execution_id")
        if not isinstance(eid, str) or not eid:
            continue
        slot = execution_activity(eid) or {}
        summary = slot.get("input_summary")
        if isinstance(summary, str) and len(summary) > ACTIVITY_SUMMARY_MAX:
            summary = summary[: ACTIVITY_SUMMARY_MAX - 1] + "…"
        out.append({
            "execution_id": eid,
            "tool": slot.get("tool"),
            "summary": summary if isinstance(summary, str) else None,
            "since": slot.get("since") or entry.get("started_at"),
        })
    for stale in known - set(live_ids):
        forget_execution(stale)
    return out


def _build_payload() -> Dict:
    """Lightweight liveness payload. The backend stamps its own receive `ts`."""
    return {
        "memory_mb": _read_memory_mb(),
        "active_executions": _count_active_executions(),
        "uptime_s": time.monotonic() - _START_MONOTONIC,
        "executions": _executions_activity(),
    }


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------
async def _post_heartbeat_once(
    client: httpx.AsyncClient, backend_url: str, mcp_key: str, agent_name: str
) -> None:
    """Build + POST one heartbeat on the shared client. Raises on transport
    error (caller swallows)."""
    payload = _build_payload()
    url = f"{backend_url}/api/agents/{agent_name}/heartbeat"
    resp = await client.post(
        url, json=payload, headers={"Authorization": f"Bearer {mcp_key}"}
    )
    # A non-2xx (e.g. 403 from a mis-provisioned key) is not a transport error,
    # so the outer loop wouldn't otherwise notice it. Surface it at debug so a
    # stuck-forever auth/provisioning bug is diagnosable — without changing the
    # silent-by-design contract (we never raise or retry on it).
    if resp.status_code >= 400:
        logger.debug("heartbeat: backend returned %s", resp.status_code)


# ---------------------------------------------------------------------------
# Loop + wiring
# ---------------------------------------------------------------------------
async def run_heartbeat_loop(interval: int = _DEFAULT_INTERVAL) -> None:
    """Background loop. Swallows every exception to keep beating.

    Reuses a single ``AsyncClient`` across beats so a 5s cadence doesn't pay
    TCP setup/teardown every tick — one warm keep-alive connection to the
    backend instead of a fresh handshake 12x/min.
    """
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    agent_name = agent_state.agent_name
    # schedule_heartbeat() already gates on both being present; narrow here too
    # so a direct call can't NoneType the POST and the values type as `str`.
    if not backend_url or not mcp_key:
        logger.info(
            "heartbeat loop: TRINITY_BACKEND_URL / TRINITY_MCP_API_KEY missing — not starting"
        )
        return
    logger.info("heartbeat loop started (interval=%ss, agent=%s)", interval, agent_name)

    # Sleep first so a just-started container isn't penalized by a beat that
    # races container init.
    await asyncio.sleep(interval)
    async with httpx.AsyncClient(timeout=_POST_TIMEOUT) as client:
        while True:
            try:
                await _post_heartbeat_once(client, backend_url, mcp_key, agent_name)
            except Exception:  # noqa: BLE001 — silent by design; loop must not die
                logger.debug("heartbeat: POST failed", exc_info=True)
            await asyncio.sleep(interval)


def schedule_heartbeat(app) -> None:
    """Attach startup/shutdown handlers, gated on backend URL + MCP key."""
    if not (os.getenv("TRINITY_BACKEND_URL") and os.getenv("TRINITY_MCP_API_KEY")):
        logger.info(
            "heartbeat disabled (needs TRINITY_BACKEND_URL + TRINITY_MCP_API_KEY)"
        )
        return

    task_ref: list = []

    @app.on_event("startup")
    async def _start_heartbeat() -> None:
        task_ref.append(asyncio.create_task(run_heartbeat_loop()))

    @app.on_event("shutdown")
    async def _stop_heartbeat() -> None:
        for task in task_ref:
            task.cancel()
