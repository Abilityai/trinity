# Trinity FAQ — Scheduling & Automation

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I schedule a recurring task for an agent?

Open the agent's detail page, go to the Schedules section, and click **Create Schedule**. Give it a name, a cron expression (for example `0 9 * * 1-5` for weekdays at 9 AM), the message or task to send, a timezone, and an optional description. You can also pick a model override (Opus, Sonnet, Haiku, Sonnet 5, Fable 5, or custom) per schedule. Each time the schedule fires, it creates an execution record with status, duration, response, and cost. See [Scheduling](../automation/scheduling.md).

## Can I set a timezone for my schedule?

Yes. Every schedule has its own timezone setting, chosen when you create or edit it, so `0 9 * * *` fires at 9 AM in that timezone rather than in UTC. The default is UTC if you don't set one. See [Scheduling](../automation/scheduling.md).

## How do I know my cron expression is valid before I save the schedule?

The schedule form checks the expression as you type, with the same grammar the scheduler uses. While creating, an inline error appears under the field once you leave it with an invalid expression; while editing, an invalid stored expression is flagged immediately; and **Create** (or **Update**) is disabled only while the field is non-empty *and* invalid. The presets **Daily 9 AM**, **Weekly Mon**, **Every 6h** and **Every 30m** fill the field for the common cadences. The server stays the authority — an expression the form accepts but the scheduler rejects still fails on save with the reason — and a schedule already stored with an unregistrable expression shows a warning triangle in its cron chip and never fires until fixed. See [Scheduling](../automation/scheduling.md#cron-expressions-are-checked-as-you-type).

## Can a scheduled run deliver its output to a person's Workspace instead of only the execution log?

Yes. Set `deliver_to_workspace_email` on the schedule over the API or with `create_agent_schedule` / `update_agent_schedule` — the UI form has no field for it, and passing `null` on an update stops delivering. The run's output then arrives as a message from the agent in that person's **Main** chat with it, rateable like any reply and delivered at most once per fire; it waits up to two minutes for a reply already in progress in that chat, and it appears on the person's next Workspace load or chat switch rather than popping up mid-conversation. You can always name yourself; naming someone else — who must be the agent's owner or someone it is shared with — takes the agent's owner or an admin. If the address can't reach the agent (never shared, share revoked, unknown), the run fails and says so on its execution row, and a malformed address is rejected when you save rather than hours later. See [Scheduling](../automation/scheduling.md#delivering-a-runs-output-to-someones-workspace).

## Why didn't my schedule fire even though it's enabled?

The most common cause is the agent-level **autonomy toggle**: it's a master switch, and no schedules fire while it's off, regardless of their individual enabled state. Next, look at the schedule's cron chip in the list — a warning triangle with the tooltip **Invalid cron expression** means the stored expression is one the scheduler can't register, and that schedule never fires until you fix it. Also check that the agent still exists and isn't deleted (schedules stop firing immediately when an agent is deleted) and, if the agent has **freeze schedules if sync failing** enabled, that its git sync hasn't failed three times in a row. If the scheduler was restarted, missed runs are only caught up within a 1-hour grace window; anything older is skipped rather than fired late. See [Scheduling](../automation/scheduling.md).

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

A loop runs the same task against one agent repeatedly, strictly one iteration at a time, up to a bounded `max_runs` (1–100) — for example "process the next backlog item" × 20. Use a loop for back-to-back bounded work sessions, agentic retry ("keep trying until the tests pass"), or short polling; use a schedule for anything recurring on a cadence slower than the loop's 1-hour delay ceiling. You start a loop from the agent's **Loops** tab, from the Workspace rail's **Loops** tab, by asking the agent in chat, via the `run_agent_loop` MCP tool, or via REST, and each iteration is a normal execution with its own cost and timeout. See [Agent Loops](../automation/agent-loops.md).

## What happens to a running loop if the backend restarts?

It carries on. A loop is a durable record, not an in-memory job: on boot, a loop that was between runs is picked up again, and a loop whose iteration was in flight continues from that run's outcome — nothing is dispatched twice and nothing is lost. The `interrupted` status in the lifecycle table only applies to loops from older builds, before loops became durable. See [Agent Loops](../automation/agent-loops.md#loop-lifecycle).

## Can I start and watch a loop from the Workspace?

Yes, if you're a platform user: open the rail's **Loops** tab in the chat and click **Start a loop** (or **Start another loop**). The small form asks for the agent (in a room, which participant), what to do each run, the number of runs, and an optional cost budget, and states the guardrails up front — it stops by itself after 3 identical replies in a row and after 3 consecutive failures, with no time limit unless you set one. Each row shows **Run N of M**, how much of each guardrail is left, **Stop** while it's active, and a status that says *why* it ended: **Done**, **Done, with errors**, **Stopped by you**, **Stopped — cost budget reached** / **time limit reached** / **it stopped making progress**, or **Failed**. External clients never see this tab, and loop runs are left out of the activity shown to them. See [Agent Loops](../automation/agent-loops.md#from-the-workspace).

## Can I just tell my agent in chat to run a loop or check something every few minutes?

Yes. Asking for repetition in chat starts a Trinity loop — or, for a single deferred follow-up, a reminder. The platform routes "run a loop" and "do this every few minutes" to these server-side primitives and withholds the harness's own loop and wake-up tools in headless runs, because those would report success and then never fire once the turn ended. The loop it starts is an ordinary one: it shows in the Loops tab and the Workspace rail, each iteration is an execution, and the standard guardrails apply. See [Agent Loops](../automation/agent-loops.md#asking-the-agent-to-loop).

## Can each loop iteration see the previous iteration's result?

Yes, through the message template. The template supports `{{run}}` (the 1-indexed run number) and `{{previous_response}}` (the trailing 2,000 characters of the previous iteration's response, empty on run 1). Because `{{previous_response}}` is lossy, don't rely on it for real artifacts — instruct the agent to keep the draft or report in a workspace file and re-read it each run, since the agent's filesystem persists across iterations. See [Agent Loops](../automation/agent-loops.md).

## How do I keep a loop from running forever or burning my budget?

Loops have several independent brakes. `max_runs` (required, capped at 100) is the guaranteed ceiling; an optional `stop_signal` ends the loop early when the agent's response contains that substring; `max_cost_usd` stops the loop at a run boundary once accumulated spend meets the budget; `max_duration_seconds` sets a wall-clock deadline (up to 7 days); and no-progress detection stops the loop when consecutive runs return identical responses (default: 3 identical runs, set the threshold to 0 to disable). All of these are checked between runs — the in-flight iteration always finishes first, so a single run can overshoot a budget or deadline. A separate failure policy governs what happens when an iteration *errors* rather than runs long — see the abort-vs-continue question below. You can also click **Stop** at any time for a graceful stop. See [Agent Loops](../automation/agent-loops.md).

## What happens to a loop when one iteration fails — does it stop or keep going?

That's set by the loop's failure policy, `on_failure`. The default is `abort`: the loop fails fast, stopping the moment an iteration errors. Switch to `continue` and the loop tolerates a failed iteration and moves on to the next run — but it still aborts if failures pile up, once it hits `max_consecutive_failures` (default 3) errors in a row; a successful run resets that streak. A continue-mode loop that finishes with some tolerated failures reports a `completed_with_errors` status. See [Agent Loops](../automation/agent-loops.md).

## How do I run many tasks in parallel on one agent?

Use fan-out: it dispatches 1–50 independent tasks to an agent concurrently (up to `max_concurrency`, default 3, max 10), waits for all of them to complete or hit the overall deadline, and returns aggregated results in input order. It's available via the `fan_out` MCP tool or `POST /api/agents/{name}/fan-out` — there is no UI, and it currently works only on the calling agent itself. Every batch gets a server-minted `fan_out_id`; each subtask is its own execution record stamped with that id and consumes one of the agent's parallel slots, and a batch can be read back while it's still running with `get_fan_out_result` or `GET /api/agents/{name}/fan-out/{fan_out_id}`. See [Fan-Out](../automation/fan-out.md).

## What are skills and playbooks, and how do I run one?

A skill is a reusable capability packaged in the platform's skills library — one or more GitHub repositories synced to Trinity. Each skill is a folder built around a `SKILL.md` instruction file, optionally bundled with supporting scripts, templates, and resources. When a skill is assigned to an agent, it becomes a playbook: the agent's **Playbooks** tab lists assigned skills with a **Run** button that sends the skill as a task, and in the **Chat** tab you can type `/` to autocomplete a playbook command with ghost text showing the syntax and argument hints. See [Skills and Playbooks](../automation/skills-and-playbooks.md).


## How do I find out which agents already have a given skill?

The **Library** page's Skills tab (`/library?tab=skills`) shows, for every skill, an *Assigned to N agents* line with chips linking straight to each agent's Skills tab. It's bounded — the first four agents, then **+N more**. Admins see the whole fleet; everyone else sees their own and shared agents, and the wording says which. Below the listing sits **Assigned but no longer in the library**: assignments whose skill was removed upstream. That list matters because revocation works by publishing a new version without the offending skill, and the package stays on each agent until you unassign it — with the **×** on its chip right there, or from the agent's Skills tab. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Can I assign a skill from the Library page?

Yes. Each skill card on the Library's Skills tab has an **Assign to…** control listing the agents you may still assign it to — agents you own (an admin sees every agent), minus those that already hold it. Pick one and click **Assign**; the delivery note appears under the control: *Assigned and delivered — available now*, *applies on next start*, or a named failure. The **×** on an agent chip unassigns the skill from that agent, and it appears only where you're allowed to make the change — an agent merely shared with you shows as a holder but carries no control. Both surfaces write the same per-agent assignment, so it doesn't matter whether you start from the skill or from the agent's **Skills** tab. There is no Sync button on the Library; to retry a failed delivery, assign the same agent again. See [Skills and Playbooks](../automation/skills-and-playbooks.md#from-the-library).

## Can a skill be a whole folder of files instead of a single markdown file?

Yes. A skill is a full-directory package, not just one markdown file: alongside the `SKILL.md` instructions it can carry scripts, templates, and any resource files the capability needs. When the skill is assigned, Trinity injects the entire directory into the agent, versioned by the folder's content so re-syncs only push real changes. This lets a skill ship helper code and assets, not only prose. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## How do I assign skills to an agent, and do I need to restart it?

No restart. Open the agent's **Skills** tab, tick the skills, and click **Save assignments** — the save delivers straight away to a running agent, and the note beside the button says what happened: *delivered — available now*, *the agent is stopped, so it applies on next start*, *still installing* (a large package outlived the 20-second wait and continues in the background), or a named failure. Each skill lands as a whole directory under `~/.claude/skills/<name>/`, so its scripts and resources come too, and the per-skill result tells you honestly whether it's missing a declared binary or environment variable. **Sync now** is the repair action for a delivery that didn't land, not a required step, and every assignment change refreshes the open **Playbooks** tab and `/` autocomplete without a reload. Admins manage the *sources* the library syncs from in **Settings → Agents**; skills themselves are edited in their GitHub repository, not in Trinity. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

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

Because skills carry executable scripts, and with fleet re-inject on, a source tracking a branch head would put every merged upstream commit onto every agent with no human in the loop — and the community catalog accepts public contributions. The bundled community source is therefore pinned to a release tag of the catalog that we bump; new upstream commits reach your fleet only when an admin moves the source to a newer tag — by editing its ref (`PUT /api/skills/sources/{id}`) or removing and re-adding it — and syncs. Custom sources track a branch by default, since you control who writes to them, but you can choose **Tag** under **Track** when adding one to pin it the same way. A pinned tag that later resolves to a different commit is **refused** (`moved_tag`) rather than adopted; annotated and lightweight tags are compared by the commit they point at, so a release tag that hasn't moved is never refused. See [Skills and Playbooks](../automation/skills-and-playbooks.md#supply-chain-posture-pinned-tags).

## The add-project-management plugin installs nothing — where did it go?

It's deprecated: it stays in the marketplace for one release as a pointer stub, and its skill now lives in agent-dev as `/agent-dev:add-project-management`. The five runtime skills it installs — `/project-init`, `/project-task`, `/project-steward`, `/project-reconcile` and `/project-intake` — are also standalone in the agent-dev plugin (now 30 skills) and mirrored into the bundled community skills catalog, so on Trinity you can assign them to a deployed agent from the Library without running the installer at all; `/project-init` materializes the project standard itself on first run. Also new in agent-dev: `/agent-dev:commit` doubles as a plain save — with no issue claimed it becomes a checkpoint commit of the agent-state files (memory, outputs, skills, `CLAUDE.md`, `template.yaml` — never `.env` or anything credential-shaped). See [Abilities Marketplace](../automation/abilities-marketplace.md#deprecated-add-project-management-v130).

## Do I need to install the trinity plugin inside my agent before it can run /trinity skills?

No. Trinity's agent image ships with the `abilityai` marketplace registered and `trinity@abilityai` installed, and every container boot re-installs it if it's missing — whether or not the agent's `template.yaml` declares it. That's what lets an agent created from a bare repository run `/trinity:onboard` in place and write its own declaration. A `plugins:` block that omits it never uninstalls it (the boot step only adds), and a declaration that points the `abilityai` marketplace name at another repository is ignored. An agent still on an image built before the pre-install pays one install at its next boot; an image built with `--build-arg TRINITY_PREINSTALL_PLUGINS=0` (air-gapped installs) skips the pre-install, and what the boot step couldn't fetch is reported in the agent's compatibility report rather than failing the start. See [Abilities Marketplace](../automation/abilities-marketplace.md#plugins-inside-a-deployed-agent).

## My template declares schedules — will Trinity create them?

Yes. A `schedules:` block in `template.yaml` is materialized as real schedules when the agent is created, through the UI, the API, and MCP alike. Each entry needs a `name`, a strict 5-field `cron`, and a `message`; up to 20 per template. A malformed entry is dropped with a named error rather than failing the creation, and every materialized schedule inherits the agent's execution timeout so it can never exceed the agent's own cap. See [Creating Agents](../agents/creating-agents.md).

## What should a schedule's message contain?

Ideally a single line that invokes one of the agent's skills by name — `/daily-briefing` — and nothing else: no inline instructions, arguments, or business logic. The logic then lives in the versioned playbook, so changing what a scheduled run does is an edit to the skill, and the execution history shows *which* playbook ran. A prose message is a second, unversioned copy of the procedure that drifts from the skill it describes. The abilities wizards generate schedules in this shape and `/create-agent:review` flags prose messages as findings. See [Scheduling](../automation/scheduling.md#schedules-from-a-template).

## My scheduled skill hangs every run and burns its whole timeout — why?

The skill almost certainly asks a question at one of its decision points. On an unattended cron there is nobody to answer, so every run blocks on the prompt until the execution times out with nothing committed. The fix belongs in the skill, not in the schedule message: give it a headless run mode — the abilities convention is a `--autonomous` argument, so the schedule message becomes `/<skill> --autonomous` — in which the skill never prompts, takes the safe default at each gate, never takes a destructive path a gate was protecting, and records any non-trivial decision as a `needs-attention` line instead of guessing. The orchestrator bundle's gated skills already ship this mode. See [Abilities Marketplace](../automation/abilities-marketplace.md#playbook-calls-the-unit-of-inter-agent-work).

## Can I use a legacy timezone name like `US/Eastern` in a schedule?

Yes — legacy IANA aliases such as `US/Eastern`, `Asia/Calcutta`, and `Europe/Kiev` resolve correctly. A timezone the platform genuinely cannot resolve is rejected when you create the schedule, with a message naming the problem, rather than being accepted and then silently never firing. See [Scheduling](../automation/scheduling.md).
