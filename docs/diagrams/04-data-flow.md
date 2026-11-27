# Trinity Data Flow Diagrams

> **Last Updated**: 2026-01-25

## What This Diagram Shows

This document illustrates the key data flows in the Trinity platform:

1. **User Chat Flow** - How messages flow from browser through backend to agent containers
2. **Agent-to-Agent Communication** - MCP-based collaboration between agents with permission checks
3. **Credential Flow** - Secure credential storage in Redis and injection into agent containers
4. **Scheduled Execution Flow** - How the dedicated scheduler service triggers automated agent tasks

---

## User Chat Flow

```
+---------------------------------------------------------------------------------+
|                         USER CHAT WITH AGENT                                    |
|                                                                                 |
|                                                                                 |
|   +----------+         +----------+         +----------+         +----------+   |
|   |  Browser |         | Frontend |         | Backend  |         |  Agent   |   |
|   |          |         |  Vue.js  |         | FastAPI  |         |Container |   |
|   +----+-----+         +----+-----+         +----+-----+         +----+-----+   |
|        |                    |                    |                    |         |
|        |  Type message      |                    |                    |         |
|        |------------------->|                    |                    |         |
|        |                    |                    |                    |         |
|        |                    |  POST /api/agents  |                    |         |
|        |                    |  /{name}/chat      |                    |         |
|        |                    |------------------->|                    |         |
|        |                    |                    |                    |         |
|        |                    |                    |  Acquire queue     |         |
|        |                    |                    |  lock (Redis)      |         |
|        |                    |                    |                    |         |
|        |                    |                    |  POST /api/chat    |         |
|        |                    |                    |------------------->|         |
|        |                    |                    |                    |         |
|        |                    |                    |         +----------+------+  |
|        |                    |                    |         |  Claude Code    |  |
|        |                    |                    |         |  --print        |  |
|        |                    |                    |         |  --output-format|  |
|        |                    |                    |         |  stream-json    |  |
|        |                    |                    |         +----------+------+  |
|        |                    |                    |                    |         |
|        |                    |                    |<-------------------|         |
|        |                    |                    |  Stream response   |         |
|        |                    |                    |                    |         |
|        |                    |                    |  Save to database  |         |
|        |                    |                    |  (chat_messages)   |         |
|        |                    |                    |                    |         |
|        |                    |                    |  Broadcast activity|         |
|        |                    |                    |  via WebSocket     |         |
|        |                    |                    |                    |         |
|        |                    |                    |  Release queue     |         |
|        |                    |                    |  lock              |         |
|        |                    |                    |                    |         |
|        |                    |<-------------------|                    |         |
|        |                    |  Response + stats  |                    |         |
|        |                    |                    |                    |         |
|        |<-------------------|                    |                    |         |
|        |  Display response  |                    |                    |         |
|        |                    |                    |                    |         |
|        |                    |                    |                    |         |
|   +----+-----+         +----+-----+         +----+-----+         +----+-----+   |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

## Agent-to-Agent Communication

```
+---------------------------------------------------------------------------------+
|                    AGENT-TO-AGENT COMMUNICATION                                 |
|                                                                                 |
|                                                                                 |
|   +--------------+     +--------------+     +--------------+     +------------+ |
|   |   Agent A    |     |  MCP Server  |     |   Backend    |     |  Agent B   | |
|   | (Orchestrator)|     |   FastMCP    |     |   FastAPI    |     |  (Worker)  | |
|   +------+-------+     +------+-------+     +------+-------+     +------+-----+ |
|          |                    |                    |                    |       |
|          |                    |                    |                    |       |
|          |  mcp__trinity__    |                    |                    |       |
|          |  chat_with_agent   |                    |                    |       |
|          |  (target="agent-b")|                    |                    |       |
|          |------------------->|                    |                    |       |
|          |                    |                    |                    |       |
|          |                    |  Extract agent     |                    |       |
|          |                    |  context from key  |                    |       |
|          |                    |                    |                    |       |
|          |                    |  Check permissions |                    |       |
|          |                    |  (agent_permissions|                    |       |
|          |                    |   table)           |                    |       |
|          |                    |------------------->|                    |       |
|          |                    |                    |                    |       |
|          |                    |<-------------------|                    |       |
|          |                    |  Permission OK     |                    |       |
|          |                    |                    |                    |       |
|          |                    |  POST /api/agents  |                    |       |
|          |                    |  /agent-b/chat     |                    |       |
|          |                    |  X-Source-Agent:   |                    |       |
|          |                    |  agent-a           |                    |       |
|          |                    |------------------->|                    |       |
|          |                    |                    |                    |       |
|          |                    |                    |  POST /api/chat    |       |
|          |                    |                    |------------------->|       |
|          |                    |                    |                    |       |
|          |                    |                    |<-------------------|       |
|          |                    |                    |  Response          |       |
|          |                    |                    |                    |       |
|          |                    |                    |  Broadcast         |       |
|          |                    |                    |  collaboration     |       |
|          |                    |                    |  event (WebSocket) |       |
|          |                    |                    |                    |       |
|          |                    |                    |  Log activity      |       |
|          |                    |                    |  (agent_activities)|       |
|          |                    |                    |                    |       |
|          |                    |<-------------------|                    |       |
|          |<-------------------|                    |                    |       |
|          |  Response from B   |                    |                    |       |
|          |                    |                    |                    |       |
|   +------+-------+     +------+-------+     +------+-------+     +------+-----+ |
|                                                                                 |
|                                                                                 |
|   Permission Rules:                                                             |
|   -----------------                                                             |
|   - Same owner: Allowed by default                                              |
|   - Explicit permission: Check agent_permissions table                          |
|   - Shared agent: Allowed if target is shared with source owner                 |
|   - System agent: Bypasses all permission checks                                |
|                                                                                 |
+---------------------------------------------------------------------------------+
```

## Credential Flow

```
+---------------------------------------------------------------------------------+
|                         CREDENTIAL MANAGEMENT FLOW                              |
|                                                                                 |
|                                                                                 |
|   +----------------------------------------------------------------------------+|
|   |                        CREDENTIAL STORAGE                                  ||
|   |                                                                            ||
|   |   User Input                   OAuth Flow                                  ||
|   |   (KEY=VALUE)                  (Google, Slack, GitHub, Notion)             ||
|   |       |                              |                                     ||
|   |       |                              |                                     ||
|   |       v                              v                                     ||
|   |   +-----------------------------------------------------------------+     ||
|   |   |                       REDIS                                      |     ||
|   |   |                                                                  |     ||
|   |   |   credentials:{id}:metadata = HASH {name, service, type, ...}    |     ||
|   |   |   credentials:{id}:secret   = STRING (JSON blob)                 |     ||
|   |   |   user:{user_id}:credentials = SET of cred_ids                   |     ||
|   |   |   agent:{agent_name}:credentials = SET of cred_ids (assigned)    |     ||
|   |   |                                                                  |     ||
|   |   +-----------------------------------------------------------------+     ||
|   |                                                                            ||
|   +----------------------------------------------------------------------------+|
|                                                                                 |
|                                      |                                          |
|                                      | Agent Creation                           |
|                                      | or Hot-Reload                            |
|                                      v                                          |
|                                                                                 |
|   +----------------------------------------------------------------------------+|
|   |                        CREDENTIAL INJECTION                                ||
|   |                                                                            ||
|   |                                                                            ||
|   |   +---------------------+      +-------------------------------------+     ||
|   |   | .mcp.json.template  |      |              .mcp.json              |     ||
|   |   |                     |      |                                     |     ||
|   |   | {                   |      | {                                   |     ||
|   |   |   "mcpServers": {   | ---> |   "mcpServers": {                   |     ||
|   |   |     "google": {     |      |     "google": {                     |     ||
|   |   |       "env": {      |      |       "env": {                      |     ||
|   |   |         "TOKEN":    |      |         "TOKEN":                    |     ||
|   |   |         "${GOOGLE_  |      |         "ya29.actual-token..."      |     ||
|   |   |          TOKEN}"    |      |       }                             |     ||
|   |   |       }             |      |     }                               |     ||
|   |   |     }               |      |   }                                 |     ||
|   |   |   }                 |      | }                                   |     ||
|   |   | }                   |      |                                     |     ||
|   |   +---------------------+      +-------------------------------------+     ||
|   |                                                                            ||
|   |                                                                            ||
|   |   Credentials also written to .env file:                                   ||
|   |                                                                            ||
|   |   ANTHROPIC_API_KEY=sk-ant-...                                             ||
|   |   GOOGLE_TOKEN=ya29.actual-token...                                        ||
|   |   TRINITY_MCP_API_KEY=trinity_mcp_...                                      ||
|   |                                                                            ||
|   +----------------------------------------------------------------------------+|
|                                                                                 |
+---------------------------------------------------------------------------------+
```

## Scheduled Execution Flow

The scheduler runs as a **dedicated standalone service** (not embedded in backend) to prevent duplicate executions when running multiple API workers.

```
+---------------------------------------------------------------------------------+
|                      SCHEDULED EXECUTION FLOW                                   |
|                                                                                 |
|                                                                                 |
|   +----------------------------------------------------------------------------+|
|   |                     DEDICATED SCHEDULER SERVICE                            ||
|   |                      (src/scheduler/main.py)                               ||
|   |                                                                            ||
|   |   +-------------------------------------------------------------------+   ||
|   |   |  SQLite: agent_schedules table                                     |   ||
|   |   |                                                                    |   ||
|   |   |  id: sched-123                                                     |   ||
|   |   |  agent_name: research-agent                                        |   ||
|   |   |  cron_expression: "0 9 * * *"  (9 AM daily)                        |   ||
|   |   |  message: "Generate daily report"                                  |   ||
|   |   |  enabled: true                                                     |   ||
|   |   |  timezone: "America/New_York"                                      |   ||
|   |   +-------------------------------------------------------------------+   ||
|   |                                                                            ||
|   |   APScheduler (AsyncIOScheduler + MemoryJobStore)                          ||
|   |   - Loads enabled schedules from SQLite on startup                         ||
|   |   - Creates CronTrigger jobs for each schedule                             ||
|   |                                                                            ||
|   +----------------------------------------------------------------------------+|
|                                                                                 |
|                                      |                                          |
|                                      | Cron trigger fires                       |
|                                      v                                          |
|                                                                                 |
|   +----------------------------------------------------------------------------+|
|   |                     DISTRIBUTED LOCKING (Redis)                            ||
|   |                                                                            ||
|   |   Key Pattern: scheduler:lock:schedule:{schedule_id}                       ||
|   |                                                                            ||
|   |   1. Try acquire lock: SET key token NX EX 600                             ||
|   |      - NX: Only if key does not exist (prevents duplicate execution)       ||
|   |      - EX 600: 10-minute expiration (auto-release on failure)              ||
|   |                                                                            ||
|   |   2. If lock acquired -> proceed to execution                              ||
|   |      If lock exists -> skip (another instance is executing)                ||
|   |                                                                            ||
|   |   3. Auto-renewal thread extends lock every 5 minutes                      ||
|   |                                                                            ||
|   |   4. On completion: Lua script releases lock (verify token ownership)      ||
|   |                                                                            ||
|   +----------------------------------------------------------------------------+|
|                                                                                 |
|                                      |                                          |
|                                      | Lock acquired                            |
|                                      v                                          |
|                                                                                 |
|   +----------------------------------------------------------------------------+|
|   |                       EXECUTION FLOW                                       ||
|   |                                                                            ||
|   |   1. Check if agent has autonomy enabled                                   ||
|   |      - If disabled, skip execution                                         ||
|   |                                                                            ||
|   |   2. Create execution record                                               ||
|   |      - INSERT INTO schedule_executions                                     ||
|   |                                                                            ||
|   |   3. No event from the scheduler                                           ||
|   |      - The backend broadcasts the run as agent_activity (chat_start)       ||
|   |        at its start and at its end                                         ||
|   |                                                                            ||
|   |   4. Send task to agent via HTTP                                           ||
|   |      - POST http://agent-{name}:8000/api/task                              ||
|   |      - Uses AgentClient with 15-minute timeout                             ||
|   |                                                                            ||
|   |   5. Parse response metrics                                                ||
|   |      - context_used, context_max, cost_usd                                 ||
|   |      - execution_log (raw Claude Code stream-json format)                  ||
|   |                                                                            ||
|   |   6. Update execution record                                               ||
|   |      - status: success/failed                                              ||
|   |      - response, error, duration, cost                                     ||
|   |                                                                            ||
|   |   7. Update schedule run times                                             ||
|   |      - last_run_at, next_run_at                                            ||
|   |                                                                            ||
|   |   8. Publish "completed" event to Redis                                    ||
|   |                                                                            ||
|   |   9. Release lock                                                          ||
|   |                                                                            ||
|   +----------------------------------------------------------------------------+|
|                                                                                 |
+---------------------------------------------------------------------------------+
```

### Execution Queue Redis Key Patterns

| Key Pattern | Type | Purpose | TTL |
|-------------|------|---------|-----|
| `agent:running:{name}` | String | Currently executing request (JSON) | 600s |
| `agent:queue:{name}` | List | Waiting requests (FIFO via LPUSH/RPOP) | None |
| `scheduler:lock:schedule:{id}` | String | Distributed lock for schedule execution | 600s |

### Execution Queue Operations

| Operation | Pattern | Purpose |
|-----------|---------|---------|
| Acquire slot | `SET key value NX EX ttl` | Atomic acquisition prevents race conditions |
| Complete/Next | Lua script (RPOP + SET) | Atomic pop-and-set prevents lost queue entries |
| List busy agents | `SCAN` (not `KEYS`) | Non-blocking iteration on large datasets |

---

## Sources

These diagrams are derived from the following feature flow documentation:

- **User Chat Flow**: [execution-queue.md](/docs/memory/feature-flows/execution-queue.md) - Lines 3-57 (User Chat Flow)
- **Agent-to-Agent Communication**: [agent-to-agent-collaboration.md](/docs/memory/feature-flows/agent-to-agent-collaboration.md) - Full flow architecture
- **Credential Flow**: [credential-injection.md](/docs/memory/feature-flows/credential-injection.md) - Flows 1-8 covering storage, injection, and hot-reload
- **Scheduled Execution Flow**:
  - [scheduler-service.md](/docs/memory/feature-flows/scheduler-service.md) - Dedicated scheduler service architecture
  - [scheduling.md](/docs/memory/feature-flows/scheduling.md) - Schedule CRUD and execution tracking
  - [execution-queue.md](/docs/memory/feature-flows/execution-queue.md) - Queue patterns and Redis key structure

---

## Related Documentation

- [System Architecture Overview](/docs/diagrams/01-system-overview.md)
- [Authentication Flow](/docs/diagrams/02-authentication-flow.md)
- [Agent Lifecycle](/docs/diagrams/03-agent-lifecycle.md)
