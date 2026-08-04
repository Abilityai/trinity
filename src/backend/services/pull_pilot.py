"""Pull-pilot predicates — the LEAF half of ``agent_service/pull_mode.py``.

Answers two questions and nothing else: *is this agent a pull pilot*, and *must
this ``(agent, trigger)`` pair reach it only via the durable queue*.

**Why this module exists separately (#1766).** These predicates are consulted
from the dispatch hot path — ``capacity_manager.acquire`` and
``backlog_service.drain_next`` — but their original home,
``services/agent_service/pull_mode.py``, sits inside a package whose
``__init__.py`` eagerly imports ``helpers``, ``lifecycle``, ``crud``, ``deploy``
and ``terminal``. So ``from services.agent_service.pull_mode import ...`` drags
the entire agent-lifecycle stack (and transitively ``models``) in behind it. In
production that import is already warm and the cost is invisible; under a unit
test that stubs ``models``/``database`` it explodes with a bare
``ImportError: cannot import name 'AgentGitConfig'`` that names nothing to do
with the actual dependency. A capacity/backlog module must not need the agent
CRUD stack to answer "is this name in an env var".

Stdlib-only by construction — keep it that way. ``pull_mode`` re-exports these
names, so every existing importer is unaffected.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Set

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
