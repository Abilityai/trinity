# Trinity System Overview

## What This Diagram Shows

This document provides a high-level architectural overview of the Trinity platform. It illustrates:

1. **Platform Layers**: How the web interface, platform services, and agent runtime relate to each other
2. **Key Differentiators**: How autonomous agents differ from reactive chatbots

Use this as a starting point for understanding Trinity's architecture before diving into specific feature flows.

---

## What is Trinity?

Trinity is the **operating system for the AI-native company** — open source, self-hosted, that you own. It deploys, orchestrates, and governs fleets of AI agents on your own hardware, and it is model-agnostic: each agent runs on Claude Code, Gemini CLI or OpenAI Codex.

```
+---------------------------------------------------------------------------------+
|                                                                                 |
|                         TRINITY PLATFORM                                        |
|                  "The operating system for the AI-native company"               |
|                                                                                 |
|   +-------------------------------------------------------------------------+  |
|   |                         WEB INTERFACE                                    |  |
|   |                                                                          |  |
|   |   Dashboard    Agent Details    File Manager    System Agent    Settings |  |
|   |      |              |               |               |             |      |  |
|   +------+--------------+---------------+---------------+-------------+------+  |
|          |              |               |               |             |         |
|          +--------------+---------------+---------------+-------------+         |
|                                      |                                          |
|                                      v                                          |
|   +-------------------------------------------------------------------------+  |
|   |                         PLATFORM SERVICES                                |  |
|   |                                                                          |  |
|   |  +----------+  +----------+  +----------+  +----------+  +----------+  |  |
|   |  | Backend  |  |   MCP    |  |  Vector  |  |  Redis   |  |  SQLite  |  |  |
|   |  | FastAPI  |  |  Server  |  |   Logs   |  | Secrets  |  |   Data   |  |  |
|   |  |  :8000   |  |  :8080   |  |  :8686   |  |  :6379   |  |          |  |  |
|   |  +----+-----+  +----+-----+  +----------+  +----------+  +----------+  |  |
|   |       |             |                                                    |  |
|   +-------+-------------+----------------------------------------------------+  |
|           |             |                                                       |
|           v             v                                                       |
|   +-------------------------------------------------------------------------+  |
|   |                         AGENT RUNTIME                                    |  |
|   |                                                                          |  |
|   |   +-------------+  +-------------+  +-------------+  +-------------+   |  |
|   |   |   Agent 1   |  |   Agent 2   |  |   Agent 3   |  |   Agent N   |   |  |
|   |   |             |  |             |  |             |  |             |   |  |
|   |   | +---------+ |  | +---------+ |  | +---------+ |  | +---------+ |   |  |
|   |   | | Claude  | |  | | Claude  | |  | | Claude  | |  | | Claude  | |   |  |
|   |   | |  Code   | |  | |  Code   | |  | |  Code   | |  | |  Code   | |   |  |
|   |   | +---------+ |  | +---------+ |  | +---------+ |  | +---------+ |   |  |
|   |   |             |  |             |  |             |  |             |   |  |
|   |   | MCP Tools   |  | MCP Tools   |  | MCP Tools   |  | MCP Tools   |   |  |
|   |   | Workspace   |  | Workspace   |  | Workspace   |  | Workspace   |   |  |
|   |   | Credentials |  | Credentials |  | Credentials |  | Credentials |   |  |
|   |   +-------------+  +-------------+  +-------------+  +-------------+   |  |
|   |                                                                          |  |
|   |                    Agent Network (172.28.0.0/16)                        |  |
|   +-------------------------------------------------------------------------+  |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

---

## Key Components

| Component | Port | Description |
|-----------|------|-------------|
| **Web Interface** | :80 | Vue.js 3 frontend with Dashboard, Agent Details, File Manager, Settings |
| **Backend (FastAPI)** | :8000 | REST API for agent management, authentication, credentials, scheduling |
| **MCP Server** | :8080 | FastMCP server exposing 29 tools for external Claude Code orchestration |
| **Vector** | :8686 | Log aggregation service capturing all container stdout/stderr |
| **Redis** | :6379 | Secrets storage and OAuth state cache |
| **SQLite** | - | Primary database for users, agents, sessions, permissions |
| **Agent Containers** | :8000 (internal) | Isolated Docker containers running Claude Code with MCP tools |
| **Agent Network** | 172.28.0.0/16 | Isolated Docker network for agent-to-agent communication |

---

## Key Differentiators

Trinity agents go beyond chatbots: they run on schedules, delegate work to other agents, persist memory across sessions, and operate without human intervention.

```
+---------------------------------------------------------------------------------+
|                                                                                 |
|   Traditional Chatbot                     Trinity Agent                         |
|   --------------------                    ----------------------                |
|                                                                                 |
|   User --> Bot --> Response               User --> Agent --> Plan              |
|       (reactive)                                              |                 |
|                                                               v                 |
|   * Single turn                           +-------------------------+           |
|   * No memory                             |  1. Decompose goal      |           |
|   * No planning                           |  2. Execute steps       |           |
|   * Human-dependent                       |  3. Delegate to others  |           |
|                                           |  4. Handle failures     |           |
|                                           |  5. Report results      |           |
|                                           +-------------------------+           |
|                                                                                 |
|                                           * Multi-session memory                |
|                                           * Autonomous execution                |
|                                           * Self-healing recovery               |
|                                           * Agent-to-agent collaboration        |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

---

## Sources

### Feature Flows
- [Agent Lifecycle](../memory/feature-flows/agent-lifecycle.md) - Agent create/start/stop/delete operations
- [MCP Orchestration](../memory/feature-flows/mcp-orchestration.md) - External MCP tool integration (29 tools)

### Architecture Documentation
- [Architecture Overview](../memory/architecture.md) - Full system architecture with database schemas

### Key Source Files

| Component | File | Description |
|-----------|------|-------------|
| Backend Entry | `src/backend/main.py` | FastAPI app initialization, router mounting |
| Database | `src/backend/database.py` | SQLite persistence layer |
| Agent Router | `src/backend/routers/agents.py` | Core CRUD, lifecycle, stats, queue, activities, terminal |
| Agent Config Router | `src/backend/routers/agent_config.py` | Per-agent settings (autonomy, read-only, resources, capabilities, capacity, timeout) |
| Agent Files Router | `src/backend/routers/agent_files.py` | Files, info, playbooks, permissions, metrics, folders |
| Agent Rename Router | `src/backend/routers/agent_rename.py` | Rename endpoint |
| Agent SSH Router | `src/backend/routers/agent_ssh.py` | SSH access endpoint |
| Agent Lifecycle | `src/backend/services/agent_service/lifecycle.py:63-171` | Container start/stop logic |
| Agent Creation | `src/backend/services/agent_service/crud.py:48-552` | Agent creation with Docker SDK |
| MCP Server | `src/mcp-server/src/server.ts:65-112` | FastMCP server configuration |
| MCP Tools | `src/mcp-server/src/tools/` | 21 MCP tool implementations |
| Frontend Entry | `src/frontend/src/main.js` | Vue.js app initialization |
| Agent Detail | `src/frontend/src/views/AgentDetail.vue` | Agent management UI |
| Vector Config | `config/vector.yaml` | Log aggregation configuration |
| Docker Compose | `docker-compose.yml` | Local service orchestration |

---

**Last Updated**: 2026-01-25
