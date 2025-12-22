# Trinity Roadmap

> **Purpose**: Phased implementation plan derived from requirements.
> Work topmost incomplete items. All items must trace to `requirements.md`.
>
> **Vision**: Trinity implements the Four Pillars of Deep Agency for System 2 AI.

---

## Current Priority Queue

### 🐛 Bug Fixes (Priority)

| Status | Item | Description | Priority |
|--------|------|-------------|----------|
| ✅ | Context % Calculation Bug | **Fixed 2025-12-12**: Main bug was in agent_server/routers/chat.py incorrectly summing input_tokens + cache_creation_tokens + cache_read_tokens, causing >100% display (130%, 289%). cache_creation and cache_read are billing SUBSETS, not additional tokens. Now uses metadata.input_tokens directly (authoritative total from modelUsage). Also fixed in scheduler_service.py (2 locations, 2025-12-06) and claude_code.py logging. | HIGH |
| ✅ | Template Detail Endpoint 404 | **Fixed 2025-12-22**: `GET /api/templates/{id}` returned 404 for GitHub templates like `github:org/repo`. Root cause: The `/` in the template ID was interpreted as path separator. Fix: Changed route from `{template_id}` to `{template_id:path}` to capture full path including slashes. | MEDIUM |
| ✅ | .env Template Endpoint Bug | **Verified 2025-12-22**: Endpoint works correctly. Code at lines 110-130 already handles both string credentials (GitHub templates) and dict credentials (local templates). Tested all GitHub templates + local templates - all pass. | MEDIUM |

---

### Phase 9: Deep Agent Core Infrastructure (Pillar I - Explicit Planning)
🚧 **In Progress** — *Critical for Deep Agent positioning*

| Status | Item | Description | Priority |
|--------|------|-------------|----------|
| ✅ | Task DAG Engine (Backend) | External task graph representation - API + storage | HIGH |
| ✅ | Task State Tracking | pending → active → completed → failed state machine | HIGH |
| ✅ | Plan Persistence | Store/restore task DAGs across sessions | HIGH |
| ✅ | Trinity Meta-Prompt Injection | Platform injects planning commands at startup | HIGH |
| ✅ | Task DAG Visualization (Dashboard) | AgentNode shows current task + progress bar in Collaboration Dashboard | HIGH |
| ✅ | **AgentDetail Plans UI** | Plans tab showing plan list, task details, status badges (2025-12-07) | HIGH |
| ✅ | **Agent Permissions (9.10)** | Permission grants control which agents can call each other (2025-12-13) | HIGH |
| ✅ | **Agent Shared Folders (9.11)** | File-based collaboration via shared Docker volumes (2025-12-13) | HIGH |
| ⏳ | Task Actions UI | Manual complete/fail/re-run buttons for tasks | LOW |
| ⏳ | Failure Recovery | Plan restructuring when steps fail (add debug nodes) | MEDIUM |
| ⏳ | Sentinel/Watchdog Agents | Low-cost monitors detecting infinite loops, human escalation | MEDIUM |
| ⏳ | Thinking Budget | Cost limits per reasoning task ($X max spend) | MEDIUM |

### Phase 10: Deep Agent Memory & Observability (Pillar III Enhancement)
🚧 **In Progress**

| Status | Item | Description | Priority |
|--------|------|-------------|----------|
| ✅ | **Agent Vector Memory (Chroma)** | Chroma DB + all-MiniLM-L6-v2 per agent (2025-12-13) | MEDIUM |
| ✅ | **Chroma MCP Server** | Auto-inject chroma-mcp into agents for MCP-based vector ops (2025-12-13) | HIGH |
| ⏳ | Memory Folding | Periodic context compression to summary files | HIGH |
| ⏳ | Reasoning Chain Logs | Capture "why" decisions, not just tool calls | MEDIUM |
| ⏳ | Cognitive Rollback | Git-based agent state restoration | MEDIUM |
| ⏳ | Vectorized Episodic Memory | Auto-store conversations for "Have I solved this before?" | LOW |

### Phase 11: Ecosystem & Enterprise
🚧 **In Progress**

| Status | Item | Description | Priority |
|--------|------|-------------|----------|
| ✅ | **System Manifest (10.7)** | Recipe-based multi-agent deployment via YAML - complete (agent creation, conflict resolution, trinity_prompt, folders, schedules, permissions, auto-start). Completed 2025-12-18. | HIGH |
| ✅ | **OpenTelemetry Integration** | OTel Collector + env var injection for Claude Code metrics export. Completed 2025-12-20. | HIGH |
| ✅ | **OpenTelemetry UI (10.8)** | Display OTel metrics in Dashboard - cost, tokens, productivity. Backend API + header summary + panel detail. Completed 2025-12-20. | HIGH |
| ✅ | **Internal System Agent (11.1)** | Auto-deployed platform orchestrator (`trinity-system`) with deletion protection, system-scoped MCP key, permission bypass. Completed 2025-12-20. | HIGH |
| ✅ | **Parallel Headless Execution (12.1)** | Stateless parallel task execution - enables orchestrator to spawn N worker tasks simultaneously. `POST /api/agents/{name}/task` bypasses queue. MCP `chat_with_agent(parallel=true)`. Completed 2025-12-22. | **HIGH** |
| ⏳ | System Manifest UI | Upload YAML, view deployment results, group agents by system prefix | MEDIUM |
| ⏳ | A2A Protocol Support | Agent discovery and negotiation across boundaries | LOW |
| ⏳ | Agent collaboration execution tracking | Extend schedule_executions | LOW |
| ⏳ | Automated sync modes (scheduled, on-stop) | GitOps enhancement | LOW |
| ⏳ | Automated secret rotation | Security enhancement | LOW |

### Phase 12: Agent Perception & Attention (Cognitive Patterns)
⏳ **Pending** — *Emergent coordination via event-driven cognition*

| Status | Item | Description | Priority |
|--------|------|-------------|----------|
| ⏳ | **Event Bus Infrastructure** | Platform-wide pub/sub system for agent event broadcasting and subscription | HIGH |
| ⏳ | Event Types & Schema | Define standard event types (task_completed, anomaly_detected, resource_available, attention_required) | HIGH |
| ⏳ | Agent Event Subscriptions | Agents declare interest in event types via template.yaml or runtime API | MEDIUM |
| ⏳ | Event Persistence & Replay | Store events for late-joining agents and debugging | MEDIUM |
| ⏳ | **Attention Amplification Pattern** | Cognitive pattern: agents select salient events from perception and amplify to others | HIGH |
| ⏳ | Salience Scoring | Agents assign importance scores to perceived events based on relevance to their goals | MEDIUM |
| ⏳ | Broadcast Amplification | High-salience events re-broadcast with amplification metadata (source_agent, salience_score, reasoning) | MEDIUM |
| ⏳ | Attention Cascade Detection | Platform detects when multiple agents amplify same event (emergent consensus) | LOW |
| ⏳ | Attention Dashboard | Visualize event flow and amplification patterns in Collaboration Dashboard | LOW |

**Cognitive Pattern: Attention Amplification**
```
1. Agent perceives events from subscribed channels
2. Agent evaluates salience: "Does this require attention?"
3. High-salience events are re-broadcast with amplification:
   {
     "type": "attention_required",
     "original_event": {...},
     "amplified_by": "agent-name",
     "salience_score": 0.92,
     "reasoning": "Anomaly in data pipeline may affect downstream agents"
   }
4. Other agents see amplified events with social proof
5. Platform tracks amplification cascades for emergent coordination
```

---

## Completed Phases

### Phase 7: GitHub Bidirectional Sync
✅ **Completed: 2025-11-29**

**Architecture Document**: `docs/GITHUB_NATIVE_AGENTS.md`

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Database schema: `agent_git_config` table | 2025-11-29 |
| ✅ | Create working branch on GitHub-template agent creation | 2025-11-29 |
| ✅ | Store repo URL, branch name, instance ID in database | 2025-11-29 |
| ✅ | POST `/api/agents/{name}/git/sync` endpoint | 2025-11-29 |
| ✅ | Git operations: stage, commit, force push | 2025-11-29 |
| ✅ | "Sync to GitHub" button in agent detail UI | 2025-11-29 |
| ✅ | Track last commit SHA and push timestamp | 2025-11-29 |
| ✅ | "Git" tab in agent detail page | 2025-11-29 |
| ✅ | Show repo, branch, last sync, commit history | 2025-11-29 |
| ✅ | Sync status indicator | 2025-11-29 |

### Phase 6: Agent Scheduling & Autonomy
✅ **Completed: 2025-11-28**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Platform scheduler service (APScheduler) | 2025-11-28 |
| ✅ | Schedule CRUD API endpoints | 2025-11-28 |
| ✅ | Cron-style scheduling support | 2025-11-28 |
| ✅ | Schedule UI on agent detail page | 2025-11-28 |
| ✅ | Scheduled executions list & logs | 2025-11-28 |
| ✅ | Enable/disable & manual trigger | 2025-11-28 |
| ✅ | Timezone support | 2025-11-28 |
| ✅ | WebSocket broadcast for execution events | 2025-11-28 |

### Phase 5: Agent Sharing & Observability
✅ **Completed: 2025-11-28**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Agent sharing (Owner/Shared/Admin access) | 2025-11-28 |
| ✅ | Context window tracking in chat header | 2025-11-28 |
| ✅ | Agent live telemetry (CPU, memory, network, uptime) | 2025-11-28 |
| ✅ | Unified activity panel for tool calls | 2025-11-28 |
| ✅ | Session cost tracking | 2025-11-28 |
| ✅ | New session reset button | 2025-11-28 |
| ✅ | Markdown rendering in chat | 2025-11-28 |

### Phase 4: Multi-Agent Support
✅ **Completed: 2025-11-27**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Trinity MCP server (12 tools) | 2025-11-27 |
| ✅ | MCP API key authentication | 2025-11-27 |
| ✅ | Per-user API keys | 2025-11-27 |
| ✅ | Inter-agent chat via MCP | 2025-11-27 |
| ✅ | MCP usage statistics | 2025-11-27 |
| ✅ | Credential hot-reload | 2025-11-28 |
| ✅ | Bulk credential import | 2025-11-27 |
| ✅ | Credential requirements visibility | 2025-11-27 |

### Phase 3.5: Credential Management
✅ **Completed: 2025-11-27**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Redis-backed credential storage | 2025-11-25 |
| ✅ | OAuth2 flows (Google, Slack, GitHub, Notion) | 2025-11-25 |
| ✅ | Credential injection at agent creation | 2025-11-25 |
| ✅ | Manual credential entry | 2025-11-25 |
| ✅ | SQLite data persistence (users, ownership, API keys) | 2025-11-27 |

### Phase 3: Enhanced Security
✅ **Completed: 2025-11-27**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Auth0 + Google OAuth integration | 2025-11-27 |
| ✅ | Domain restriction (@ability.ai) | 2025-11-27 |
| ✅ | Container security (non-root, cap-drop) | 2025-11-26 |
| ✅ | Audit logging service | 2025-11-26 |
| ✅ | Network isolation | 2025-11-26 |
| ✅ | Development mode bypass | 2025-11-27 |

### Phase 2: Web Management Interface
✅ **Completed: 2025-11-25**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Vue.js frontend with Tailwind | 2025-11-22 |
| ✅ | Agent list dashboard | 2025-11-22 |
| ✅ | Agent creation UI | 2025-11-22 |
| ✅ | Agent start/stop controls | 2025-11-22 |
| ✅ | Real-time WebSocket updates | 2025-11-22 |
| ✅ | GCP production deployment | 2025-11-25 |
| ✅ | SSL/TLS via Let's Encrypt | 2025-11-25 |

### Phase 1: Base Infrastructure
✅ **Completed: 2025-11-22**

| Status | Item | Completed |
|--------|------|-----------|
| ✅ | Universal agent base image | 2025-11-20 |
| ✅ | Multi-runtime support (Python, Node, Go) | 2025-11-20 |
| ✅ | Claude Code installation | 2025-11-20 |
| ✅ | FastAPI backend | 2025-11-21 |
| ✅ | Docker SDK integration | 2025-11-21 |
| ✅ | Agent creation/deletion | 2025-11-21 |
| ✅ | Chat via backend API | 2025-11-22 |
| ✅ | Template system (GitHub + local) | 2025-11-22 |

---

## Backlog

Items not yet scheduled. Will be prioritized as needed.

| Priority | Item | Requirement |
|----------|------|-------------|
| **High** | **Event Bus Infrastructure** | Phase 12 - Platform-wide pub/sub for agent event broadcasting/subscription |
| **High** | **Attention Amplification Pattern** | Phase 12 - Cognitive pattern for salience-based event amplification |
| Low | Task DAG Graph Visualization | 9.8 - Visual dependency graph (Vue Flow) - backend ready, UI nice-to-have |
| Medium | Kubernetes deployment scripts | 10.3 Kubernetes Deployment |
| Medium | Helm charts | 10.3 Kubernetes Deployment |
| Low | Automated testing pipeline | Non-functional |
| Low | Performance monitoring dashboard | Non-functional |
| Low | Agent resource usage alerts | Non-functional |

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-12-22 | Parallel Headless Execution (12.1) | Two execution modes: Sequential Chat (maintains context with --continue) and Parallel Task (stateless, no lock). Enables orchestrators to spawn N parallel worker tasks. Based on Claude Code headless mode research. |
| 2025-12-20 | Internal System Agent (11.1) | Platform needs a privileged orchestrator that auto-deploys on startup, executes system-level user requests, and cannot be deleted. Enables unified multi-agent coordination. |
| 2025-12-08 | Deprioritize Task DAG Graph Viz | Backend workplan system complete; text-based UI sufficient for now; graph viz is nice-to-have |
| 2025-12-05 | Deep Agent positioning | Trinity = "Four Pillars of Deep Agency" platform. Pillar I (Explicit Planning) is the priority gap. |
| 2025-11-26 | Docker as source of truth | Eliminates in-memory registry issues with multiple workers |
| 2025-11-27 | SQLite + Redis hybrid | SQLite for relations, Redis for secrets - survives restarts |
| 2025-11-27 | No external agent UI ports | Security - all access via authenticated backend API |
| 2025-11-28 | Auth0 domain restriction | Enterprise security - @ability.ai only |

---

## How to Use This Document

1. **Check current phase** - Work on items in "Current Priority Queue"
2. **Pick topmost ⏳** - Start with first incomplete item
3. **Update on completion** - Change ⏳ to ✅, add date
4. **Move to completed** - Once all items done, move phase to "Completed Phases"
5. **Add new items** - Must trace to `requirements.md` requirement ID
