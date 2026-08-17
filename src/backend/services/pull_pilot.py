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


# Which autonomous triggers can STRUCTURALLY reach the durable queue (#2048).
#
# This is dispatch topology, not policy. ``capacity_manager.acquire`` can only
# hand a row to the queue when its producer passed
# ``overflow_policy="queue_persistent"``, and exactly one of the three producers
# does:
#
#   task_execution_service   → "reject"            — scheduler/ALL cron, fan-out
#   dispatch_admission_svc   → "queue_in_memory"   — sequential chat, human chat
#   chat_execution_service   → "queue_persistent"  — POST /task            ← only
#
# ``POST /task`` derives its trigger in ``_derive_task_trigger``, which can only
# produce ``{self_task, agent, mcp, manual, event}``. Intersect that with the
# autonomous set and two survive: ``agent`` (agent-to-agent ``chat_with_agent``)
# and ``event`` (#1578's task-completion loopback, which POSTs to the same route
# under an internal-secret-gated ``X-Event-Trigger``).
#
# ``schedule``, ``webhook``, ``loop``, ``fan_out`` and ``reminder`` are declared
# autonomous but reach dispatch through the ``"reject"`` producer, so on a
# cron-driven agent the pilot flag is INERT. That was previously invisible:
# ``pull_owns_dispatch`` is consulted behind an ``overflow_policy ==
# "queue_persistent"`` short-circuit, so for a cron row it is never called at
# all, and the row takes the push path indistinguishably from the flag being
# unset. Naming the reachable subset here is what makes the gap legible; see
# ``note_unreachable_pull_trigger`` for the runtime signal at the other producer.
#
# Intersected with ``_AUTONOMOUS_TRIGGERS`` rather than replacing it, so this
# stays a NARROWING of the single source of truth: dropping a trigger there
# still drops it here, and widening reach is a deliberate edit to this set.
PULL_REACHABLE_TRIGGERS = frozenset({"agent", "event"})


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

        # Narrowed to what dispatch can actually deliver (#2048). A no-op on
        # today's runtime — this predicate is only consulted behind an
        # ``overflow_policy == "queue_persistent"`` check, and that producer can
        # only emit ``{agent, event}`` from the autonomous set — but it stops the
        # predicate from *claiming* reach it does not have. See
        # ``PULL_REACHABLE_TRIGGERS``.
        return triggered_by in (_AUTONOMOUS_TRIGGERS & PULL_REACHABLE_TRIGGERS)
    except Exception:  # noqa: BLE001 — unresolvable trigger set ⇒ push, as today
        logger.warning(
            "[#1766] could not resolve the autonomous-trigger set for %s "
            "(trigger=%r); falling back to push dispatch",
            agent_name, triggered_by,
        )
        return False


# One log line per (agent, trigger) per process. The producer below runs on
# every cron fire, so an un-deduped warning would emit thousands of identical
# lines a day and train operators to filter it out — which is how a signal
# meant to be noticed becomes noise. Unbounded growth is not a concern: the key
# space is (pilot agents × 5 unreachable triggers), and only pilots ever reach it.
_UNREACHABLE_NOTED: Set[tuple] = set()


def note_unreachable_pull_trigger(agent_name: str, triggered_by: Optional[str]) -> bool:
    """Record that a PILOT agent's autonomous row is being pushed because its
    producer cannot reach the durable queue. Returns True when a line was
    emitted. Never raises (#2048).

    This is the observability half of the issue: before it, a pushed autonomous
    row on a pilot agent had two indistinguishable causes — the flag was not
    applied, or the trigger structurally cannot be pulled — and BOTH were
    silent. ``PULL_MIGRATION_TESTING.md`` §9 M1 told operators that a pushed
    autonomous row means "the producer gate did not engage — check the backend
    actually has ``PULL_MODE_PILOT_AGENTS`` in its env", which for a ``schedule``
    row sends them hunting for an env var that is present and correct.

    Deliberately a log line and not a metric or an operator-queue item: this is
    a property of the build, not an incident. It is constant for a given
    (agent, trigger) until the dispatch topology changes, so alerting on it
    would fire forever on any cron-driven pilot. It exists so that the ONE
    operator who runs M1, sees ~0 ``pulled``, and asks why gets a truthful
    answer from the backend log instead of a wrong one from the runbook.

    Fail-safe like everything else here: a bookkeeping error must never
    interfere with dispatching a real execution.
    """
    try:
        if not is_pull_pilot_agent(agent_name):
            return False
        from services.task_execution_service import _AUTONOMOUS_TRIGGERS

        # Only the genuinely-stranded case. A reachable trigger arriving here is
        # not stranded — it simply took a producer that had a slot free.
        if triggered_by not in _AUTONOMOUS_TRIGGERS:
            return False
        if triggered_by in PULL_REACHABLE_TRIGGERS:
            return False

        key = (agent_name, triggered_by)
        if key in _UNREACHABLE_NOTED:
            return False
        _UNREACHABLE_NOTED.add(key)

        logger.warning(
            "[#2048] pull pilot %r: trigger %r is autonomous but CANNOT reach the "
            "durable queue — this producer dispatches with overflow_policy='reject', "
            "so the row is pushed regardless of PULL_MODE_PILOT_AGENTS. The flag is "
            "applied and correct; the dispatch topology is the limit. Reachable "
            "triggers today: %s. A cron-only agent is not a viable soak pilot "
            "(#1766) — see docs/testing/PULL_MIGRATION_TESTING.md §9.",
            agent_name, triggered_by, sorted(PULL_REACHABLE_TRIGGERS),
        )
        return True
    except Exception:  # noqa: BLE001 — diagnostics must never break dispatch
        return False
