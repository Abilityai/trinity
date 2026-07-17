# Feature: System-Emitted Task Completion Events (#1578)

## Overview

The **backend** deterministically emits `agent.task.completed` / `agent.task.failed`
at **every CAS-won execution terminal**, delivered over the existing EVT-001
subscription-dispatch machinery. A caller/orchestrator that subscribed to a worker's
`agent.task.*` events is **woken with an automatic report-back task** when a long async
task finishes — instead of polling `get_execution_result`. Implements the missing half
of `TARGET_ARCHITECTURE.md` §Async-First Communication and is a down-payment on Epic
#1045 → #1081 (pull coordination).

**Additive & inert by default:** zero matching subscriptions ⇒ zero `agent_events`
rows, zero dispatch, unchanged behavior. No schema change, no migration, no feature
flag, no new config.

## System-emitted vs agent-emitted (the core distinction)

EVT-001 ([agent-event-subscriptions.md](agent-event-subscriptions.md)) carries only
**agent-emitted** events: an agent's LLM calls the `emit_event` tool, and the emit route
derives `source_agent` from the MCP auth context. Every event before #1578 was "an
agent's LLM decided to emit."

These are the first **system-emitted** events: synthesized by the deterministic backend
chokepoint with **no LLM in the loop**, `source_agent` = the executing agent, in the
reserved `agent.task.*` namespace. Same tables (`agent_events`,
`agent_event_subscriptions`), same `find_matching → trigger_subscription` delivery;
different producer.

## User Story

As an orchestrator agent, I dispatch a long task to a worker
(`chat_with_agent(parallel=true, async=true)`), my turn ends, and the worker finishes
minutes later. I want to be **woken with a report-back task** carrying the worker's
outcome — so I can report to the human — instead of holding a coroutine or polling.

## Entry Points

- **Producer (backend, deterministic):** every CAS-won terminal writer — no user-facing
  entry. See the coverage table below.
- **Consumer:** `subscribe_to_event(source_agent=<worker>, event_type="agent.task.completed" | "agent.task.failed", target_message="…{{payload.status}}…")`
  (MCP) or `POST /api/agents/{orchestrator}/event-subscriptions`. Requires the standard
  EVT-001 permission (subscriber must be permitted to call the source agent). Cross-agent
  only — self-subscription to `agent.task.*` is blocked.

## Terminal-writer coverage (the core of the fix)

Every terminal writer gates on a `db.update_execution_status(...)` CAS returning a `won`
bool. The shared helper is invoked **only on `won`**, fire-and-forget, fail-open. This is
the invariant: any *future* terminal writer that bypasses the helper silently won't emit.

| Terminal writer | Terminals | File | Emits |
|---|---|---|---|
| `apply_result` SUCCESS branch | success | `services/task_execution_service.py` (won-only side effects) | `agent.task.completed` |
| `apply_result` failure branch | agent-HTTP-error, CANCELLED, SKILL_NOT_FOUND | same (`if won:`) | `agent.task.failed` |
| `_write_terminal_and_gate` | **timeout, budget-exhausted, unexpected-exception** (+ circuit-open/capacity/ephemeral inline) | `services/task_execution_service.py` (`if won and agent_name:`) | `agent.task.failed` |
| #1083 lease-reaper (async died) | LEASE_EXPIRED | `services/cleanup_service.py::_process_stale_slot_reclaims` (both `if updated:` sites) | `agent.task.failed` |
| pull sink `apply_task_result` | success/failure | `services/pull_coordination_service.py` (`if won:`) — **dark** until a pull pilot | completed/failed |
| bulk watchdog sweeps | stale/no-session (bulk `mark_*_failed` → COUNT, no per-row context) | `cleanup_service.py` | ⚠️ **documented residual** (per-row emit needs iteration) |

Both #1083 caller paths (inline sync + async result-callback) converge on `apply_result`,
so both are covered there. The async-callback replay-ACK (`_AUTHORITATIVE_TERMINALS`,
`routers/agents.py`) + the CAS block guarantee no double-emit.

## Flow

```
[terminal writer]  ── CAS won? ──▶ spawn_task_terminal_event(agent, eid, terminal_status, summary, ...)
                                        │  (fire-and-forget, fail-open)
                                        ▼
                       emit_task_terminal_event (services/event_dispatch_service.py)
                         1. read the execution row once
                         2. recursion-break: row.triggered_by == "event" ⇒ return (no emit)
                         3. find_matching_event_subscriptions(agent, event_type)
                              └─ empty ⇒ return (NO agent_events row, NO dispatch)  ── AC #1/#5
                         4. build flat payload {execution_id, status, triggered_by,
                              summary_or_error, duration_ms, cost, fan_out_id, loop_id}
                         5. create_agent_event(...)
                         6. for each sub: trigger_subscription(sub, event)
                                             │  loopback POST /api/agents/{subscriber}/task
                                             ▼  (async, admin JWT, X-Event-Trigger tag)
                                     subscriber's queue ── report-back task
```

## Backend Layer

### Shared emit helper — `services/event_dispatch_service.py` (NEW)

- `emit_task_terminal_event(agent_name, execution_id, *, terminal_status, summary_or_error, duration_ms, cost)`
  — async, fail-open (whole body try/except-swallowed). Matching-sub gated; recursion-break;
  reads `triggered_by`/`fan_out_id`/`loop_id` (+ duration/cost fallback) from the row once.
- `spawn_task_terminal_event(...)` — sync strong-ref `asyncio.create_task` wrapper; every
  terminal writer calls this one wrapper (no per-module spawner, no `await`).
- **Status → event.** `terminal_status == SUCCESS` → `agent.task.completed`; else
  `agent.task.failed`. Branch on the **status string**, never on `TaskExecutionErrorCode`
  identity (fieldless-`@dataclass` `__eq__` returns True for any two members — #1085 footgun).
  The payload `status` is `envelope.status.value` (a plain `(str, Enum)`), so
  `{{payload.status}}` interpolates as `"success"`, not `"TaskExecutionStatus.SUCCESS"`.
- **Extraction (Invariant #1).** `trigger_subscription` / `_interpolate_template` /
  `_get_internal_token` moved verbatim out of `routers/event_subscriptions.py` so a service
  (`task_execution_service`) can reuse the dispatch primitive without importing a router.
  The router imports them back. Verified cycle-free.

### Reserved namespace + loop safety (three layers) — `routers/event_subscriptions.py`

`RESERVED_EVENT_PREFIX = "agent.task."`.

1. **Reject reserved emit** — both emit routes (`emit_event`, `emit_event_for_agent`) 400
   on `agent.task.*` (agents cannot spoof the deterministic backend contract).
2. **Block reserved self-subscription** — `source == subscriber` under `agent.task.*` is
   rejected on **create AND update** (400). The PUT guard closes the bypass where a benign
   `foo.bar` self-sub is updated into `agent.task.completed`.
3. **Recursion-break (the decisive one)** — `trigger_subscription`, when the dispatched
   event is reserved-namespace, stamps the loopback `/task` with `X-Event-Trigger`;
   `routers/chat.py` persists that spawned execution's `triggered_by = "event"`
   (`RESERVED_EVENT_TRIGGER`, already a reserved value in `_AUTONOMOUS_TRIGGERS`); and the
   emit helper suppresses re-emission when the terminating execution carries it. Breaks
   self / A↔B / A→B→C→A auto-emit cycles at the root — the autonomous-runaway class
   deterministic backend auto-emit would otherwise introduce (each hop = a full LLM turn +
   spend). A benign `foo.bar`→completion chain is unaffected (its task keeps its normal
   `triggered_by`).

## Delivery — best-effort, pull-transitional (honest scoping)

Delivery reuses EVT-001 subscription dispatch (`trigger_subscription` → HTTP loopback
`POST /api/agents/{subscriber}/task`, async, minted with a short-lived admin JWT). It wakes
a subscriber whose container is **running** — including a #1402 *parked-but-running*
orchestrator. It is **NOT durable**: a *stopped* subscriber's loopback returns 503 and is
swallowed — the `agent_events` row persists, the wake does not. The durable "reply lands in
the caller's queue" successor is the pull migration's queue (Epic #1045/#1081). The
loopback + admin-JWT internals are **pull-transitional** (a service self-calling its own
HTTP API, backend-pinned coroutine, admin scope) — NOT a stable contract; pull replaces
them.

It is deliberately **not** the WS `event_bus` (RELIABILITY-003) — a broadcast can't wake a
parked/stopped agent; a queued task can.

## Content-trust note

`summary_or_error` (the worker's response / error, `sanitize_response`-cleaned +
truncated to `TASK_EVENT_SUMMARY_MAX ≈ 2000`) is injected into the subscriber's task prompt
for a richer wake. `sanitize_response` strips credentials, **not** prompt-injection — this
is the same interpolation surface EVT-001 already exposes for agent-emitted payloads, now
produced deterministically. Flagged here honestly rather than removed (per the issue spec).

## Data model

Reuses the EVT-001 tables — no schema change:
- `agent_events` — one row per emitted terminal event (only when a subscription matched).
- `agent_event_subscriptions` — the subscriptions (`UNIQUE(subscriber_agent, source_agent, event_type)`).
- The recursion-break tag reuses the existing `schedule_executions.triggered_by` TEXT column
  (value `"event"`).

## Testing

`tests/unit/test_1578_task_completion_events.py` (27 tests, pure unit — no live backend):

- **Emit helper** — fired (success/failed/cancelled → right event type + payload), not-fired
  on no matching sub (AC #1/#5), recursion-break (`triggered_by="event"` ⇒ no emit),
  status-as-`.value`, payload `fan_out_id`/`loop_id`, duration/cost row fallback, summary
  truncation, fail-open (db raises ⇒ no propagation), `execution_id=None` no-op.
- **Every CAS-won writer** spawns on won / not on lost CAS: `apply_result` inline path
  (success + failure), `_write_terminal_and_gate` (the **timeout class** — the critical
  regression pin), the #1083 lease-reaper (`_process_stale_slot_reclaims`), the pull sink
  (`apply_task_result`, incl. replayed-terminal ⇒ no emit).
- **Both #1083 terminal paths, genuinely** — an inline `apply_result` call AND the async
  result-callback endpoint (`agent_execution_result`) driving the **real** `apply_result` →
  emit. Two distinct entry points, not two tests on the same inline call.
- **Reserved-namespace guards** (router coroutines) — emit `agent.task.completed` → 400;
  create self-sub → 400; **PUT** benign self-sub → reserved → 400; cross-agent subscribe →
  allowed.

Run: `pytest tests/unit/test_1578_task_completion_events.py -q`.

## Related Flows

- [agent-event-subscriptions.md](agent-event-subscriptions.md) — the EVT-001 pub/sub this
  reuses for delivery (agent-emitted events; the producer contrast).
- [task-execution-service.md](task-execution-service.md) — `apply_result` +
  `_write_terminal_and_gate`, the terminal writers this hooks; the #1083 async dispatch +
  result-callback path both terminal paths converge on.
- [redelivery-governor.md](redelivery-governor.md) — the #1085 correlated-failure controls
  over the same #1083 re-delivery path.
