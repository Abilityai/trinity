# Trinity FAQ — Advanced Features

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## Can I talk to my agent by voice in the browser?

Yes. Open the agent's **Chat** tab and click the microphone button next to the chat input — a full-screen overlay appears with an animated orb, and you speak naturally with roughly 280ms response latency. Gemini Live handles the speech-to-speech conversation, while Claude Code stays the agent's reasoning engine: when your request needs real work (files, tools, research), Gemini delegates it to the agent and speaks the result. Transcripts are saved to the chat session when you end the call. Voice replies on messaging channels and phone calls are separate features — see the [Channels FAQ](channels-and-messaging.md). See [Voice Chat](../advanced/voice-chat.md).

## Why don't I see the microphone button in my agent's chat?

Voice requires a `GEMINI_API_KEY` configured in **Settings → AI Keys** and the `VOICE_ENABLED` flag on (it defaults to on when the key is present). Your browser must also grant microphone permission. Voice is only available in authenticated chat — it does not appear on public agent links — and only one voice session per agent can run at a time. See [Voice Chat](../advanced/voice-chat.md).

## Can I change my agent's voice or how it speaks?

Both are per-agent settings. Each agent has a persisted Gemini voice (default **Kore**) that applies to the browser voice overlay and to outbound phone calls; owners can change it via the voice picker or the `/api/agents/{name}/voice/name` endpoint. To change the persona — tone, focus, response style — place a `voice-agent-system-prompt.md` file in the agent's workspace (`/home/developer/`); this controls Gemini's behavior in voice sessions independently of the agent's main `CLAUDE.md`. Without one, Trinity auto-generates a prompt from the agent's template info. See [Voice Chat](../advanced/voice-chat.md).

## What is Workspace Mode?

Workspace Mode is a full-page voice surface with a live canvas next to the orb: while you talk, the agent can paint the canvas with formatted text, Mermaid diagrams, images, and static HTML layouts — useful for walkthroughs and design reviews. It's BETA and admin-gated: an admin must set `WORKSPACE_ENABLED=true`, and voice itself must be available. The **Workspace** button then appears in the agent header (the agent must be running), and the canvas keeps a 40-snapshot history you can step back through. All canvas content is sanitized, and HTML panels render as static layout only — agent-supplied scripts never execute. See [Voice Chat](../advanced/voice-chat.md#workspace-mode-beta).

## Can Trinity generate images?

Yes. The platform has a two-step Gemini pipeline: it first refines your prompt using best-practice templates for the use case, then generates the image from the refined prompt, returned as base64 or a URL. You call it via `POST /api/image/generate`, and it requires a Gemini API key configured in **Settings → AI Keys**. The same pipeline powers agent avatars and other platform features internally. See [Image Generation](../advanced/image-generation.md).

## How do agent avatars work?

Every agent can have an AI-generated avatar. You can generate one from an identity prompt, upload a reference image so the avatar matches its style, and regenerate fresh variations from an existing avatar at any time. The Agent Detail page cycles through emotion-based variants every 30 seconds, and an admin button in Settings generates default robot-style avatars for every agent that doesn't have a custom one. See [Agent Avatars](../advanced/agent-avatars.md).

## Why did my agent's avatar generation fail?

The Generate dialog shows a classified reason rather than a generic error. `not_configured` means no image-generation API key is set — add `GEMINI_API_KEY` in **Settings → AI Keys**. `safety_filter` means the upstream model blocked the request — reword the identity prompt. `invalid_input` means the prompt or reference image was rejected, while `rate_limited` and `timeout` usually just need a retry. See [Agent Avatars](../advanced/agent-avatars.md#generation-failures).

## Can my agent build its own dashboard?

Yes. An agent controls its Dashboard tab entirely by writing a `dashboard.yaml` file in its workspace — no API call needed, the file is read on each dashboard request. Eleven widget types are supported (`metric`, `status`, `progress`, `table`, `list`, `chart`, `text`, `badge`, `countdown`, `link`, `image`), values are tracked historically so metrics grow sparklines and trend arrows, and a Platform Metrics section (tasks, success rate, cost, health) is auto-injected at the bottom of every dashboard. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md).

## What's the difference between Source mode and Working Branch mode in GitHub sync?

Source mode (the default) is pull-only: the agent pulls from the repo but never pushes — used for deploying agent code from a canonical source. Working Branch mode is bidirectional: the agent gets its own branch and pushes changes back, which suits agents that modify their own code; each working branch is ownership-locked to a single agent so concurrent pushes can't clobber each other. To sync from a specific branch, use the `github:owner/repo@branch` syntax at creation or the `source_branch` parameter in MCP. See [GitHub Sync](../integrations/github-sync.md).

## Does my agent push its changes to GitHub automatically?

In Working Branch mode, yes: an auto-sync heartbeat inside the agent stages, commits, and pushes in-container changes about every 15 minutes, and you can toggle it per agent. Trinity also polls sync health every 60 seconds — after 3 consecutive failures (broken remote, expired PAT, upstream divergence) an alert appears in the Operating Room's Needs Response tab, and the fleet dashboard shows a per-agent sync health indicator. You can always trigger a manual **Pull** or **Sync** from the agent detail page. See [GitHub Sync](../integrations/github-sync.md) and [Operating Room](../operations/operating-room.md).

## What is a fork-to-own template?

Some templates declare `fork_to_own: required` in their `template.yaml`, meaning they're meant to be owned by you rather than run from the shared upstream repo. At creation, Trinity copies the template into a repository under your own GitHub account (private by default) using your PAT, points the agent's `origin` at it so everything the agent commits stays in a repo you control, and keeps a read-only `upstream` remote so you can pull in later template updates with a single `git pull upstream <branch>`. You need a GitHub PAT with repo-creation scope configured first. See [Creating Agents](../agents/creating-agents.md#fork-to-own-templates) and [GitHub PAT Setup](../integrations/github-pat-setup.md).

## What is the A2A agent card?

It's a standardized discovery endpoint — `GET /api/agents/{name}/a2a/agent-card` — that publishes an agent's capabilities in A2A v1.0 format so external orchestrators (AWS Bedrock, Azure Copilot, Google ADK) can discover and call the agent without knowing Trinity's internal API. The card is generated from the agent's `template.yaml` and container labels, works even when the agent is stopped (it falls back to a partial card), and never returns a server error. It currently requires authentication; the public `/.well-known/agent-card.json` route is not served yet. See [A2A Agent Card](../integrations/a2a-protocol.md).

## Can my agent run a long multi-stage pipeline, and can I watch its progress?

Yes — pipelines are owned by the agent, not by Trinity. The `/agent-dev:add-pipeline` skill scaffolds a long-running, multi-stage pipeline inside the agent (a `pipeline.yaml` definition, per-instance state, tick/status/recover/pause/resume skills, and a heartbeat schedule that advances stages). The agent publishes its pipeline definitions and state to a read surface under `~/.trinity/`, and Trinity exposes them read-only through the `list_agent_pipelines` and `get_agent_pipeline_state` MCP tools — the platform displays progress but never runs a central DAG engine. See the [agent-dev Plugin](../abilities/agent-dev-plugin.md).

## What are agent reports?

Reports are structured results — summaries, metrics, findings — that an agent publishes via the `report` MCP tool so you can see its output without digging through chat transcripts. Published reports appear on the agent's **Reports** tab, and agents created with the wizard plugins ship ready to publish them. See the [MCP Server](../integrations/mcp-server.md) tool reference.

## What is the abilities plugin marketplace?

It's the official agent development toolkit for Claude Code: a curated registry of plugins covering the full agent lifecycle, added once with `/plugin marketplace add abilityai/abilities`. Five plugins are available: **create-agent** (guided wizards for scaffolding new agents), **agent-dev** (playbooks, memory systems, backlog workflow, pipelines), **trinity** (connect, onboard, deploy, and sync agents on Trinity), **dev-methodology** (documentation-driven development workflow), and **utilities** (ops and productivity skills). Install each with `/plugin install <name>@abilityai`. See [Abilities Marketplace](../automation/abilities-marketplace.md).

## How do I deploy an agent I built with Claude Code to Trinity?

Run `/trinity:connect` once per machine — it authenticates against your Trinity instance and saves the MCP connection config. Then run `/trinity:onboard` in the agent's directory: it checks the agent for Trinity compatibility, creates any required files (`template.yaml`, `.mcp.json.template`), and deploys it, after which the agent runs 24/7 on the platform. To push later changes, use `git push` or `/trinity:sync`. See [Building Agents](../guides/building-agents.md).

## What files does my own agent template need?

The core set: a `CLAUDE.md` (the agent's identity and instructions — it becomes the system prompt), a `template.yaml` (Trinity deployment config), and a `.mcp.json.template` declaring MCP servers with `${VAR}` placeholders that resolve from injected credentials at runtime. Optional but recommended: skills in `.claude/skills/`, a `dashboard.yaml` for custom metrics, and an `.env.example` documenting expected credentials. Everything in the template repository is copied into the agent's home directory at creation, and `/trinity:onboard` can generate the required files for you. See [Building Agents](../guides/building-agents.md) and [Creating Agents](../agents/creating-agents.md).

## What is the Brain Orb?

The Brain Orb is a self-rendering "mind" page for knowledge-base agents like the bundled **Cornelius** second brain: a live 3D knowledge graph drawn from the agent's own notes, edges, and activity, shown on the agent's **Brain** tab. It's capability-gated — it appears only for agents that ship the `brain-orb` capability and only when the platform's Brain Orb flag is enabled (off by default). The agent owns the graph's generation and scope; Trinity just reads and renders it. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#related-the-brain-orb).
