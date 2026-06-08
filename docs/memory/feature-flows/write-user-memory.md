# Feature: write_user_memory MCP Tool (MEM-001, #888, #895)

## Overview
Agents can persist per-user memory blobs scoped to a single (agent, user_email) pair. This replaces the unsafe pattern of writing to `~/.claude/projects/memory/`, which is shared across all users of an agent and leaks PII between sessions.

**#895 update**: storage was split into two named sections so the agent-deliberate writes from this tool don't clobber the every-5-message conversation summarizer (and vice versa). The MCP wire format is unchanged — the tool still accepts a single `memory_text` field — but the backend now routes that value to the `agent_notes` section of the JSON blob stored on disk. Channel sessions (Slack/Telegram/WhatsApp) now also inject this memory into the agent's system prompt, gated on `verified_email and not is_group`.

## User Story
As an agent serving multiple users via public link / Slack / Telegram / WhatsApp, I want to remember facts about each individual user (name, preferences, timezone) so that future sessions are personalized — without contaminating any other user's context.

## The PII Leak it Fixed
Before #888, agents that needed to remember user-specific facts had no safe write surface. The only available path was writing to the agent filesystem (`~/.claude/projects/memory/` or similar), which is a single shared namespace across all users of that agent. Writing a user's email, name, or preferences there made it visible to every other user that same agent served.

The fix is a server-side gated write: the agent never supplies a user email. The backend resolves it from the execution record, preventing an agent from writing memory for an arbitrary user.

## Entry Points
- **MCP Tool**: `write_user_memory` in `src/mcp-server/src/tools/memory.ts:30`
- **API**: `POST /api/agents/{agent_name}/user-memory`

## MCP Tool Layer

### Tool Definition
- `src/mcp-server/src/tools/memory.ts:30` — `writeUserMemory` tool
- Registered in `src/mcp-server/src/server.ts:217` via `createMemoryTools(client, requireApiKey)`

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `execution_id` | yes | Current execution ID from the system prompt "Execution Context" block |
| `memory_text` | yes | Complete updated memory blob (max 8000 chars) — replaces previous content entirely |
| `agent_name` | no | Override; defaults to the `agentName` in the agent-scoped MCP key's auth context |

### Agent Name Resolution
`src/mcp-server/src/tools/memory.ts:85-99` — resolves agent name in priority order:
1. Explicit `agent_name` parameter
2. `authContext.agentName` (from agent-scoped MCP key, scope `"agent"`)
3. Error if neither is available

### Client Call
`src/mcp-server/src/client.ts:1087` — `apiClient.writeUserMemory(resolvedAgent, { execution_id, memory_text })`
- Makes `POST /api/agents/{agent_name}/user-memory` with the calling user's MCP API key as Bearer token

## Backend Layer

### Endpoint
- `src/backend/routers/public_memory.py:41` — `POST /api/agents/{agent_name}/user-memory`
- Router prefix `/api/agents`, mounted in `src/backend/main.py:830`

### Business Logic
`src/backend/routers/public_memory.py:42-93`

1. **Authorization check** — `db.can_user_access_agent(current_user.username, agent_name)`: calling user (resolved from MCP API key) must have access to the agent. Returns 403 if not.
2. **Execution lookup** — `db.get_execution(body.execution_id)`: the execution must exist. Returns 404 if not.
3. **Execution ownership check** — `execution.agent_name != agent_name`: the execution must belong to the agent named in the path. Returns 403 if mismatch.
4. **Channel gate** — `triggered_by` must be one of `{"public", "slack", "telegram", "whatsapp"}`. Scheduled tasks and agent-to-agent executions are rejected with 422.
5. **Email extraction** — `execution.source_user_email` is read directly from the execution record. Agent never supplies this value. Returns 422 if missing or malformed.
6. **Section write (#895)** — `db.update_public_user_memory_agent_notes(agent_name, user_email, memory_text)`. The helper reads the existing JSON blob, replaces only the `agent_notes` key, and writes back — `conversation_summary` (written by the background summarizer) is left untouched. The row is created on demand inside the helper.

### Database Operations
- **Table**: `public_user_memory` (schema at `src/backend/db/schema.py:515`) — unchanged by #895
- **Unique constraint**: `(agent_name, user_email)` — one blob per user per agent
- **Storage shape (#895)**: `memory_text TEXT` now holds a JSON object with two named keys (no schema migration required):
  ```json
  { "agent_notes": "...", "conversation_summary": "..." }
  ```
  `agent_notes` is written by this tool. `conversation_summary` is written by the background summarizer (`services/platform_prompt_service.summarize_user_memory_background`). Legacy plaintext rows (written before #895) are transparently surfaced as `conversation_summary` on read — see `db/public_links._parse_memory_blob`.
- **Read** (`db.get_or_create_public_user_memory`): `SELECT` by `(agent_name, user_email)`; `INSERT` if not found. Returns the parsed dict `{id, agent_name, user_email, agent_notes, conversation_summary, message_count, created_at, updated_at}` — `src/backend/db/public_links.py:get_or_create_user_memory`
- **Write — agent_notes only** (`db.update_public_user_memory_agent_notes`): read-modify-write that replaces only the `agent_notes` key — `src/backend/db/public_links.py:update_user_memory_agent_notes`
- **Write — conversation_summary only** (`db.update_public_user_memory_conversation_summary`): used by the background summarizer; never invoked from this tool — `src/backend/db/public_links.py:update_user_memory_conversation_summary`
- **Index**: `idx_public_user_memory_lookup ON public_user_memory(agent_name, user_email)` — `src/backend/db/schema.py:1166`

### Table Schema
```sql
CREATE TABLE IF NOT EXISTS public_user_memory (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    user_email TEXT NOT NULL,
    memory_text TEXT NOT NULL DEFAULT '',  -- JSON blob since #895
    message_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(agent_name, user_email)
);
```

### Response
```json
{ "success": true, "agent_name": "my-agent", "user_email": "user@example.com" }
```

## Auth / Access Control Summary

| Check | Mechanism | Failure |
|-------|-----------|---------|
| Caller has access to agent | `db.can_user_access_agent` | 403 |
| Execution exists | `db.get_execution(execution_id)` | 404 |
| Execution belongs to this agent | `execution.agent_name == agent_name` | 403 |
| Execution was user-facing | `triggered_by in {public,slack,telegram,whatsapp}` | 422 |
| Verified user email present | `execution.source_user_email` non-null + regex | 422 |

The agent never provides the user's email — it supplies only `execution_id`. The backend resolves the email from `schedule_executions.source_user_email`, which is written at execution creation time by the channel adapters (public, Slack, Telegram, WhatsApp) from their respective verified-email primitives.

## Side Effects
- None. No WebSocket broadcast. No audit log entry (informational `logger.info` only).

## Error Handling
| Error Case | HTTP Status | Detail |
|------------|-------------|--------|
| Caller cannot access agent | 403 | Not authorized |
| Execution not found | 404 | Execution not found |
| Execution belongs to different agent | 403 | Execution does not belong to this agent |
| Triggered by schedule / agent / MCP | 422 | write_user_memory is only available during user-facing sessions (...) |
| No verified email on execution | 422 | No verified user email associated with this execution |

## Key Files
| File | Role |
|------|------|
| `src/mcp-server/src/tools/memory.ts` | MCP tool definition and execute handler |
| `src/mcp-server/src/client.ts:1087` | `writeUserMemory()` HTTP client method |
| `src/mcp-server/src/server.ts:217` | Tool registration |
| `src/backend/routers/public_memory.py` | FastAPI endpoint + all validation logic |
| `src/backend/db/public_links.py:509` | `get_or_create_public_user_memory` + `update_public_user_memory` |
| `src/backend/db/schema.py:515` | `public_user_memory` table DDL |
| `src/backend/main.py:95,830` | Router import and mount |

## Related Flows
- [public-agent-links.md](public-agent-links.md) — web public chat sessions (read path that injects this memory into the system prompt; same split-storage rules)
- [unified-channel-access-control.md](unified-channel-access-control.md) — Slack/Telegram/WhatsApp channel sessions that also inject memory (#895), gated on `verified_email and not is_group`
- [execution-context-injection.md](execution-context-injection.md) — how `execution_id` is surfaced in the agent system prompt
