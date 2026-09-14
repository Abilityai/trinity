# Trinity FAQ — Agents

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What are the different ways to create an agent?

Three ways, all equivalent: in the UI, click **Create Agent** in the Dashboard header (or **Use Template** on the Library page), pick a template source, enter a name (lowercase, hyphens allowed), and click **Create**; via the API, `POST /api/agents` with `{"name": "my-agent", "template": "github:Org/repo"}`; or via the MCP tool `create_agent`. In every case Trinity clones the template, builds an isolated Docker container from the base image, copies the template files into `/home/developer/`, and starts the agent automatically. See [Creating Agents](../agents/creating-agents.md).

## Can I create an agent from a private GitHub repository or a specific branch?

Yes. Use the `github:Org/repo` template format, and append `@branch` to build from a specific branch: `github:Org/repo@branch`. Private repositories require a GitHub personal access token configured before you use them as a template source. See [Creating Agents](../agents/creating-agents.md) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Can I create an agent from a public GitHub repository without a token?

Yes. A `github:owner/repo` template that points at a **public** repository clones anonymously — no personal access token required. This is source-mode only, though: without a token the agent can read and run the template but cannot push its changes back, and features that write to GitHub (the working-branch auto-sync heartbeat, fork-to-own) stay unavailable until you add a token. Add a per-agent or personal PAT later to unlock write access. See [Creating Agents](../agents/creating-agents.md) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Do I need to write my own template to create an agent?

No. The **Library** page (formerly Templates -- the old `/templates` path redirects to `/library`) ships a curated **Starter Templates** section (recommended starters: `scout`, `sage`, `scribe`) auto-discovered from the platform's `config/agent-templates/` directory, plus any GitHub templates an admin has configured as cards. If you want a blank slate, choose **From Scratch** — it creates a minimal agent with a default `CLAUDE.md` you can build on. A `github:owner/repo` template doesn't even need a `template.yaml`: Trinity creates the agent from the repository as it is, and because the `trinity` plugin is pre-installed in every agent, that agent can make itself Trinity-compatible afterwards by running `/trinity:onboard` in its own chat. See [Creating Agents](../agents/creating-agents.md) and the [Advanced Features FAQ](advanced-features.md#i-created-an-agent-straight-from-a-bare-github-repo-how-do-i-make-it-trinity-compatible).

## What goes in template.yaml?

`template.yaml` is the agent's metadata file: `display_name`, `description`, `resources`, `credentials`, and `runtime` (which CLI harness the agent uses, plus an optional model override). It can also declare `data_paths` (globs under `data/` holding runtime data, auto-added to the agent's `.gitignore`), `schedules:` (recurring work created with the agent), `plugins:` (the Claude Code marketplace plugins the agent depends on, re-installed on every boot), and `fork_to_own: required` (copy the template into your own GitHub repo at creation). The rest of a template is `CLAUDE.md` (agent instructions), `.mcp.json.template` (MCP config with `${VAR}` placeholders), and `.env.example` (required credentials). Note that GitHub template metadata is cached for 10 minutes, so `template.yaml` edits may not appear immediately. See [Creating Agents](../agents/creating-agents.md).

## How do I make sure my agent's Claude Code plugins survive a rebuild or a move?

Declare them in a `plugins:` block in `template.yaml` — a `marketplaces:` list (name plus `owner/repo` or an https source) and an `installed:` list of `plugin@marketplace` entries. Trinity materializes the block at creation as a committed, secret-free `~/.trinity/plugins.yaml`, and on every container boot the agent re-installs anything declared but missing, headlessly. Plugins installed by hand inside the agent are *not* captured back — they live in gitignored Claude Code state and vanish when the workspace is reconstituted from git onto a fresh volume or another host — so add them to the template too. One plugin you never have to declare is `trinity@abilityai`: the platform pre-installs it in the agent image and re-ensures it on every boot whether or not the template lists it (an omitted entry is never uninstalled). Adding the block to an existing agent takes effect on its next restart; the abilities `/trinity:sync plugins` skill installs the difference right away. See [Creating Agents](../agents/creating-agents.md#declared-plugins).

## Why did creating my agent fail with an error about the base image?

Every template's `base_image` is validated against an allowlist, and blocked images are rejected with HTTP 403. By default only `trinity-agent-base:*` images are permitted; an admin can configure additional allowed images. Either switch the template to the standard base image or ask your admin to extend the allowlist. See [Creating Agents](../agents/creating-agents.md).

## What's the difference between the Claude Code, Codex, and Gemini runtimes?

The runtime is the CLI harness that executes the agent inside its container, chosen via `runtime.type` in `template.yaml`: `claude-code` (default), `codex` (OpenAI Codex), or `gemini-cli`. They differ in auth (Codex uses an `OPENAI_API_KEY` credential — Trinity logs the CLI in with it before the first turn, so an API-key Codex agent works out of the box and a rotated key is picked up on the next turn — or a ChatGPT-plan login you run yourself with `codex login`; Codex skips Claude-subscription auto-assignment, and Gemini uses a Gemini API key), instruction file (Codex reads `AGENTS.md` — Trinity mirrors the template's `CLAUDE.md` into it at startup), session resume (a Codex agent cannot resume, so its Workspace turns replay the visible history as text rather than carrying working memory forward), and cost reporting (estimated for Codex, actual for the others). The runtime is fixed at creation: to change it, recreate the agent from a template that declares a different runtime. See [Agent Runtimes](../agents/agent-runtimes.md).

## How do I start and stop an agent?

Toggle the Running/Stopped switch on the Dashboard (Grid tiles and List rows both carry it) or the Agent Detail page — a spinner shows while the state changes. Via the API, use `POST /api/agents/{name}/start` and `POST /api/agents/{name}/stop`; via MCP, `start_agent(name)` and `stop_agent(name)`. See [Managing Agents](../agents/managing-agents.md).

## Do my agents come back on their own after the server reboots?

Yes. Agent containers are created with Docker's `unless-stopped` restart policy, so after a host reboot or a Docker daemon restart every agent that was running comes back by itself and its schedules resume — nobody has to start agents by hand. An agent you deliberately stopped stays stopped, because the policy honours Docker's manual-stop flag, so a quarantined agent is never resurrected by a reboot. Agents may briefly show as stopped while Docker brings them back. The policy is set when the container is created, so agents created before it shipped adopt it on their next recreate (a resource, runtime, or base-image change), not on a plain restart; an upgrading install can sweep its existing fleet once. See [Managing Agents](../agents/managing-agents.md#start-and-stop) and the [restart-policy migration guide](../../migrations/AGENT_RESTART_POLICY_2026-09.md).

## What survives an agent restart, and what survives a recreate?

A plain stop/start keeps the same container, so everything in it survives — except `/tmp`, which is RAM-backed scratch space. A recreate replaces the container (this happens on resource, capability, or mount changes, per-agent API key changes, and image upgrades), but the agent's home directory `/home/developer` lives on a durable named Docker volume, so your workspace files and anything under `data/` survive recreates, image upgrades, template re-pulls, and subscription switches. A template re-pull resets git-tracked files to the template's main branch but never wipes untracked files. Only files written outside the home volume are lost on recreate. See [Agent Data & Portability](../agents/agent-data.md).

## What happens when I delete an agent — is everything gone immediately?

No. Deleting stops and removes the container immediately, but it is a soft delete: the workspace volumes, schedules, chat history, sharing records, permissions, and credentials are all preserved until a retention sweep purges them (default: 180 days). Schedules stop firing right away. During the retention window an admin can recover the agent — recovery restores the record but does not recreate the container, so start the agent afterwards. See [Managing Agents](../agents/managing-agents.md).

## Can I create a short-lived, disposable agent that cleans itself up?

Yes, with an enterprise-tier feature: a disposable ("ghost") agent is created with a hard budget — a maximum number of runs and/or a time limit. When the budget is spent it is discarded immediately and completely: the container and its data are removed with no soft-delete window and no reserved name. This is aimed at one-off jobs, often spawned on demand by another agent, rather than long-lived agents you manage by hand. In a community build agents are always long-lived (no budget). See [Managing Agents](../agents/managing-agents.md).

## Why can't I create a new agent with the same name as one I deleted?

Because deletion is a soft delete, the old agent's record still exists and reserves the name until the retention sweep purges it (default: 180 days). Pick a different name, or ask an admin to recover the soft-deleted agent if you actually want it back. See [Managing Agents](../agents/managing-agents.md).

## What happens when I rename an agent?

Renaming is atomic: Trinity updates the agent's name across every database record that references it (schedules, executions, chat history, sharing, permissions, tags, skills, shared files, and more), renames the Docker container, and broadcasts the change live over WebSocket. Click the pencil icon next to the agent name on the Agent Detail page, or use `PUT /api/agents/{name}/rename` / the MCP tool `rename_agent`. Only owners and admins can rename, and system agents cannot be renamed. See [Managing Agents](../agents/managing-agents.md).

## How do I change an agent's friendly display name without renaming it?

Set a **display label**. The label is the human-facing name shown across the UI — the header, agent pickers, search, sort, and the activity timeline — and editing it never moves the immutable slug, so URLs, MCP tool names, schedules, and webhooks all keep working. It's owner-only, and clearing it back to blank falls back to showing the slug. This is the change you want for everyday relabeling. See [Managing Agents](../agents/managing-agents.md).

## What's the difference between renaming an agent and giving it a display label?

Renaming moves the immutable slug — the agent's canonical name that URLs, MCP tool names, schedules, and webhooks all resolve to — so it's the heavier operation and rewrites records across the platform. A display label is a presentation-only friendly name layered on top of the unchanged slug. Prefer the label for day-to-day relabeling and reserve a rename for when you genuinely need the canonical name to change. See [Managing Agents](../agents/managing-agents.md).

## How is the Agent Detail page organized?

It lands on **Overview** (trends, health, recent activity) and puts everything else in tabs — Tasks, Chat, Reports, Canvas, Schedules, Loops, Playbooks, Credentials, Payments, Files, Info, plus owner-only tabs such as Access, Sharing, Permissions, Folders, Skills, and Settings; Dashboard, Brain, Git, and A2A appear only when the agent supports them, and tabs that don't fit the window collapse into a **More ▾** menu. Every tab is deep-linkable with `?tab=`. The header above the tabs holds live "now" state — status, CPU/memory gauges, cost, and quick controls such as the Running and Autonomy switches — and two doors into other surfaces: **Workspace** opens the agent in the Workspace, and **Talk** starts a voice call there. The **Chat** tab is stateless (each message starts fresh) and carries a **Continue in Workspace →** link that opens the Workspace in its own browser tab when you want a conversation that keeps its memory. There is no browser Terminal tab any more (see the SSH question below), and `?tab=session` redirects to the Workspace. See [Managing Agents](../agents/managing-agents.md#the-agent-detail-page).

## What's in an agent's Settings tab?

It's the owner-only home for per-agent configuration, in sections: **Guardrails** (max turns for chat and for tasks; the other overrides are API-only), **Parallel Capacity** (how many tasks may run at once, up to the admin's fleet ceiling, with live slot usage), **Expose via MCP** (publish the agent as its own MCP tool), **Trinity access key** (the agent-scoped key it uses to call Trinity's MCP tools, with health status and a **Regenerate** action — the running container is replaced to pick up the new key), **Reliability** (the dispatch circuit breaker, plus **Wake this agent when an operator answers**, which starts a turn as soon as an approval or question is answered instead of waiting for the agent's next scheduled run), and **Voice** (spoken voice notes on messaging channels). A **Cross-model validation** section appears only on entitled installations. Resources, read-only mode, and the Running/Autonomy switches stay in the agent header and on the Dashboard. See [Agent Configuration](../agents/agent-configuration.md#the-settings-tab).

## How do I limit how much CPU and memory an agent can use?

Click the gear button ("Configure resources") in the agent header to open the resource modal. Memory options are 1g through 64g and CPU options are 1, 2, 4, 8, or 16 cores; either limit can be left as "Inherit default". Limits are enforced at the container level via Linux cgroups and take effect on the next restart. Admins set the fleet-wide defaults for new agents under Settings. See [Agent Configuration](../agents/agent-configuration.md).

## How long can an agent task run before it times out?

Each agent has an execution timeout, configurable from 60 to 7200 seconds (default: 3600 seconds / 60 minutes), applied to every trigger method — tasks, chat, schedules, MCP, and paid endpoints. The agent timeout is also the ceiling for its schedules: lowering it below an active schedule's own timeout is rejected with `400 error=agent_timeout_below_active_schedules`, and creating a schedule with a longer timeout than the agent cap is rejected too. Manage it via `GET`/`PUT /api/agents/{name}/timeout`. See [Agent Configuration](../agents/agent-configuration.md).

## What does read-only mode do?

Read-only mode prevents the agent from modifying source files (`*.py`, `*.js`, and so on) inside its container by intercepting `Write`, `Edit`, and `NotebookEdit` tool calls with hooks. Generated output is still allowed under `output/*` and `content/*`. Toggle it in the agent header. Codex agents enforce the same restriction through the Codex sandbox (`--sandbox read-only`) instead of tool hooks. See [Agent Configuration](../agents/agent-configuration.md).

## What does the autonomy toggle actually control?

Autonomy is the master gate for an agent's scheduled operations: turning it off means none of that agent's schedules fire, and turning it back on lets them resume. It doesn't touch each schedule's own on/off switch — a schedule you disabled individually stays disabled when autonomy comes back on, and one you left enabled resumes automatically. You can flip it from the Dashboard (Grid or List), the Agent Detail header, or via `PUT /api/agents/{name}/autonomy`. It only affects schedules — it is not a general on/off switch for the agent itself. See [Agent Configuration](../agents/agent-configuration.md) and [Scheduling](../automation/scheduling.md).

## Why can't my agent schedule its own wake-up or run a cron job from inside a task?

Because a task, schedule, loop, or MCP call runs the agent as a one-shot turn, and nothing it starts survives the end of that turn. Claude Code's built-in tools that promise a *later* event — scheduled wake-ups, cron entries, workflow and task-output notifications, messages to other local sessions, push notifications, remote triggers — would let the agent plan around an event that never arrives, so Trinity withholds that whole tool family from headless runs and points the agent at the platform's own mechanisms instead: `run_agent_loop` for repetition, `set_reminder` for a deferred self-trigger, and `chat_with_agent` to reach another agent. Subagents are unaffected (the turn waits for them). A background shell command still running when the turn ends is killed, and the execution records that it was, instead of reporting a clean success. The tool denial reaches an agent on its next recreate after the base image is rebuilt; the prompt guidance applies as soon as the platform is updated. See [Agent Runtimes](../agents/agent-runtimes.md#headless-runs-on-claude-code).

## How do I move an agent to another Trinity instance?

Export the agent's runtime data with `POST /api/agents/{name}/data/export` (or the MCP tool `export_agent_data`) — it captures everything under `/home/developer/data` as a tar archive with a manifest. On the destination instance, recreate the agent from the same template, then import the archive with `POST /api/agents/{name}/data/import` (or `import_agent_data`). Import only writes inside `data/` and skips any entries that try to escape it. Export requires a running agent, and a per-agent lock prevents concurrent export/import. See [Agent Data & Portability](../agents/agent-data.md).

## Can I get shell access to my agent's container?

Yes, over SSH with a key you supply — the browser terminal tab that used to live on the agent page has been retired. Access is admin-only and off by default: an admin switches on **Enable SSH Access** under **Settings → Access**, then requests credentials for a *running* agent with `POST /api/agents/{name}/ssh-access` (or the MCP tool `get_agent_ssh_access`), passing a public key (`ssh-keygen -t ed25519`) and a `ttl_hours` (default 4, maximum 24). Trinity injects the key into the container's `authorized_keys` and returns a ready-to-run `ssh` command with the host and the agent's own port from the 2222–2262 range; the key is removed when the TTL expires. Password authentication is not supported, and the server never generates or sees a private key. See [Agent Terminal](../agents/agent-terminal.md).

## How do I browse and edit the files inside my agent's workspace?

Open the **Files** tab on the Agent Detail page: the left panel is a searchable file tree of the workspace (`/home/developer/`), and the right panel previews the selected file — images, video, audio, PDF, and text are supported. Text files can be edited and saved inline, **New folder** creates a directory in place (workspace-confined), and files can be deleted with warnings on protected paths. Toggle **Show hidden files** to reveal dotfiles like `.env` and `.claude/`. Downloads are supported up to 100 MB per file. See [Agent Files](../agents/agent-files.md).

## How do I get a file into my agent's container, and can I send a ZIP?

Attach it to a message. On the Agent Detail **Chat** tab, images reach the agent as vision content and everything else — plain text, CSV, JSON, and ZIP — is written to `/home/developer/uploads/` inside the container, readable by name; a ZIP is stored as-is, not extracted, and because the paperclip picker filters to images and text-like files you drop a ZIP onto the input instead (3 files per message, 5 MB each). In the Workspace, files you attach or drop on the conversation land in the agent's inbox and appear under **Files you sent** in the rail (up to 20 per drop, 25 MB each). For bulk runtime data, use the data import endpoint, which restores a tar archive into `data/`. See [Agent Chat](../agents/agent-chat.md#file-attachments) and [Agent Data & Portability](../agents/agent-data.md).

## Where can I see my agent's logs?

The **Logs** tab on the Agent Detail page shows the container's stdout/stderr with auto-refresh and smart auto-scroll, also available via `GET /api/agents/{name}/logs` or the MCP tool `get_agent_logs`. All container logs are additionally captured by the Vector log aggregator into structured JSON files — `/data/logs/platform.json` for platform services and `/data/logs/agents.json` for agents — which you can query with `docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .`. Anything your agent prints to stdout or stderr lands there automatically. See [Agent Logs and Telemetry](../agents/agent-logs.md).

## What is the compatibility report on the Overview tab, and can Trinity fix what it finds?

Once an agent is running, Trinity checks its workspace against a catalogue of best-practice conventions — a valid `template.yaml`, a non-gitignored `.claude/` directory, defined playbooks, accidentally committed secrets, and more — and shows the findings in the Agent Detail **Overview** tab, ranked HARD / SOFT / INFO. It also reports whether the platform-provided Trinity plugin is present and, if not, why it was withheld. It is purely advisory and never blocks creation or deployment; Claude-specific checks are skipped for Codex and Gemini agents. The nine gitignore-related findings offer a one-click **Fix** button that rewrites the agent's `.gitignore` in place — the change stays uncommitted until the agent's next git sync. Use **Re-run analysis** to re-check at any time. See [Creating Agents](../agents/creating-agents.md#compatibility-validation).

## I already have a GitHub repo — how do I turn it into an agent?

Create an agent from it and pick an **import intent**. **Clone** (the default) keeps the agent wired to that remote and pushes back to it. **Fork** forks it into your own GitHub account first, with `upstream` pointing at the original. **Copy** takes a one-time snapshot of the files, strips the git history, and gives the agent a standalone workspace with no remote, no token, and no sync. Copy is the right choice when you want to start *from* someone's repository without staying attached to it. After creation the dialog runs the compatibility check inline and shows the result before you leave. See [Creating Agents](../agents/creating-agents.md).

## My agent was created from a public template and can't push anywhere. Can I fix that without recreating it?

Yes — use **Bind to your own repo** on the agent's **Git** tab. Trinity creates the destination repository under your account (private by default), pushes the agent's *current* workspace history into it, repoints `origin`, saves your token as the agent's own, and rebuilds the container so the change survives a restart. The agent keeps its name, identity, name reservation, data volumes, and history — nothing is re-provisioned. The agent must be running, and the action is owner-only and human-only (there is deliberately no MCP tool, since it needs your personal token). See [GitHub Sync](../integrations/github-sync.md).

## Why did a Push untrack some of my agent's files, and where do I see what changed?

Before every push Trinity rebuilds the agent's `.gitignore` around its own rules — managed defaults (caches, virtualenvs, local databases, generated content) above your rules, and a non-overridable protected floor (credential files, the agent's `.trinity/` state) below them — and then untracks anything the rules now cover. Because git is last-match-wins, a `!negation` you wrote keeps winning over the defaults, so a file you chose to keep is never silently dropped; the one exception is a negation *beneath* a directory-form pattern such as `node_modules/`, which git never descends into — those are reported as shadowed rather than fixed. The push then tells you what it did: the sync response and the `git_sync` MCP result carry `removed_paths`, `unignored_paths`, and `shadowed_negations`, the Git tab's toast and the commit message state the counts and paths, and when the tracked set actually changed Trinity also files an operator-queue notice, so an unattended scheduled sync can't untrack files for weeks unnoticed. A newly un-ignored path that was a secret is already in the remote's history — rotate it and remove the rule. See [GitHub Sync](../integrations/github-sync.md#what-a-push-does-to-gitignore).

## Can my agent disable or edit its own guardrails?

No. The hook scripts live in root-owned `/opt/trinity/`, and the registration that makes Claude Code run them lives in its admin-controlled managed settings (`/etc/claude-code/managed-settings.json`) — root-owned, read-only, taking precedence over user and project settings, and outside the git-synced working tree — so neither an edit inside the container nor a push to the agent's repository can remove it. The credential-file protection hook refuses writes to those paths as well. On every boot the container checks that the registration is present and unwritable and logs `GUARDRAILS: ERROR` if not. Owners can *tighten* guardrails per agent (turn limits in the Settings tab; deny lists and disallowed tools via the API) but never loosen the baseline. See [Agent Guardrails](../agents/agent-guardrails.md).

## Where do I see which credentials an agent still needs?

The **Credentials** tab shows a per-variable checklist: what the agent needs (read from its live workspace, so a forked or hand-edited agent reports its actual requirements), which are already set (probed from the agent's own `.env` — names only, values are never read), and where to get the missing ones via the template author's setup link. Fill values in directly on the checklist. It renders even when the agent is stopped, and a failed status probe is reported as a failure rather than shown as "nothing configured". See [Credential Management](../credentials/credential-management.md).

## Where does the list of GitHub templates come from?

Three tiers, in order: an admin-curated list in Settings, if one exists — in which case nothing else is consulted; otherwise a curated remote registry fetched over HTTPS, so the starter catalogue can refresh without upgrading Trinity; otherwise the bundled defaults, which are empty. Registry results are cached for about an hour with a durable last-known-good copy, and every failure degrades quietly to the next tier, so agent creation never depends on a registry being reachable. See [Creating Agents](../agents/creating-agents.md).
