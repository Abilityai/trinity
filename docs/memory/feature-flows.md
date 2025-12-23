# Feature Flows Index

> **Purpose**: Maps features to detailed vertical slice documentation.
> Each flow documents the complete path from UI → API → Database → Side Effects.

> **Important (2025-12-07)**: The Dashboard page now contains the Agent Network visualization (previously in a separate AgentNetwork.vue). Route `/network` redirects to `/`. See [agent-network.md](feature-flows/agent-network.md) for details.

> **Updated (2025-12-12)**: Critical bug fix:
> - **Agent Chat**: Context percentage >100% bug fixed - was incorrectly summing `input_tokens + cache_creation_tokens + cache_read_tokens`. Cache tokens are billing breakdowns (subsets), not additional tokens. Now uses `metadata.input_tokens` directly (authoritative total from modelUsage). See [agent-chat.md](feature-flows/agent-chat.md) for details.
>
> **Updated (2025-12-09)**: Bug fixes:
> - **Agent Lifecycle**: File persistence fixed - `startup.sh` now checks for existing `.git` or `.trinity-initialized` before re-cloning (Pillar III compliance)
> - **Agent Network**: Initial `activityState` set in `convertAgentsToNodes()` - running agents show "Idle" not "Offline"

> **Updated (2025-12-07)**: Several feature flows updated for recent changes:
> - **Template Processing**: Templates.vue now dynamically fetches from API (was static). CreateAgentModal has new `initialTemplate` prop and `created` event.
> - **Credential Injection**: OAuth provider buttons removed from Credentials.vue UI (backend API still available).
> - **Agent Network**: AgentNode.vue card layout fixes (flex, always-show progress bar, button alignment).
> - **Agent Lifecycle**: CreateAgentModal enhancements documented.

> **Important (2025-12-06)**: The agent-server has been refactored from a monolithic `agent-server.py` file into a modular package at `docker/base-image/agent_server/`. All feature flows referencing agent-server have been updated with new file paths and line numbers.

---

## Documented Flows

| Flow | Priority | Document | Description |
|------|----------|----------|-------------|
| Authentication Mode System | High | [auth0-authentication.md](feature-flows/auth0-authentication.md) | **Runtime** dual-mode auth: dev (local login) + prod (Auth0 OAuth), JWT mode claims (Updated 2025-12-05) |
| Agent Lifecycle | High | [agent-lifecycle.md](feature-flows/agent-lifecycle.md) | Create, start, stop, delete Docker containers (Updated 2025-12-19) |
| Agent Chat | High | [agent-chat.md](feature-flows/agent-chat.md) | Chat with Claude Code agents, streaming responses |
| Credential Injection | High | [credential-injection.md](feature-flows/credential-injection.md) | Redis storage, hot-reload, OAuth2 flows (Updated 2025-12-19) |
| Agent Scheduling | High | [scheduling.md](feature-flows/scheduling.md) | Cron-based automation, APScheduler, execution tracking |
| Activity Monitoring | Medium | [activity-monitoring.md](feature-flows/activity-monitoring.md) | Real-time tool execution tracking |
| Agent Logs & Telemetry | Medium | [agent-logs-telemetry.md](feature-flows/agent-logs-telemetry.md) | Container logs viewing and live metrics |
| Template Processing | Medium | [template-processing.md](feature-flows/template-processing.md) | GitHub and local template handling |
| Agent Sharing | Medium | [agent-sharing.md](feature-flows/agent-sharing.md) | Email-based sharing, access levels |
| MCP Orchestration | Medium | [mcp-orchestration.md](feature-flows/mcp-orchestration.md) | 12 MCP tools for external agent management |
| GitHub Sync | Medium | [github-sync.md](feature-flows/github-sync.md) | Bidirectional GitHub sync for agents (Phase 7) |
| Agent Info Display | Medium | [agent-info-display.md](feature-flows/agent-info-display.md) | Template metadata display in Info tab (Req 9.3) |
| Agent-to-Agent Collaboration | High | [agent-to-agent-collaboration.md](feature-flows/agent-to-agent-collaboration.md) | Inter-agent communication via Trinity MCP (Implemented 2025-11-29) |
| Persistent Chat Tracking | High | [persistent-chat-tracking.md](feature-flows/persistent-chat-tracking.md) | Database-backed chat persistence with full observability (Implemented 2025-12-01) |
| File Browser | Medium | [file-browser.md](feature-flows/file-browser.md) | Browse and download workspace files via web UI (Implemented 2025-12-01) |
| Agent Network (Dashboard) | High | [agent-network.md](feature-flows/agent-network.md) | Real-time visual graph showing agents and messages - **now integrated into Dashboard.vue at `/`** (Updated 2025-12-19) |
| Agent Network Replay Mode | High | [agent-network-replay-mode.md](feature-flows/agent-network-replay-mode.md) | Time-compressed replay of historical messages with VCR controls and timeline scrubbing - **now in Dashboard.vue** (Updated 2025-12-19) |
| Unified Activity Stream | High | [activity-stream.md](feature-flows/activity-stream.md) | Centralized persistent activity tracking with WebSocket broadcasting (Implemented 2025-12-02, Req 9.7) |
| Activity Stream Collaboration Tracking | High | [activity-stream-collaboration-tracking.md](feature-flows/activity-stream-collaboration-tracking.md) | Complete vertical slice: MCP → Database → Dashboard visualization (Implemented 2025-12-02, Req 9.7) |
| **Workplan System** | High | [workplan-system.md](feature-flows/workplan-system.md) | **Pillar I** - Platform injection, agent-server API, workplan visibility (Updated 2025-12-20, Req 9.1) |
| **Workplan UI** | High | [workplan-ui.md](feature-flows/workplan-ui.md) | WorkplanPanel component in AgentDetail - view/manage workplans with summary stats, filtering, and detail modal (Updated 2025-12-19, Req 9.8) |
| **Execution Queue** | Critical | [execution-queue.md](feature-flows/execution-queue.md) | Parallel execution prevention via Redis-backed queue. Serializes User Chat, Scheduler, and MCP requests with 429 handling and defense-in-depth (Implemented 2025-12-06) |
| **Agents Page UI Improvements** | Medium | [agents-page-ui-improvements.md](feature-flows/agents-page-ui-improvements.md) | Activity indicators, context stats, task progress, sorting - reusing Collaboration Dashboard APIs (Implemented 2025-12-07, Updated 2025-12-19) |
| **Testing Agents Suite** | High | [testing-agents.md](feature-flows/testing-agents.md) | Automated pytest suite (179/179 passing) + 8 local test agents for manual verification - agent-server refactored to modular package (Updated 2025-12-19) |
| **Agent Custom Metrics** | High | [agent-custom-metrics.md](feature-flows/agent-custom-metrics.md) | Agent-defined custom metrics in template.yaml displayed in UI - 6 metric types with auto-refresh (Implemented 2025-12-10, Req 9.9) (Updated 2025-12-19) |
| **Agent-to-Agent Permissions** | High | [agent-permissions.md](feature-flows/agent-permissions.md) | Fine-grained control over which agents can communicate with each other via MCP tools (Implemented 2025-12-10, Updated 2025-12-19, Req 9.10) |
| **Agent Shared Folders** | High | [agent-shared-folders.md](feature-flows/agent-shared-folders.md) | File-based collaboration via shared Docker volumes mounted at `/home/developer/shared-out` and `/home/developer/shared-in/{agent}` (Implemented 2025-12-13, Updated 2025-12-19, Req 9.11) |
| **Agent Vector Memory** | Medium | [vector-memory.md](feature-flows/vector-memory.md) | Per-agent Chroma vector database with MCP server integration for semantic memory (Implemented 2025-12-13, Req 10.4 + 10.5, Updated 2025-12-19) |
| **System-Wide Trinity Prompt** | High | [system-wide-trinity-prompt.md](feature-flows/system-wide-trinity-prompt.md) | Admin-configurable custom instructions injected into all agents' CLAUDE.md at startup (Updated 2025-12-19) |
| **Dark Mode / Theme Switching** | Low | [dark-mode-theme.md](feature-flows/dark-mode-theme.md) | Client-side theme system with Light/Dark/System modes, localStorage persistence, Tailwind class strategy (Implemented 2025-12-14) |
| **System Manifest Deployment** | High | [system-manifest.md](feature-flows/system-manifest.md) | Recipe-based multi-agent deployment via YAML manifest - complete with permissions, folders, schedules, auto-start (Completed 2025-12-18, Req 10.7) |
| **OpenTelemetry Integration** | Medium | [opentelemetry-integration.md](feature-flows/opentelemetry-integration.md) | OTel metrics export from Claude Code agents to Prometheus via OTEL Collector - cost, tokens, productivity metrics with Dashboard UI (Phase 2.5 UI completed 2025-12-20) |
| **Internal System Agent** | High | [internal-system-agent.md](feature-flows/internal-system-agent.md) | Platform operations manager (trinity-system) with fleet ops API, health monitoring, schedule control, and emergency stop. Ops-only scope. **2025-12-21**: OTel access fix, 5-step reinitialize, Cost Monitoring flow (Req 11.1, 11.2) |
| **System Agent UI** | High | [system-agent-ui.md](feature-flows/system-agent-ui.md) | Admin-only `/system-agent` page with fleet overview cards, quick actions (Emergency Stop, Restart All, Pause/Resume Schedules), and Operations Console chat interface (Req 11.3 - Created 2025-12-20) |
| **Local Agent Deployment** | High | [local-agent-deploy.md](feature-flows/local-agent-deploy.md) | Deploy Trinity-compatible local agents via MCP tool - packages directory, auto-imports credentials, versioned deployment (Implemented 2025-12-21) |
| **Parallel Headless Execution** | High | [parallel-headless-execution.md](feature-flows/parallel-headless-execution.md) | Stateless parallel task execution via `POST /task` endpoint - bypasses queue, enables orchestrator-worker patterns (Implemented 2025-12-22, Req 12.1) |
| **Public Agent Links** | Medium | [public-agent-links.md](feature-flows/public-agent-links.md) | Shareable public links for unauthenticated agent access with optional email verification, usage tracking, and rate limiting (Implemented 2025-12-22, Req 12.2) |
| **First-Time Setup** | High | [first-time-setup.md](feature-flows/first-time-setup.md) | Admin password wizard on fresh install, bcrypt hashing, API key configuration in Settings, login block until setup complete (Implemented 2025-12-23, Req 11.4 / Phase 12.3) |

---

## Core Specifications

| Document | Purpose |
|----------|---------|
| [TRINITY_COMPATIBLE_AGENT_GUIDE.md](../TRINITY_COMPATIBLE_AGENT_GUIDE.md) | **Single-agent guide** — Creating Trinity-compatible agents, template structure, Four Pillars, planning system |
| [MULTI_AGENT_SYSTEM_GUIDE.md](../MULTI_AGENT_SYSTEM_GUIDE.md) | **Multi-agent guide** — Building coordinated multi-agent systems, architecture patterns, shared folders, deployment |

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

### API Calls
\`\`\`javascript
await api.method(`/api/endpoint`)
\`\`\`

## Backend Layer
### Endpoints
- `src/backend/main.py:line` - endpoint_handler()

### Business Logic
1. Step one
2. Step two
3. Step three

## Data Layer
### Database Operations
- Query: Description
- Update: Description

## Side Effects
- WebSocket broadcast: `{type, data}`
- Audit log entry

## Error Handling
- Error case → HTTP status

## Security Considerations
- Authorization checks
- Rate limiting

## Related Flows
- Upstream: Previous flow
- Downstream: Next flow
```

---

## How to Create a Flow Document

1. Run `/feature-flow-analysis {feature-name}`
2. Or manually:
   - Find UI entry point (grep for button/action)
   - Trace to API call (check store action)
   - Find backend endpoint (grep route)
   - Document database operations
   - Note side effects (WebSocket, audit)
   - **Add Testing section** with step-by-step verification instructions
3. Update this index after creating

## Testing Section Standard

Every feature flow MUST include a Testing section with:
- **Prerequisites**: What needs to be running/configured
- **Test Steps**: Numbered steps with Action → Expected → Verify
- **Edge Cases**: Boundary conditions and error scenarios
- **Cleanup**: How to reset state after testing
- **Status**: ✅ Working / 🚧 Not Tested / ⚠️ Issues / ❌ Broken

See `docs/TESTING_GUIDE.md` for template and examples.
