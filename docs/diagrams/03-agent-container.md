# Trinity Agent Container Architecture

> **Last Updated**: 2026-01-25

## What This Diagram Shows

This document details the internal architecture of a Trinity agent container - the isolated execution environment where Claude Code (or Gemini CLI) runs. Each agent is a Docker container built from the `trinity-agent-base` image with:

- Multi-runtime support (Python, Node.js, Go)
- Internal FastAPI server for agent operations
- MCP server integrations
- Persistent workspace with credential injection
- Security constraints and network isolation

## Sources

| Component | Source File | Lines |
|-----------|-------------|-------|
| Base Image Dockerfile | `docker/base-image/Dockerfile` | Full file |
| Agent Server Main | `docker/base-image/agent_server/main.py` | 1-88 |
| API Routers | `docker/base-image/agent_server/routers/` | Directory |
| Startup Script | `docker/base-image/startup.sh` | Full file |
| Security Constants | `src/backend/services/agent_service/lifecycle.py` | 32-50 |
| Container Creation | `src/backend/services/agent_service/crud.py` | 468-500 |
| Skills Injection | `src/backend/services/skill_service.py` | 188-277 |

---

## Agent Container Anatomy

```
+----------------------------------------------------------------------------------+
|                         AGENT CONTAINER                                          |
|                      (trinity-agent-base image)                                  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                          RUNTIMES                                          |  |
|  |                                                                            |  |
|  |   +---------------+  +---------------+  +---------------+                  |  |
|  |   |  Python 3.11  |  |  Node.js 20   |  |   Go 1.23     |                  |  |
|  |   +---------------+  +---------------+  +---------------+                  |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                      AGENT SERVER (FastAPI :8000)                          |  |
|  |                                                                            |  |
|  |   # Info & Health (routers/info.py)                                        |  |
|  |   GET  /                      Root endpoint - API info                     |  |
|  |   GET  /health                Health check                                 |  |
|  |   GET  /api/agent/info        Agent information                            |  |
|  |   GET  /api/template/info     Template metadata from template.yaml         |  |
|  |   GET  /api/metrics           Custom metrics from template                 |  |
|  |                                                                            |  |
|  |   # Chat & Execution (routers/chat.py)                                     |  |
|  |   POST /api/chat              Execute message via runtime                  |  |
|  |   POST /api/task              Stateless parallel execution (no context)    |  |
|  |   GET  /api/chat/history      Get conversation history                     |  |
|  |   GET  /api/chat/session      Get context window stats                     |  |
|  |   GET  /api/model             Get current model                            |  |
|  |   PUT  /api/model             Set model for session                        |  |
|  |   DELETE /api/chat/history    Clear conversation and reset session         |  |
|  |                                                                            |  |
|  |   # Execution Management (routers/chat.py)                                 |  |
|  |   POST /api/executions/{id}/terminate  Terminate running execution         |  |
|  |   GET  /api/executions/running         List running executions             |  |
|  |   GET  /api/executions/{id}/status     Get execution status                |  |
|  |   GET  /api/executions/{id}/stream     SSE stream execution log            |  |
|  |                                                                            |  |
|  |   # File Browser (routers/files.py)                                        |  |
|  |   GET    /api/files           List workspace files (tree structure)        |  |
|  |   GET    /api/files/download  Download file content (max 100MB)            |  |
|  |   GET    /api/files/preview   Preview with MIME type                       |  |
|  |   PUT    /api/files           Update/create file                           |  |
|  |   DELETE /api/files           Delete file/directory                        |  |
|  |                                                                            |  |
|  |   # Credentials (routers/credentials.py)                                   |  |
|  |   POST /api/credentials/inject   Write credential files (validated)        |  |
|  |   GET  /api/credentials/status   Get credential file status                |  |
|  |                                                                            |  |
|  |   # Trinity Injection (routers/trinity.py)                                 |  |
|  |   GET  /api/trinity/status    Check Trinity injection status               |  |
|  |   POST /api/trinity/inject    Inject Trinity meta-prompt                   |  |
|  |   POST /api/trinity/reset     Reset Trinity injection                      |  |
|  |                                                                            |  |
|  |   # Git Sync (routers/git.py)                                              |  |
|  |   GET  /api/git/status        Git repository status                        |  |
|  |   POST /api/git/commit        Create commit                                |  |
|  |   POST /api/git/push          Push to remote                               |  |
|  |   POST /api/git/pull          Pull from remote                             |  |
|  |                                                                            |  |
|  |   # Dashboard (routers/dashboard.py)                                       |  |
|  |   GET  /api/dashboard         Dashboard data aggregate                     |  |
|  |                                                                            |  |
|  |   # WebSocket                                                              |  |
|  |   WS  /ws/chat                Streaming chat WebSocket                     |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                         RUNTIME CLI                                        |  |
|  |                                                                            |  |
|  |   +----------------------------------------------------------------------+ |  |
|  |   |  Claude Code (claude-code npm package)                               | |  |
|  |   |  - AI-powered coding assistant                                       | |  |
|  |   |  - File operations (read, write, edit)                               | |  |
|  |   |  - Bash command execution                                            | |  |
|  |   |  - Web search and fetch                                              | |  |
|  |   |  - MCP tool integration                                              | |  |
|  |   |  - Headless mode for automation                                      | |  |
|  |   +----------------------------------------------------------------------+ |  |
|  |                                                                            |  |
|  |   +----------------------------------------------------------------------+ |  |
|  |   |  Gemini CLI (@google/gemini-cli npm package)                         | |  |
|  |   |  - Alternative runtime for Gemini models                             | |  |
|  |   |  - Multi-modal capabilities                                          | |  |
|  |   |  - 1M+ context window support                                        | |  |
|  |   +----------------------------------------------------------------------+ |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                      FILE SYSTEM (/home/developer/)                        |  |
|  |                                                                            |  |
|  |   workspace/                   Agent's working directory (from template)   |  |
|  |   +-- CLAUDE.md                Agent instructions + Trinity injection      |  |
|  |   +-- template.yaml            Agent metadata and configuration            |  |
|  |   +-- .claude/                 Claude Code configuration                   |  |
|  |   |   +-- commands/            Slash commands                              |  |
|  |   |   +-- skills/              Symlinks to assigned skills                 |  |
|  |   |   |   +-- {name} -> ~/.claude/skills-library/{name}                    |  |
|  |   |   +-- skills-library/      Read-only mount of skills library (ro)      |  |
|  |   |       +-- {skill-name}/                                                |  |
|  |   |           +-- SKILL.md     Skill instructions                          |  |
|  |   +-- [project files]          Template-specific files                     |  |
|  |                                                                            |  |
|  |   content/                     Generated assets (gitignored)               |  |
|  |   +-- videos/                                                              |  |
|  |   +-- audio/                                                               |  |
|  |   +-- images/                                                              |  |
|  |   +-- exports/                                                             |  |
|  |                                                                            |  |
|  |   shared-out/                  Exposed shared folder (if enabled)          |  |
|  |   shared-in/                   Mounted folders from other agents           |  |
|  |   +-- {agent-name}/            Per-agent mount points                      |  |
|  |                                                                            |  |
|  |   .env                         Credentials (KEY=VALUE format)              |  |
|  |   .mcp.json                    Generated MCP configuration                 |  |
|  |   .mcp.json.template           Template with ${VAR} placeholders           |  |
|  |   .trinity/                    Trinity platform files                      |  |
|  |   +-- prompt.md                Trinity meta-prompt                         |  |
|  |   +-- setup.sh                 Persistent package installation script      |  |
|  |   .trinity-initialized         Marker for first-time initialization        |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                         MCP SERVERS                                        |  |
|  |                                                                            |  |
|  |   +-------------+  +-------------+  +-------------+  +-------------+       |  |
|  |   |  Trinity    |  |  Google     |  |   Slack     |  |   GitHub    |       |  |
|  |   |    MCP      |  | Workspace   |  |    MCP      |  |    MCP      |       |  |
|  |   | (injected)  |  |    MCP      |  |             |  |             |       |  |
|  |   +-------------+  +-------------+  +-------------+  +-------------+       |  |
|  |                                                                            |  |
|  |   Configured via .mcp.json with credentials from .env                      |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
|  +----------------------------------------------------------------------------+  |
|  |                      SECURITY CONSTRAINTS                                  |  |
|  |                                                                            |  |
|  |   RESTRICTED_CAPABILITIES (Default):                                       |  |
|  |   - NET_BIND_SERVICE         Bind to ports < 1024                          |  |
|  |   - SETGID, SETUID           Change user/group (for su/sudo)               |  |
|  |   - CHOWN                    Change file ownership                         |  |
|  |   - SYS_CHROOT               Use chroot                                    |  |
|  |   - AUDIT_WRITE              Write to audit log                            |  |
|  |                                                                            |  |
|  |   FULL_CAPABILITIES (When full_capabilities=true):                         |  |
|  |   - All RESTRICTED_CAPABILITIES plus:                                      |  |
|  |   - DAC_OVERRIDE             Bypass file permission checks (apt)           |  |
|  |   - FOWNER                   Bypass permission checks on file owner        |  |
|  |   - FSETID                   Don't clear setuid/setgid bits                |  |
|  |   - KILL                     Send signals to processes                     |  |
|  |   - MKNOD                    Create special files                          |  |
|  |   - NET_RAW                  Use raw sockets (ping, etc.)                  |  |
|  |   - SYS_PTRACE               Trace processes (debugging)                   |  |
|  |                                                                            |  |
|  |   Always Applied:                                                          |  |
|  |   - User: developer (UID 1000) - non-root                                  |  |
|  |   - CAP_DROP: ALL (then add back needed capabilities)                      |  |
|  |   - security_opt: apparmor:docker-default                                  |  |
|  |   - tmpfs /tmp: noexec,nosuid,size=100m                                    |  |
|  |   - Isolated network (no external UI ports)                                |  |
|  |                                                                            |  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

## Agent Lifecycle

```
+----------------------------------------------------------------------------------+
|                          AGENT LIFECYCLE                                         |
|                                                                                  |
|                                                                                  |
|   CREATE                                                                         |
|   ------                                                                         |
|                                                                                  |
|   +------------+    +---------------+    +---------------+    +---------------+  |
|   | Template   |--->| Clone/Copy    |--->| Create        |--->| Start         |  |
|   | Selected   |    | Workspace     |    | Container     |    | Container     |  |
|   +------------+    +---------------+    +---------------+    +---------------+  |
|                                                |                    |            |
|                                                |                    v            |
|                                                |           +---------------+     |
|                                                |           | Inject        |     |
|                                                |           | Trinity       |     |
|                                                |           | Meta-Prompt   |     |
|                                                |           +---------------+     |
|                                                |                    |            |
|                                                |                    v            |
|                                                |           +---------------+     |
|                                                |           | Inject        |     |
|                                                |           | Skills        |     |
|                                                |           | (symlinks)    |     |
|                                                |           +---------------+     |
|                                                |                    |            |
|                                                v                    v            |
|                                        +----------------------------------+      |
|                                        |         AGENT RUNNING            |      |
|                                        |                                  |      |
|                                        |  - Runtime CLI ready             |      |
|                                        |  - MCP servers connected         |      |
|                                        |  - API endpoints active          |      |
|                                        |  - Credentials loaded            |      |
|                                        |  - Skills linked                 |      |
|                                        +----------------------------------+      |
|                                                        |                         |
|                         +-----------------------------+|+------------------+     |
|                         |                              |                   |     |
|                         v                              v                   v     |
|                  +---------------+            +---------------+  +-------------+ |
|                  |    STOP       |            |   RESTART     |  |   DELETE    | |
|                  |               |            |               |  |             | |
|                  | Container     |            | Stop + Start  |  | Stop +      | |
|                  | preserved     |            | Workspace     |  | Remove      | |
|                  | Data intact   |            | preserved     |  | container   | |
|                  |               |            |               |  | + volumes   | |
|                  +---------------+            +---------------+  +-------------+ |
|                         |                                                        |
|                         |                                                        |
|                         v                                                        |
|                  +---------------+                                               |
|                  |    START      |                                               |
|                  |               |                                               |
|                  | Resume from   |                                               |
|                  | saved state   |                                               |
|                  +---------------+                                               |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

## Template Structure

```
+----------------------------------------------------------------------------------+
|                         AGENT TEMPLATE STRUCTURE                                 |
|                                                                                  |
|   config/agent-templates/{template-name}/                                        |
|   +-- template.yaml              Required: Agent metadata                        |
|   +-- CLAUDE.md                  Required: Agent instructions                    |
|   +-- .mcp.json.template         Optional: MCP config with ${VAR} placeholders   |
|   +-- .claude/                   Optional: Claude Code configuration             |
|   |   +-- commands/              Optional: Slash commands                        |
|   |       +-- {command}.md                                                       |
|   +-- [other files]              Project-specific files                          |
|                                                                                  |
|   +--------------------------------------------------------------------------+   |
|   |                        template.yaml                                      |   |
|   |                                                                           |   |
|   |   name: research-agent                                                    |   |
|   |   display_name: Research Agent                                            |   |
|   |   description: Autonomous research and analysis                           |   |
|   |   version: "1.0.0"                                                        |   |
|   |   author: Trinity Team                                                    |   |
|   |                                                                           |   |
|   |   capabilities:                                                           |   |
|   |     - web-research                                                        |   |
|   |     - document-analysis                                                   |   |
|   |     - report-generation                                                   |   |
|   |                                                                           |   |
|   |   resources:                                                              |   |
|   |     cpu: "2"                                                              |   |
|   |     memory: "4g"                                                          |   |
|   |                                                                           |   |
|   |   credentials:                                                            |   |
|   |     - name: OPENAI_API_KEY                                                |   |
|   |       description: OpenAI API key for embeddings                          |   |
|   |       required: false                                                     |   |
|   |                                                                           |   |
|   |   skills:                                                                 |   |
|   |     - verification                                                        |   |
|   |     - systematic-debugging                                                |   |
|   |                                                                           |   |
|   +--------------------------------------------------------------------------+   |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

## Skills Architecture

```
+----------------------------------------------------------------------------------+
|                         SKILLS INJECTION                                         |
|                                                                                  |
|   Skills Library (Backend)                   Agent Container                     |
|   +-----------------------+                  +---------------------------+       |
|   | /skills-library/      |    Read-Only     | ~/.claude/skills-library/ |       |
|   |   verification/       |    Mount         |   verification/           |       |
|   |     SKILL.md         |   ==========>    |     SKILL.md              |       |
|   |   tdd/               |                   |   tdd/                    |       |
|   |     SKILL.md         |                   |     SKILL.md              |       |
|   |   ...                |                   |   ...                     |       |
|   +-----------------------+                  +---------------------------+       |
|                                                         |                        |
|                                               Symlinks  |                        |
|                                               Created   v                        |
|                                              +---------------------------+       |
|                                              | ~/.claude/skills/         |       |
|                                              |   verification -> ../skills-library/verification |
|                                              |   tdd -> ../skills-library/tdd                    |
|                                              +---------------------------+       |
|                                                                                  |
|   Skills are assigned per-agent in database and symlinked on container start    |
|   Path format: ~/.claude/skills/{skill-name}/SKILL.md                           |
|                                                                                  |
+----------------------------------------------------------------------------------+
```

## Related Documentation

- [Agent Lifecycle](../memory/feature-flows/agent-lifecycle.md) - Full lifecycle management details
- [Agent Terminal](../memory/feature-flows/agent-terminal.md) - Web terminal implementation
- [Container Capabilities](../memory/feature-flows/container-capabilities.md) - Security configuration
- [Skills Management](../memory/requirements.md#skills-management) - Skills system requirements

---

**Status**: Current as of 2026-01-25
