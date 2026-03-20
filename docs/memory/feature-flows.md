# Feature Flows Index

> **Purpose**: Maps features to detailed vertical slice documentation.
> Each flow documents the complete path from UI → API → Database → Side Effects.
>
> For detailed change history, see `changelog.md`.

---

## Recent Updates

| Date | ID | Feature | Flow |
|------|-----|---------|------|
| 2026-03-20 | ROLE-001 | 4-tier role model (admin/creator/operator/user), require_role() helper, user management API + Settings UI | [role-model.md](feature-flows/role-model.md) |
| 2026-03-19 | CHAT-AC | Playbook autocomplete in chat input — slash-command dropdown, ghost text, arg hints | [playbook-autocomplete.md](feature-flows/playbook-autocomplete.md) |
| 2026-03-14 | MOB-001 | Mobile Admin — agent chat, autonomy toggle, task sending | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) |
| 2026-03-14 | MOB-001 | Mobile Admin PWA — standalone `/m` page with Agents/Ops/System tabs, installable PWA | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) |
| 2026-03-12 | TIMEOUT-001 | Per-agent configurable execution timeout (default 15 min), dynamic slot TTL | [task-execution-service.md](feature-flows/task-execution-service.md), [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-03-12 | #90 | Fix stuck executions on slot acquisition failure — try block covers all execution steps | [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-11 | #81 | Default model for headless tasks — prevents misleading "token expired" errors when agent settings contain incompatible model | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-11 | SCHED-ASYNC-001 | Scheduler async fire-and-forget with DB polling, status overwrite guard, cleanup timeout 30→120 min | [scheduler-service.md](feature-flows/scheduler-service.md), [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-11 | CLEANUP-001 | Background cleanup service for stale executions, activities, and slots | [cleanup-service.md](feature-flows/cleanup-service.md) |
| 2026-03-10 | AVATAR | Avatar display in Dashboard Timeline tiles (lg size, border ring) | [agent-avatars.md](feature-flows/agent-avatars.md), [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-09 | AVATAR | Avatar image optimization — WebP conversion via Pillow, stable emotion cache keys | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-09 | CAPACITY-001 | Scheduled tasks route through TaskExecutionService — capacity meter now tracks cron/manual executions | [parallel-capacity.md](feature-flows/parallel-capacity.md), [scheduler-service.md](feature-flows/scheduler-service.md), [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-09 | SEC | Security hardening: WS auth, internal API secret, agent ACL on chat/credentials, DOMPurify | [credential-injection.md](feature-flows/credential-injection.md), [scheduler-service.md](feature-flows/scheduler-service.md) |
| 2026-03-08 | AVATAR-003 | Default avatar generation — admin button in Settings, robot/android aesthetic | [agent-avatars.md](feature-flows/agent-avatars.md), [platform-settings.md](feature-flows/platform-settings.md) |
| 2026-03-08 | OPS-001 | Operating Room — consolidated Events + Cost Alerts into 4-tab layout | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | OPS-001 | Operating Room — restart-resilient sync, refresh button, stale prompt detection | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | — | Fix `--session-id` UUID validation failure in headless task execution | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-07 | OPS-001 | Operating Room — full implementation (backend, sync service, frontend, meta-prompt) | [operating-room.md](feature-flows/operating-room.md) |
| 2026-03-08 | AVATAR-002 | Emotion avatar variants with 30s cycling on AgentDetail page | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-08 | AVATAR-001 | Agent avatars with reference image system, variation regeneration, dark mode style | [agent-avatars.md](feature-flows/agent-avatars.md) |
| 2026-03-07 | IMG-001 | Platform image generation via Gemini two-step pipeline | [image-generation.md](feature-flows/image-generation.md) |
| 2026-03-06 | — | Headless task session isolation + permission mode validation | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) |
| 2026-03-04 | TMPL-001 | Admin-configurable GitHub template repositories via Settings UI | [platform-settings.md](feature-flows/platform-settings.md), [templates-page.md](feature-flows/templates-page.md) |
| 2026-03-04 | THINK-001 | Dynamic thinking status extended to Public Chat (async mode + SSE) | [public-agent-links.md](feature-flows/public-agent-links.md) |
| 2026-03-04 | NVM-001 | Nevermined x402 payment integration for agent monetization | [nevermined-payments.md](feature-flows/nevermined-payments.md) |
| 2026-03-04 | EXEC-024 | Unified task execution service for all callers | [task-execution-service.md](feature-flows/task-execution-service.md) |
| 2026-03-03 | SUB-003 | Agent assign/unassign controls in subscription expanded rows | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-03 | SUB-002 | Subscription management rewrite: token-based auth via env var | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-03 | THINK-001 | Dynamic thinking status labels in Chat tab via SSE streaming | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) |
| 2026-03-03 | CAPACITY-001 | Capacity meter UI on Agents page and Dashboard timeline (Phase 2) | [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar on Dashboard nodes | [agent-network.md](feature-flows/agent-network.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar in timeline tiles | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-03 | #60 | Success rate bar replaces context bar on Agents page | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #55 | Agents page filtering by name, status, and tags | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #54 | Two-row agent tiles, gap spacing, persistent tag filter | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) |
| 2026-03-03 | #51 | Per-agent Files tab restored with two-panel file manager | [file-browser.md](feature-flows/file-browser.md) |
| 2026-03-03 | #52 | Templates restored to main NavBar | NavBar component change (no new flow) |
| 2026-03-03 | #53 | Agent Detail: removed sub-nav, widened panel, reduced padding | Layout change to AgentDetail.vue (no new flow) |
| 2026-03-02 | SUB-001 | Subscription credential priority fix (superseded by SUB-002) | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-03-02 | MON-001/SUB-001 | Subscription credential health monitoring and auto-remediation | [subscription-credential-health.md](feature-flows/subscription-credential-health.md) |
| 2026-03-02 | MODEL-001 | Model selection for tasks and schedules | [model-selection.md](feature-flows/model-selection.md) |
| 2026-03-02 | FILTER-001 | Dashboard filter persistence (time range, quick tags) | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-03-01 | RENAME-001 | Agent rename via UI pencil icon, MCP tool, or REST API | [agent-rename.md](feature-flows/agent-rename.md) |
| 2026-02-28 | GIT-002 | Git branch support for agent creation | [github-sync.md](feature-flows/github-sync.md) |
| 2026-02-28 | CAPACITY-001 | Per-agent parallel execution capacity | [parallel-capacity.md](feature-flows/parallel-capacity.md) |
| 2026-02-27 | PLAYBOOK-001 | Playbooks Tab - invoke agent skills from UI | [playbooks-tab.md](feature-flows/playbooks-tab.md) |
| 2026-02-27 | REFRESH-001 | Dashboard Timeline refresh fix | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) |
| 2026-02-25 | ORG-001 | Tag Clouds visualization | [tag-clouds.md](feature-flows/tag-clouds.md) |
| 2026-02-25 | SLACK-001 | Slack Integration for Public Links | [slack-integration.md](feature-flows/slack-integration.md) |
| 2026-02-24 | DOCKER-001 | Async Docker operations | [async-docker-operations.md](feature-flows/async-docker-operations.md) |
| 2026-02-23 | DASH-001 | Dynamic Dashboards with sparklines | [dynamic-dashboards.md](feature-flows/dynamic-dashboards.md) |
| 2026-02-23 | MON-001 | Agent Monitoring Service | [agent-monitoring.md](feature-flows/agent-monitoring.md) |
| 2026-02-22 | SUB-001 | Subscription Management | [subscription-management.md](feature-flows/subscription-management.md) |
| 2026-02-21 | PERF-001 | Task List Performance | [tasks-tab.md](feature-flows/tasks-tab.md) |
| 2026-02-20 | EXEC-023 | Continue Execution as Chat | [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) |
| 2026-02-20 | NOTIF-001 | Agent Notifications | [agent-notifications.md](feature-flows/agent-notifications.md) |
| 2026-02-20 | AUDIT-001 | Execution Origin Tracking | [AUDIT-001-execution-origin-tracking.md](feature-flows/AUDIT-001-execution-origin-tracking.md) |
| 2026-02-19 | CHAT-001 | Authenticated Chat Tab | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) |
| 2026-02-17 | ORG-001 | Agent Tags & System Views | [agent-tags.md](feature-flows/agent-tags.md) |
| 2026-02-17 | CFG-007 | Read-Only Mode | [read-only-mode.md](feature-flows/read-only-mode.md) |
| 2026-02-17 | PUB-005 | Public Chat Session Persistence | [public-agent-links.md](feature-flows/public-agent-links.md) |

---

## Documented Flows

### Core Agent Features

| Flow | Document | Description |
|------|----------|-------------|
| Agent Lifecycle | [agent-lifecycle.md](feature-flows/agent-lifecycle.md) | Create, start, stop, delete Docker containers |
| Agent Rename | [agent-rename.md](feature-flows/agent-rename.md) | Rename agents via UI, MCP, or API (RENAME-001) |
| Agent Terminal | [agent-terminal.md](feature-flows/agent-terminal.md) | Browser-based xterm.js terminal with Claude/Gemini/Bash modes |
| Credential Injection | [credential-injection.md](feature-flows/credential-injection.md) | CRED-002: Direct file injection, encrypted git storage |
| Agent Scheduling | [scheduling.md](feature-flows/scheduling.md) | Cron-based automation with APScheduler |
| Scheduler Service | [scheduler-service.md](feature-flows/scheduler-service.md) | Standalone scheduler with Redis distributed locks |
| Execution Queue | [execution-queue.md](feature-flows/execution-queue.md) | Redis-based parallel execution prevention |
| Execution Termination | [execution-termination.md](feature-flows/execution-termination.md) | Stop running executions via process registry |
| Parallel Headless Execution | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) | Stateless parallel task execution via POST /task |
| Parallel Capacity | [parallel-capacity.md](feature-flows/parallel-capacity.md) | Per-agent parallel execution slot tracking |
| Task Execution Service | [task-execution-service.md](feature-flows/task-execution-service.md) | Unified execution lifecycle for all task callers (EXEC-024) |

### Dashboard & Monitoring

| Flow | Document | Description |
|------|----------|-------------|
| Agent Network (Dashboard) | [agent-network.md](feature-flows/agent-network.md) | Real-time visual graph at `/` |
| Dashboard Timeline View | [dashboard-timeline-view.md](feature-flows/dashboard-timeline-view.md) | Graph/Timeline mode toggle with execution boxes |
| Replay Timeline | [replay-timeline.md](feature-flows/replay-timeline.md) | Waterfall-style timeline visualization |
| Activity Stream | [activity-stream.md](feature-flows/activity-stream.md) | Centralized persistent activity tracking |
| Activity Monitoring | [activity-monitoring.md](feature-flows/activity-monitoring.md) | Real-time tool execution tracking |
| Agent Monitoring (Health) | [agent-monitoring.md](feature-flows/agent-monitoring.md) | Fleet-wide health checks (MON-001) |
| Subscription Credential Health | [subscription-credential-health.md](feature-flows/subscription-credential-health.md) | Credential health monitoring, auto-remediation, alerts |
| Host Telemetry | [host-telemetry.md](feature-flows/host-telemetry.md) | Host CPU/memory/disk in Dashboard header |
| Agent Logs & Telemetry | [agent-logs-telemetry.md](feature-flows/agent-logs-telemetry.md) | Live metrics in AgentHeader |
| Agent Dashboard | [agent-dashboard.md](feature-flows/agent-dashboard.md) | Agent-defined dashboard via dashboard.yaml |
| Dynamic Dashboards | [dynamic-dashboards.md](feature-flows/dynamic-dashboards.md) | Historical widget values with sparklines (DASH-001) |

### Agent Detail UI

| Flow | Document | Description |
|------|----------|-------------|
| Tasks Tab | [tasks-tab.md](feature-flows/tasks-tab.md) | Task execution UI with history |
| Playbooks Tab | [playbooks-tab.md](feature-flows/playbooks-tab.md) | Invoke agent skills from UI (PLAYBOOK-001) |
| Authenticated Chat Tab | [authenticated-chat-tab.md](feature-flows/authenticated-chat-tab.md) | Simple chat UI with dynamic status labels (CHAT-001, THINK-001) |
| Playbook Autocomplete | [playbook-autocomplete.md](feature-flows/playbook-autocomplete.md) | Slash-command autocomplete for playbooks in chat input |
| Execution Log Viewer | [execution-log-viewer.md](feature-flows/execution-log-viewer.md) | Modal for viewing execution transcripts |
| Execution Detail Page | [execution-detail-page.md](feature-flows/execution-detail-page.md) | Dedicated page for execution details |
| Continue Execution as Chat | [continue-execution-as-chat.md](feature-flows/continue-execution-as-chat.md) | Resume executions as interactive chat (EXEC-023) |
| Agent Avatars | [agent-avatars.md](feature-flows/agent-avatars.md) | AI-generated avatars with reference images, emotion variants, and default generation (AVATAR-001/002/003) |
| Agent Info Display | [agent-info-display.md](feature-flows/agent-info-display.md) | Template metadata in Info tab |
| Per-Agent File Manager | [file-browser.md](feature-flows/file-browser.md) | Two-panel file manager in Agent Detail Files tab |
| File Manager (Deprecated) | [file-manager.md](feature-flows/file-manager.md) | Former standalone `/files` page — replaced by per-agent Files tab |

### Collaboration & Permissions

| Flow | Document | Description |
|------|----------|-------------|
| Agent-to-Agent Collaboration | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md) | Inter-agent communication via MCP |
| Agent Permissions | [agent-permissions.md](feature-flows/agent-permissions.md) | Agent communication permissions |
| Agent Sharing | [agent-sharing.md](feature-flows/agent-sharing.md) | Email-based sharing with access levels |
| Agent Shared Folders | [agent-shared-folders.md](feature-flows/agent-shared-folders.md) | File collaboration via shared volumes |
| Agent Tags & System Views | [agent-tags.md](feature-flows/agent-tags.md) | Tagging and saved filters (ORG-001) |
| Tag Clouds | [tag-clouds.md](feature-flows/tag-clouds.md) | Visual grouping on Dashboard |

### Authentication & Security

| Flow | Document | Description |
|------|----------|-------------|
| Email Authentication | [email-authentication.md](feature-flows/email-authentication.md) | Passwordless email login |
| Admin Login | [admin-login.md](feature-flows/admin-login.md) | Password-based admin auth |
| First-Time Setup | [first-time-setup.md](feature-flows/first-time-setup.md) | Admin password wizard |
| MCP API Keys | [mcp-api-keys.md](feature-flows/mcp-api-keys.md) | API key management |
| Execution Origin Tracking | [AUDIT-001-execution-origin-tracking.md](feature-flows/AUDIT-001-execution-origin-tracking.md) | Track who triggered executions |

### Public Access & Monetization

| Flow | Document | Description |
|------|----------|-------------|
| Public Agent Links | [public-agent-links.md](feature-flows/public-agent-links.md) | Shareable public links with optional email verification |
| Slack Integration | [slack-integration.md](feature-flows/slack-integration.md) | Slack as delivery channel for public links (SLACK-001) |
| Nevermined x402 Payments | [nevermined-payments.md](feature-flows/nevermined-payments.md) | Per-agent paid API via x402 payment protocol (NVM-001) |

### Mobile & PWA

| Flow | Document | Description |
|------|----------|-------------|
| Mobile Admin PWA | [mobile-admin-pwa.md](feature-flows/mobile-admin-pwa.md) | Standalone mobile admin at `/m` with agent chat, autonomy toggle, Ops/System tabs (MOB-001) |

### Platform Services

| Flow | Document | Description |
|------|----------|-------------|
| Image Generation | [image-generation.md](feature-flows/image-generation.md) | Gemini-powered two-step image generation pipeline (IMG-001) |

### MCP & Integration

| Flow | Document | Description |
|------|----------|-------------|
| MCP Orchestration | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) | 45+ MCP tools for agent orchestration |
| Trinity Connect | [trinity-connect.md](feature-flows/trinity-connect.md) | Local-remote agent sync via WebSocket |

### GitHub Integration

| Flow | Document | Description |
|------|----------|-------------|
| GitHub Sync | [github-sync.md](feature-flows/github-sync.md) | Source mode (pull-only) or Working Branch mode |
| GitHub Repo Initialization | [github-repo-initialization.md](feature-flows/github-repo-initialization.md) | Initialize GitHub sync for existing agents |

### Skills Management

| Flow | Document | Description |
|------|----------|-------------|
| Skills CRUD | [skills-crud.md](feature-flows/skills-crud.md) | Admin CRUD for platform skills |
| Skill Assignment | [skill-assignment.md](feature-flows/skill-assignment.md) | Owner assigns skills to agents |
| Skill Injection | [skill-injection.md](feature-flows/skill-injection.md) | Automatic injection on agent start |
| Skills on Agent Start | [skills-on-agent-start.md](feature-flows/skills-on-agent-start.md) | Detailed startup injection flow |
| MCP Skill Tools | [mcp-skill-tools.md](feature-flows/mcp-skill-tools.md) | 8 MCP tools for skill management |
| Skills Management UI | [skills-management.md](feature-flows/skills-management.md) | Frontend UI documentation |
| Skills Library Sync | [skills-library-sync.md](feature-flows/skills-library-sync.md) | GitHub repository sync |

### Notifications & Events

| Flow | Document | Description |
|------|----------|-------------|
| Agent Notifications | [agent-notifications.md](feature-flows/agent-notifications.md) | Agent-to-platform notifications (NOTIF-001) |
| Events Page UI | [events-page.md](feature-flows/events-page.md) | Consolidated into Operating Room Notifications tab |
| Operating Room | [operating-room.md](feature-flows/operating-room.md) | Unified operator command center: queue, notifications, cost alerts (OPS-001) |

### Configuration & Settings

| Flow | Document | Description |
|------|----------|-------------|
| Autonomy Mode | [autonomy-mode.md](feature-flows/autonomy-mode.md) | Agent autonomous operation toggle |
| AutonomyToggle Component | [autonomy-toggle-component.md](feature-flows/autonomy-toggle-component.md) | Reusable Vue toggle component |
| Read-Only Mode | [read-only-mode.md](feature-flows/read-only-mode.md) | Code protection via hooks (CFG-007) |
| Agent Resource Allocation | [agent-resource-allocation.md](feature-flows/agent-resource-allocation.md) | Per-agent memory/CPU limits |
| Container Capabilities | [container-capabilities.md](feature-flows/container-capabilities.md) | Full capabilities mode |
| Model Selection | [model-selection.md](feature-flows/model-selection.md) | LLM model selection for terminal, tasks, and schedules |
| Platform Settings | [platform-settings.md](feature-flows/platform-settings.md) | Admin settings page |
| SSH Access | [ssh-access.md](feature-flows/ssh-access.md) | Ephemeral SSH credentials |
| Subscription Management | [subscription-management.md](feature-flows/subscription-management.md) | Claude Max/Pro subscription tokens via env var (SUB-002) |

### System & Infrastructure

| Flow | Document | Description |
|------|----------|-------------|
| Internal System Agent | [internal-system-agent.md](feature-flows/internal-system-agent.md) | Platform operations manager (trinity-system) |
| System Manifest | [system-manifest.md](feature-flows/system-manifest.md) | Recipe-based multi-agent deployment |
| System-Wide Trinity Prompt | [system-wide-trinity-prompt.md](feature-flows/system-wide-trinity-prompt.md) | Admin-configurable prompt injection |
| Vector Logging | [vector-logging.md](feature-flows/vector-logging.md) | Centralized log aggregation |
| OpenTelemetry Integration | [opentelemetry-integration.md](feature-flows/opentelemetry-integration.md) | OTel metrics export |
| Async Docker Operations | [async-docker-operations.md](feature-flows/async-docker-operations.md) | Non-blocking Docker SDK wrappers |
| Cleanup Service | [cleanup-service.md](feature-flows/cleanup-service.md) | Background recovery of stale executions, activities, and slots (CLEANUP-001) |

### Templates & Pages

| Flow | Document | Description |
|------|----------|-------------|
| Template Processing | [template-processing.md](feature-flows/template-processing.md) | GitHub and local template handling |
| Templates Page | [templates-page.md](feature-flows/templates-page.md) | `/templates` route for browsing |
| API Keys Page | [api-keys-page.md](feature-flows/api-keys-page.md) | `/api-keys` page UI flow |
| Agents Page UI | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) | Horizontal row tiles with success rate bars, filtering, responsive breakpoints |
| Alerts Page | [alerts-page.md](feature-flows/alerts-page.md) | Consolidated into Operating Room Cost Alerts tab |

### Chat & Sessions

| Flow | Document | Description |
|------|----------|-------------|
| Persistent Chat Tracking | [persistent-chat-tracking.md](feature-flows/persistent-chat-tracking.md) | Database-backed chat persistence |
| Web Terminal | [web-terminal.md](feature-flows/web-terminal.md) | Browser-based terminal for System Agent |

### Testing & Development

| Flow | Document | Description |
|------|----------|-------------|
| Testing Agents Suite | [testing-agents.md](feature-flows/testing-agents.md) | Automated pytest suite (1460+ tests) |
| Local Agent Deployment | [local-agent-deploy.md](feature-flows/local-agent-deploy.md) | Deploy local agents via MCP |
| Dark Mode / Theme | [dark-mode-theme.md](feature-flows/dark-mode-theme.md) | Client-side theme system |

---

## Process Engine Flows

The Process Engine enables BPMN-inspired workflow orchestration with AI agents.

**Index Document**: [process-engine/README.md](feature-flows/process-engine/README.md)

| Flow | Document | Description |
|------|----------|-------------|
| Process Definition | [process-definition.md](feature-flows/process-engine/process-definition.md) | YAML schema, validation, versioning |
| Process Execution | [process-execution.md](feature-flows/process-engine/process-execution.md) | Execution engine, step handlers, state machine |
| Process Monitoring | [process-monitoring.md](feature-flows/process-engine/process-monitoring.md) | Real-time WebSocket events |
| Human Approval | [human-approval.md](feature-flows/process-engine/human-approval.md) | Approval gates, inbox, timeout handling |
| Process Scheduling | [process-scheduling.md](feature-flows/process-engine/process-scheduling.md) | Cron triggers, timer steps |
| Process Analytics | [process-analytics.md](feature-flows/process-engine/process-analytics.md) | Cost tracking, metrics, trends |
| Sub-Processes | [sub-processes.md](feature-flows/process-engine/sub-processes.md) | Parent-child linking, breadcrumbs |
| Agent Roles (EMI) | [agent-roles-emi.md](feature-flows/process-engine/agent-roles-emi.md) | Executor/Monitor/Informed pattern |
| Process Templates | [process-templates.md](feature-flows/process-engine/process-templates.md) | Bundled and user templates |
| Onboarding & Docs | [onboarding-documentation.md](feature-flows/process-engine/onboarding-documentation.md) | Process Wizard, Help panel |
| Execution List Page | [execution-list-page.md](feature-flows/execution-list-page.md) | `/executions` route |
| Process Dashboard | [process-dashboard.md](feature-flows/process-dashboard.md) | `/process-dashboard` analytics |

---

## Archived Flows

Preserved in `feature-flows/archive/` for historical reference.

| Flow | Status | Document | Reason |
|------|--------|----------|--------|
| Auth0 Authentication | REMOVED | [archive/auth0-authentication.md](feature-flows/archive/auth0-authentication.md) | Replaced by email auth (2026-01-01) |
| Agent Chat | DEPRECATED | [archive/agent-chat.md](feature-flows/archive/agent-chat.md) | Replaced by Agent Terminal |
| Agent Vector Memory | REMOVED | [archive/vector-memory.md](feature-flows/archive/vector-memory.md) | Templates should define their own |
| Agent Network Replay | SUPERSEDED | [archive/agent-network-replay-mode.md](feature-flows/archive/agent-network-replay-mode.md) | Replaced by Dashboard Timeline |
| System Agent UI | CONSOLIDATED | [archive/system-agent-ui.md](feature-flows/archive/system-agent-ui.md) | Uses regular AgentDetail.vue |
| Skills Management | SPLIT | [archive/skills-management.md](feature-flows/archive/skills-management.md) | Split into dedicated flows |

---

## Requirements Specs

### Implemented

| Document | Status | Description |
|----------|--------|-------------|
| [DEDICATED_SCHEDULER_SERVICE.md](../requirements/DEDICATED_SCHEDULER_SERVICE.md) | ✅ | Standalone scheduler service |
| [EXTERNAL_PUBLIC_URL.md](../requirements/EXTERNAL_PUBLIC_URL.md) | ✅ | External URL for public links |
| [EXECUTION_ORIGIN_TRACKING.md](../requirements/EXECUTION_ORIGIN_TRACKING.md) | ✅ | Track who triggered executions |
| [AGENT_SYSTEMS_AND_TAGS.md](../requirements/AGENT_SYSTEMS_AND_TAGS.md) | ✅ | Tags and System Views |
| [NEVERMINED_PAYMENT_INTEGRATION.md](../requirements/NEVERMINED_PAYMENT_INTEGRATION.md) | ✅ | Per-agent x402 payment monetization |

### Pending

| Document | Priority | Description |
|----------|----------|-------------|
| [PUBLIC_EXTERNAL_ACCESS_SETUP.md](../requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md) | MEDIUM | Infrastructure setup for public access |

---

## Core Specifications

| Document | Purpose |
|----------|---------|
| [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../TRINITY_COMPATIBLE_AGENT_GUIDE.md) | Creating Trinity-compatible agents |
| [MULTI_AGENT_SYSTEM_GUIDE.md](../MULTI_AGENT_SYSTEM_GUIDE.md) | Building multi-agent systems |

---

## Flow Document Template

Save flows to: `docs/memory/feature-flows/{feature-name}.md`

```markdown
# Feature: {Feature Name}

## Overview
Brief description of what this feature does.

## User Story
As a [user type], I want to [action] so that [benefit].

## Entry Points
- **UI**: `src/frontend/src/views/Component.vue` - Action trigger
- **API**: `METHOD /api/endpoint`

## Frontend Layer
### Components
- `Component.vue:line` - handler() method

### State Management
- `stores/store.js` - action name

## Backend Layer
### Endpoints
- `src/backend/routers/file.py:line` - endpoint_handler()

### Business Logic
1. Step one
2. Step two

## Data Layer
### Database Operations
- Query: Description
- Update: Description

## Side Effects
- WebSocket broadcast: `{type, data}`

## Error Handling
- Error case → HTTP status

## Testing
### Prerequisites
- Services running
- Test user logged in

### Test Steps
1. **Action**: Do X
   **Expected**: Y happens
   **Verify**: Check Z

## Related Flows
- [related-flow.md](feature-flows/related-flow.md)
```

---

## How to Create a Flow Document

1. Run `/feature-flow-analysis {feature-name}`
2. Or manually trace: UI → API → Backend → Database → Side Effects
3. Add Testing section with step-by-step verification
4. Update this index after creating

See `docs/TESTING_GUIDE.md` for testing template and examples.
