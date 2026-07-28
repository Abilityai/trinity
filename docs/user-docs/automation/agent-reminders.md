# Agent Self-Reminders

Let a running agent schedule a **one-shot future re-invocation of itself** with a message it writes — "check this PR in 2 hours", "review the deploy tomorrow at 9am". When the timer fires, Trinity dispatches a normal execution of that same agent carrying the reminder message. Nothing is held open in between; the reminder is a durable row that fires once, later.

Reminders are the **time-deferred sibling** of [Agent Loops](agent-loops.md). A loop runs iterations back-to-back *now*; a reminder fires *once, at a chosen time*. Unlike a loop — which lives in memory and does not survive a restart — a reminder is durable: it is persisted and re-armed automatically, so it still fires after a backend or scheduler restart.

There is no web UI for reminders. They are a backend and MCP primitive that agents drive themselves. Fired reminders show up in the standard [Executions](../operations/executions.md) list and the agent's Overview timeline under a distinct **Reminders** category.

## Concepts

- **Reminder** — A one-shot, durable, self-scheduled task. An agent sets a reminder on **itself** with a message and a fire time. At that time, Trinity runs a normal execution of the agent with that message.
- **Self-only** — An agent can set, list, and cancel only *its own* reminders. A reminder against a sibling agent is rejected (`403`).
- **Durable** — Reminders survive a restart. The fire time is stored, not held in memory, and re-armed on boot. A reminder due during a restart fires when the service comes back rather than being lost.
- **Autonomy-gated** — Reminders only fire while the agent's **autonomy mode** is on, the same master switch that governs [schedules](scheduling.md). Autonomy is **off by default on a newly created agent**.

### Reminder vs. Loop vs. Schedule

Pick by *when* and *how often* the work should run:

| Tool | Cardinality | Timing | Durable across restart |
|------|-------------|--------|------------------------|
| **Reminder** | Once | A future instant you choose | Yes |
| **[Loop](agent-loops.md)** | Up to `max_runs` times | Back-to-back, starting now | No (marked `interrupted`) |
| **[Schedule](scheduling.md)** | Recurring | A cron cadence | Yes |

Reminders are the agent-initiated, one-shot, durable counterpart to cron [schedules](scheduling.md) — use a reminder for a single deferred follow-up, a schedule for a recurring cadence.

## How It Works

An agent sets a reminder one of two ways — pick exactly one:

- **`delay_seconds`** — fire this many seconds from now (e.g. `7200` for two hours). Best for relative follow-ups.
- **`fire_at`** — an absolute ISO 8601 timestamp (e.g. `2026-07-25T09:00:00Z`). Best for "tomorrow at 9am".

Optional per-reminder overrides — `model`, `timeout_seconds`, and `allowed_tools` — apply to the execution that fires, exactly as they would for a one-off task. When the reminder fires, the resulting execution shares the agent's normal capacity budget alongside chat, schedules, and loops.

**Listing** returns pending reminders by default (soonest fire first); pass a status filter of `all` to see fired, cancelled, and failed reminders too.

**Cancelling** a still-pending reminder stops it from firing. Cancelling an already-cancelled reminder is a no-op success; a reminder that has already fired, is mid-fire, or has failed cannot be cancelled (`409`). An unknown reminder id returns `404`.

### Autonomy must be on

A reminder set on an agent whose autonomy mode is **off** is accepted and **held**: it stays `pending`, is never armed, and does not fire — even once its fire time passes. Turn autonomy on and it fires past-due within about a minute. Nothing is lost, but nothing happens either.

Because autonomy defaults to off on a new agent, this is the usual reason a first reminder "never fires".

How to tell:

- The create response and `list_reminders` set **`autonomy_hold: true`** on any reminder in this state, and the `set_reminder` tool result adds an explicit `warning`. An agent should relay that to the user rather than reporting the reminder as scheduled.
- The scheduler logs a count each reconcile pass, e.g. `2 reminder(s) held: their agent's autonomy is disabled.`

Chat is *not* autonomy-gated, so an agent can be mid-conversation, accept "remind me in an hour", and still be unable to fire it. Check autonomy when a user asks for a reminder.

**Where fired reminders appear** — a fired reminder is a normal execution row (with `triggered_by: "reminder"`), so it appears on the Executions page, in per-execution detail, and as its own **Reminders** category in the agent's analytics timeline — never folded into Scheduled.

## For Agents

Agents drive reminders via MCP tools or REST. The self-only rule holds on both surfaces: an agent-scoped key may act only on the agent it is bound to.

### MCP Tools

| Tool | Description |
|------|-------------|
| `set_reminder` | Schedule a one-shot self-reminder. Args: `message`, plus `fire_at` (ISO timestamp) **or** `delay_seconds` — exactly one. Optional: `model`, `timeout_seconds`, `allowed_tools` |
| `list_reminders` | List the agent's reminders (pending by default; `status: "all"` for every state), soonest fire first |
| `cancel_reminder` | Cancel a pending reminder by id |

```typescript
mcp__trinity__set_reminder({
  agent_name: "my-agent",
  message: "Re-check the open PR — is CI green yet? If so, merge it.",
  delay_seconds: 7200
})
// → { success: true, reminder: { id: "rem_...", status: "pending", fire_at: "..." } }
```

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/reminders` | POST | Create a one-shot reminder; returns 201 with the reminder |
| `/api/agents/{name}/reminders` | GET | List reminders (`?status=pending` default, `?status=all` for every state) |
| `/api/agents/{name}/reminders/{id}/cancel` | POST | Cancel a pending reminder |

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full request and response schemas.

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/reminders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Review the deploy health dashboard and report anomalies.",
    "fire_at": "2026-07-25T09:00:00Z"
  }'
```

Both create boundaries accept an optional `Idempotency-Key` header, so a naive retry does not schedule a duplicate reminder.

## Limitations

- **Self-only** — An agent can set, list, and cancel only its own reminders. A reminder targeting another agent returns `403`.
- **One-shot** — A reminder fires exactly once. For a recurring cadence, use a [schedule](scheduling.md); for repeated passes now, use a [loop](agent-loops.md).
- **Delay bounds** — The fire time must be at least **60 seconds** and at most **30 days** out.
- **Per-agent caps** — At most **25 pending** reminders and **100 created per day** per agent. Beyond either, `set_reminder` returns `429`.
- **Timeout cap** — A per-reminder `timeout_seconds` cannot exceed the agent's execution timeout cap.
- **No web UI** — Reminders are set and managed by the agent; there is no dashboard form. The *result* of a fired reminder is visible in Executions.

## See Also

- [Agent Loops](agent-loops.md) — Bounded sequential repetition; the immediate sibling of reminders
- [Scheduling](scheduling.md) — Cron-based recurring tasks; the durable, recurring counterpart
- [Executions](../operations/executions.md) — Where a fired reminder appears
- [Agent Configuration](../agents/agent-configuration.md) — Execution timeout and parallel task limits
