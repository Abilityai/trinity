# Trinity FAQ — Scheduling & Automation

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I schedule a recurring task for an agent?

Open the agent's detail page, go to the Schedules section, and click **Create Schedule**. Give it a name, a cron expression (for example `0 9 * * 1-5` for weekdays at 9 AM), the message or task to send, a timezone, and an optional description. You can also pick a model override (Opus, Sonnet, Haiku, or custom) per schedule. Each time the schedule fires, it creates an execution record with status, duration, response, and cost. See [Scheduling](../automation/scheduling.md).

## Can I set a timezone for my schedule?

Yes. Every schedule has its own timezone setting, chosen when you create or edit it, so `0 9 * * *` fires at 9 AM in that timezone rather than in UTC. The default is UTC if you don't set one. See [Scheduling](../automation/scheduling.md).

## Why didn't my schedule fire even though it's enabled?

The most common cause is the agent-level **autonomy toggle**: it's a master switch, and no schedules fire while it's off, regardless of their individual enabled state. Also check that the agent still exists and isn't deleted — schedules stop firing immediately when an agent is deleted. If the scheduler was restarted, missed runs are only caught up within a 1-hour grace window; anything older is skipped rather than fired late. See [Scheduling](../automation/scheduling.md).

## What's the difference between disabling a schedule and turning off autonomy?

Disabling a schedule affects just that one schedule; its siblings keep firing. The autonomy toggle is agent-level: turning it off disables all of the agent's schedules at once, and turning it on re-enables them. Use the per-schedule toggle for fine-grained control and autonomy as the emergency brake or "pause everything" switch. See [Scheduling](../automation/scheduling.md).

## Can I run a schedule right now without waiting for the next cron tick?

Yes. Click **Run Now** on the schedule in the UI, or call `POST /api/agents/{name}/schedules/{id}/trigger` via the API. Manual triggers always fire — they bypass the pre-check hook entirely, even if the hook would have skipped the run. The result appears in the execution history like any other run. See [Scheduling](../automation/scheduling.md).

## What happens when a schedule fires while the agent is already busy?

Each agent has a configurable number of parallel task slots (default 3). If all slots are taken when a scheduled task arrives, the task is queued in a persistent, first-in-first-out backlog instead of being dropped, and it runs as soon as a slot frees up. Queued tasks that sit unprocessed for more than 24 hours are expired. Retries also count against the same slots. See [Scheduling](../automation/scheduling.md).

## How long can a scheduled task run before it times out?

Each agent has an execution timeout cap (default 60 minutes, configurable from 1 minute up to 2 hours). A schedule can set its own `timeout_seconds`; when unset it inherits the agent's cap, and when set it can never exceed it. Creating a schedule with a timeout above the agent cap fails with a validation error, and lowering the agent cap below an active schedule's timeout is rejected too — raise the agent cap first, then the schedule timeout. See [Scheduling](../automation/scheduling.md).

## Do failed scheduled runs retry automatically?

Yes, by default. Each schedule has `max_retries` (default 1, range 0–5) and `retry_delay_seconds` (default 60, range 30–600); set `max_retries: 0` to disable retries. Rate-limit errors use double the delay, capped at 300 seconds. Each retry creates a new execution record linked to the original via `retry_of_execution_id`, and the execution list groups retries under their parent run. See [Scheduling](../automation/scheduling.md).

## What happens to the execution history if I delete a schedule?

Deleting a schedule is a soft delete: it stops firing immediately, but the schedule row and all its execution records are preserved. An admin can recover a soft-deleted schedule, and if it was enabled it rejoins the scheduler shortly after recovery. Soft-deleted schedules are permanently purged after a retention period (30 days by default). See [Scheduling](../automation/scheduling.md).

## Can my agent skip a scheduled run when there's nothing to do?

Yes, with the pre-check hook. If the agent's template ships an executable at `~/.trinity/pre-check`, Trinity runs it before each cron tick: empty stdout with exit 0 records a `skipped` execution at zero cost without ever invoking the model, while non-empty stdout becomes the actual task message. The hook is language-agnostic (any shebang works) and fail-open — a broken or slow hook never suppresses a run. Manual "Run Now" triggers bypass it. See [Scheduling](../automation/scheduling.md).

## How do I trigger a schedule from an external system like CI/CD?

Enable a webhook on the schedule: open the schedule's **Webhook** panel and click **Enable webhook**, which mints a public URL containing a 256-bit token. Any external system can then `POST` to that URL — no Trinity account or JWT needed — and gets back `202 Accepted`. You can include an optional `{"context": "..."}` body (up to 4,000 characters) that is appended to the schedule's message, and every call is audit-logged. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## What should I do if my webhook URL leaks?

Rotate or revoke it: **Rotate URL** mints a new token and the old URL returns 404 immediately, while **Revoke** turns the webhook off entirely. For defense in depth, enable **Signature authentication** — Trinity shows you a signing secret exactly once, and every request must then carry an `X-Trinity-Signature: sha256=<hex>` header computed as HMAC-SHA256 of the raw request body; unsigned or badly signed calls are rejected with 401. Note that rotating the URL also clears the signing secret, so re-enable signing afterward. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## Why are my webhook calls getting rejected with 429?

Webhook triggers are rate-limited to 10 calls per 60-second window per webhook token (configurable by the operator), with an additional per-IP limit protecting the endpoint before token lookup. When you exceed the limit you get a 429 response — back off and retry after the window passes. If you need to fire more often than that, batch the work into fewer triggers or use the context body to pass multiple items in one call. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## What is an agent loop and when should I use one instead of a schedule?

A loop runs the same task against one agent repeatedly, strictly one iteration at a time, up to a bounded `max_runs` (1–100) — for example "process the next backlog item" × 20. Use a loop for back-to-back bounded work sessions, agentic retry ("keep trying until the tests pass"), or short polling; use a schedule for anything recurring on a cadence slower than the loop's 1-hour delay ceiling. You start a loop from the agent's **Loops** tab, via the `run_agent_loop` MCP tool, or via REST, and each iteration is a normal execution with its own cost and timeout. See [Agent Loops](../automation/agent-loops.md).

## Can each loop iteration see the previous iteration's result?

Yes, through the message template. The template supports `{{run}}` (the 1-indexed run number) and `{{previous_response}}` (the trailing 2,000 characters of the previous iteration's response, empty on run 1). Because `{{previous_response}}` is lossy, don't rely on it for real artifacts — instruct the agent to keep the draft or report in a workspace file and re-read it each run, since the agent's filesystem persists across iterations. See [Agent Loops](../automation/agent-loops.md).

## How do I keep a loop from running forever or burning my budget?

Loops have several independent brakes. `max_runs` (required, capped at 100) is the guaranteed ceiling; an optional `stop_signal` ends the loop early when the agent's response contains that substring; `max_cost_usd` stops the loop at a run boundary once accumulated spend meets the budget; `max_duration_seconds` sets a wall-clock deadline (up to 7 days); and no-progress detection stops the loop when consecutive runs return identical responses (default: 3 identical runs, set the threshold to 0 to disable). All of these are checked between runs — the in-flight iteration always finishes first, so a single run can overshoot a budget or deadline. You can also click **Stop** at any time for a graceful stop. See [Agent Loops](../automation/agent-loops.md).

## How do I run many tasks in parallel on one agent?

Use fan-out: it dispatches 1–50 independent tasks to an agent concurrently (up to `max_concurrency`, default 3, max 10), waits for all of them to complete or hit the overall deadline, and returns aggregated results in input order. It's available via the `fan_out` MCP tool or `POST /api/agents/{name}/fan-out` — there is no UI, and it currently works only on the calling agent itself. Each subtask creates its own execution record sharing a common `fan_out_id`, and each consumes one of the agent's parallel slots. See [Fan-Out](../automation/fan-out.md).

## What are skills and playbooks, and how do I run one?

A skill is a reusable capability defined as a markdown file in the platform's skills library (a GitHub repository synced to Trinity). When a skill is assigned to an agent, it becomes a playbook: the agent's **Playbooks** tab lists assigned skills with a **Run** button that sends the skill as a task, and in the **Chat** tab you can type `/` to autocomplete a playbook command with ghost text showing the syntax and argument hints. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## How do I assign skills to an agent, and do I need to restart it?

Open the agent's detail page, go to the skills/playbooks section, and assign skills from the library. Assigned skills are injected into the agent's `.claude/commands/` directory on its next start, where they become slash commands. To push skill changes into a running agent without a restart, use the `sync_agent_skills` MCP tool, which re-injects the assigned skills in place. Admins manage the library itself (sync from GitHub, create, edit, delete) from Settings. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## How can I see whether a schedule is actually performing well?

Three places, no setup required. The Schedules tab shows inline stats per schedule (7-day success rate, average duration, last-run status dot); the agent's Overview tab has a "Schedules performance" section rolling up every schedule over a 7/14/30-day window; and clicking **Show execution history** on a schedule opens a detailed Analytics card with run counts, success rate, duration percentiles (p50/p95/p99), total cost, top tools called, and a daily timeline, switchable between 24h, 7d, and 30d windows. The same data is available via the API and works even when the agent is stopped. See [Scheduling](../automation/scheduling.md).
