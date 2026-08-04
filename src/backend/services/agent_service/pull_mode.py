"""Per-agent PULL_MODE opt-in (#946 / #1081 Phase 2).

Decides whether an agent runs the agent-side pull worker pool
(``docker/base-image/agent_server/services/pull_worker.py``) and builds the env
vars that turn it on. **Default OFF.** An agent opts in ONLY when its name is in
the fleet allowlist env ``PULL_MODE_PILOT_AGENTS`` (comma-separated). Not in the
allowlist ⇒ ``pull_mode_env_vars`` returns ``{}`` ⇒ no vars injected ⇒ the
agent-server gate stays OFF ⇒ the existing push path is byte-for-byte unchanged.

**Reconciliation with #1293** (``MCP_AGENT_CHAT_PULL_ENABLED``): that flag is the
global **producer**-routing switch (how agent→agent chat is *enqueued*, decided in
the MCP server). This is the orthogonal per-agent **consumer** switch (whether a
given agent *pulls* queued work). They live at different layers (global MCP env vs
one agent's container env) and gate different sides of the same durable queue, so
they are deliberately separate flags — not a duplicated one.

**Auth (least-privilege, #307/#1159).** The worker authenticates to the two pull
seams (``/api/internal/next-task`` + ``/api/internal/tasks/{id}/result``) with the
agent's OWN agent-scoped MCP key — already injected into every agent as
``TRINITY_MCP_API_KEY`` — mirroring ``heartbeat.py`` / ``result_callback.py``. The
seams accept that scoped key (validated via ``authorize_heartbeat``) as an
alternate to the internal secret, so the master ``INTERNAL_API_SECRET`` is NEVER
injected into an agent container: a compromised pilot can only claim/report for
ITSELF, never reach the other ``/api/internal/*`` endpoints. This module injects
ONLY the two non-secret pull knobs below.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


def _pilot_allowlist() -> Set[str]:
    raw = os.getenv("PULL_MODE_PILOT_AGENTS", "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def is_pull_pilot_agent(agent_name: str) -> bool:
    """True when ``agent_name`` is in the ``PULL_MODE_PILOT_AGENTS`` allowlist."""
    return agent_name in _pilot_allowlist()


def pull_owns_dispatch(agent_name: str, triggered_by: Optional[str]) -> bool:
    """True when this ``(agent, trigger)`` pair must reach the agent ONLY by the
    agent claiming it from the durable queue — the backend neither pushes it nor
    drains it (#1766, the pilot-scoped slice of #1081 Phase 5 "capacity becomes
    physical").

    Without this, a pilot agent runs BOTH systems at once: the backend still
    admits-and-pushes whenever a slot is free (``acquire`` had no pilot branch),
    so a row only ever queued on overflow, and the backend's own
    ``backlog_service.drain_next`` then raced the agent's worker for it. Two
    independent capacity counters (Redis ZSET vs the container's worker pool)
    meant a pilot could run up to 2x ``max_parallel_tasks`` — invisible to canary
    S-02, which counts ``ZCARD`` only. Making the pilot flag a true either/or
    restores one capacity owner per agent.

    **Interactive turns are deliberately excluded.** Only the autonomous trigger
    set queues; a human chat / Session-tab turn keeps today's synchronous push
    path and today's Redis session lock. That is the scope cut in
    ``TARGET_ARCHITECTURE.md`` Open Question 7 ("Does human-interactive chat
    belong in the queue at all?" — *under consideration, not decided*), and it is
    load-bearing here: one FIFO ordered by ``queued_at`` would park a human turn
    behind N batch tasks until the held connection timed out, and N competing
    workers could claim two turns of the same session concurrently — the exact
    concurrent ``--resume`` on one JSONL the session lock exists to prevent.

    Fail-safe: any error resolving the trigger set returns ``False``, i.e. the
    unchanged push behaviour. The dangerous direction would be silently claiming
    dispatch for a trigger we could not classify.
    """
    if not is_pull_pilot_agent(agent_name):
        return False
    try:
        # Lazy: task_execution_service imports the capacity stack, and this is
        # called from inside it. Single source of truth for the trigger set —
        # never a second copy that can drift.
        from services.task_execution_service import _AUTONOMOUS_TRIGGERS

        return triggered_by in _AUTONOMOUS_TRIGGERS
    except Exception:  # noqa: BLE001 — unresolvable trigger set ⇒ push, as today
        logger.warning(
            "[#1766] could not resolve the autonomous-trigger set for %s "
            "(trigger=%r); falling back to push dispatch",
            agent_name, triggered_by,
        )
        return False


# The container env keys this module manages. Recreate (``lifecycle.py``) pops
# these BEFORE re-applying ``pull_mode_env_vars`` so de-piloting an agent
# actually clears the baked pull flag: ``pull_mode_env_vars`` returns ``{}`` for
# a non-pilot, so a bare ``.update()`` would leave a stale ``TRINITY_PULL_MODE=
# true`` baked in and the worker would keep running after de-pilot (#1081 B1 —
# mirrors the guardrails / stall-limit set-or-clear idiom in ``lifecycle.py``).
PULL_MODE_ENV_KEYS = ("TRINITY_PULL_MODE", "TRINITY_MAX_PARALLEL_TASKS")


def pull_mode_env_vars(agent_name: str) -> Dict[str, str]:
    """Env vars to inject into an opted-in pilot agent's container. Empty dict for
    every non-pilot agent (the default) — the safety property the agent-server
    gate relies on. ``.update()`` this into the container ``env_vars`` at create
    (``crud.py``) and recreate (``lifecycle.py``).

    Deliberately NON-secret: the worker authenticates with the agent's own
    ``TRINITY_MCP_API_KEY`` (already injected by the platform), so the master
    internal secret is never placed in an agent container (#307/#1159)."""
    if not is_pull_pilot_agent(agent_name):
        return {}

    # Pool bound = the agent's effective max_parallel_tasks (ceiling-clamped).
    try:
        from services.settings_service import get_effective_max_parallel_tasks

        size = get_effective_max_parallel_tasks(agent_name)
    except Exception:  # noqa: BLE001 — never block agent creation on a settings read
        size = 3

    return {
        "TRINITY_PULL_MODE": "true",
        "TRINITY_MAX_PARALLEL_TASKS": str(size),
    }
