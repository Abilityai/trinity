# Trinity User Documentation

> Auto-generated from source code. Run `/generate-user-docs` to update. Last sync: 2026-07-03.

## What's New

- [Release highlights](whats-new/README.md) — user-facing changes per release, newest first ([v0.7.0](whats-new/v0.7.0.md))

## Watch

- [Video Library](videos.md) — workshops, demos, and deep-dives, newest first

## Guides

- [Deploying Trinity](guides/deploying-trinity.md) — Cloud vs self-hosted setup, step-by-step
- [Using Trinity](guides/using-trinity.md) — UI tour: dashboard, agents, monitoring
- [Building Agents](guides/building-agents.md) — Create, develop, deploy with Claude Code + abilities

## Getting Started

- [Overview](getting-started/overview.md) — What is Trinity, key concepts, architecture
- [Setup](getting-started/setup.md) — Installation, first-time setup, login
- [Quick Start](getting-started/quick-start.md) — Create your first agent in 5 minutes
- [Roles and Permissions](getting-started/roles-and-permissions.md) — 4-tier role model, user management
- [Getting Help](getting-started/help.md) — Docs Q&A bot, community resources

## Agents

- [Creating Agents](agents/creating-agents.md) — Templates, GitHub repos, from scratch
- [Agent Runtimes](agents/agent-runtimes.md) — Claude Code, OpenAI Codex, Gemini CLI
- [Managing Agents](agents/managing-agents.md) — Start/stop, rename, delete, health
- [Agent Data & Portability](agents/agent-data.md) — Runtime data paths, export/import across instances
- [Agent Chat](agents/agent-chat.md) — Chat interface, voice, streaming, history
- [Agent Session](agents/agent-session.md) — Resumable Claude session with full context
- [Agent Terminal](agents/agent-terminal.md) — Web terminal, SSH access, mode switching
- [Agent Files](agents/agent-files.md) — File browser, virtual filesystem, shared folders
- [Agent Logs](agents/agent-logs.md) — Log viewing, telemetry, Vector aggregation
- [Agent Configuration](agents/agent-configuration.md) — Autonomy, read-only, resources, timeout
- [Agent Guardrails](agents/agent-guardrails.md) — Deterministic safety enforcement, bash deny-lists, credential protection
- [Self-Execute](agents/self-execute.md) — Background tasks during chat, result injection

## Credentials

- [Credential Management](credentials/credential-management.md) — Adding, editing, hot-reload, encrypted backup
- [OAuth Credentials](credentials/oauth-credentials.md) — OAuth2 flows for Google, Slack, GitHub, Notion
- [Subscription Credentials](credentials/subscription-credentials.md) — Shared Claude subscriptions, auto-assign, auto-switch

## Collaboration

- [Agent Network](collaboration/agent-network.md) — Multi-agent communication, async collaboration, DAG visualization
- [Agent Permissions](collaboration/agent-permissions.md) — Who can call whom, access control
- [Event Subscriptions](collaboration/event-subscriptions.md) — Pub/sub between agents, message templates
- [System Manifest](collaboration/system-manifest.md) — Recipe-based multi-agent deployment

## Automation

- [Scheduling](automation/scheduling.md) — Cron schedules, execution queue, misfire handling
- [Skills and Playbooks](automation/skills-and-playbooks.md) — Skills library, assignment, chat autocomplete
- [Approvals](automation/approvals.md) — Human-in-the-loop approval gates
- [Fan-Out](automation/fan-out.md) — Parallel task dispatch and result collection
- [Agent Loops](automation/agent-loops.md) — Bounded sequential task repetition with templates and stop signals

## Operations

- [Dashboard](operations/dashboard.md) — Network graph, timeline view, tag clouds
- [Operations Page](operations/operating-room.md) — Unified tabbed view: operator queue, notifications, health, executions
- [Monitoring](operations/monitoring.md) — Fleet health checks, agent heartbeats, cleanup service
- [Executions](operations/executions.md) — Fleet execution list, stats, detail, live streaming, termination
- [Audit Trail](operations/audit-trail.md) — Append-only administrative action log
- [Agent Quotas](operations/agent-quotas.md) — Per-role agent creation limits

## Sharing and Access

- [Agent Sharing & Access](sharing-and-access/agent-sharing.md) — Access tab (operators), Sharing tab (external clients, channels, client roster)
- [Access Control](sharing-and-access/access-control.md) — Cross-channel email verification, access requests
- [Public Links](sharing-and-access/public-links.md) — Public chat URLs, email verification, session memory
- [Tags and Organization](sharing-and-access/tags-and-organization.md) — Tags, filtering, system views
- [Mobile Admin](sharing-and-access/mobile-admin.md) — Mobile PWA at /m

## Integrations

- [GitHub PAT Setup](integrations/github-pat-setup.md) — Personal Access Token configuration for GitHub features
- [GitHub Sync](integrations/github-sync.md) — Source mode, working branch mode, branch selection
- [Slack Integration](integrations/slack-integration.md) — Multi-agent channels, DMs, thread routing
- [Telegram Integration](integrations/telegram-integration.md) — Bot setup, group chats, privacy mode, trigger modes
- [WhatsApp Integration](integrations/whatsapp-integration.md) — Twilio binding, sandbox setup, email verification
- [MCP Server](integrations/mcp-server.md) — 93 MCP tools, API keys, dedicated per-agent tools
- [A2A Agent Card](integrations/a2a-protocol.md) — A2A v1.0 discovery for external orchestrators
- [Nevermined Payments](integrations/nevermined-payments.md) — x402 payment monetization

## CLI

- [Trinity CLI](cli/trinity-cli.md) — Command-line agent management, multi-instance profiles, deployment

## Abilities (Agent Development Toolkit)

- [Overview](abilities/overview.md) — Plugin marketplace introduction, quick start
- [create-agent Plugin](abilities/create-agent-plugin.md) — Agent creation wizards (13 wizards)
- [agent-dev Plugin](abilities/agent-dev-plugin.md) — Development tools, memory systems, git sync, backlog cycle, pipelines
- [trinity Plugin](abilities/trinity-plugin.md) — Platform deployment, sync, remote loops, instance provisioning
- [dev-methodology Plugin](abilities/dev-methodology-plugin.md) — Documentation-driven development
- [utilities Plugin](abilities/utilities-plugin.md) — Ops and productivity tools

## Dev Announcements

- [Dev Announcements](dev-announcements/) — Timestamped archive of all `/announce` messages sent to Discord and Slack

## Advanced

- [Voice Chat](advanced/voice-chat.md) — Real-time voice via Gemini Live API
- [Voice Replies](advanced/voice-replies.md) — Agents speak channel replies as voice notes (ElevenLabs TTS)
- [VoIP Telephony](advanced/voip-telephony.md) — Agents place outbound phone calls via Twilio + Gemini Live
- [Image Generation](advanced/image-generation.md) — Gemini two-step image pipeline
- [Agent Avatars](advanced/agent-avatars.md) — AI-generated avatars, emotion variants
- [Dynamic Dashboards](advanced/dynamic-dashboards.md) — Custom agent dashboards via YAML

## API Reference

- [Authentication](api-reference/authentication.md) — JWT tokens, API keys, auth flows
- [Agent API](api-reference/agent-api.md) — Agent CRUD, lifecycle, configuration endpoints
- [Chat API](api-reference/chat-api.md) — Chat, voice, streaming, public/paid endpoints
- [Webhook Triggers](api-reference/webhook-triggers.md) — Internal triggers, event webhooks
