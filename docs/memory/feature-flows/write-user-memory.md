# Feature: write_user_memory MCP Tool (MEM-001, #888, #895)

## Overview
Agents can persist per-user memory blobs scoped to a single (agent, user_email) pair. This replaces the unsafe pattern of writing to Claude Code's auto-memory (under `~/.claude/projects/`) or the repo's `memory/` directory — both are shared across all users of an agent and leak PII between sessions.

**#895 update**: storage was split into two named sections so the agent-deliberate writes from this tool don't clobber the every-5-message conversation summarizer (and vice versa). The MCP wire format is unchanged — the tool still accepts a single `memory_text` field — but the backend now routes that value to the `agent_notes` section of the JSON blob stored on disk. Channel sessions (Slack/Telegram/WhatsApp) now also inject this memory into the agent's system prompt, gated on `verified_email and not is_group`.

## User Story
As an agent serving multiple users via public link / Slack / Telegram / WhatsApp, I want to remember facts about each individual user (name, preferences, timezone) so that future sessions are personalized — without contaminating any other user's context.

## The PII Leak it Fixed
Before #888, agents that needed to remember user-specific facts had no safe write surface. The only available path was writing to the agent filesystem (Claude Code's auto-memory under `~/.claude/projects/`, or similar), which is a single shared namespace across all users of that agent. Writing a user's email, name, or preferences there made it visible to every other user that same agent served.

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

1. **Authorization check** — `assert_agent_access(current_user, agent_name)`, plus the agent-key self-gate: an agent-scoped key writes only its own agent's memory. Returns 403 if not.
2. **Execution lookup** — `db.get_execution(body.execution_id)`: the execution must exist. Returns 404 if not.
3. **Execution ownership check** — `execution.agent_name != agent_name`: the execution must belong to the agent named in the path. Returns 403 if mismatch.
4. **Whose memory** — never sent by the agent:
   - a scheduled run addressed to one person (ent#637) → that seat, read off the row (`schedule_seat_memory.seat_for_execution`);
   - a Slack / Telegram / WhatsApp turn → `source_channel_client`, ent#549's stamp for the verified speaker of a **one-to-one** conversation (ent#419 AC 4). A group turn carries the unlocker's address in `source_user_email`, is never shown that memory and has other members' text in its prompt, so it is refused with `422 memory_not_one_to_one`;
   - a `public` turn (public link, Workspace) → `execution.source_user_email`;
   - anything else (a schedule that names no one, agent-to-agent, MCP) → 422.
5. **Email check** — the resolved address must look like an email. Returns 422 if missing or malformed.
6. **Normalise + screen (ent#419)** — `user_memory_guard.prepare_write(memory_text, previous=db.get_public_user_memory_agent_notes(...))`:
   - strips the platform's own presentation from a copy-back, structurally: inside a copied envelope only the agent-notes section is kept (the conversation summary and the seat decisions are dropped, never merged into the notes); the `│ ` margin, the block's headings, envelope lines, the framing, the reminder, their blank spacing and a trailing `---` go — never a `>`, since "> 100 staff" is a fact;
   - screens the result: `memory_prompt_marker`, `memory_instruction`, `memory_tool_directive`, `memory_link_in_instruction`. A hit is `422 {code, message, line}` (the line of the text the agent sent) and **nothing is stored** — no memory change, no history row. A hit the stored notes already produced (same rule, same matched text) is not counted again, so text from before the screen never locks the memory; old lines reordered, re-contextualised or respelled into a new hit, and a phrase completed by a new line, are judged afresh. The refusal is logged with the rule and the line, never the text.
7. **Write + history (#895, ent#637)** — `db.write_public_user_memory_agent_notes(agent_name, user_email, stored_text, execution_id=…, triggered_by=…, schedule_id=…)`: in one transaction, replaces only the `agent_notes` key of the JSON blob (the summarizer's `conversation_summary` is untouched) and records a `public_user_memory_writes` row (previous + new notes, which run). The row is created on demand. This is the only `agent_notes` writer in `src/backend` (AST-pinned).

### Database Operations
- **Table**: `public_user_memory` (schema at `src/backend/db/schema.py:515`) — unchanged by #895
- **Unique constraint**: `(agent_name, user_email)` — one blob per user per agent
- **Storage shape (#895)**: `memory_text TEXT` now holds a JSON object with two named keys (no schema migration required):
  ```json
  { "agent_notes": "...", "conversation_summary": "..." }
  ```
  `agent_notes` is written by this tool. `conversation_summary` is written by the background summarizer (`services/platform_prompt_service.summarize_user_memory_background`), which never reads a gated request that did not run, nor its notice (trinity#3402 — rows with a `gate_hold` are left out of `get_recent_public_chat_messages` by default). Legacy plaintext rows (written before #895) are transparently surfaced as `conversation_summary` on read — see `db/public_links._parse_memory_blob`.
- **Read** (`db.get_or_create_public_user_memory`): `SELECT` by `(agent_name, user_email)`; `INSERT` if not found. Returns the parsed dict `{id, agent_name, user_email, agent_notes, conversation_summary, message_count, created_at, updated_at}` — `src/backend/db/public_links.py:get_or_create_user_memory`
- **Read — agent_notes only, no create** (`db.get_public_user_memory_agent_notes`): what the screen compares a write against; never creates the row, so a refused write leaves nothing behind — `src/backend/db/public_links.py:get_user_memory_agent_notes`
- **Write — agent_notes only, recorded** (`db.write_public_user_memory_agent_notes`): replaces only the `agent_notes` key and records the write, in one transaction — `src/backend/db/public_links.py:write_user_memory_agent_notes` (ent#637; the unrecorded `update_user_memory_agent_notes` section writer was removed by ent#419)
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
{ "success": true, "agent_name": "my-agent", "user_email": "user@example.com", "write_id": "…" }
```

## Anti-Poisoning Gates (trinity-enterprise#419)

Memory is written from conversations with arbitrary people and re-injected into every later prompt for that person, so it is treated as a persistent prompt-injection vector. Both layers live in `src/backend/services/user_memory_guard.py` (a stdlib-only leaf):

- **The screen** (`prepare_write`) runs on both writers of the injected block: this endpoint, and the background summarizer through `prepare_summary`, which drops a tripping line and keeps the rest (no agent is in the loop to rephrase; a summary that loses every line leaves the previous one). Each chat message is clamped before the summarizer formats it, and every pass is linear, so hostile text cannot stall the event loop. It is a cheap, named, pattern-based screen at one boundary, not prompt-injection detection (that stance is ent#409). It reads three folded views of each line — NFKC, invisible characters dropped *and* read as a space, case-folded or case-preserving, line-anchored or joined — so zero-width, full-width and split-across-lines spellings of the same words are caught. Preferences, open loops and links stated as facts are stored; a paraphrase or another language gets past it by design.
- **The envelope** (`envelope`, called by `platform_prompt_service.format_user_memory_block`) is how the notes and the summary enter the prompt: a framing paragraph, then platform lines carrying a per-render nonce, every stored line (`splitlines()`) behind a `│ ` margin, and a reminder after the close. The content cannot reproduce the delimiters (envelope lines dropped, any other copy defanged by `utils/untrusted_text.defang_bracket_markers`), cannot know the nonce, and carries no control characters (a NUL in the prompt argument fails the agent's spawn).

The MCP tool description names the four refusal codes and the margin to drop.

## Auth / Access Control Summary

| Check | Mechanism | Failure |
|-------|-----------|---------|
| Caller has access to agent | `assert_agent_access` + agent-key self-gate | 403 |
| Execution exists | `db.get_execution(execution_id)` | 404 |
| Execution belongs to this agent | `execution.agent_name == agent_name` | 403 |
| Channel turn is one-to-one | `source_channel_client` set (ent#549) | 422 `memory_not_one_to_one` |
| Execution serves a person | seat run, channel turn or `public` | 422 |
| Verified user email present | resolved address + regex | 422 |
| Text passes the screen | `user_memory_guard.prepare_write` | 422 `{code, message, line}` |

The agent never provides the user's email — it supplies only `execution_id`. The backend resolves the person from the execution record: the seat a scheduled run is addressed to, ent#549's one-to-one stamp on a channel turn, or `source_user_email` on a public-link / Workspace turn.

## Side Effects
- None. No WebSocket broadcast. No audit log entry (informational `logger.info` only).

## Error Handling
| Error Case | HTTP Status | Detail |
|------------|-------------|--------|
| Caller cannot access agent | 403 | Not authorized |
| Execution not found | 404 | Execution not found |
| Execution belongs to different agent | 403 | Execution does not belong to this agent |
| Triggered by a schedule that names no one / agent / MCP | 422 | write_user_memory is only available during user-facing sessions (...) |
| A group (or unverified) channel turn | 422 | `{"code": "memory_not_one_to_one", ...}` |
| No verified email on execution | 422 | No verified user email associated with this execution |
| The text trips the screen | 422 | `{"code": "memory_<rule>", "message": "Line N …", "line": N}` |

## Key Files
| File | Role |
|------|------|
| `src/mcp-server/src/tools/memory.ts` | MCP tool definition and execute handler |
| `src/mcp-server/src/client.ts:1087` | `writeUserMemory()` HTTP client method |
| `src/mcp-server/src/server.ts:217` | Tool registration |
| `src/backend/routers/public_memory.py` | FastAPI endpoint + all validation logic |
| `src/backend/services/user_memory_guard.py` | The screen (`prepare_write`, `prepare_summary`, `screen_field`) and the envelope (`envelope`, `margin_lines`) — ent#419 |
| `src/backend/utils/untrusted_text.py` | `defang_bracket_markers` — shared with the Telegram group-history block |
| `src/backend/db/public_links.py` | `get_or_create_user_memory`, `get_user_memory_agent_notes`, `write_user_memory_agent_notes` (+ history, undo) |
| `src/backend/db/schema.py:515` | `public_user_memory` table DDL |
| `src/backend/main.py:95,830` | Router import and mount |

## Related Flows
- [public-agent-links.md](public-agent-links.md) — web public chat sessions (read path that injects this memory into the system prompt; same split-storage rules)
- [unified-channel-access-control.md](unified-channel-access-control.md) — Slack/Telegram/WhatsApp channel sessions that also inject memory (#895), gated on `verified_email and not is_group`
- [execution-context-injection.md](execution-context-injection.md) — how `execution_id` is surfaced in the agent system prompt
