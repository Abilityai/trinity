"""Agent-side pipeline-state change watcher (trinity-enterprise#533).

The #919 contract is a pair of files the **agent** publishes in its own
container — Trinity is a read surface over them and owns no DAG (CLAUDE.md
Rule #8). That makes the agent the only party that can know when a stage
advanced: the backend can only ask, and asking faster is exactly the load the
Work read's cache exists to bound. So this loop watches
``~/.trinity/pipeline-state/`` and tells the backend *a file changed* — one
fact, no pipeline semantics.

Mirrors ``heartbeat.py`` deliberately, down to its contract: sleeps first,
never raises out of the loop, swallows every exception (a backend blip must
never touch the agent), and is gated on ``TRINITY_BACKEND_URL`` +
``TRINITY_MCP_API_KEY`` both being present — so an old image or a
mis-provisioned agent simply never notifies, and the Workspace card falls back
to its 12 s poll exactly as it did before this existed.

Steady state costs one ``os.scandir`` per second and **no network at all**: the
canonical writer (`abilities` → `agent-dev:add-pipeline`) ticks every 15
minutes, so a POST is an event, not a stream.

The id pre-filter here is convenience, not a security boundary — the backend
re-validates every id with the same grammar before anything is published, and
a mismatch fails safe (422 → a debug line → the poll covers it).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .config import TRINITY_DIR
from .state import agent_state

logger = logging.getLogger(__name__)

#: Where the agent publishes instance state (#919).
STATE_DIR: Path = TRINITY_DIR / "pipeline-state"

_TICK_SECONDS = 1.0
_POST_TIMEOUT = 2  # seconds — keep the notice cheap; never block the agent

# Bounds on a scan that runs forever. A pathological tree costs a bounded
# number of stat calls, not an unbounded walk.
_MAX_DIRS = 64
_MAX_FILES_PER_DIR = 64
#: Notices sent in one tick. A burst is coalesced by the backend too (20/10 s
#: per agent); this keeps the agent from spending the whole budget at once.
_MAX_POSTS_PER_TICK = 4
#: Never read more than this to pull one `current_stage` out of a state file.
_MAX_STAGE_READ_BYTES = 256 * 1024
#: The `pipelines.ts` grammar, re-stated (the backend is the authority).
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
#: `current_stage` is free-form by schema, so it is bounded, not validated.
_MAX_STAGE_CHARS = 80

#: (pipeline_id, instance_id) -> (st_mtime_ns, st_size)
Signature = Dict[Tuple[str, str], Tuple[int, int]]


def _valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value)) and ".." not in value


def snapshot() -> Signature:
    """One level of the state dir, as ``{(pipeline, instance): (mtime_ns, size)}``.

    A directory is a pipeline, a ``*.json`` file inside it is an instance.
    Everything else — a stray file at the top level, a nested directory, a
    non-JSON name, an id the grammar rejects, an entry that vanished between
    the listing and the ``stat`` — is dropped here. Returns ``{}`` rather than
    raising for a missing directory (the common case: most agents run no
    pipeline at all) or for any other filesystem error.
    """
    out: Signature = {}
    try:
        with os.scandir(STATE_DIR) as pipelines:
            for pipe_index, pipe in enumerate(pipelines):
                if pipe_index >= _MAX_DIRS:
                    break
                try:
                    if not pipe.is_dir() or not _valid_id(pipe.name):
                        continue
                    with os.scandir(pipe.path) as instances:
                        for file_index, entry in enumerate(instances):
                            if file_index >= _MAX_FILES_PER_DIR:
                                break
                            try:
                                name = entry.name
                                if not name.endswith(".json") or not entry.is_file():
                                    continue
                                instance_id = name[: -len(".json")]
                                if not _valid_id(instance_id):
                                    continue
                                stat = entry.stat()
                                out[(pipe.name, instance_id)] = (stat.st_mtime_ns, stat.st_size)
                            except OSError:
                                continue
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except Exception:  # noqa: BLE001 — a watcher must never raise into the loop
        logger.debug("pipeline-state watch: scan failed", exc_info=True)
        return {}
    return out


def changed(prev: Signature, cur: Signature) -> List[Tuple[str, str]]:
    """Keys that are new or whose ``(mtime_ns, size)`` moved.

    Both halves of the signature matter. The canonical writer copies its
    atomically-written state onto the read surface with a plain ``cp``
    (truncate + write), so a reader can catch the file mid-copy; because the
    size keeps moving, the next tick fires **again** and the stale/truncated
    read heals itself.

    A **deletion is not a change**. Removing an instance is not a stage
    advance, and reporting it would let a cleanup pass storm the backend for
    no card update at all.
    """
    return [key for key, sig in cur.items() if prev.get(key) != sig]


def read_stage(path: Path) -> Optional[str]:
    """``current_stage`` from one state file, bounded — or ``None``.

    Best-effort by contract: a missing, oversized, truncated, non-JSON or
    non-dict file is ``None``, and the notice still goes out (the stage is a
    convenience for the card, the *fact of the change* is the payload). The
    value rides a fleet-wide channel, so it is trimmed and length-bounded here
    as well as on the backend.
    """
    try:
        if path.stat().st_size > _MAX_STAGE_READ_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — agent-written, any shape, any moment
        return None
    if not isinstance(document, dict):
        return None
    stage = document.get("current_stage")
    if not isinstance(stage, str):
        return None
    stage = stage.strip()
    if not stage or len(stage) > _MAX_STAGE_CHARS:
        return None
    return stage


async def _post_change_once(
    client: httpx.AsyncClient,
    backend_url: str,
    mcp_key: str,
    agent_name: str,
    pipeline_id: str,
    instance_id: str,
    stage: Optional[str],
) -> None:
    """POST one change notice on the shared client. Raises on transport error
    (the caller swallows it)."""
    url = f"{backend_url}/api/agents/{agent_name}/pipeline-state/changed"
    resp = await client.post(
        url,
        json={"pipeline_id": pipeline_id, "instance_id": instance_id, "stage": stage},
        headers={"Authorization": f"Bearer {mcp_key}"},
    )
    # Not a transport error, so the loop would not otherwise see it. Debug-log
    # it (the heartbeat's rule) so a stuck auth/provisioning bug is diagnosable
    # without changing the silent-by-design contract: never raise, never retry.
    if resp.status_code >= 400:
        logger.debug("pipeline-state watch: backend returned %s", resp.status_code)


async def run_pipeline_state_watch_loop(interval: float = _TICK_SECONDS) -> None:
    """Background loop. Swallows every exception so it can never die."""
    backend_url = os.getenv("TRINITY_BACKEND_URL")
    mcp_key = os.getenv("TRINITY_MCP_API_KEY")
    agent_name = agent_state.agent_name
    if not backend_url or not mcp_key:
        logger.info(
            "pipeline-state watch: TRINITY_BACKEND_URL / TRINITY_MCP_API_KEY missing — not starting"
        )
        return
    logger.info("pipeline-state watch started (tick=%ss, agent=%s)", interval, agent_name)

    # Sleep first, then take the baseline SILENTLY: on a restart every instance
    # on disk is "new" to this process, and announcing them would turn every
    # container recreate into a burst of phantom stage advances.
    await asyncio.sleep(interval)
    previous = snapshot()

    async with httpx.AsyncClient(timeout=_POST_TIMEOUT) as client:
        while True:
            try:
                current = snapshot()
                for pipeline_id, instance_id in changed(previous, current)[:_MAX_POSTS_PER_TICK]:
                    stage = read_stage(STATE_DIR / pipeline_id / f"{instance_id}.json")
                    try:
                        await _post_change_once(
                            client, backend_url, mcp_key, agent_name,
                            pipeline_id, instance_id, stage,
                        )
                    except Exception:  # noqa: BLE001 — one bad POST, not the loop
                        logger.debug("pipeline-state watch: POST failed", exc_info=True)
                # Advance past everything seen this tick, including keys whose
                # notice was capped away: the next real write moves them again,
                # and the 12 s poll covers the gap. Retrying them instead would
                # let one noisy pipeline monopolise the per-tick budget.
                previous = current
            except Exception:  # noqa: BLE001 — silent by design; the loop must live
                logger.debug("pipeline-state watch: tick failed", exc_info=True)
            await asyncio.sleep(interval)


def schedule_pipeline_state_watch(app) -> None:
    """Attach startup/shutdown handlers, gated on backend URL + MCP key."""
    if not (os.getenv("TRINITY_BACKEND_URL") and os.getenv("TRINITY_MCP_API_KEY")):
        logger.info(
            "pipeline-state watch disabled (needs TRINITY_BACKEND_URL + TRINITY_MCP_API_KEY)"
        )
        return

    task_ref: list = []

    @app.on_event("startup")
    async def _start_pipeline_state_watch() -> None:
        task_ref.append(asyncio.create_task(run_pipeline_state_watch_loop()))

    @app.on_event("shutdown")
    async def _stop_pipeline_state_watch() -> None:
        for task in task_ref:
            task.cancel()
