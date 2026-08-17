# Trinity FAQ — Scheduling & Automation

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I schedule a recurring task for an agent?

Open the agent's detail page, go to the Schedules section, and click **Create Schedule**. Give it a name, a cron expression (for example `0 9 * * 1-5` for weekdays at 9 AM), the message or task to send, a timezone, and an optional description. You can also pick a model override (Opus, Sonnet, Haiku, Sonnet 5, Fable 5, or custom) per schedule. Each time the schedule fires, it creates an execution record with status, duration, response, and cost. See [Scheduling](../automation/scheduling.md).

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

## Can an agent schedule itself to run again later?

Yes, with a self-reminder. While it's running, an agent can set a one-shot reminder — a future re-invocation of *itself* carrying a message it writes ("check whether the build passed"). When the time comes, Trinity dispatches a normal execution of that same agent with that message. Reminders are durable, so a pending one survives a backend or container restart and still fires. See [Agent Reminders](../automation/agent-reminders.md).

## How is an agent self-reminder different from a cron schedule?

A schedule is a recurring, owner-created cron job that fires on a cadence you define in the UI or API. A reminder is one-shot and agent-initiated: the agent decides during a run to wake itself once at a later time, without an owner setting anything up. Use schedules for standing recurring work; reminders are for an agent deferring a single follow-up it discovered mid-task. See [Agent Reminders](../automation/agent-reminders.md).

## How do I see or cancel the reminders an agent set for itself?

When a reminder fires it runs as a normal execution, so it shows up in the agent's Executions list grouped under a **Reminders** trigger bucket. The agent manages its own pending reminders through its tools — listing and cancelling them — and this is self-only: an agent can only see and cancel the reminders it set for itself, not another agent's. See [Agent Reminders](../automation/agent-reminders.md).

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

Loops have several independent brakes. `max_runs` (required, capped at 100) is the guaranteed ceiling; an optional `stop_signal` ends the loop early when the agent's response contains that substring; `max_cost_usd` stops the loop at a run boundary once accumulated spend meets the budget; `max_duration_seconds` sets a wall-clock deadline (up to 7 days); and no-progress detection stops the loop when consecutive runs return identical responses (default: 3 identical runs, set the threshold to 0 to disable). All of these are checked between runs — the in-flight iteration always finishes first, so a single run can overshoot a budget or deadline. A separate failure policy governs what happens when an iteration *errors* rather than runs long — see the abort-vs-continue question below. You can also click **Stop** at any time for a graceful stop. See [Agent Loops](../automation/agent-loops.md).

## What happens to a loop when one iteration fails — does it stop or keep going?

That's set by the loop's failure policy, `on_failure`. The default is `abort`: the loop fails fast, stopping the moment an iteration errors. Switch to `continue` and the loop tolerates a failed iteration and moves on to the next run — but it still aborts if failures pile up, once it hits `max_consecutive_failures` (default 3) errors in a row; a successful run resets that streak. A continue-mode loop that finishes with some tolerated failures reports a `completed_with_errors` status. See [Agent Loops](../automation/agent-loops.md).

## How do I run many tasks in parallel on one agent?

Use fan-out: it dispatches 1–50 independent tasks to an agent concurrently (up to `max_concurrency`, default 3, max 10), waits for all of them to complete or hit the overall deadline, and returns aggregated results in input order. It's available via the `fan_out` MCP tool or `POST /api/agents/{name}/fan-out` — there is no UI, and it currently works only on the calling agent itself. Each subtask creates its own execution record sharing a common `fan_out_id`, and each consumes one of the agent's parallel slots. See [Fan-Out](../automation/fan-out.md).

## What are skills and playbooks, and how do I run one?

A skill is a reusable capability packaged in the platform's skills library — one or more GitHub repositories synced to Trinity. Each skill is a folder built around a `SKILL.md` instruction file, optionally bundled with supporting scripts, templates, and resources. When a skill is assigned to an agent, it becomes a playbook: the agent's **Playbooks** tab lists assigned skills with a **Run** button that sends the skill as a task, and in the **Chat** tab you can type `/` to autocomplete a playbook command with ghost text showing the syntax and argument hints. See [Skills and Playbooks](../automation/skills-and-playbooks.md).


## How do I find out which agents already have a given skill?

The **Library** page's Skills tab (`/library?tab=skills`) shows, for every skill, an *Assigned to N agents* line with chips linking straight to each agent's Skills tab. It's bounded — the first four agents, then **+N more**. Admins see the whole fleet; everyone else sees their own and shared agents, and the wording says which. Below the listing sits **Assigned but no longer in the library**: assignments whose skill was removed upstream. That list matters because revocation works by publishing a new version without the offending skill, and the package stays on each agent until it's unassigned there. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Can I assign a skill from the Library page?

No — the Library is a browse-and-audit surface. Assignment stays a per-agent action on that agent's **Skills** tab, so there is exactly one place where the change is made. What the Library adds is the fleet-wide read: who holds each skill, and which assignments have outlived their skill. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Can a skill be a whole folder of files instead of a single markdown file?

Yes. A skill is a full-directory package, not just one markdown file: alongside the `SKILL.md` instructions it can carry scripts, templates, and any resource files the capability needs. When the skill is assigned, Trinity injects the entire directory into the agent, versioned by the folder's content so re-syncs only push real changes. This lets a skill ship helper code and assets, not only prose. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## How do I assign skills to an agent, and do I need to restart it?

Open the agent's detail page and go to the **Skills** tab. Pick skills from the library, save, and click **Sync now** to copy them into a running agent — or just leave it, and they arrive on the agent's next start. Each skill lands as a whole directory under `~/.claude/skills/<name>/`, so its scripts and resources come too, and the per-skill result tells you honestly whether it landed clean or is missing a declared dependency. Admins manage the *sources* the library syncs from in **Settings → Agents**; skills themselves are edited in their GitHub repository, not in Trinity. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## How can I see whether a schedule is actually performing well?

Three places, no setup required. The Schedules tab shows inline stats per schedule (7-day success rate, average duration, last-run status dot); the agent's Overview tab has a "Schedules performance" section rolling up every schedule over a 7/14/30-day window; and clicking **Show execution history** on a schedule opens a detailed Analytics card with run counts, success rate, duration percentiles (p50/p95/p99), total cost, top tools called, and a daily timeline, switchable between 24h, 7d, and 30d windows. The same data is available via the API and works even when the agent is stopped. See [Scheduling](../automation/scheduling.md).

## Can the skills library sync from more than one repository?

Yes. Trinity syncs from any number of GitHub repositories: a bundled public community catalog that ships pre-configured, plus custom repositories your admin adds in **Settings → Agents**. When two sources ship the same skill name, the lower-priority number wins — custom sources default to 100 and the community source to 1000, so your own repository always wins a clash. Nothing is overwritten silently: the winning skill is marked with which sources it shadows, in the library listing and as a warning at injection time. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Where do skills have to live inside a source repository?

One of three layouts, tried in order: a root `catalog.yaml` with a `skills_root:` key naming the directory; a `skills/` directory containing at least one `<name>/SKILL.md`; or the legacy `.claude/skills/`. Existing repositories keep working with no configuration, and an invalid declaration falls through to the next layout rather than blanking the source. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Will a skills-library update reach my running agents automatically?

Only if you turn it on — both automation settings default to off. Under **Settings → Agents → Skills Library → Automation**, enable **Auto-sync** to pull sources on an interval (default hourly), and **Fleet re-inject** to push changed packages to running agents. Re-inject fires only when a commit actually moved, so a no-op pull never sweeps the fleet, and stopped agents pick changes up on their next start. The panel shows the last sync status and the last fleet report, and raises an operator alert if any agent failed. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## What happens when I unassign a skill — do the files stay on the agent?

No. Unassigning removes the injected package, using the manifest recorded at injection time, so only files the platform wrote are deleted; anything the agent authored survives, and directories left empty are cleaned up. If the agent is stopped, busy, or unreachable, the unassignment still succeeds and the removal is reported as deferred — the agent reconciles on its next start. A reconcile that would strip an unusually large number of skills from one agent refuses outright and raises an operator alert instead. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Why is the community skills source pinned to a tag instead of tracking a branch?

Because skills carry executable scripts, and with fleet re-inject on, a source tracking a branch head would put every merged upstream commit onto every agent with no human in the loop — and the community catalog accepts public contributions. The community source is therefore pinned to a tag we bump; custom sources, whose write access you control, track a branch. If a pinned tag is later moved to a different commit, Trinity **refuses** it rather than adopting it. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## My template declares schedules — will Trinity create them?

Yes. A `schedules:` block in `template.yaml` is materialized as real schedules when the agent is created, through the UI, the API, and MCP alike. Each entry needs a `name`, a strict 5-field `cron`, and a `message`; up to 20 per template. A malformed entry is dropped with a named error rather than failing the creation, and every materialized schedule inherits the agent's execution timeout so it can never exceed the agent's own cap. See [Creating Agents](../agents/creating-agents.md).

## Can I use a legacy timezone name like `US/Eastern` in a schedule?

Yes — legacy IANA aliases such as `US/Eastern`, `Asia/Calcutta`, and `Europe/Kiev` resolve correctly. A timezone the platform genuinely cannot resolve is rejected when you create the schedule, with a message naming the problem, rather than being accepted and then silently never firing. See [Scheduling](../automation/scheduling.md).
