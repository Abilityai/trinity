# Sequential Agent Loops

Run the same task against one agent repeatedly, in order, with a bounded run count — for example "process the next backlog item" × 20. Start a loop once, get back a `loop_id`, and disconnect: the loop runs server-side, dispatching each iteration through the standard execution pipeline.

Loops are the sequential counterpart to [Fan-Out](fan-out.md) (parallel batch) and a single chat turn (one-shot).

> 📺 **Watch:** [Build Autonomous Loops for Your AI Agents](https://youtu.be/q3YvFYtuhec) *(Jun 2026)* · [all videos](../videos.md)

## Concepts

- **Loop** — A server-managed sequence of up to `max_runs` task executions against one agent. Iterations run strictly one at a time, in order.
- **Message Template** — The task message, re-rendered each iteration. Supports two placeholders:
  - `{{run}}` — the 1-indexed run number
  - `{{previous_response}}` — the trailing 2,000 characters of the previous iteration's response (empty on run 1)
- **Fixed mode** — No stop signal set: the loop runs exactly `max_runs` iterations.
- **Until mode** — A `stop_signal` is set: after each iteration, Trinity checks whether the agent's response contains the stop signal as a plain substring. On match, the loop ends early with reason `stop signal matched`. A recommended sentinel is `[[DONE]]` — tell the agent to emit it when the work is finished.
- **Cooperative stop** — Stopping a loop never kills the in-flight iteration. The current run finishes, then the loop exits with reason `stopped by user`.
- **Cost budget** — An optional `max_cost_usd` ceiling on total loop spend. It is checked **between runs**, not mid-run: before each iteration Trinity sums the cost of completed runs and, once that meets or exceeds the budget, stops the loop with reason `budget exhausted` **before starting the next run**. The current run always finishes, so one run — including the very first — can overshoot the budget by any amount; this is a guardrail against runaway loops, not a hard mid-call cap. A run that reports no cost counts as `$0` toward the budget (the loop is still bounded by `max_runs`).

## How It Works

1. Open an agent's detail page and select the **Loops** tab.
2. Click **Run Loop** (the agent must be running — the button is disabled otherwise).
3. Fill in the form:
   - **Message template** (required) — e.g. `Process item {{run}}. Previous result: {{previous_response}}`
   - **Max runs** (required, 1–100)
   - **Stop signal** (optional) — substring that ends the loop early
   - **Delay between runs (seconds)** (0–3600)
   - **Timeout per run (seconds)** (10–7200; defaults to the agent's execution timeout)
   - **Model** — per-loop model override (defaults to the agent default)
   - **Allowed tools** — restrict the agent's tools for every iteration, or leave **All Tools (Unrestricted)**
4. Click **Start Loop**. The loop appears in the list below with a live status badge and a **Run N / M** progress counter.
5. Click a loop row to expand it: a per-run table shows each iteration's status, cost, duration, and a response preview, and the **Last response** is rendered as markdown below the table.
6. Click **Stop** on an active loop to request a graceful stop. The badge shows the current iteration finishing, then the loop ends with reason `stopped by user`.

The panel updates in real time via WebSocket events as each run completes, with a periodic backstop refresh while a loop is active.

### Loop Lifecycle

| Status | Meaning |
|--------|---------|
| `queued` / `running` | Loop active; iterations dispatching |
| `completed` | Reached max runs, or the stop signal matched |
| `stopped` | Stopped by a user, or the cost budget (`max_cost_usd`) was met/exceeded at a run boundary; the in-flight iteration finished first |
| `failed` | An iteration failed (task error or exception) — the loop aborts at that iteration |
| `interrupted` | The backend restarted mid-loop; loops do **not** auto-resume |

### Observability

Every iteration is a normal execution: it creates its own row with `triggered_by: "loop"` and the parent `loop_id`, visible on the Executions page and per-execution detail view. In analytics timeline views (the agent's Overview tab and execution charts), loop executions appear as their own **Loops** category with a distinct color — they are not folded into Scheduled.

## Common Patterns

These three shapes cover most loop use cases. Pick by what ends the loop: a count, a condition, or a clock.

### Iterative refinement (fixed mode + chaining)

Run a fixed number of progressively better passes, feeding each result into the next:

```json
{
  "message": "Improve the draft. Previous version: {{previous_response}}",
  "max_runs": 4
}
```

`{{previous_response}}` is lossy — only the trailing 2,000 characters carry over. When iterations build a real artifact (a draft, a migration, a report), don't rely on it: instruct the agent to keep the artifact in a file in its workspace and re-read it each run — `Refine the draft in report.md — read it first, improve it, write it back.` The agent's filesystem persists across runs; the loop context doesn't.

### Agentic retry (until mode)

Keep trying until it works, bounded by a safety cap:

```json
{
  "message": "Attempt the migration. If it succeeds, paste the passing output and end your reply with [[DONE]]. Otherwise, fix the error and report what failed.",
  "max_runs": 8,
  "stop_signal": "[[DONE]]",
  "timeout_per_run": 600
}
```

The loop exits with `stop signal matched` on the first success, and `max runs reached` if it never succeeds within the cap.

### Bounded polling (until mode + delay)

Watch something external on a cadence, without holding a connection open:

```json
{
  "message": "Check the deploy health endpoint. If healthy, reply [[DONE]]; else report the current status.",
  "max_runs": 30,
  "stop_signal": "[[DONE]]",
  "delay_seconds": 120
}
```

This polls every 2 minutes for up to an hour and exits the moment the condition holds. For cadences slower than the 1-hour delay ceiling, a loop is the wrong tool — use a [schedule](scheduling.md) instead.

### Backlog draining (until mode + a work skill)

If the agent has a work-queue skill (for example the abilities `work-loop` pattern: pick one issue, do it, close it, exit), a loop turns it into a bounded autonomous session:

```json
{
  "message": "Run /work-loop. Process exactly one backlog item. If the backlog is empty, reply [[DONE]].",
  "max_runs": 20,
  "stop_signal": "[[DONE]]"
}
```

Each iteration is one unit of work with its own execution row, cost, and timeout — far more observable than one giant "do everything" task.

## Writing Good Until-Mode Loops

Until mode is only as reliable as the stop condition you write. Three rules:

1. **Tie the sentinel to verifiable evidence, not self-assessment.** Models skew positive when grading their own work — `reply [[DONE]] when the report is good` exits on the first pass. Anchor the sentinel to something checkable: passing test output, an HTTP 200, zero remaining TODO markers. If the condition is inherently subjective, keep `max_runs` low instead.
2. **Always set the cap.** `stop_signal` is best-effort — the agent has to actually emit it. `max_runs` is guaranteed. The cap is the safety net, not the expected exit.
3. **Set `timeout_per_run`.** A hung iteration silently stalls the whole sequence. Size it to the task (e.g. 600s for a test suite) rather than inheriting a long agent default.

And once a loop is running, **watch for stalls**: if consecutive responses are near-identical (same failure, same output, no state change), the loop is burning budget without progress. Stop it rather than letting it run to the cap.

## For Agents

Agents (and scripts) start loops via MCP tools or REST. The permission model matches `chat_with_agent`: owner, admin, or shared access on the target agent; agent-scoped MCP keys need an explicit permission grant.

### From Claude Code: `/trinity:loop`

If you work in Claude Code with the [abilities trinity plugin](../abilities/trinity-plugin.md) installed, `/trinity:loop` is the fastest way to start a loop — it parses a natural-language request (`/trinity:loop @ci-agent run the test suite until it passes, max 10`), picks the mode, writes the sentinel instruction into the message for you, fires `run_agent_loop`, and watches progress locally. See [trinity Plugin — Remote Loops](../abilities/trinity-plugin.md#remote-loops-trinityloop).

### MCP Tools

| Tool | Description |
|------|-------------|
| `run_agent_loop` | Start a loop; returns `loop_id` immediately. `agent_name` is required for user-scoped keys; agent-scoped keys default to the bound agent |
| `get_loop_status` | Loop status plus per-run summaries (run number, execution id, status, response preview, cost, duration) and the last full response |
| `stop_loop` | Graceful stop; returns `stopping` (runner signaled) or `already_done` (loop already terminal) |

```typescript
mcp__trinity__run_agent_loop({
  agent_name: "my-agent",
  message: "Process the next backlog item ({{run}} of 20). Reply [[DONE]] if the backlog is empty.",
  max_runs: 20,
  stop_signal: "[[DONE]]",
  delay_seconds: 30
})
// → { success: true, loop_id: "loop_...", status: "queued", ... }
```

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/loops` | POST | Start a loop; returns 202 with `{loop_id, status, agent_name, max_runs}` |
| `/api/agents/{name}/loops` | GET | List the agent's loops, most recent first (`?status=`, `?limit=` 1–200, default 50) |
| `/api/loops/{loop_id}` | GET | Status, per-run summaries, last full response (404 unknown; 403 if the caller is neither the initiator nor has agent access) |
| `/api/loops/{loop_id}/stop` | POST | Graceful stop → `{status: "stopping" \| "already_done"}` |

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/loops \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Process item {{run}}. Previous result: {{previous_response}}",
    "max_runs": 10,
    "stop_signal": "[[DONE]]",
    "delay_seconds": 5
  }'
```

### Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `message` | — (required) | 1–100,000 chars | Template; supports `{{run}}` and `{{previous_response}}` |
| `max_runs` | — (required) | 1–100 | Hard ceiling on iterations |
| `stop_signal` | none (fixed mode) | ≤200 chars | Substring that ends the loop early; whitespace-stripped, blank = fixed mode |
| `delay_seconds` | 0 | 0–3600 | Pause between iterations |
| `timeout_per_run` | agent's execution timeout | 10–7200 | Per-iteration timeout in seconds |
| `max_cost_usd` | none (no budget) | > 0 | Total USD spend budget; checked **between runs** (the current run always finishes). Stops with reason `budget exhausted` once spend meets/exceeds it. Runs reporting no cost count as `$0`. |
| `model` | agent default | — | Model override applied to every iteration |
| `allowed_tools` | unrestricted | — | Tool restrictions applied to every iteration |

## Limitations

- **One agent, sequential** — A loop targets a single agent and never runs iterations in parallel. For parallel batches, use [Fan-Out](fan-out.md).
- **Shared capacity** — Each iteration goes through the agent's normal capacity admission and counts against its `max_parallel_tasks` budget alongside chat, schedules, and other traffic.
- **Fail-fast** — Any failed iteration ends the loop with status `failed`; remaining runs are skipped.
- **No resume after restart** — A backend restart marks in-flight loops `interrupted`. They do not auto-resume; start a new loop.
- **Stop is not instant** — The in-flight iteration always finishes; only subsequent iterations are skipped.
- **Bounded inputs** — `max_runs` is capped at 100, delay at 1 hour, and per-run timeout at 2 hours.

## See Also

- [trinity Plugin](../abilities/trinity-plugin.md) — `/trinity:loop`, the conversational entry point from Claude Code
- [Fan-Out](fan-out.md) — Parallel batch counterpart
- [Scheduling](scheduling.md) — Cron-based recurring tasks; the right tool for cadences slower than 1 hour
- [Executions](../operations/executions.md) — Where each loop iteration appears
- [Agent Configuration](../agents/agent-configuration.md) — Execution timeout and parallel task limits
