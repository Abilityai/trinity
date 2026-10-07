# Trinity FAQ — Agents

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What are the different ways to create an agent?

Three ways, all equivalent: in the UI, click **Create Agent** in the Dashboard header (or **Use Template** on the Library page), pick a template source, enter a name (lowercase, hyphens allowed), and click **Create**; via the API, `POST /api/agents` with `{"name": "my-agent", "template": "github:Org/repo"}`; or via the MCP tool `create_agent`. In every case Trinity clones the template, builds an isolated Docker container from the base image, copies the template files into `/home/developer/`, and starts the agent automatically. See [Creating Agents](../agents/creating-agents.md).

## Can I create an agent from a private GitHub repository or a specific branch?

Yes. Use the `github:Org/repo` template format, and append `@branch` to build from a specific branch: `github:Org/repo@branch`. Private repositories require a GitHub personal access token configured before you use them as a template source. See [Creating Agents](../agents/creating-agents.md) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Can I create an agent from a public GitHub repository without a token?

Yes. A `github:owner/repo` template that points at a **public** repository clones anonymously — no personal access token required. This is source-mode only, though: without a token the agent can read and run the template but cannot push its changes back, and features that write to GitHub (the working-branch auto-sync heartbeat, fork-to-own) stay unavailable until you add a token. Add a per-agent or personal PAT later to unlock write access. See [Creating Agents](../agents/creating-agents.md) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Do I need to write my own template to create an agent?

No. The **Library** page (formerly Templates -- the old `/templates` path redirects to `/library`) ships a curated **Starter Templates** section (recommended starters: `scout`, `sage`, `scribe`) auto-discovered from the platform's `config/agent-templates/` directory, plus any GitHub templates an admin has configured as cards. If you want a blank slate, choose **From Scratch** — it creates a minimal agent with a default `CLAUDE.md` you can build on. A `github:owner/repo` template doesn't even need a `template.yaml`: Trinity creates the agent from the repository as it is, and because the `trinity` plugin is pre-installed in every agent, that agent can make itself Trinity-compatible afterwards by running `/trinity:onboard` in its own chat. See [Creating Agents](../agents/creating-agents.md) and the [Advanced Features FAQ](advanced-features.md#i-created-an-agent-straight-from-a-bare-github-repo--how-do-i-make-it-trinity-compatible).

## What goes in template.yaml?

`template.yaml` is the agent's metadata file: `display_name`, `description`, `resources`, `credentials`, and `runtime` (which CLI harness the agent uses, plus an optional model override). It can also declare `data_paths` (globs under `data/` holding runtime data, auto-added to the agent's `.gitignore`), `schedules:` (recurring work created with the agent), `plugins:` (the Claude Code marketplace plugins the agent depends on, re-installed on every boot), and `fork_to_own: required` (copy the template into your own GitHub repo at creation). The rest of a template is `CLAUDE.md` (agent instructions), `.mcp.json.template` (MCP config with `${VAR}` placeholders), and `.env.example` (required credentials). Note that GitHub template metadata is cached for 10 minutes, so `template.yaml` edits may not appear immediately. See [Creating Agents](../agents/creating-agents.md).

## How do I make sure my agent's Claude Code plugins survive a rebuild or a move?

Declare them in a `plugins:` block in `template.yaml` — a `marketplaces:` list (name plus `owner/repo` or an https source) and an `installed:` list of `plugin@marketplace` entries. Trinity materializes the block at creation as a committed, secret-free `~/.trinity/plugins.yaml`, and on every container boot the agent re-installs anything declared but missing, headlessly. Plugins installed by hand inside the agent are *not* captured back — they live in gitignored Claude Code state and vanish when the workspace is reconstituted from git onto a fresh volume or another host — so add them to the template too. One plugin you never have to declare is `trinity@abilityai`: the platform pre-installs it in the agent image and re-ensures it on every boot whether or not the template lists it (an omitted entry is never uninstalled). Adding the block to an existing agent takes effect on its next restart; the abilities `/trinity:sync plugins` skill installs the difference right away. See [Creating Agents](../agents/creating-agents.md#declared-plugins).

## Why did creating my agent fail with an error about the base image?

Every template's `base_image` is validated against an allowlist, and blocked images are rejected with HTTP 403. By default only `trinity-agent-base:*` images are permitted; an admin can configure additional allowed images. Either switch the template to the standard base image or ask your admin to extend the allowlist. See [Creating Agents](../agents/creating-agents.md).

## What's the difference between the Claude Code, Codex, and Gemini runtimes?

The runtime is the CLI harness that executes the agent inside its container, chosen via `runtime.type` in `template.yaml`: `claude-code` (default), `codex` (OpenAI Codex), or `gemini-cli`. They differ in auth (Codex uses an `OPENAI_API_KEY` credential — Trinity logs the CLI in with it before the first turn, so an API-key Codex agent works out of the box and a rotated key is picked up on the next turn — or a ChatGPT-plan login you run yourself with `codex login`; Codex skips Claude-subscription auto-assignment, and Gemini uses the platform Gemini key from Settings), instruction file (Codex reads `AGENTS.md` — at startup Trinity copies the template's `CLAUDE.md` into it only when the workspace has no `AGENTS.md`; one you ship yourself is left as it is), session resume (only Claude Code resumes a session in the Workspace — a Codex agent's Workspace turns replay the visible history as text, and Gemini's headless turns never resume), safety hooks (read-only mode and the guardrail hooks are Claude Code hooks; Codex enforces read-only through its sandbox), and cost reporting (reported by Claude Code, estimated from tokens for Gemini and Codex). The runtime is fixed at creation: to change it, recreate the agent from a template that declares a different runtime. See [Agent Runtimes](../agents/agent-runtimes.md).

## How do I start and stop an agent?

Toggle the Running/Stopped switch on the Dashboard (Grid tiles and List rows both carry it) or the Agent Detail page — a spinner shows while the state changes. Via the API, use `POST /api/agents/{name}/start` and `POST /api/agents/{name}/stop`; via MCP, `start_agent(name)` and `stop_agent(name)`. See [Managing Agents](../agents/managing-agents.md).

## Do my agents come back on their own after the server reboots?

Yes. Agent containers are created with Docker's `unless-stopped` restart policy, so after a host reboot or a Docker daemon restart every agent that was running comes back by itself and its schedules resume — nobody has to start agents by hand. An agent you deliberately stopped stays stopped, because the policy honours Docker's manual-stop flag, so a quarantined agent is never resurrected by a reboot. Agents may briefly show as stopped while Docker brings them back. The policy is set when the container is created, so agents created before it shipped adopt it on their next recreate (a resource, runtime, or base-image change), not on a plain restart; an upgrading install can sweep its existing fleet once. See [Managing Agents](../agents/managing-agents.md#start-and-stop) and the [restart-policy migration guide](../../migrations/AGENT_RESTART_POLICY_2026-09.md).

## What survives an agent restart, and what survives a recreate?

A plain stop/start keeps the same container, so everything in it survives — except `/tmp`, which is RAM-backed scratch space. A recreate replaces the container (this happens on resource, capability, or mount changes, per-agent API key changes, and image upgrades), but the agent's home directory `/home/developer` lives on a durable named Docker volume, so your workspace files and anything under `data/` survive recreates, image upgrades, template re-pulls, and subscription switches. A template re-pull resets git-tracked files to the template's main branch but never wipes untracked files. Only files written outside the home volume are lost on recreate. See [Agent Data & Portability](../agents/agent-data.md).

## What happens when I delete an agent — is everything gone immediately?

No. Deleting stops and removes the container immediately, but it is a soft delete: the workspace volumes, schedules, chat history, sharing records, permissions, and credentials are all preserved until a retention sweep purges them (default: 180 days). Schedules stop firing right away. During the retention window an admin can recover the agent — recovery restores the record but does not recreate the container, so start the agent afterwards. See [Managing Agents](../agents/managing-agents.md).

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

It's the owner-only home for per-agent configuration, in sections: **Guardrails** (max turns for chat and for tasks — both default to 50, settable from 1 to 500, blank inherits the platform default; the other overrides are API-only), **Parallel Capacity** (how many tasks may run at once, up to the admin's fleet ceiling, with live slot usage), **Expose via MCP** (publish the agent as its own MCP tool), **Trinity access key** (the agent-scoped key it uses to call Trinity's MCP tools, with health status and a **Regenerate** action — the running container is replaced to pick up the new key), **Reliability** (the dispatch circuit breaker, plus **Wake this agent when an ask it raised ends**, which starts a turn as soon as an approval or question is answered, cancelled or expires instead of waiting for the agent's next scheduled run), **Git sync** (auto-sync, pulling from GitHub, and pausing schedules while sync fails), and **Voice** (spoken voice notes on messaging channels). Further sections can appear on installations with an enterprise entitlement. Resources, read-only mode, and the Running/Autonomy switches stay in the agent header and on the Dashboard. See [Agent Configuration](../agents/agent-configuration.md#the-settings-tab).

## How do I limit how much CPU and memory an agent can use?

Click the gear button ("Configure resources") in the agent header to open the resource modal. Memory options are 1g through 64g and CPU options are 1, 2, 4, 8, or 16 cores; either limit can be left as "Inherit default". Limits are enforced at the container level via Linux cgroups and take effect on the next restart. Admins set the fleet-wide defaults for new agents under Settings. If you pick more cores than the Docker host has, the container gets the host's CPU count instead of failing to start. See [Agent Configuration](../agents/agent-configuration.md).

## How long can an agent task run before it times out?

Each agent has an execution timeout, configurable from 60 to 7200 seconds (default: 3600 seconds / 60 minutes), applied to every trigger method — tasks, chat, schedules, MCP, and paid endpoints. The agent timeout is also the ceiling for its schedules: lowering it below an active schedule's own timeout is rejected with `400 error=agent_timeout_below_active_schedules`, and creating a schedule with a longer timeout than the agent cap is rejected too. Manage it via `GET`/`PUT /api/agents/{name}/timeout`. See [Agent Configuration](../agents/agent-configuration.md).

## What does read-only mode do?

Read-only mode prevents the agent from modifying source and config files (`*.py`, `*.js`, `*.yaml`, `CLAUDE.md`, `.env`, and so on) inside its container by intercepting `Write`, `Edit`, `MultiEdit`, and `NotebookEdit` tool calls with a hook. Generated output is still allowed under `content/*`, `output/*`, `reports/*` and `exports/*`, plus `*.log` and `*.txt` files. Toggle it in the agent header. The configuration lives in a root-owned file the agent cannot edit. Codex agents enforce the same restriction through the Codex sandbox (`--sandbox read-only`) instead of tool hooks; Gemini CLI turns are not covered. See [Agent Configuration](../agents/agent-configuration.md).

## What does the autonomy toggle actually control?

Autonomy is the master gate for an agent's scheduled operations: turning it off means none of that agent's schedules fire, and turning it back on lets them resume. It doesn't touch each schedule's own on/off switch — a schedule you disabled individually stays disabled when autonomy comes back on, and one you left enabled resumes automatically. You can flip it from the Dashboard (Grid or List), the Agent Detail header, or via `PUT /api/agents/{name}/autonomy`. It only affects schedules — it is not a general on/off switch for the agent itself. See [Agent Configuration](../agents/agent-configuration.md) and [Scheduling](../automation/scheduling.md).

## Why can't my agent schedule its own wake-up or run a cron job from inside a task?

Because a task, schedule, loop, or MCP call runs the agent as a one-shot turn, and nothing it starts survives the end of that turn. Claude Code's built-in tools that promise a *later* event — scheduled wake-ups, cron entries, workflow and task-output notifications, messages to other local sessions, push notifications, remote triggers — would let the agent plan around an event that never arrives, so Trinity withholds that whole tool family from headless runs and points the agent at the platform's own mechanisms instead: `run_agent_loop` for repetition, `set_reminder` for a deferred self-trigger, and `chat_with_agent` to reach another agent. Subagents are unaffected (the turn waits for them). A background shell command still running when the turn ends is killed, and the execution records that it was, instead of reporting a clean success. The tool denial reaches an agent on its next recreate after the base image is rebuilt; the prompt guidance applies as soon as the platform is updated. See [Agent Runtimes](../agents/agent-runtimes.md#headless-runs-on-claude-code).

## How do I move an agent to another Trinity instance?

Export the agent's runtime data with `POST /api/agents/{name}/data/export` (or the MCP tool `export_agent_data`) — it captures everything under `/home/developer/data` as a tar archive with a manifest. On the destination instance, recreate the agent from the same template, then import the archive with `POST /api/agents/{name}/data/import` (or `import_agent_data`). Import only writes inside `data/` and skips any entries that try to escape it. Export requires a running agent, and a per-agent lock prevents concurrent export/import. Two size caps apply: the export itself is refused with `413` above `AGENT_DATA_EXPORT_MAX_BYTES` (default 5 GB), and the inline `?format=base64` variant that the MCP tool uses has its own smaller cap (`AGENT_DATA_INLINE_MAX_BYTES`, default 10 MB) — above it the response directs you to the streaming download instead. See [Agent Data & Portability](../agents/agent-data.md).

## Can I get shell access to my agent's container?

Yes, over SSH with a key you supply — the browser terminal tab that used to live on the agent page has been retired. Access is admin-only and off by default: an admin switches on **Enable SSH Access** under **Settings → Access**, then requests credentials for a *running* agent with `POST /api/agents/{name}/ssh-access` (or the MCP tool `get_agent_ssh_access`), passing a public key (`ssh-keygen -t ed25519`) and a `ttl_hours` (default 4, maximum 24). Trinity injects the key into the container's `authorized_keys` and returns a ready-to-run `ssh` command with the host and the agent's own port from the 2222–2262 range; the key is removed when the TTL expires. Password authentication is not supported, and the server never generates or sees a private key. See [Agent Terminal](../agents/agent-terminal.md).

## How do I browse and edit the files inside my agent's workspace?

Open the **Files** tab on the Agent Detail page: the left panel is a searchable file tree of the workspace (`/home/developer/`), and the right panel previews the selected file — images, video, audio, PDF, and text are supported. Text files can be edited and saved inline, **New folder** creates a directory in place (workspace-confined), and files can be deleted with warnings on protected paths. Toggle **Show hidden files** to reveal dotfiles like `.env` and `.claude/`. Downloads are supported up to 100 MB per file. See [Agent Files](../agents/agent-files.md).

## How do I get a file into my agent's container, and can I send a ZIP?

Attach it to a message. On the Agent Detail **Chat** tab, images reach the agent as vision content and everything else — plain text, CSV, JSON, and ZIP — is written to `/home/developer/uploads/` inside the container, readable by name; a ZIP is stored as-is, not extracted, and because the paperclip picker filters to images and text-like files you drop a ZIP onto the input instead (3 files per message, 5 MB each). In the Workspace, files you attach or drop on the conversation land in the agent's inbox and appear under **Files you sent** in the rail (up to 20 per drop, 25 MB each). For bulk runtime data, use the data import endpoint, which restores a tar archive into `data/`. See [Agent Chat](../agents/agent-chat.md#file-attachments) and [Agent Data & Portability](../agents/agent-data.md).

## Where can I see my agent's logs?

The **Logs** tab on the Agent Detail page shows the container's stdout/stderr in a fixed-height scrollable panel: **Auto-refresh (10s)** is a toggle, a line-count selector sets how many lines are fetched, a refresh button fetches on demand, and auto-scroll is smart (new content scrolls to the bottom until you scroll up yourself). The same log is available via `GET /api/agents/{name}/logs` or the MCP tool `get_agent_logs`. All container logs are additionally captured by the Vector log aggregator into structured JSON files — `/data/logs/platform.json` for platform services and `/data/logs/agents.json` for agents — which you can query with `docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .`. Anything your agent prints to stdout or stderr lands there automatically. See [Agent Logs and Telemetry](../agents/agent-logs.md).

## What is the compatibility report on the Overview tab, and can Trinity fix what it finds?

Once an agent is running, Trinity checks its workspace against a catalogue of best-practice conventions — a valid `template.yaml`, a non-gitignored `.claude/` directory, defined playbooks, accidentally committed secrets, and more — and shows the findings in the Agent Detail **Overview** tab, ranked HARD / SOFT / INFO. It also reports whether the platform-provided Trinity plugin is present and, if not, why it was withheld. It is purely advisory and never blocks creation or deployment; Claude-specific checks are skipped for Codex and Gemini agents. The nine gitignore-related findings offer a one-click **Fix** button that rewrites the agent's `.gitignore` in place — the change stays uncommitted until the agent's next git sync. Use **Re-run analysis** to re-check at any time. The headline counts only must-fix findings, and a template with no `resources` block passes because the agent inherits the fleet-wide default. See [Creating Agents](../agents/creating-agents.md#compatibility-validation).

## Is there a limit on how many canvases an agent can hold?

Yes: each agent can hold 100 canvases (`CANVAS_MAX_PER_AGENT` in `.env`, default 100), and the Canvas tab shows the count as you approach it. At the limit the agent can still *update* every canvas it has, but creating a *new* one is refused with a message telling it to retire one first — nothing is ever deleted automatically to make room. An agent that hits this is usually writing a new canvas per run instead of keeping one per topic current; ask it to reuse a canvas, or clear the finished ones from the **Canvas** tab on Agent Detail (**Delete**, or **Manage** to select several). Deleting the default `main` canvas is fine — the agent recreates it the next time it writes. Pinning, deleting and sharing canvases from the Workspace are covered in the [Chat & Sessions FAQ](chat-and-sessions.md#can-i-pin-or-delete-canvases-and-who-is-allowed-to). See [Agent Canvas → Removing canvases](../agents/agent-canvas.md#removing-canvases).

## I already have a GitHub repo — how do I turn it into an agent?

Create an agent from it and pick an **import intent**. **Clone** (the default) keeps the agent wired to that remote; it pushes back on its own working branch when the repository is yours, and is pull-only otherwise. **Fork** forks it into your own GitHub account first, with `upstream` pointing at the original. **Copy** takes a one-time snapshot of the files, strips the git history, and gives the agent a standalone workspace with no remote, no token, and no sync. Copy is the right choice when you want to start *from* someone's repository without staying attached to it. After creation the dialog runs the compatibility check inline and shows the result before you leave. See [Creating Agents](../agents/creating-agents.md).

## Why was my new agent created pull-only instead of getting its own working branch?

An agent gets a working branch, auto-sync, and paused schedules while sync fails only when the repository is yours: not a shared catalog template, owned by the GitHub account your token belongs to, reached with your own token rather than the platform-wide one, and a push check passes. An organization's repository stays pull-only by default, even when your token can write to it. The create response's `git_mode.reason` names which condition failed and how to fix it — usually "add your own GitHub token" or "fork it to your own repository". A `kind: "deployment"` create, including `trinity deploy --repo`, is always pull-only. See [GitHub Sync](../integrations/github-sync.md#creating-an-agent-with-sync).

## How do I turn my agent's auto-sync on or off?

Open the agent's **Settings** tab and use the **Git sync** section: **Auto-sync to GitHub every 15 minutes**, **Pull changes from GitHub on every sync cycle**, and **Pause schedules while sync is failing**. All three apply on the next cycle with no restart. The same switches are `PUT /api/agents/{name}/git/auto-sync`, `PUT /api/agents/{name}/git/pull-sync` and `PUT /api/agents/{name}/git/freeze-schedules-if-failing`. An agent with no GitHub repository shows a note pointing you to the **Git** tab. See [GitHub Sync](../integrations/github-sync.md#turning-auto-sync-on-or-off).

## My agent was created from a public template and can't push anywhere. Can I fix that without recreating it?

Yes — use **Bind to your own repo** on the agent's **Git** tab. Trinity creates the destination repository under your account (private by default), pushes the agent's *current* workspace history into it, repoints `origin`, saves your token as the agent's own, and rebuilds the container so the change survives a restart. The agent keeps its name, identity, name reservation, data volumes, and history — nothing is re-provisioned. The agent must be running, and the action is owner-only and human-only (there is deliberately no MCP tool, since it needs your personal token). See [GitHub Sync](../integrations/github-sync.md).

## Why did a Push untrack some of my agent's files, and where do I see what changed?

Before every push Trinity rebuilds the agent's `.gitignore` around its own rules — managed defaults (caches, virtualenvs, local databases, generated content) above your rules, and a non-overridable protected floor (credential files, the agent's `.trinity/` state) below them — and then untracks anything the rules now cover. Because git is last-match-wins, a `!negation` you wrote keeps winning over the defaults, so a file you chose to keep is never silently dropped; the one exception is a negation *beneath* a directory-form pattern such as `node_modules/`, which git never descends into — those are reported as shadowed rather than fixed. The push then tells you what it did: the sync response and the `git_sync` MCP result carry `removed_paths`, `unignored_paths`, and `shadowed_negations`, the Git tab's toast and the commit message state the counts and paths, and when the tracked set actually changed Trinity also files an operator-queue notice, so an unattended scheduled sync can't untrack files for weeks unnoticed. A newly un-ignored path that was a secret is already in the remote's history — rotate it and remove the rule. See [GitHub Sync](../integrations/github-sync.md#what-a-push-does-to-gitignore).

## Does my agent's `.claude/settings.json` get committed to its repository?

Yes, normally. It is not gitignored, so a template's project settings and the hooks a plugin registers travel with the repository. Trinity checks the content on every commit it makes and keeps the file out when the staged copy registers hooks under `/opt/trinity/`, carries a credential-bearing key such as `env` or `apiKeyHelper`, or is not valid JSON. It then keeps the last committed copy (or untracks the file when there is no usable one) and never touches the copy on the agent's disk. See [GitHub Sync](../integrations/github-sync.md#what-happens-to-claudesettingsjson).

## Why does my agent's `.gitignore` contain rules I never wrote, right after creation?

Because Trinity merges its canonical `.gitignore` into every auto-syncing GitHub agent immediately after creation, before the first sync cycle can commit runtime state or credential files — the same rules the bundled templates already ship with, so an agent created from any GitHub repository never auto-commits caches, virtualenvs, local databases, or its `.env` back to your repo. Your own rules stay in place beneath the managed defaults, and a `!negation` you write keeps winning (see the push question above for how the rules are ordered). Declaring `data_paths` in `template.yaml` appends those globs to the same file. See [Creating Agents](../agents/creating-agents.md) and [Agent Data & Portability](../agents/agent-data.md).

## Can my agent disable or edit its own guardrails?

No. The hook scripts live in root-owned `/opt/trinity/`, and the registration that makes Claude Code run them lives in its admin-controlled managed settings (`/etc/claude-code/managed-settings.json`) — root-owned, read-only, taking precedence over user and project settings, and outside the git-synced working tree — so neither an edit inside the container nor a push to the agent's repository can remove it. The credential-file protection hook refuses writes to those paths as well, and the Bash deny-list refuses the obvious `sudo` commands against them. Each hook runs as a fixed command with an empty environment under an isolated interpreter, so a shell prefix or startup file the agent sets cannot run ahead of it. On every boot the container checks that the registration is present and unwritable and logs `GUARDRAILS: ERROR` if not, and the agent's `/health` re-checks the registration on every request. Owners can *tighten* guardrails per agent (turn limits in the Settings tab; deny lists and disallowed tools via the API) but never loosen the baseline. See [Agent Guardrails](../agents/agent-guardrails.md).

## Where do I see which credentials an agent still needs?

The **Credentials** tab shows a per-variable checklist: what the agent needs (read from its live workspace, so a forked or hand-edited agent reports its actual requirements), which are already set (probed from the agent's own `.env` — names only, values are never read), and where to get the missing ones via the template author's setup link. Fill values in directly on the checklist. It renders even when the agent is stopped, and a failed status probe is reported as a failure rather than shown as "nothing configured". See [Credential Management](../credentials/credential-management.md).

## Where does the list of GitHub templates come from?

Three tiers, in order: an admin-curated list in Settings, if one exists — in which case nothing else is consulted; otherwise a curated remote registry fetched over HTTPS, so the starter catalogue can refresh without upgrading Trinity; otherwise the bundled defaults, which are empty. Registry results are cached for about an hour with a durable last-known-good copy, and every failure degrades quietly to the next tier, so agent creation never depends on a registry being reachable. See [Creating Agents](../agents/creating-agents.md).

## What does "What is this repository?" mean when I create an agent from my own repo?

It tells Trinity how to bind the agent to git. **An agent** means the repository *is* the agent — its memory, skills and state — so it saves its work to its own branch. **A deployment of a codebase** means the repository is a product the agent runs, so it only pulls updates and never pushes. The question appears only for a custom repository with the **Clone** intent; list templates are always pull-only, and copy, fork and fork-to-own don't need it. After creation the dialog shows what Trinity decided and warns plainly when an agent was created pull-only, and the Git tab shows **Agent · own branch**, **Agent · own repo**, or **Pull-only**. See [Creating Agents](../agents/creating-agents.md#importing-an-existing-github-repository).

## I asked for 8 CPUs but the server only has 4 — will my agent fail to start?

No. Docker refuses a CPU limit above the host's CPU count, so Trinity caps the request at the number of CPUs the host has and logs a warning. The agent's setting keeps the value you chose, so the agent is not recreated on every start. See [Agent Configuration](../agents/agent-configuration.md#resource-allocation).

## Does a Gemini agent remember earlier turns?

In chat, yes: each chat turn resumes the agent's own Gemini session, never another run's, and a model change or a history reset starts fresh. Headless turns — tasks, schedules, and Workspace turns — never resume a Gemini session, so they run without the previous turn's working memory. If you need a conversation that carries working memory forward in the Workspace, use a Claude Code agent. See [Agent Runtimes](../agents/agent-runtimes.md#gemini-cli-turns).

## Why did my agent's chat reply show up as a failure even though it wrote some text?

Because the Claude Code CLI reported that the turn ended in an error. Trinity records such a turn as failed, with the real cause, rather than presenting partial text or an error message as the agent's answer. The one exception is when the session transcript on disk proves the turn actually finished; then the answer is kept and marked as recovered. See [Agent Runtimes](../agents/agent-runtimes.md#how-failures-are-reported).

## Do guardrails and read-only mode protect agents on every runtime?

Not equally. The guardrail hooks and read-only mode are Claude Code tool hooks. Codex enforces read-only mode through its own sandbox, and Gemini CLI turns pass through neither. Credential redaction applies to every runtime, because the backend scrubs execution output whatever produced it. The turn limit is enforced only on Claude Code; on Gemini and Codex the execution timeout bounds the run. See [Agent Guardrails](../agents/agent-guardrails.md).

## Why did the Canvas tab jump to a different canvas while I was reading?

Because the agent created a new canvas, and the panel follows a newly created canvas so you see what was just made. It waits while you are in **Manage**, searching, or sharing, and picking a canvas yourself in the meantime cancels the switch. A rewrite of a canvas that already exists never moves you. See [Agent Canvas](../agents/agent-canvas.md#how-it-works).

## How do I give every agent on my instance the same rules?

Write them in the **Trinity Prompt** (Settings → General, admin only). Trinity adds it after the platform instructions on every chat and task turn, for every agent and every runtime, so a saved change reaches running agents from their next turn with no restart. Add fleet rules only, keep it short, and never copy the platform's own instructions into it. A system manifest with a top-level `prompt:` replaces it. See [Recommended Trinity Prompt](../agents/recommended-fleet-prompt.md).

## My agent started a background task on itself and heard nothing back. Should it send the task again?

No. The `execution_id` it got back is a receipt: the task arrived and is queued, running or done. Re-sending, especially a reworded version, can run the work twice. Read the outcome with `get_execution_result(agent_name, execution_id)` — `running` does not mean stuck — or turn on result injection so the answer lands in the chat when it finishes. See [Self-Execute](../agents/self-execute.md).
