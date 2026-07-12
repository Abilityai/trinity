# Trinity FAQ — Agents

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What are the different ways to create an agent?

Three ways, all equivalent: in the UI, click **Create Agent** on the Dashboard or Agents page, pick a template source, enter a name (lowercase, hyphens allowed), and click **Create**; via the API, `POST /api/agents` with `{"name": "my-agent", "template": "github:Org/repo"}`; or via the MCP tool `create_agent`. In every case Trinity clones the template, builds an isolated Docker container from the base image, copies the template files into `/home/developer/`, and starts the agent automatically. See [Creating Agents](../agents/creating-agents.md).

## Can I create an agent from a private GitHub repository or a specific branch?

Yes. Use the `github:Org/repo` template format, and append `@branch` to build from a specific branch: `github:Org/repo@branch`. Private repositories require a GitHub personal access token configured before you use them as a template source. See [Creating Agents](../agents/creating-agents.md) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Do I need to write my own template to create an agent?

No. The Templates page ships a curated **Starter Templates** section (recommended starters: `scout`, `sage`, `scribe`) auto-discovered from the platform's `config/agent-templates/` directory, plus any GitHub templates an admin has configured as cards. If you want a blank slate, choose **From Scratch** — it creates a minimal agent with a default `CLAUDE.md` you can build on. See [Creating Agents](../agents/creating-agents.md).

## What goes in template.yaml?

`template.yaml` is the agent's metadata file: `display_name`, `description`, `resources`, `credentials`, and `runtime` (which CLI harness the agent uses, plus an optional model override). It can also declare `data_paths` (globs under `data/` holding runtime data, auto-added to the agent's `.gitignore`) and `fork_to_own: required` (copy the template into your own GitHub repo at creation). The rest of a template is `CLAUDE.md` (agent instructions), `.mcp.json.template` (MCP config with `${VAR}` placeholders), and `.env.example` (required credentials). Note that GitHub template metadata is cached for 10 minutes, so `template.yaml` edits may not appear immediately. See [Creating Agents](../agents/creating-agents.md).

## Why did creating my agent fail with an error about the base image?

Every template's `base_image` is validated against an allowlist, and blocked images are rejected with HTTP 403. By default only `trinity-agent-base:*` images are permitted; an admin can configure additional allowed images. Either switch the template to the standard base image or ask your admin to extend the allowlist. See [Creating Agents](../agents/creating-agents.md).

## What's the difference between the Claude Code, Codex, and Gemini runtimes?

The runtime is the CLI harness that executes the agent inside its container, chosen via `runtime.type` in `template.yaml`: `claude-code` (default), `codex` (OpenAI Codex), or `gemini-cli`. They differ in auth (Codex uses `OPENAI_API_KEY` and skips Claude-subscription auto-assignment; Gemini uses a Gemini API key), instruction file (Codex reads `AGENTS.md` — Trinity mirrors the template's `CLAUDE.md` into it at startup), session resume (the Session tab is hidden for Codex agents, though Chat keeps full continuity), and cost reporting (estimated for Codex, actual for the others). The runtime is fixed at creation: to change it, recreate the agent from a template that declares a different runtime. See [Agent Runtimes](../agents/agent-runtimes.md).

## How do I start and stop an agent?

Toggle the Running/Stopped switch on the Dashboard, the Agents page, or the Agent Detail page — a spinner shows while the state changes. Via the API, use `POST /api/agents/{name}/start` and `POST /api/agents/{name}/stop`; via MCP, `start_agent(name)` and `stop_agent(name)`. See [Managing Agents](../agents/managing-agents.md).

## What survives an agent restart, and what survives a recreate?

A plain stop/start keeps the same container, so everything in it survives — except `/tmp`, which is RAM-backed scratch space. A recreate replaces the container (this happens on resource, capability, or mount changes, per-agent API key changes, and image upgrades), but the agent's home directory `/home/developer` lives on a durable named Docker volume, so your workspace files and anything under `data/` survive recreates, image upgrades, template re-pulls, and subscription switches. A template re-pull resets git-tracked files to the template's main branch but never wipes untracked files. Only files written outside the home volume are lost on recreate. See [Agent Data & Portability](../agents/agent-data.md).

## What happens when I delete an agent — is everything gone immediately?

No. Deleting stops and removes the container immediately, but it is a soft delete: the workspace volumes, schedules, chat history, sharing records, permissions, and credentials are all preserved until a retention sweep purges them (default: 180 days). Schedules stop firing right away. During the retention window an admin can recover the agent — recovery restores the record but does not recreate the container, so start the agent afterwards. See [Managing Agents](../agents/managing-agents.md).

## Why can't I create a new agent with the same name as one I just deleted?

Because deletion is a soft delete, the old agent's record still exists and reserves the name until the retention sweep purges it (default: 180 days). Pick a different name, or ask an admin to recover the soft-deleted agent if you actually want it back. See [Managing Agents](../agents/managing-agents.md).

## What happens when I rename an agent?

Renaming is atomic: Trinity updates the agent's name across every database record that references it (schedules, executions, chat history, sharing, permissions, tags, skills, shared files, and more), renames the Docker container, and broadcasts the change live over WebSocket. Click the pencil icon next to the agent name on the Agent Detail page, or use `PUT /api/agents/{name}/rename` / the MCP tool `rename_agent`. Only owners and admins can rename, and system agents cannot be renamed. See [Managing Agents](../agents/managing-agents.md).

## How do I limit how much CPU and memory an agent can use?

Click the gear button ("Configure resources") in the agent header to open the resource modal. Memory options are 1g through 64g and CPU options are 1, 2, 4, 8, or 16 cores; either limit can be left as "Inherit default". Limits are enforced at the container level via Linux cgroups and take effect on the next restart. Admins set the fleet-wide defaults for new agents under Settings. See [Agent Configuration](../agents/agent-configuration.md).

## How long can an agent task run before it times out?

Each agent has an execution timeout, configurable from 60 to 7200 seconds (default: 3600 seconds / 60 minutes), applied to every trigger method — tasks, chat, schedules, MCP, and paid endpoints. The agent timeout is also the ceiling for its schedules: lowering it below an active schedule's own timeout is rejected with `400 error=agent_timeout_below_active_schedules`, and creating a schedule with a longer timeout than the agent cap is rejected too. Manage it via `GET`/`PUT /api/agents/{name}/timeout`. See [Agent Configuration](../agents/agent-configuration.md).

## What does read-only mode do?

Read-only mode prevents the agent from modifying source files (`*.py`, `*.js`, and so on) inside its container by intercepting `Write`, `Edit`, and `NotebookEdit` tool calls with hooks. Generated output is still allowed under `output/*` and `content/*`. Toggle it in the agent header. Codex agents enforce the same restriction through the Codex sandbox (`--sandbox read-only`) instead of tool hooks. See [Agent Configuration](../agents/agent-configuration.md).

## What does the autonomy toggle actually control?

Autonomy is the master switch for an agent's scheduled operations: turning it off pauses all of that agent's schedules at once, and turning it back on resumes them. You can flip it from the Dashboard, the Agents page, or the Agent Detail view, or via `PUT /api/agents/{name}/autonomy`. It only affects schedules — it is not a general on/off switch for the agent itself. See [Agent Configuration](../agents/agent-configuration.md) and [Scheduling](../automation/scheduling.md).

## How do I move an agent to another Trinity instance?

Export the agent's runtime data with `POST /api/agents/{name}/data/export` (or the MCP tool `export_agent_data`) — it captures everything under `/home/developer/data` as a tar archive with a manifest. On the destination instance, recreate the agent from the same template, then import the archive with `POST /api/agents/{name}/data/import` (or `import_agent_data`). Import only writes inside `data/` and skips any entries that try to escape it. Export requires a running agent, and a per-agent lock prevents concurrent export/import. See [Agent Data & Portability](../agents/agent-data.md).

## Can I get shell access to my agent's container?

Yes, via SSH — but it is admin-only and disabled by default. An admin first enables `ssh_access_enabled` under Settings → Ops Settings, then generates ephemeral credentials with `POST /api/agents/{name}/ssh-access` or the MCP tool `get_agent_ssh_access`; both key-based and password-based access are supported and expire automatically after their TTL. Agents listen on incrementing SSH ports starting at 2222. See [Agent Terminal](../agents/agent-terminal.md).

## How do I browse and edit the files inside my agent's workspace?

Open the **Files** tab on the Agent Detail page: the left panel is a searchable file tree of the workspace (`/home/developer/`), and the right panel previews the selected file — images, video, audio, PDF, and text are supported. Text files can be edited and saved inline, **New folder** creates a directory in place (workspace-confined), and files can be deleted with warnings on protected paths. Toggle **Show hidden files** to reveal dotfiles like `.env` and `.claude/`. Downloads are supported up to 100 MB per file. See [Agent Files](../agents/agent-files.md).

## Where can I see my agent's logs?

The **Logs** tab on the Agent Detail page shows the container's stdout/stderr with auto-refresh and smart auto-scroll, also available via `GET /api/agents/{name}/logs` or the MCP tool `get_agent_logs`. All container logs are additionally captured by the Vector log aggregator into structured JSON files — `/data/logs/platform.json` for platform services and `/data/logs/agents.json` for agents — which you can query with `docker exec trinity-vector sh -c "tail -50 /data/logs/agents.json" | jq .`. Anything your agent prints to stdout or stderr lands there automatically. See [Agent Logs and Telemetry](../agents/agent-logs.md).

## What is the compatibility report on the Overview tab, and can Trinity fix what it finds?

Once an agent is running, Trinity checks its workspace against roughly 100 best-practice conventions — a valid `template.yaml`, a non-gitignored `.claude/` directory, accidentally committed secrets, and more — and shows the findings in the Agent Detail **Overview** tab, ranked HARD / SOFT / INFO. It is purely advisory and never blocks creation or deployment; Claude-specific checks are skipped for Codex and Gemini agents. The ten gitignore-related findings offer a one-click **Fix** button that rewrites the agent's `.gitignore` in place — the change stays uncommitted until the agent's next git sync. Use **Re-run analysis** to re-check at any time. See [Creating Agents](../agents/creating-agents.md).
