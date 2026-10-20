# Agent Configuration

Per-agent settings for autonomy, read-only mode, resources, capabilities, execution timeout, reliability, and runtime.

## How It Works

Each agent has independent configuration options that control its behavior, resource usage, and execution constraints. Settings are managed through the Agent Detail page or the API.

### The Settings Home

The Agent Detail page has one **Settings** tab (visible to owners only), pinned at the end of the tab strip — it never collapses into the **More ▾** menu. Inside it, a left rail lists panes, each answering one question. The pane you are on is in the URL as `?tab=settings&section=<pane>`, so a reload stays put and the browser's Back button steps between panes. On narrow windows the rail becomes a **Section** select above the pane.

| Pane | `?section=` | Question | What it holds |
|------|-------------|----------|---------------|
| **General** | `general` | What this agent is | **Identity** — display name, agent id (**Rename…**), avatar (**Change…**, **Next emotion**), the template's description, tags. **Model and funding** — runtime, model, funding. **Execution** — execution timeout, parallel capacity, read-only mode, memory and CPU. **Template** — what the agent was deployed from, with a **Technical details** dialog holding the template metadata. **Danger zone** — reset the workspace to main (only when a repository is bound) and delete |
| **Guardrails** | `guardrails` | What it may do | **Bounds** — guardrail rules behind **Manage rules**, [permissions to change itself](#permissions-to-change-itself), the number of skills that need approval with **Open Skills**. **Unprompted work** — autonomy, read-only here. **Checks** — cross-model validation, shown only when the instance has the matching entitlement. **Reliability** — the dispatch circuit breaker and wake-on-ask-ended |
| **People** | `people` | Who works with it | The former Access tab, unchanged — see [Agent Sharing](../sharing-and-access/agent-sharing.md) |
| **Channels** | `channels` | Where outsiders reach it | **Voice replies** (see [Voice Replies](../advanced/voice-replies.md)), and a row that opens the **Sharing** tab, which still holds chat policy, channels, public links and the client roster |
| **Integrations** | `integrations` | What calls it, what it calls | **Programmatic access** — **Expose via MCP** and the agent's **Trinity access key** (see [MCP Server](../integrations/mcp-server.md#dedicated-agent-tools-expose-via-mcp)); rows that open the **Permissions**, **A2A** and **Payments** tabs, each shown only when you can see that tab |
| **Storage** | `storage` | What it keeps and shares | **Repository** — the bound repository, **Bind to your own repo**, **GitHub authentication** and the **Sync** switches (see [GitHub Sync](../integrations/github-sync.md)). **Credentials** (see [Credential Management](../credentials/credential-management.md)). **Shared folders** and **File sharing** (see [Agent Files](agent-files.md)) |

**Find a setting.** The search box above the rail filters the panes by the words you would use for a control — `timeout` keeps General, `token` and `folders` keep Storage — even when the word is not in a pane's name. A query that matches nothing says so and suggests words to try.

**One restart banner.** Every change that waits for a restart is named in one bar at the top of Settings, whichever pane you are on: **File sharing**, **Shared folders**, **Memory and CPU**, **Guardrail rules**, **GitHub token** and **Funding**. The bar carries one button — **Restart now** for a running agent, **Start now** for a stopped one — and keeps its footprint when nothing is waiting (*Every change here is live*). The list clears when the agent actually comes up: Trinity settles it on the agent's status edge from stopped to running, not on the button, so a stop and start from the header settles it too, and a start that fails leaves every entry listed because nothing applied. After the restart each row re-reads the backend and lists itself again if its change still has not taken.

**Who sees what.** Settings is owner-only. The system agent gets **General** (read-only), **Guardrails** (without the autonomy section) and **Integrations**. A `?section=` naming a pane you cannot see lands on the first pane you can.

**Where the old tabs went.** The Info, Credentials, Access and Folders tabs are gone, and their old links still resolve: `?tab=info` opens General, `?tab=guardrails` opens Guardrails, `?tab=credentials` and `?tab=folders` open Storage, `?tab=access` opens People. The page header keeps identity, the running state and badges, the **Autonomy** chip, Start/Stop, the bound GitHub repository with its branch and push/pull counts, the tags (owners edit them from the header or here) and a small context-window ring — nothing else. Live CPU, memory and spend are read on the **Overview** tab. The **Git** tab keeps the operational half of git: status, pending changes, recent commits, **Pull**, **Push** and **Refresh**.

The sections below say which pane holds each setting. Everything is also available through the API.

### Permissions to Change Itself

An agent's own key acts for its owner, so without a fence it could rewrite any agent its owner holds. Trinity fences those changes behind permissions that an instance admin grants per agent. No agent holds any of them by default. People and the system agent never need them.

The **Permissions to change itself** card under **Settings › Guardrails › Bounds** shows one toggle per permission, with what it allows, what is refused without it, and who granted it and when:

| Toggle | Permission | Lets the agent | Without it |
|--------|-----------|----------------|------------|
| **Change skills** | `skills.manage` | Assign, unassign and re-deliver skills, and set skill approval gates on other agents — see [Skills and Playbooks](../automation/skills-and-playbooks.md#who-can-change-an-agents-skills) | Every skill change is refused, its own included |
| **Change other agents' schedules** | `schedules.manage` | Create, change, enable, disable or delete another agent's schedules and their webhooks | It still manages its own schedules, never another agent's |
| **Change instructions** | `instructions.manage` | Write `CLAUDE.md`, `AGENTS.md` and `.claude/` files (not skills) through the Files API, and reset an agent to `main` while keeping its state | Those writes are refused, its own instructions included |
| **Manage other agents** | `agents.manage` | Create, deploy and delete agents, deploy a system, and change an agent's model, resources, timeout, public-channel model, read-only mode and guardrails | It can still spawn and discard short-lived (ephemeral) helpers |
| **Start projects** | `projects.manage` | Start a project on its owner's behalf, on installations that offer projects | It still works on projects it is already on |

Bounds that hold even with a permission:

- The agent reaches only agents its **owner owns**. Any other agent answers as not found.
- It can never lift its **own** read-only mode or guardrails.
- It deletes only agents it spawned.
- No permission covers autonomy, the API-key setting, capabilities, parallel capacity or renaming. Those stay with a person. The card shows autonomy beside the toggles as a switch a person sets, never a grant. Adding people to a project or changing who can see it is never granted either.

The owner and admins see the card; anyone else does not. Only an instance admin, signed in to the UI, can flip a toggle: owners see the state with *Only an instance admin can change these*. Every grant and revocation is audited. A system agent and an ephemeral agent cannot receive a permission, and an agent still being calibrated cannot receive **Change instructions**.

**When an agent is refused.** An agent without the permission gets a `403` that names it (`skill_management_not_permitted`, `schedule_management_not_permitted`, `instruction_management_not_permitted`, `agent_management_not_permitted` or `project_management_not_permitted`), and the refusal is audited. The message tells the agent to raise a question to its operator whose title includes the permission id (for example `schedules.manage`). The card lists those open requests under **Open permission requests**, with a link to Operations. Answering the request grants nothing: an admin grants the permission on this card.

**After upgrading.** No agent holds the schedule, instruction or agent permissions after the upgrade that introduces them. An orchestrating agent that creates agents, writes another agent's schedules or instructions, or reconfigures agents is refused until an admin grants the permission.

The fence covers the platform's routes, not the agent's own container: an agent can still edit files in its own workspace with its own tools.

### Parallel Capacity

How many tasks the agent may run concurrently (`max_parallel_tasks`, default 3). Work beyond the limit queues and drains as slots free up.

- Set it under **Settings › General › Execution › Parallel capacity**. The row shows the effective value in jobs, how many are running now and the fleet ceiling; the editor accepts a whole number from 1 to the ceiling.
- **Fleet ceiling (admin):** an admin caps the whole fleet via `GET`/`PUT /api/settings/max-parallel-tasks-ceiling` (default 10, range 1–32). Owners pick any value up to the ceiling. If an agent's stored value exceeds a later-lowered ceiling, the stored value is kept but the *effective* limit is clamped to the ceiling — the row says so (*Stored N is above the fleet ceiling, so M applies*).
- API: `GET /api/agents/{name}/capacity` returns the stored value, the ceiling, and the effective limit.

### Autonomy Mode

Master gate for the agent's scheduled operations.

- Toggle from the Dashboard (Grid tile or List row) or the **Autonomy** chip in the Agent Detail header. **Settings › Guardrails › Unprompted work** shows the switch read-only for now; the chip stays the control
- When disabled, none of the agent's schedules fire
- The toggle does **not** change each schedule's own on/off switch: a schedule you disabled stays disabled when you turn autonomy back on, and one you left enabled resumes automatically
- API: `GET /api/agents/{name}/autonomy` and `PUT /api/agents/{name}/autonomy`

### Read-Only Mode

Prevents modification of source files (`*.py`, `*.js`, etc.) inside the agent container.

- Toggle under **Settings › General › Execution › Read-only mode**
- Uses a `PreToolUse` hook to intercept `Write`, `Edit`, `MultiEdit`, and `NotebookEdit` tool calls. Codex agents enforce it through the Codex sandbox instead
- Blocked by default: source and config files (`*.py`, `*.js`, `*.ts`, `*.sh`, `*.yaml`, `*.json`, `CLAUDE.md`, `.claude/*`, `.env`, and similar)
- Allowed by default: `content/*`, `output/*`, `reports/*`, `exports/*`, `*.log`, `*.txt` (generated files are permitted)
- The backend writes the mode's configuration to a root-owned file in the container (`/opt/trinity/read-only-config.json`), which the agent cannot edit. A copy under `~/.trinity/` decides only while the root-owned file is missing
- API: `GET /api/agents/{name}/read-only` and `PUT /api/agents/{name}/read-only`

### Resource Allocation

Per-agent memory and CPU limits, enforced at the container level (Linux cgroups).

- Edit **Memory and CPU** under **Settings › General › Execution**. Each limit can also be left as **Inherit default**; the option names the platform default it inherits.
- Memory options: 1g, 2g, 4g, 8g, 16g, 32g, 64g. CPU options: 1, 2, 4, 8, 16 cores.
- A change is saved at once and applied on the next restart: the row carries a *restart* badge and the bar at the top of Settings lists **Memory and CPU** until the agent comes back up. Nothing restarts on save.
- **CPU is capped at the host's CPU count.** Docker refuses a CPU limit above the number of CPUs on the host. If you choose more cores than the host has, the container gets the host's CPU count instead and a warning is logged. The setting keeps the value you chose, so the agent is not recreated on every start.
- API: `GET /api/agents/{name}/resources` and `PUT /api/agents/{name}/resources`
- **Full capabilities mode**: grants containers system-level access (Docker socket, network tools) when needed

**Fleet-wide defaults (admin):** the default CPU and memory for *new* agent containers are set platform-wide via `GET`/`PUT /api/settings/agent-defaults/resources` (admin-only; CPU 1/2/4/8/16, memory 1g--32g). Changes apply to new containers only -- restart existing agents to pick up new defaults.

**Scratch space (`/tmp`):** each agent's `/tmp` is a RAM-backed tmpfs, hardened `noexec,nosuid`. Its size is operator-configurable via the `AGENT_TMP_SIZE` environment variable on the backend (default `512m`; accepts `<int>m`/`<int>g`); the `noexec,nosuid` flags are fixed. Because the tmpfs counts against the agent's memory limit and blocks execution, heavy build/scratch work (pip and npm installs, compiling extensions) is redirected to a disk-backed `TMPDIR` at `/home/developer/.tmp` on the home volume. The `/tmp` size is applied at container create, so a changed `AGENT_TMP_SIZE` is picked up on recreate, not a plain restart.

### Execution Timeout

Configurable time limit for agent executions.

- Edit **Execution timeout** under **Settings › General › Execution**. The control takes whole minutes, 1 to 120 (default 60); the API stores seconds. An empty, fractional or out-of-range value is refused before any request is sent, with a message that names the range.
- When the backend refuses the save because an active schedule runs longer than the new cap, the row says how many schedules and how long the longest runs, and offers **Open Schedules**. Shorten that schedule first, or keep the cap at or above its timeout.
- A saved timeout applies to new runs; a schedule or task can set a shorter timeout, never a longer one.
- Range: 60--7200 seconds (default: 3600 seconds / 60 minutes).
- Applies to all trigger methods: task, chat, schedule, MCP, and paid endpoints.
- Slot TTL is set to the timeout value plus a 5-minute buffer.
- **Schedule ceiling**: the agent timeout is the ceiling for every one of its schedules. Setting an agent timeout below an active schedule's `timeout_seconds` is rejected with `400 error=agent_timeout_below_active_schedules`. Conversely, creating or updating a schedule with `timeout_seconds > agent.execution_timeout_seconds` is rejected with `400 error=schedule_timeout_exceeds_agent_cap`. Raise the agent cap first, then the schedule.
- API: `GET /api/agents/{name}/timeout` and `PUT /api/agents/{name}/timeout`

### Dispatch Circuit Breaker

Optional per-agent protection (default: off) that stops sending new work to an agent whose container repeatedly answers with authentication failures -- for example, an expired API key. Instead of queueing tasks that are doomed to fail, the platform rejects new executions immediately with `503` and a `Retry-After` header, and fails any tasks already queued for that agent.

How it behaves when enabled:

- After 3 consecutive authentication failures, the breaker opens and new dispatches fast-fail.
- Recovery is automatic: after a cooldown, one probe execution is let through. Success closes the breaker; failure extends the cooldown (exponential backoff).
- Timeouts and ordinary task errors do **not** trip the breaker -- only auth-type failures count.

Two switches must both be on for the breaker to engage: the per-agent toggle (owner-only) and the platform-wide `DISPATCH_BREAKER_ENABLED` environment variable (also off by default).

When a breaker is open, the agent header and the agent's Dashboard tile show a "⚡ circuit open" badge, and the Overview tab's health panel shows a "Circuit open" chip. Switch the breaker on or off under **Settings › Guardrails › Reliability**, or via the API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/circuit-breaker` | GET | Current state of both breakers (dispatch + transport), plus config flags |
| `/api/agents/{name}/circuit-breaker` | PUT | Enable or disable the per-agent breaker (`{"enabled": true}`, owner-only) |
| `/api/agents/{name}/circuit-breaker/reset` | POST | Force both breakers closed without waiting for cooldown (admin-only) |

### Wake When an Ask Ends

How one of the agent's parked requests (an approval, a question) ended always reaches the agent, but by default it is only read on the agent's *next* turn. An agent with no schedule has no next turn, so an approved action would wait indefinitely. Turn on **Wake this agent when an ask it raised ends** under **Settings › Guardrails › Reliability** and the agent is woken for one turn when a person answers (trigger `operator_response`) — and also when an ask is cancelled or expires, so it stops waiting for an answer that will not come (trigger `operator_ending`, one turn per agent per cancellation sweep or expiry). Off by default.

- Each wake-up is one turn the owner pays for: one per answer, one per cancellation or expiry event. A stopped agent is not woken.

- Only the owner or an admin can change it, and never with an agent's key: an agent's own key is refused with `403` even though it acts for the owner. Each wake is a turn the owner pays for, so an agent cannot switch on its own wake-ups. An agent can still read the setting.
- API: `GET /api/agents/{name}/operator-resume` and `PUT /api/agents/{name}/operator-resume` (`{"enabled": true}`)
- See [Approvals](../automation/approvals.md) for the operator side, and for how an agent reads an ending back (`get_my_ask`).

### Funding

Controls which credential the agent uses for Claude: the platform API key or a Claude subscription.

- Choose under **Settings › General › Model and funding › Funding**: **Platform API key** or one of your subscriptions. The badge beside it reads **Subscription**, **API key** or **Not set**. The cost figures on Overview follow this choice
- Trinity replaces the container as the switch requires; while the container's environment still disagrees with the saved choice, the bar at the top of Settings lists **Funding**

### Model Selection

Choose the Claude model used for tasks and scheduled executions.

- Change it under **Settings › General › Model and funding › Model**. The model is read from the agent, so the row can be read or changed only while the agent is running; stopped, it says *Start the agent to read or change its model*
- Available models lead with the current generation: **Opus 5.5** (the most capable Opus, marked *(latest)*), **Fable 5.1** (most capable, best for the longest tasks) and **Sonnet 5** (fast and smart, with a 1M-token context window), followed by the prior Opus, Sonnet and Haiku generations. **Opus 5** and **Fable 5** are still offered and still supported; they are simply no longer the latest in their tiers.
- Custom model input is supported.
- If Claude Code refuses the model — an id it does not know, or one newer than the agent's Claude Code version — the run fails with error code `model_unsupported` (HTTP `400`) and the API's own message. It is not treated as an authentication failure, so the agent is not switched to another subscription or to the platform API key. Pick a supported model, or rebuild the base image so the agent runs a newer Claude Code.
- Selection is persisted to `localStorage`; `model_used` is recorded in the execution audit trail.
- **Platform default**: when an agent has no model override, executions use the platform default model configured in Settings → Platform. The UI now surfaces this fallback so empty selections aren't mistaken for failures.

### Runtime

Set via `runtime.type` in `template.yaml`.

- `claude-code` (default)
- `gemini-cli`
- `codex` (OpenAI Codex)

See [Agent Runtimes](agent-runtimes.md) for capability differences (session resume, cost reporting, MCP support).

## For Agents

Agents inherit their configuration at container creation time. Changes to resource allocation, API key, or runtime trigger a container recreate. Changes to autonomy, read-only mode, timeout, circuit breaker, and model selection take effect on the next execution without restarting the container.

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/capability-grants` | GET | All five self-change permissions, held or not, with who granted each and when. Owner or admin |
| `/api/agents/{name}/capability-grants/{capability}` | PUT | Grant or revoke one — `{"granted": true\|false}`. An admin in a signed-in session only; any API key is refused. `422 unknown_capability`, `system_agent_not_grantable`, `ephemeral_agent_not_grantable` or `calibrating_agent` |

An agent's own key changes resources, timeout, the public-channel model and the chat model, or another agent's read-only mode and guardrails, only while it holds **Manage other agents**, and only on agents its owner owns. Autonomy, capabilities, parallel capacity and the API-key setting refuse every agent key. See [Authentication → What a Key Is Not](../api-reference/authentication.md#what-a-key-is-not).

## See Also

- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
- [Agent Guardrails](agent-guardrails.md)
