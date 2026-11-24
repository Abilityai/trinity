# Trinity Service Architecture

> **Last Updated**: 2026-02-06

## What This Diagram Shows

This document visualizes Trinity's containerized service architecture:

1. **Container Topology** - Platform containers (frontend, backend, MCP server, Vector) and agent containers running on isolated Docker networks
2. **Backend Service Architecture** - FastAPI router and service module organization with data layer components
3. **MCP Server Architecture** - FastMCP-based tool registry with authentication and 29 MCP tools

## Sources

| Component | Source File(s) |
|-----------|---------------|
| Container Topology | `docker-compose.yml`, `docs/memory/architecture.md:20-45` |
| Backend Routers | `src/backend/main.py:29-58`, `src/backend/routers/*.py` |
| Backend Services | `src/backend/services/`, `src/backend/services/agent_service/` |
| Data Layer | `src/backend/database.py`, `src/backend/services/credential_encryption.py` |
| MCP Server | `src/mcp-server/src/server.ts`, `src/mcp-server/src/tools/` |
| Vector Logging | `config/vector.yaml`, `docs/memory/feature-flows/vector-logging.md` |
| Process Engine | `src/backend/services/process_engine/`, `docs/memory/feature-flows/process-engine/` |

---

## Container Topology

```
+----------------------------------------------------------------------------------+
|                              DOCKER HOST                                         |
|                                                                                  |
|  +-------------------------------------------------------------------------+    |
|  |                        PLATFORM CONTAINERS                               |    |
|  |                                                                          |    |
|  |  +-------------+  +-------------+  +-------------+  +-------------+     |    |
|  |  |  frontend   |  |   backend   |  | mcp-server  |  |   Vector    |     |    |
|  |  |             |  |             |  |             |  |             |     |    |
|  |  |   Vue.js    |  |   FastAPI   |  |   FastMCP   |  | Log Aggreg. |     |    |
|  |  |   Nginx     |  |   Python    |  |   Node.js   |  |   (Rust)    |     |    |
|  |  |             |  |             |  |             |  |             |     |    |
|  |  |  Port 3000  |  |  Port 8000  |  |  Port 8080  |  |  internal   |     |    |
|  |  +------+------+  +------+------+  +------+------+  +------+------+     |    |
|  |         |                |                |                |            |    |
|  |         |    REST/WS     |    REST        |    Docker      |            |    |
|  |         +----------------+----------------+    Socket      |            |    |
|  |                          |                       |         |            |    |
|  |  +-------------+  +------+------+  +-------------+---------+            |    |
|  |  |    redis    |  |   SQLite    |  |  /data/logs/                       |    |
|  |  |             |  |             |  |  platform-YYYY-MM-DD.json          |    |
|  |  |  Secrets    |  |  ~/trinity- |  |  agents-YYYY-MM-DD.json            |    |
|  |  |  OAuth      |  |  data/      |  |                                    |    |
|  |  |  Cache      |  |  trinity.db |  +------------------------------------+    |
|  |  |             |  |             |                                       |    |
|  |  |  Port 6379  |  |  (bind mt)  |                                       |    |
|  |  +-------------+  +-------------+                                       |    |
|  |                                                                          |    |
|  +-------------------------------------------------------------------------+    |
|                                                                                  |
|  +-------------------------------------------------------------------------+    |
|  |                        AGENT CONTAINERS                                  |    |
|  |                     Network: trinity-agent-network                       |    |
|  |                        Subnet: 172.28.0.0/16                            |    |
|  |                                                                          |    |
|  |  +--------------+  +--------------+  +--------------+  +--------------+ |    |
|  |  |agent-research|  | agent-writer |  |agent-analyst |  |trinity-system| |    |
|  |  |              |  |              |  |              |  |   (SYSTEM)   | |    |
|  |  | Claude Code  |  | Claude Code  |  | Claude Code  |  | Claude Code  | |    |
|  |  | Agent Server |  | Agent Server |  | Agent Server |  | Agent Server | |    |
|  |  | MCP Tools    |  | MCP Tools    |  | MCP Tools    |  | Fleet Ops    | |    |
|  |  |              |  |              |  |              |  |              | |    |
|  |  | :8000 (int)  |  | :8000 (int)  |  | :8000 (int)  |  | :8000 (int)  | |    |
|  |  | :2222 (SSH)  |  | :2223 (SSH)  |  | :2224 (SSH)  |  | :2225 (SSH)  | |    |
|  |  +--------------+  +--------------+  +--------------+  +--------------+ |    |
|  |                                                                          |    |
|  +-------------------------------------------------------------------------+    |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

---

## Backend Service Architecture

```
+----------------------------------------------------------------------------------+
|                              BACKEND (FastAPI)                                   |
|                                                                                  |
|  +--------------------------------------------------------------------------+   |
|  |                              ROUTERS                                      |   |
|  |                                                                           |   |
|  |  +----------+ +----------+ +----------+ +----------+ +----------+        |   |
|  |  |   auth   | |  agents  | |   chat   | |schedules | |   git    |        |   |
|  |  +----+-----+ +----+-----+ +----+-----+ +----+-----+ +----+-----+        |   |
|  |       |            |            |            |            |               |   |
|  |  +----------+ +----------+ +----------+ +----------+ +----------+        |   |
|  |  | settings | |  sharing | |   creds  | |templates | | mcp_keys |        |   |
|  |  +----+-----+ +----+-----+ +----+-----+ +----+-----+ +----+-----+        |   |
|  |       |            |            |            |            |               |   |
|  |  +----------+ +----------+ +----------+ +----------+ +----------+        |   |
|  |  | systems  | |   ops    | |   logs   | |activities| |   audit  |        |   |
|  |  +----+-----+ +----+-----+ +----+-----+ +----+-----+ +----+-----+        |   |
|  |       |            |            |            |            |               |   |
|  |  +----------+ +----------+ +----------+ +----------+ +----------+        |   |
|  |  |processes | |executions| |approvals | | alerts   | | triggers |        |   |
|  |  +----+-----+ +----+-----+ +----+-----+ +----+-----+ +----+-----+        |   |
|  |       |            |            |            |            |               |   |
|  |  +----------+ +----------+ +----------+                                  |   |
|  |  |proc_tmpls| |  skills  | |   docs   |                                  |   |
|  |  +----+-----+ +----+-----+ +----+-----+                                  |   |
|  |       |            |            |                                         |   |
|  +-------+------------+------------+-----------------------------------------+   |
|          |            |            |                                             |
|  +-------+------------+------------+---------------------------------------------+
|  |                           SERVICES                                        |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     agent_service/                                  |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  |  |lifecycle | |  crud    | | terminal | |permissions| | folders  | |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  |  |  files   | | metrics  | |  queue   | |  deploy  | | api_key  | |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     process_engine/                                 |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  |  |  domain  | |  engine  | |  repos   | | services | |  events  | |  |   |
|  |  |  +----------+ +----------+ +----------+ +----------+ +----------+ |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------+ +--------------+ +--------------+ +--------------+     |   |
|  |  |docker_service| |scheduler_svc | |  git_service | |skill_service |     |   |
|  |  +--------------+ +--------------+ +--------------+ +--------------+     |   |
|  |                                                                           |   |
|  |  +--------------+ +--------------+ +--------------+ +--------------+     |   |
|  |  |agent_client  | |log_archive   | |github_service| |email_service |     |   |
|  |  +--------------+ +--------------+ +--------------+ +--------------+     |   |
|  |                                                                           |   |
|  +--------------------------------------------------------------------------+   |
|                                                                                  |
|  +--------------------------------------------------------------------------+   |
|  |                           DATA LAYER                                      |   |
|  |                                                                           |   |
|  |  +------------------------------+  +--------------------------------+    |   |
|  |  |         database.py          |  |    credential_encryption.py    |    |   |
|  |  |                              |  |         (CRED-002)             |    |   |
|  |  |  * Users                     |  |  * AES-256-GCM encryption      |    |   |
|  |  |  * Agent ownership           |  |  * .credentials.enc files     |    |   |
|  |  |  * Sharing                   |  |  * Auto-import on startup      |    |   |
|  |  |  * Permissions               |  |                                |    |   |
|  |  |  * Schedules                 |  |  Credentials stored in agent:  |    |   |
|  |  |  * Chat sessions             |  |  * .env (KEY=VALUE)            |    |   |
|  |  |  * Activities                |  |  * .mcp.json                   |    |   |
|  |  |  * Process definitions       |  |  * .credentials.enc (backup)   |    |   |
|  |  |  * Process executions        |  |                                |    |   |
|  |  |  * Skills                    |  |  Redis: OAuth state only       |    |   |
|  |  |         +---------+          |  |                                |    |   |
|  |  |         | SQLite  |          |  |                                |    |   |
|  |  |         +---------+          |  |                                |    |   |
|  |  +------------------------------+  +--------------------------------+    |   |
|  |                                                                           |   |
|  +--------------------------------------------------------------------------+   |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

---

## MCP Server Architecture

```
+----------------------------------------------------------------------------------+
|                           MCP SERVER (FastMCP)                                   |
|                                                                                  |
|  +--------------------------------------------------------------------------+   |
|  |                         AUTHENTICATION                                    |   |
|  |                                                                           |   |
|  |   Request --> Extract API Key --> Validate with Backend --> McpAuthContext|   |
|  |                                                                           |   |
|  |   Context: { userId, userEmail, keyName, agentName?, scope }             |   |
|  |                                                                           |   |
|  |   Scopes:  user (human)  |  agent (container)  |  system (trinity-system)|   |
|  |                                                                           |   |
|  +--------------------------------------------------------------------------+   |
|                                                                                  |
|  +--------------------------------------------------------------------------+   |
|  |                           29 MCP TOOLS                                    |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     AGENT MANAGEMENT (13 tools)                     |  |   |
|  |  |                                                                     |  |   |
|  |  |  list_agents       get_agent          get_agent_info               |  |   |
|  |  |  create_agent      delete_agent       start_agent     stop_agent   |  |   |
|  |  |  list_templates    reload_credentials get_credential_status        |  |   |
|  |  |  get_agent_ssh_access  deploy_local_agent  initialize_github_sync  |  |   |
|  |  |                                                                     |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     AGENT INTERACTION (3 tools)                     |  |   |
|  |  |                                                                     |  |   |
|  |  |  chat_with_agent --> Permission Check --> Proxy to Agent Container |  |   |
|  |  |  get_chat_history                                                   |  |   |
|  |  |  get_agent_logs                                                     |  |   |
|  |  |                                                                     |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     SYSTEM MANAGEMENT (4 tools)                     |  |   |
|  |  |                                                                     |  |   |
|  |  |  deploy_system      list_systems      restart_system               |  |   |
|  |  |  export_system_manifest                                             |  |   |
|  |  |                                                                     |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     SKILLS MANAGEMENT (8 tools)                     |  |   |
|  |  |                                                                     |  |   |
|  |  |  list_skills        get_skill          create_skill   delete_skill |  |   |
|  |  |  assign_skill_to_agent    remove_skill_from_agent                  |  |   |
|  |  |  sync_agent_skills        execute_procedure                        |  |   |
|  |  |                                                                     |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |  |                     DOCUMENTATION (1 tool)                          |  |   |
|  |  |                                                                     |  |   |
|  |  |  get_agent_requirements                                             |  |   |
|  |  |                                                                     |  |   |
|  |  +--------------------------------------------------------------------+  |   |
|  |                                                                           |   |
|  +--------------------------------------------------------------------------+   |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

---

## Vector Log Aggregator

```
+----------------------------------------------------------------------------------+
|                           VECTOR LOG AGGREGATOR                                  |
|                                                                                  |
|  Container stdout/stderr                                                         |
|        |                                                                         |
|        v                                                                         |
|   Docker Engine                                                                  |
|        |                                                                         |
|   Docker Socket (/var/run/docker.sock) -- read-only mount                       |
|        |                                                                         |
|        v                                                                         |
|   Vector (timberio/vector:0.43.1-alpine)                                        |
|        |                                                                         |
|   VRL Transform (enrich, parse JSON, extract level)                             |
|        |                                                                         |
|   +----+----+----+                                                               |
|   |              |                                                               |
|   v              v                                                               |
| route_platform  route_agents                                                     |
| (is_platform)   (is_agent)                                                       |
|   |              |                                                               |
|   v              v                                                               |
| /data/logs/     /data/logs/                                                      |
| platform-YYYY-  agents-YYYY-                                                     |
| MM-DD.json      MM-DD.json                                                       |
|   |              |                                                               |
|   +------+-------+                                                               |
|          |                                                                       |
|          v                                                                       |
| LogArchiveService (nightly APScheduler job)                                      |
|          |                                                                       |
|          v                                                                       |
| /data/archives/*.json.gz (compressed, with metadata sidecar)                    |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

**Container Naming Convention:**
- Platform: `trinity-*` (backend, frontend, redis, mcp-server, vector)
- Agents: `agent-*` (all Trinity-managed agent containers)

**Health Check:** `docker exec trinity-vector wget -q -O - http://127.0.0.1:8686/health` (the API is not published on the host)

---

## Related Documentation

| Topic | File |
|-------|------|
| Full Architecture | `docs/memory/architecture.md` |
| Vector Logging Feature | `docs/memory/feature-flows/vector-logging.md` |
| Process Engine | `docs/memory/feature-flows/process-engine/README.md` |
| MCP Server Types | `src/mcp-server/src/types.ts` |
| Database Schema | `src/backend/database.py`, `src/backend/db_models.py` |
