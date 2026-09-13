# Trinity FAQ — Advanced Features

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## Can I talk to my agent by voice in the browser?

Yes. Click **Talk** in the agent's header, beside **Workspace** — Trinity opens the Workspace on that agent and the call starts. From inside the Workspace, the call button — the leftmost control in the composer — starts a call in the chat you are already in. The orb takes the conversation column, the agent's canvas takes the column beside it, and you speak naturally with roughly 280ms response latency. Gemini Live handles the speech-to-speech conversation, while Claude Code stays the agent's reasoning engine: when your request needs real work (files, tools, research), Gemini hands it to the agent, which runs it as a turn in that very chat — with its skills, files and memory — and Gemini speaks the result. When the call ends, the spoken turns sit in that chat as one collapsed **Voice call · N min** block and the agent's next typed turn knows what was said. A call is one chat with one agent — rooms have no voice mode. Voice replies on messaging channels and phone calls are separate features — see the [Channels FAQ](channels-and-messaging.md). See [Voice Chat](../advanced/voice-chat.md).

## I clicked Talk and the call didn't start — why?

**Talk** is always there; it takes you to the Workspace, and the Workspace tells you why a call cannot start rather than leaving you with a button that does nothing. The usual reasons: no `GEMINI_API_KEY` in **Settings → AI Keys**, the `VOICE_ENABLED` flag off (it defaults to on when the key is present), the browser refusing microphone permission, or a page served over plain http (a microphone needs a secure page). A call also won't start while a reply to a typed message is still being written — wait for it, then try again — and a pasted or bookmarked link with `voice=1` in it opens the chat but starts nothing; only the button starts a call. Voice is for signed-in platform users only — it does not appear on public agent links or for external clients signed in with a portal code. See [Voice Chat](../advanced/voice-chat.md#when-the-control-is-disabled).

## What can the agent do during a voice call, and can I limit it?

The call acts as the agent, in the chat it belongs to, over a tool surface that is fixed when the session starts and cannot grow afterwards. The platform offers `run_task` — Gemini says it's checking, hands the request to the agent, which runs it as a normal resumable turn in that chat while the orb shows an amber badge (past about 20 seconds Gemini keeps talking and the reply lands in the chat when the work finishes) — plus six canvas verbs for drawing on the agent's `main` canvas as it talks. Nothing fleet-wide is available: a call cannot list agents, message other agents or fan work out. An agent's `template.yaml` can narrow that set but never widen it — `voice: tools: [run_task, show_markdown]` keeps only those two, an empty list means no tools at all, a name the platform doesn't offer is dropped with a warning, and declaring nothing keeps the platform default. Every `run_task` is written to the audit log. See [Voice Chat](../advanced/voice-chat.md#per-agent-tool-surface).

## How long can a voice call last, and how do I end it?

A Workspace call lasts at most 30 minutes by default (`WORKSPACE_VOICE_MAX_DURATION`): thirty seconds before the limit the agent is told to wrap up out loud, and at the limit the call ends and the chat records it. End one sooner with **End call** in the status line, the orb's End button, or **Esc** — though a file preview or confirm dialog opened mid-call takes the first Esc for itself, and the call ends on the next. Switching chats, New chat and ⌘J wait until the call ends; leaving the page ends it. The transcript is saved turn by turn on the server, so a dropped connection or a restart mid-call loses nothing already said. **Mute** on the orb silences your microphone, and talking over the agent interrupts it. See [Voice Chat](../advanced/voice-chat.md#limits-and-what-you-hear).

## Can I change my agent's voice or how it speaks?

Both are per-agent settings. Each agent has a persisted Gemini voice (default **Kore**) that applies to the browser voice call and to outbound phone calls; owners can change it via the voice picker or the `/api/agents/{name}/voice/name` endpoint. To change the persona — tone, focus, response style — place a `voice-agent-system-prompt.md` file in the agent's workspace (`/home/developer/`); this controls Gemini's behavior in voice sessions independently of the agent's main `CLAUDE.md`. Without one, Trinity auto-generates a prompt from the agent's template info. See [Voice Chat](../advanced/voice-chat.md).

## What happened to Workspace Mode?

It was a separate full-page voice surface at `/agents/{name}/workspace`. That page is gone — its address now redirects to `/workspace?agent={name}` — and its two valuable halves live on in better places. The **canvas** — where the agent paints formatted text, Mermaid diagrams, images and static HTML layouts while it talks — is now the agent's own durable canvas, shown beside the orb during a call and on the Canvas tab afterwards. The **call** is now the Workspace's voice mode, inside a real chat. The **Workspace** button in the agent header opens the Workspace, and **Talk** beside it opens it with the call starting. All canvas content is still sanitized, and HTML panels still render as static layout only — agent-supplied scripts never execute. See [Voice Chat](../advanced/voice-chat.md#voice-mode-in-the-workspace).

## What is the agent canvas, and what can it show?

A canvas is a surface the agent keeps *current* — a status board, a running tally, the latest version of an analysis — rewritten in place, where reports accumulate as a record and `dashboard.yaml` widgets belong to the Dashboard tab. It shows on the agent's **Canvas** tab, on the Workspace rail's **Canvas** tab, and beside the orb during a voice call, which draws on the same default **`main`** canvas — so an agent and its call share one board, and named canvases keep separate surfaces. A canvas is an ordered list of blocks — `kpi`, `table`, `chart` (bar, stacked bar, line, area, pie, donut), `timeline`, `markdown` (which may carry `chart`, `kpi`, `table` and `mermaid` fences that render as figures inside the prose), `html`, `image`, `diagram` (Mermaid) and `json` — optionally arranged on a starter layout (**dashboard**, **report**, **brief**, **status-board**) with named slots, and styled with a small platform design kit (`ck-card`, `ck-grid-2/3/4`, `ck-callout`, …) so it looks designed in light and dark without the agent touching CSS. Everything is sanitized before it renders: scripts never run, `<style>` is stripped, and only the kit's classes survive. See [Agent Canvas](../agents/agent-canvas.md).

## How do I get my agent to build a canvas — does it need a skill?

Just ask in chat: "Put a dashboard of this week's pipeline on your canvas." In the Workspace, the rail's Canvas tab shows **Ask for a canvas** while the agent has none, which pre-fills the request. No skill is needed — the platform prompt every agent receives teaches the block kinds, the fences, the four layouts and the design kit, so a fresh agent produces a designed canvas without coaching (a fuller `canvas` reference skill is planned for the skills library but is not yet published, so there is nothing to assign). Ask for changes the same way: the agent patches only the blocks that changed, the header timestamp moves, and it gains **may be out of date** when the agent has done work since without refreshing. Prefer data blocks — ask for "a KPI row" or "a table of open items" — over raw HTML. See [Agent Canvas](../agents/agent-canvas.md#tips).

## Who can see an agent's canvas?

Its audience decides. `operator` (the default) shows a canvas on Agent Detail and to signed-in platform users in the Workspace; `roster` also shows it to the people the agent is shared with, in their Workspace — an external client signed in with a portal code sees `roster` canvases only. Ask the agent to "share this canvas with the team" to widen it. When the agent writes a canvas during a conversation, the write tells it whether the person asking can actually see the result, so it can widen the audience instead of reporting a success nobody sees. See [Agent Canvas](../agents/agent-canvas.md).

## Can Trinity generate images?

Yes. The platform has a two-step Gemini pipeline: it first refines your prompt using best-practice templates for the use case, then generates the image from the refined prompt, returned as base64 or a URL. You call it via `POST /api/image/generate`, and it requires a Gemini API key configured in **Settings → AI Keys**. The same pipeline powers agent avatars and other platform features internally. See [Image Generation](../advanced/image-generation.md).

## How do agent avatars work?

Every agent can have an AI-generated avatar. You can generate one from an identity prompt, upload a reference image so the avatar matches its style, and regenerate fresh variations from an existing avatar at any time — a regenerated avatar shows up straight away rather than keeping its old face cached. The Agent Detail page cycles through emotion-based variants every 30 seconds, and an admin button in Settings generates default robot-style avatars for every agent that doesn't have a custom one. See [Agent Avatars](../advanced/agent-avatars.md).

## Why did my agent's avatar generation fail?

The Generate dialog shows a classified reason rather than a generic error. `not_configured` means no image-generation API key is set — add `GEMINI_API_KEY` in **Settings → AI Keys**. `safety_filter` means the upstream model blocked the request — reword the identity prompt. `invalid_input` means the prompt or reference image was rejected, while `rate_limited` and `timeout` usually just need a retry. See [Agent Avatars](../advanced/agent-avatars.md#generation-failures).

## Can my agent build its own dashboard?

Yes. An agent controls its Dashboard tab entirely by writing a `dashboard.yaml` file in its workspace — no API call needed, the file is read on each dashboard request. Eleven widget types are supported (`metric`, `status`, `progress`, `text`, `markdown`, `table`, `list`, `link`, `image`, `divider`, `spacer`) — `markdown` renders formatted text, and there is no chart widget: give a `metric` or `progress` widget a stable `id` and the platform records its value on every fetch and draws the sparkline and trend arrow for you. A Platform Metrics section (tasks, success rate, cost, health) is auto-injected at the bottom of every dashboard unless the file sets `platform_metrics: false`. This is separate from the agent's canvas, which is a free-form surface rather than a widget grid. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md).

## Why does my agent's Dashboard tab say some widgets were skipped?

Because `dashboard.yaml` used a widget type outside the closed set of eleven, or left out a required field. `chart`, `badge` and `countdown` have never been widget types — an agent that writes `type: chart` gets that widget stripped by the agent server before the dashboard reaches the UI, listed in the tab's *widgets skipped due to validation errors* banner, and named in the agent's compatibility report; the rest of the dashboard still renders. For a trend line, use a `metric` or `progress` widget with a stable `id`: its sparkline is drawn from recorded history once it has more than one point, and a widget without an `id` is keyed by position, so moving it starts a new series. Only a missing `title` or an empty `sections` list invalidates the whole dashboard. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#widget-reference).

## What's the difference between Source mode and Working Branch mode in GitHub sync?

Source mode (the default) is pull-only: the agent pulls from the repo but never pushes — used for deploying agent code from a canonical source. Working Branch mode is bidirectional: the agent gets its own branch and pushes changes back, which suits agents that modify their own code; each working branch is ownership-locked to a single agent so concurrent pushes can't clobber each other. To sync from a specific branch, use the `github:owner/repo@branch` syntax at creation or the `source_branch` parameter in MCP. See [GitHub Sync](../integrations/github-sync.md).

## Does my agent push its changes to GitHub automatically?

In Working Branch mode, yes: an auto-sync heartbeat inside the agent stages, commits, and pushes in-container changes about every 15 minutes, and you can toggle it per agent. Trinity also polls sync health every 60 seconds — after 3 consecutive failures (broken remote, expired PAT, upstream divergence) an alert appears in the Operating Room's Needs Response tab, and the fleet dashboard shows a per-agent sync health indicator. You can always trigger a manual **Pull** or **Sync** from the agent detail page. See [GitHub Sync](../integrations/github-sync.md) and [Operating Room](../operations/operating-room.md).

## What is a fork-to-own template?

Some templates declare `fork_to_own: required` in their `template.yaml`, meaning they're meant to be owned by you rather than run from the shared upstream repo. At creation, Trinity copies the template into a repository under your own GitHub account (private by default) using your PAT, points the agent's `origin` at it so everything the agent commits stays in a repo you control, and keeps a read-only `upstream` remote so you can pull in later template updates with a single `git pull upstream <branch>`. You need a GitHub PAT with repo-creation scope configured first. See [Creating Agents](../agents/creating-agents.md#fork-to-own-templates) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## What is the A2A agent card?

It's a standardized discovery endpoint — `GET /api/agents/{name}/a2a/agent-card` — that publishes an agent's capabilities in A2A `0.3.0` format so external orchestrators (AWS Bedrock, Azure Copilot, Google ADK) can discover and call the agent without knowing Trinity's internal API. The card is generated from the agent's `template.yaml` and container labels, works even when the agent is stopped (it falls back to a partial card), and never returns a server error. This endpoint requires authentication.

Agents you explicitly **expose** for A2A also get a public discovery card at `GET /a2a/{name}/.well-known/agent-card.json` and a JSON-RPC task endpoint at `POST /a2a/{name}`, so an outside orchestrator can task them. Exposure is per-agent and off by default; tasking always requires a Trinity MCP API key. See [A2A Protocol](../integrations/a2a-protocol.md).

## Can my agent run a long multi-stage pipeline, and can I watch its progress?

Yes — pipelines are owned by the agent, not by Trinity. The `/agent-dev:add-pipeline` skill scaffolds a long-running, multi-stage pipeline inside the agent (a `pipeline.yaml` definition, per-instance state, tick/status/recover/pause/resume skills, and a heartbeat schedule that advances stages). The agent publishes its pipeline definitions and state to a read surface under `~/.trinity/`, and Trinity exposes them read-only through the `list_agent_pipelines` and `get_agent_pipeline_state` MCP tools — the platform displays progress but never runs a central DAG engine. See the [agent-dev Plugin](../abilities/agent-dev-plugin.md).

## What are agent reports?

Reports are structured results — summaries, metrics, findings — that an agent publishes via the `report` MCP tool so you can see its output without digging through chat transcripts. Published reports appear on the agent's **Reports** tab, and agents created with the wizard plugins ship ready to publish them. See the [MCP Server](../integrations/mcp-server.md) tool reference.

## What is the abilities plugin marketplace?

It's the official agent development toolkit for Claude Code: a curated registry of plugins covering the full agent lifecycle, added once with `/plugin marketplace add abilityai/abilities`. Five plugins are available: **create-agent** (guided wizards for scaffolding new agents), **agent-dev** (playbooks, memory systems, backlog workflow, pipelines, orchestration, a shared canon layer, fleet analysis and migration), **trinity** (connect, onboard, deploy, and sync agents on Trinity), **dev-methodology** (documentation-driven development workflow), and **utilities** (ops and productivity skills). Install each with `/plugin install <name>@abilityai`. See [Abilities Marketplace](../automation/abilities-marketplace.md).

## How do I deploy an agent I built with Claude Code to Trinity?

Run `/trinity:connect` once per machine — it authenticates against your Trinity instance and saves the MCP connection config — and add a GitHub token under **Settings → GitHub token** (a fine-grained PAT with *Contents: Read*; public repos need none). Then push the agent to a GitHub repo and run `/trinity:onboard` in its directory: it checks the agent for Trinity compatibility, creates any required files (`template.yaml` with declared credentials, schedules and plugins, `.env.example`, `.mcp.json.template`), and deploys **from the repository** — Trinity clones it and tracks the branch, so later updates are `git push` followed by `/trinity:sync`. Deploying from local files remains a fallback when there is no repo yet. Remember to turn on the new agent's autonomy toggle in the UI before you expect its schedules to fire. See [Building Agents](../guides/building-agents.md).

## I created an agent straight from a bare GitHub repo — how do I make it Trinity-compatible?

You don't have to adapt the repo locally. Trinity tolerates a repo with no `template.yaml` (the agent just lands with no declared resources, schedules, or plugins), so create the agent as-is, then run `/trinity:onboard` *inside* that agent — send it `/trinity:onboard in-place` as a chat message. The skill detects it is running in a deployed agent, writes the Trinity files (including a `plugins:` block that declares at least `trinity@abilityai`), installs the declared plugins immediately, commits and pushes the result back to the repo, reconciles declared schedules, and finishes with the platform's own compatibility report. The push-back matters: a repo-deployed agent tracks its branch pull-only, so a file written only in the container is lost on the next reset — if the agent has no write credentials the skill says so and prints the patch rather than pretending. No bootstrap is needed first: the `trinity` plugin is provided by the platform — the agent image ships with `trinity@abilityai` installed and every container boot re-installs it if it's missing, whether or not `template.yaml` declares it — so a new agent runs `/trinity:onboard in-place` straight away; only an agent still on an agent image built before that pre-install needs the one-time terminal bootstrap the docs describe. See [Onboarding a deployed agent in place](../abilities/trinity-plugin.md#onboarding-a-deployed-agent-in-place).

## How do I keep a deployed agent's schedules and plugins in sync with its template.yaml?

Run `/trinity:sync schedules` and `/trinity:sync plugins` (both also run automatically after a `push`, `pull`, or `deploy`, and read-only under `status`). Each treats `template.yaml` as the design truth: it creates schedules or installs plugins that are declared but missing on the instance, and *reports* anything live that isn't declared — never deleting a schedule or uninstalling a plugin, since removals are the operator's act. Schedules are matched by name; a plugin installed by reconciliation loads on the agent's next execution. See [trinity Plugin](../abilities/trinity-plugin.md).

## What files does my own agent template need?

The core set: a `CLAUDE.md` (the agent's identity and instructions — it becomes the system prompt), a `template.yaml` (Trinity deployment config), and a `.mcp.json.template` declaring MCP servers with `${VAR}` placeholders that resolve from injected credentials at runtime. Optional but recommended: skills in `.claude/skills/`, a `dashboard.yaml` for custom metrics, and an `.env.example` documenting expected credentials. Everything in the template repository is copied into the agent's home directory at creation, and `/trinity:onboard` can generate the required files for you. See [Building Agents](../guides/building-agents.md) and [Creating Agents](../agents/creating-agents.md).

## What is the Brain Orb?

The Brain Orb is a self-rendering "mind" page for knowledge-base agents like the bundled **Cornelius** second brain: a live 3D knowledge graph drawn from the agent's own notes, edges, and activity, shown on the agent's **Brain** tab. It's capability-gated — it appears only for agents that ship the `brain-orb` capability and only when the platform's Brain Orb flag is enabled (off by default). The agent owns the graph's generation and scope; Trinity just reads and renders it. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#related-the-brain-orb).
