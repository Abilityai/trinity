# Scheduling

Cron-based automation for agents using APScheduler. Schedule recurring tasks with timezone support, execution history, and manual triggers.

> 📺 **Watch:** [Trinity Platform Demo](https://youtu.be/ivljtZqsxeo) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Schedule** -- A cron expression paired with a message or task sent to an agent at the specified times.
- **Execution** -- Each time a schedule fires, it creates an execution record with status, duration, response, cost, and model used.
- **Autonomy Mode** -- Master toggle that enables or disables all schedules for an agent. Schedules will not fire if autonomy is off.
- **Scheduler Service** -- Standalone service with Redis distributed locks. Uses async fire-and-forget dispatch with DB polling for status.
- **Misfire Handling** -- If the scheduler restarts, missed jobs within a 1-hour grace window are caught up and fired immediately (`misfire_grace_time=3600`, `coalesce=True`, `max_instances=1`).

## How It Works

![Agent Schedules tab showing three active weekly schedules with cron expressions and execution history](../images/agent-schedules-tab.png)

1. Open the agent detail page and go to the scheduling section.
2. Click **Create Schedule**.
3. Configure: name, cron expression (e.g., `0 9 * * 1-5` for weekdays at 9 AM), message/task, timezone, and description.
4. Optionally select a model override (Opus, Sonnet, Haiku, or custom).
5. Enable or disable individual schedules with the toggle.
6. View execution history with status, duration, and cost.
7. Click **Run Now** to trigger a schedule immediately.
8. Use the autonomy toggle to control all schedules at once.

### Execution Flow

1. Scheduler fires and sends a POST to `/api/internal/execute-task` with `async_mode=True`.
2. Backend spawns a background task and returns immediately.
3. Scheduler polls the database every 10 seconds until execution completes.
4. Execution record is updated with response, cost, and duration.

## For Agents

### MCP Tools

| Tool | Description |
|------|-------------|
| `list_agent_schedules(name)` | List schedules |
| `create_agent_schedule(name, ...)` | Create schedule |
| `get_agent_schedule(name, id)` | Get schedule details |
| `update_agent_schedule(name, id, ...)` | Update schedule |
| `delete_agent_schedule(name, id)` | Delete schedule |
| `toggle_agent_schedule(name, id)` | Enable or disable |
| `trigger_agent_schedule(name, id)` | Manual trigger |
| `get_schedule_executions(name, id)` | Execution history |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/schedules` | GET | List schedules |
| `/api/agents/{name}/schedules` | POST | Create schedule |
| `/api/agents/{name}/schedules/{id}` | GET/PUT/DELETE | CRUD operations |
| `/api/agents/{name}/schedules/{id}/enable` | POST | Enable schedule |
| `/api/agents/{name}/schedules/{id}/disable` | POST | Disable schedule |
| `/api/agents/{name}/schedules/{id}/trigger` | POST | Manual trigger |
| `/api/agents/{name}/schedules/{id}/executions` | GET | Execution history |
| `/api/agents/{name}/schedules/{id}/analytics` | GET | Per-schedule analytics (see below) |
| `/api/agents/{name}/schedules/analytics-summary` | GET | Per-schedule performance rollup for the whole agent (`?window=7d\|14d\|30d`) |

## Per-Schedule Analytics

Each schedule has an analytics view summarizing how it has been performing over a selectable time window.

### At-a-Glance Scorecards

Performance shows up in two places without opening any analytics card:

- **Schedules tab** — each schedule row carries inline stats: 7-day success rate, average duration, and a last-run status dot.
- **Agent Overview tab** — a **"Schedules performance"** section rolls up every schedule for the selected window (success rate, average duration, run count, tool-call count per schedule) and deep-links to the Schedules tab. Tool counts are sampled over the newest runs.

Both are fed by one endpoint: `GET /api/agents/{name}/schedules/analytics-summary?window=7d|14d|30d` — one row per schedule, including zero-run schedules.

### Detailed Analytics Card

1. Open the agent's Schedules tab.
2. Click **Show execution history** on a schedule.
3. An **Analytics** card appears below the history with a window toggle (**24h / 7d / 30d**).

The card shows run counts, success rate, duration percentiles (p50 / p95 / p99), total cost, the top tools the schedule called, and a daily timeline of successes, failures, and cost.

### Via the API

```
GET /api/agents/{name}/schedules/{id}/analytics?window_hours=168
```

- `window_hours` must be one of `24`, `168` (7 days), or `720` (30 days). Default: `168`.
- Returns execution counts by status, success rate, duration p50/p95/p99, cost totals, the top 5 tool calls, and a gap-filled daily timeline (UTC day buckets).
- Read-only — works even when the agent is stopped. Full response schema at [Backend API Docs](http://localhost:8000/docs).

On high-traffic schedules the duration percentiles are computed over the newest 5,000 successful runs; the response reports `sampled: true` when this cap applies. Counts and the timeline always cover the full window.

## Automatic Retry

Failed executions can automatically retry with configurable delay and attempt limits.

### Configuration

| Field | Default | Range | Description |
|-------|---------|-------|-------------|
| `max_retries` | 1 | 0-5 | Max retry attempts (0 = disabled) |
| `retry_delay_seconds` | 60 | 30-600 | Delay between retries |

New schedules default to 1 retry. Set `max_retries: 0` to disable.

### Retry Behavior

1. Execution fails (error or timeout)
2. If `max_retries > 0` and attempts remain, scheduler waits `retry_delay_seconds`
3. Rate-limit errors (429) use 2x delay, capped at 300 seconds
4. Retry creates a new execution record linked to the original
5. Process repeats until success or max retries exhausted

### Execution Grouping

Retries are linked to their original execution:

| Field | Description |
|-------|-------------|
| `attempt_number` | Which attempt (1 = first try, 2 = first retry) |
| `retry_of_execution_id` | Links to original execution |

The execution list groups retries under their parent execution for clarity.

### Statuses

| Status | Meaning |
|--------|---------|
| `pending_retry` | Failed, retry scheduled but not yet fired |
| `running` | Retry in progress |
| `success` / `failed` | Final outcome |

## Pre-Check Hook

An optional executable shipped by an agent template that gates each cron tick before Claude is invoked. When present, it lets the agent decide at runtime whether the scheduled work is actually needed — so empty polls (no new PRs, no new emails, no alerts) consume zero tokens.

### How It Works

1. The template ships `~/.trinity/pre-check` as an executable file with a shebang (`#!/usr/bin/env python3`, `#!/bin/bash`, etc.).
2. Before each scheduled cron tick, Trinity runs the file inside the agent container.
3. The scheduler acts on the output:

| Pre-check result | Scheduler action |
|---|---|
| No `~/.trinity/pre-check` file | Fire as usual (backward-compatible) |
| Exit 0, non-empty stdout | Fire — stdout becomes the chat message (overrides the schedule's configured message) |
| Exit 0, empty stdout | Skip — record a `skipped` execution row, no Claude invocation, zero cost |
| Exit non-zero | Fail-open — log the error and fire with the original message |
| Timeout (>60s) or error | Fail-open — fire with the original message |

### Key Behaviors

- **Language-agnostic** — the hook is exec'd directly by Trinity; the interpreter is chosen by the file's shebang. Any language that produces a binary or has a system interpreter works.
- **Manual triggers bypass the hook entirely** — clicking "Run Now" always fires, regardless of what the hook would return.
- **Skipped executions appear in the execution list** with status `skipped`, zero cost, and a reason string. They do not count against retry limits.
- **Fail-open** — a broken or slow hook never suppresses a scheduled invocation. The schedule fires as usual and the error is logged.

### For Template Authors

Place the hook at `~/.trinity/pre-check` (no extension), make it executable (`chmod +x`), and include a shebang. The hook receives no arguments. It should print the work description to stdout if there is work, or print nothing (empty stdout) if there is nothing to do.

Example (Python):
```python
#!/usr/bin/env python3
import sys
# ... check for new items ...
if new_items:
    print(f"Review {len(new_items)} new PRs: {', '.join(new_items)}")
# else: exit 0 with empty stdout → Trinity records a skip
```

## Per-Schedule Timeout

Each schedule has an optional `timeout_seconds`. When it is unset (`null`), the schedule inherits the agent's `execution_timeout_seconds` cap. When set, it cannot exceed that cap:

- Creating or updating a schedule with `timeout_seconds > agent.execution_timeout_seconds` returns `400 error=schedule_timeout_exceeds_agent_cap`.
- Lowering the agent cap below an active schedule's timeout returns `400 error=agent_timeout_below_active_schedules`.

Raise the agent cap first, then raise the schedule timeout.

## Limitations

- Execution timeout is per-agent configurable (default 60 minutes, max 2 hours).
- Parallel execution is controlled by per-agent capacity slots (default 3).
- Missed jobs are only caught up within the 1-hour grace window.
- Retries count against the agent's parallel capacity slots.
- Pre-check hooks run with the same permissions as the agent's normal tool calls (`developer` user inside the container).

## See Also

- [Managing Agents](../agents/managing-agents.md) — agent lifecycle, start/stop
- [Agent Configuration](../agents/agent-configuration.md) — autonomy mode, execution timeout
- [Agent Loops](agent-loops.md) — bounded sequential task repetition
- [Approvals](approvals.md) — human-in-the-loop gates for scheduled work
