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


# Which autonomous triggers can STRUCTURALLY reach the durable queue
# (#2048 named the set; #2391 widened it).
#
# This is dispatch topology, not policy. ``capacity_manager.acquire`` can only
# hand a row to the queue when its producer passed
# ``overflow_policy="queue_persistent"``, and two of the three producers do:
#
#   task_execution_service   → "reject" | "queue_persistent"  ← #2391, pilot-gated
#                              scheduler/ALL cron, webhooks, reminders, loops,
#                              fan-out, A2A, operator resumes
#   dispatch_admission_svc   → "queue_in_memory"   — sequential chat, human chat
#   chat_execution_service   → "queue_persistent"  — POST /task
#
# ``POST /task`` derives its trigger in ``_derive_task_trigger``, which can only
# produce ``{self_task, agent, mcp, manual, event}``; from the autonomous set
# that contributes ``agent`` (agent-to-agent ``chat_with_agent``) and ``event``
# (#1578's task-completion loopback, which POSTs to the same route under an
# internal-secret-gated ``X-Event-Trigger``).
#
# ``task_execution_service`` contributes the autonomous triggers with **no
# synchronous result consumer**. ``schedule``, ``webhook`` and ``reminder``
# (#2391) reach it from the scheduler, which dispatches with ``async_mode=True``
# and then polls ``schedule_executions`` for the terminal, so nobody holds a
# coroutine waiting on a return value — the same property
# ``ASYNC_DISPATCH_ELIGIBLE_TRIGGERS`` (#1083) selects on. ``loop`` (#2523) and
# ``fan_out`` (#2524) joined them once their orchestrators stopped holding the
# work in a coroutine: a loop is advanced by execution terminals, and a fan-out
# batch's aggregate is a query over ``fan_out_id``. Neither dispatch has a
# reader any more.
#
# ``a2a`` and ``operator_response`` joined in #2524 too, through
# ``task_execution_service.dispatch_and_await_terminal``. Both have a caller that
# genuinely needs the answer in-line — ``routers/a2a`` turns ``result.response``
# into the JSON-RPC artifact it hands a remote caller (ent#157), and
# ``operator_resume_service`` records ``result.status`` as the dispatch receipt
# for a turn that spends money on a person's answer (ent#329). Neither needs a
# receipt to poll; each needs to BLOCK CORRECTLY while the turn happens
# elsewhere, which the adapter does by waiting out the queue and rebuilding the
# result from the row.
#
# **The set is currently equal to ``_AUTONOMOUS_TRIGGERS``, and it stays an
# explicit allow-list anyway.** That is the point of it: a trigger added to the
# autonomous set later must be reviewed against dispatch topology rather than
# inheriting reach by default. ``note_unreachable_pull_trigger`` below is the
# runtime half of the same guard and is, deliberately, currently unreachable.
#
# Intersected with ``_AUTONOMOUS_TRIGGERS`` rather than replacing it, so this
# stays a NARROWING of the single source of truth: dropping a trigger there
# still drops it here, and widening reach is a deliberate edit to this set.
PULL_REACHABLE_TRIGGERS = frozenset(
    {"agent", "event", "schedule", "webhook", "reminder", "loop", "fan_out",
     "a2a", "operator_response"}
)


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

        # Narrowed to what dispatch can actually deliver. Since #2391 this
        # predicate is also the PRODUCER-SIDE gate: ``task_execution_service``
        # asks it whether to dispatch with ``overflow_policy="queue_persistent"``
        # instead of ``"reject"``, so a False here is the exact condition under
        # which scheduled capacity semantics stay byte-for-byte as they were.
        # See ``PULL_REACHABLE_TRIGGERS`` for why each trigger is in or out.
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
# space is (pilot agents × unreachable triggers, currently zero), and only
# pilots ever reach it.
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
    actually has ``PULL_MODE_PILOT_AGENTS`` in its env", which for a stranded
    row sends them hunting for an env var that is present and correct.

    Deliberately a log line and not a metric or an operator-queue item: this is
    a property of the build, not an incident. It is constant for a given
    (agent, trigger) until the dispatch topology changes, so alerting on it
    would fire forever on any pilot that runs loops or fan-out. It exists so
    that the ONE operator who runs M1, sees a pushed autonomous row, and asks
    why gets a truthful answer from the backend log instead of a wrong one from
    the runbook.

    #2391, #2523 and #2524 emptied the stranded set entirely, so this is
    currently **unreachable by design** — kept as the runtime half of the
    ``PULL_REACHABLE_TRIGGERS`` allow-list, for the trigger somebody adds to
    ``_AUTONOMOUS_TRIGGERS`` next without checking whether dispatch can deliver
    it. A silently-stale reach set is exactly the bug #2048 was.

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
            "durable queue — its caller consumes the execution result synchronously, "
            "so this row is dispatched with overflow_policy='reject' and pushed "
            "regardless of PULL_MODE_PILOT_AGENTS. The flag is applied and correct; "
            "the dispatch topology is the limit. Reachable triggers today: %s. "
            "Every autonomous trigger is pullable as of #2524, so seeing this "
            "at all means a NEW trigger was added without being classified — "
            "see docs/testing/PULL_MIGRATION_TESTING.md §9.",
            agent_name, triggered_by, sorted(PULL_REACHABLE_TRIGGERS),
        )
        return True
    except Exception:  # noqa: BLE001 — diagnostics must never break dispatch
        return False
