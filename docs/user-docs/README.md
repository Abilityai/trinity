# Trinity User Documentation

> Auto-generated from source code. Run `/generate-user-docs` to update. Last sync: 2026-10-06.

Trinity is the operating system for the AI-native company — open source, self-hosted, that you own. It deploys, orchestrates, and governs fleets of AI agents on your own hardware, and it is model-agnostic: each agent runs on Claude Code, Gemini CLI or OpenAI Codex.

## What's New

- [Release highlights](whats-new/README.md) — user-facing changes per release, newest first ([v0.9.5](whats-new/v0.9.5.md) latest; [v0.9.0](whats-new/v0.9.0.md) illustrated)

## FAQ

- [Trinity FAQ](faq/README.md) — 610+ grounded answers to common questions, organized by topic ([troubleshooting](faq/troubleshooting.md) for symptom → fix)

## Watch

- [Video Library](videos.md) — workshops, demos, and deep-dives, newest first

## Guides

- [Deploying Trinity](guides/deploying-trinity.md) — Cloud vs self-hosted setup, step-by-step
- [Local Development](guides/deploying/local-development.md) — Docker Desktop, dev compose, hot reload, base image build
- [Single Server](guides/deploying/single-server.md) — VPS or bare metal: prebuilt images or source, `.env` reference, database backend, Vultr
- [Deploy on DigitalOcean](guides/deploying/digitalocean.md) — One command from your terminal to an HTTPS Droplet: doctl and an optional domain; you connect Claude on first sign-in
- [Deploy on AWS](guides/deploying/aws.md) — Script install or CloudFormation stack, the instance-ID claim, IP refresh
- [Public Access](guides/deploying/public-access.md) — Cloudflare Tunnel, the public webhook surface, DNS
- [Hardening](guides/deploying/hardening.md) — Take a one-click or provisioned instance to a real domain, a Cloudflare Tunnel or a private network, public ports closed
- [Upgrading](guides/deploying/upgrading.md) — Pre-flight, backup, pull, rebuild, restart, verify, rollback
- [Backup and Restore](guides/deploying/backup-and-restore.md) — Automatic nightly backups, manual SQLite and PostgreSQL backup and restore
- [Monitoring](guides/deploying/monitoring.md) — Six health probes, fleet-health API, resource thresholds, recovery patterns
- [Trinity Ops Agent](guides/deploying/ops-agent.md) — A Claude Code agent that operates your instance locally or over SSH
- [Using Trinity](guides/using-trinity.md) — UI tour: dashboard, agents, monitoring
- [Building Agents](guides/building-agents.md) — Create, develop, deploy with Claude Code + abilities

## Getting Started

- [Overview](getting-started/overview.md) — What is Trinity, key concepts, architecture
- [Setup](getting-started/setup.md) — Installation, admin account (including the browser claim on 1-Click installs), the first-run setup sequence, login
- [Quick Start](getting-started/quick-start.md) — Create your first agent in 5 minutes
- [Roles and Permissions](getting-started/roles-and-permissions.md) — 4-tier role model, user management
- [Getting Help](getting-started/help.md) — Docs Q&A bot, community resources

## Agents

- [Creating Agents](agents/creating-agents.md) — Templates, GitHub repos, from scratch
- [Agent Runtimes](agents/agent-runtimes.md) — Claude Code, OpenAI Codex, Gemini CLI: auth, cost reporting, session resume, which safety controls apply
- [Managing Agents](agents/managing-agents.md) — Start/stop, rename, delete, health
- [Agent Data & Portability](agents/agent-data.md) — Runtime data paths, export/import across instances
- [Agent Chat](agents/agent-chat.md) — The stateless per-turn chat on the agent page, streaming, history
- [Agent Canvas](agents/agent-canvas.md) — A surface the agent keeps current: blocks, the design kit, starter layouts, sharing and PDF, pinning and clean-up
- [Continuous Conversations](agents/agent-session.md) — What resuming preserves, auto-compact, per-turn limits
- [Agent Terminal](agents/agent-terminal.md) — Ephemeral key-based SSH access; the WebSocket terminal for API clients
- [Agent Files](agents/agent-files.md) — File browser, virtual filesystem, shared folders
- [Agent Logs](agents/agent-logs.md) — Log viewing, telemetry, Vector aggregation
- [Agent Configuration](agents/agent-configuration.md) — The Settings tab: guardrails, capacity, MCP exposure, access key, reliability (breaker, wake on answer), autonomy, resources, timeout
- [Agent Guardrails](agents/agent-guardrails.md) — Deterministic safety enforcement, bash deny-lists, credential protection, tamper-proof hook registration
- [Recommended Trinity Prompt](agents/recommended-fleet-prompt.md) — Research-backed fleet rules for the instance-wide Trinity prompt, and what not to put there
- [Self-Execute](agents/self-execute.md) — Background tasks during chat, result injection

## Credentials

- [Platform Keys](credentials/platform-keys.md) — Claude, GitHub, email (Resend) and Gemini keys, set from the browser
- [Credential Management](credentials/credential-management.md) — Adding, editing, hot-reload, encrypted backup
- [OAuth Credentials](credentials/oauth-credentials.md) — OAuth2 flows for Google, Slack, GitHub, Notion
- [Subscription Credentials](credentials/subscription-credentials.md) — Shared Claude subscriptions, auto-assign, auto-switch

## Collaboration

- [Agent Network](collaboration/agent-network.md) — Multi-agent communication, async collaboration, Timeline replay
- [Agent Permissions](collaboration/agent-permissions.md) — Who can call whom, access control
- [Event Subscriptions](collaboration/event-subscriptions.md) — Pub/sub + automatic task-completion report-back
- [Shared Sessions (Rooms)](collaboration/rooms.md) — Several agents converse in one shared room
- [System Manifest](collaboration/system-manifest.md) — Recipe-based multi-agent deployment, resilient deploy

## Automation

- [Scheduling](automation/scheduling.md) — Cron schedules, execution queue, misfire handling
- [Skills and Playbooks](automation/skills-and-playbooks.md) — Multi-source skills library, assignment, injection, auto-sync
- [Approvals](automation/approvals.md) — Asks and approvals: authoring limits, Something else, Discuss and Dismiss, platform alert conditions
- [Fan-Out](automation/fan-out.md) — Parallel task dispatch and result collection
- [Agent Loops](automation/agent-loops.md) — Bounded sequential task repetition, templates, stop signals, failure policy
- [Agent Reminders](automation/agent-reminders.md) — One-shot durable deferred self-triggers
- [Abilities Marketplace](automation/abilities-marketplace.md) — Claude Code plugin marketplace: the 5 plugins, playbook-call convention, declared plugins, four-step workflow

## Operations

- [Dashboard](operations/dashboard.md) — Timeline, Grid, and List views, org overlay, subscription pressure chips, the getting-started checklist
- [Operations Page](operations/operating-room.md) — Unified tabbed view: operator queue, notifications, health, executions
- [Monitoring](operations/monitoring.md) — Fleet health checks, agent heartbeats, cleanup service, retention sweeps
- [Executions](operations/executions.md) — Fleet execution list, stats, detail, live streaming, termination
- [Agent Reports](operations/agent-reports.md) — Structured results agents publish, with rendering, filters, and export
- [Audit Trail](operations/audit-trail.md) — Append-only administrative action log
- [Telemetry](operations/telemetry.md) — Local product events and opt-in fleet sharing
- [Agent Quotas](operations/agent-quotas.md) — Per-role agent creation limits

## Sharing and Access

- [Agent Sharing & Access](sharing-and-access/agent-sharing.md) — Access tab (operators), Sharing tab (external clients, channels, client roster)
- [Access Control](sharing-and-access/access-control.md) — Cross-channel email verification, access requests
- [Public Links](sharing-and-access/public-links.md) — Public chat URLs, email verification, session memory, Connect Slack
- [Workspace](sharing-and-access/workspace.md) — The chat app: the Inbox, chat tabs and the pinned Main chat, the composer (typeahead, model picker, files, voice, replies), the rail (Info, Files, Loops, Canvas, Work), keyboard shortcuts, agent pages
- [Tags and Organization](sharing-and-access/tags-and-organization.md) — Tags, filtering, system views
- [Mobile Admin](sharing-and-access/mobile-admin.md) — Mobile PWA at /m

## Integrations

- [GitHub PAT Setup](integrations/github-pat-setup.md) — Personal Access Token configuration for GitHub features
- [GitHub Sync](integrations/github-sync.md) — Agent vs pull-only repositories, source and working-branch modes, auto-sync and pull sync
- [Slack Integration](integrations/slack-integration.md) — Multi-agent channels, DMs, thread routing
- [Telegram Integration](integrations/telegram-integration.md) — Bot setup, group chats, privacy mode, trigger modes
- [WhatsApp Integration](integrations/whatsapp-integration.md) — Twilio binding, sandbox setup, email verification
- [MCP Server](integrations/mcp-server.md) — 154 MCP tools, API keys, inline email sign-in, dedicated per-agent tools
- [A2A Protocol](integrations/a2a-protocol.md) — A2A `0.3.0` in both directions: inbound tasking, outbound calls to external agents, x402 payments
- [Nevermined Payments](integrations/nevermined-payments.md) — x402 payment monetization

## CLI

- [Trinity CLI](cli/trinity-cli.md) — Command-line agent management, multi-instance profiles, deployment

## Abilities (Agent Development Toolkit)

- [Overview](abilities/overview.md) — Plugin marketplace introduction, quick start
- [create-agent Plugin](abilities/create-agent-plugin.md) — The interview-driven `custom` wizard, the website scaffold, and review/adjust/clone tooling
- [agent-dev Plugin](abilities/agent-dev-plugin.md) — Development tools, memory systems, git sync, backlog cycle, pipelines, orchestration, canon, fleet analysis
- [trinity Plugin](abilities/trinity-plugin.md) — Repository-first deployment, onboarding in place, sync, remote loops, instance provisioning
- [dev-methodology Plugin](abilities/dev-methodology-plugin.md) — Documentation-driven development
- [utilities Plugin](abilities/utilities-plugin.md) — Ops and productivity tools

## Dev Announcements

- [Dev Announcements](dev-announcements/) — Timestamped archive of all `/announce` messages sent to Discord and Slack

## Advanced

- [Voice Chat](advanced/voice-chat.md) — Voice mode inside the Workspace conversation: the orb takes the call, the transcript stays in the thread
- [Voice Replies](advanced/voice-replies.md) — Agents speak individual channel replies as voice notes (ElevenLabs TTS)
- [VoIP Telephony](advanced/voip-telephony.md) — Agents place outbound phone calls via Twilio + Gemini Live
- [Image Generation](advanced/image-generation.md) — Gemini two-step image pipeline
- [Agent Avatars](advanced/agent-avatars.md) — AI-generated avatars, emotion variants
- [Dynamic Dashboards](advanced/dynamic-dashboards.md) — Custom agent dashboards via YAML, declared metrics, and [binding a widget to one series with `dims:`](advanced/dynamic-dashboards.md#one-series-per-tile-dims)

## API Reference

- [Authentication](api-reference/authentication.md) — JWT tokens, API keys, auth flows
- [Agent API](api-reference/agent-api.md) — Agent CRUD, lifecycle, configuration endpoints
- [Chat API](api-reference/chat-api.md) — Chat, voice, streaming, public/paid endpoints
- [Webhook Triggers](api-reference/webhook-triggers.md) — Internal triggers, event webhooks
