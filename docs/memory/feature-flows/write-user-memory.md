# Feature: write_user_memory MCP Tool (MEM-001, #888, #895)

## Overview
Agents can persist per-user memory blobs scoped to a single (agent, user_email) pair. This replaces the unsafe pattern of writing to Claude Code's auto-memory (under `~/.claude/projects/`) or the repo's `memory/` directory — both are shared across all users of an agent and leak PII between sessions.

**#895 update**: storage was split into two named sections so the agent-deliberate writes from this tool don't clobber the every-5-message conversation summarizer (and vice versa). The MCP wire format is unchanged — the tool still accepts a single `memory_text` field — but the backend now routes that value to the `agent_notes` section of the JSON blob stored on disk. Channel sessions (Slack/Telegram/WhatsApp) now also inject this memory into the agent's system prompt, gated on `verified_email and not is_group`.

## User Story
As an agent serving multiple users via public link / Slack / Telegram / WhatsApp, I want to remember facts about each individual user (name, preferences, timezone) so that future sessions are personalized — without contaminating any other user's context.

## The PII Leak it Fixed
Before #888, agents that needed to remember user-specific facts had no safe write surface. The only available path was writing to the agent filesystem (Claude Code's auto-memory under `~/.claude/projects/`, or similar), which is a single shared namespace across all users of that agent. Writing a user's email, name, or preferences there made it visible to every other user that same agent served.

The fix is a server-side gated write: the agent never supplies a user email. The backend resolves it from the execution record, preventing an agent from writing memory for an arbitrary user.

### Auto-memory is off on a turn that serves someone else (#4156)
A tool alone did not close the leak. Claude Code's auto-memory is on by default, and its own system prompt says "If the user explicitly asks you to remember something, save it immediately" — into the shared directory. On a turn where a person says "remember …" that harness instruction competes with this flow's platform-prompt section, and once (Sonnet 5, a Workspace turn by a non-owner) the model wrote the file and replied "Saved".

So the platform takes the shared sink out of those turns instead of out-arguing it. `memory_subject.keeps_shared_auto_memory(row, owner_email)` keeps auto-memory on only for the agent's own scope (a headless run, or the owner's turn — the scope `scope_for` folds the owner into); every other turn (another person, an anonymous visitor, a channel group, a seat run for someone else, a trigger off the allowlist) is sent `disable_auto_memory: true` on all three wires — the push `/api/task` payload (`task_execution_service.auto_memory_off_for`), the `/api/chat` payload (`build_chat_payload`) and the pull claim envelope (`pull_coordination_service._auto_memory_off`). The agent server puts `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in that one spawn's env (`execution_env.auto_memory_env_layer`, above `.env`), which Claude Code (pinned 2.1.281) reads to leave the memory system out of the turn: no "save it" instruction, no read of other people's notes, no write. The owner's and headless turns are sent nothing, so their spawn is unchanged. Fail-closed: a row that cannot be read turns it off. Claude runtime only (`AgentRuntime.supports_auto_memory_switch`); Gemini and Codex have no such memory.

**Limits.** A model can still write any file it is told to; this removes the instruction and the sink, not the `Write` tool. Claude Code applies a settings `env` block from the agent-writable user and workspace `settings.json` over the process env, so an agent (or its owner) that pins the variable there can turn it back on. How often the original failure happened per model was not measured.

## Entry Points
- **MCP Tool**: `write_user_memory` in `src/mcp-server/src/tools/memory.ts:40`
- **API**: `POST /api/agents/{agent_name}/user-memory`

## Frontend Layer
None in this flow: the agent writes through MCP. The person sees what an agent remembers about them, and the writes behind it, in the Workspace agent page (`PortalAgentMemory.vue`, ent#637 — see [schedule-workspace-delivery.md](schedule-workspace-delivery.md)). A refused write leaves nothing there to see.

## MCP Tool Layer

### Tool Definition
- `src/mcp-server/src/tools/memory.ts:40` — `writeUserMemory` tool
- Registered in `src/mcp-server/src/server.ts:680` via `createMemoryTools(client, requireApiKey)`

### Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `execution_id` | yes | Current execution ID from the system prompt "Execution Context" block. A fallback: the platform's `X-Trinity-Execution-Id` header wins (below) |
| `memory_text` | yes | Complete updated memory blob (max 8000 chars) — replaces previous content entirely |
| `agent_name` | no | Leave out; defaults to the `agentName` in the agent-scoped MCP key's auth context, and naming another agent is refused |

### Agent Name Resolution
`src/mcp-server/src/tools/memory.ts:107-122` — resolves agent name in priority order:
1. Explicit `agent_name` parameter
2. `authContext.agentName` (from agent-scoped MCP key, scope `"agent"`)
3. Error if neither is available

### Which Turn (#4121)
The id sent is `resolveExecutionId(authContext, execution_id)`, the rule the effect-guarded tools use (`send_message`, `share_file`, `send_voice_reply`): the `X-Trinity-Execution-Id` header (`${TRINITY_EXECUTION_ID:-manual}`, set per spawn) wins over the typed value. The door refuses a run that is not live, and a resumed session copies ids from its own history, so a typed id alone would lose a live turn's note. An interactive Claude Code or Gemini session sends `manual`, which is no execution (404), so this tool cannot write through whoever's turn happens to be live from a web terminal. Codex omits the header outside a turn, so there the typed value goes out; it is also the fallback for agent images older than the header.

### Client Call
`src/mcp-server/src/client.ts:3020` — `apiClient.writeUserMemory(resolvedAgent, { execution_id, memory_text })`
- Makes `POST /api/agents/{agent_name}/user-memory` with the agent's own MCP key as Bearer token

## Backend Layer

### Endpoint
- `src/backend/routers/public_memory.py:56` — `POST /api/agents/{agent_name}/user-memory`
- Router prefix `/api/agents`, mounted in `src/backend/main.py:1433`

### Business Logic
`src/backend/routers/public_memory.py:57-207`

1. **Authorization check** — the agent-key self-gate first (`assert_own_agent`, trinity-enterprise#840: an agent-scoped key writes only its own agent's memory, so an agent key naming another agent — inside its reach or not — gets that refusal), then the agent's-own-door check (#4121: no `agent_name` on the principal, so any human, the owner and the system key included, is `403 agent_only`, and a Workspace-only account meets the operator floor's `workspace_only` before this; the run the caller names decides whose notes are replaced, so a human the agent is shared with could otherwise name any person's run), then `assert_agent_access(current_user, agent_name)`. Returns 403 if not.
2. **Execution lookup** — `db.get_execution(body.execution_id)`: the execution must exist. Returns 404 if not.
3. **Execution ownership check** — `execution.agent_name != agent_name`: the execution must belong to the agent named in the path. Returns 403 if mismatch.
4. **The turn is live (#4121)** — the row must be `running`, or `422 memory_turn_not_live`. An agent's own key could otherwise name a finished run of anyone it served (a seat run, a one-to-one channel turn, a signed-in public turn) and replace that person's notes. The same rule as the search door ([retrieval-memory.md](retrieval-memory.md)).
5. **Whose memory** — never sent by the agent:
   - a scheduled run addressed to one person (ent#637) → that seat, read off the row (`schedule_seat_memory.seat_for_execution`);
   - a Slack / Telegram / WhatsApp turn → `source_channel_client`, ent#549's stamp for the verified speaker of a **one-to-one** conversation (ent#419 AC 4). A group turn carries the unlocker's address in `source_user_email`, is never shown that memory and has other members' text in its prompt, so it is refused with `422 memory_not_one_to_one`;
   - a `public` turn (public link, Workspace) → `execution.source_user_email`;
   - anything else (a schedule that names no one, agent-to-agent, MCP) → 422.
6. **Email check** — the resolved address must look like an email. Returns 422 if missing or malformed.
7. **No other live turn resolves elsewhere (#4121)** — `memory_service.another_live_scope(agent, execution_id, scope_for(agent, email))`, the ent#926 ledger rule: nothing proves which live turn a request comes from, so while another live turn on the agent resolves to a different scope the write is `422 memory_turn_ambiguous`. A turn that serves no one (anonymous, group, a trigger outside the allowlist) is a different scope; an unreadable ledger counts as one.
8. **Normalise + screen (ent#419)** — `user_memory_guard.prepare_write(memory_text, previous=db.get_public_user_memory_agent_notes(...))`:
   - strips the platform's own presentation from a copy-back, structurally: inside a copied envelope only the agent-notes section is kept (the conversation summary and the seat decisions are dropped, never merged into the notes); the `│ ` margin, the block's headings, envelope lines, the framing, the reminder, their blank spacing and a trailing `---` go — never a `>`, since "> 100 staff" is a fact;
   - screens the result: `memory_prompt_marker`, `memory_instruction`, `memory_tool_directive`, `memory_link_in_instruction`. A hit is `422 {code, message, line}` (the line of the text the agent sent) and **nothing is stored** — no memory change, no history row. A hit the stored notes already produced (same rule, same matched text) is not counted again, so text from before the screen never locks the memory; old lines reordered, re-contextualised or respelled into a new hit, and a phrase completed by a new line, are judged afresh. The refusal is logged with the rule and the line, never the text.
9. **Write + history (#895, ent#637)** — `db.write_public_user_memory_agent_notes(agent_name, user_email, stored_text, execution_id=…, triggered_by=…, schedule_id=…)`: in one transaction, replaces only the `agent_notes` key of the JSON blob (the summarizer's `conversation_summary` is untouched) and records a `public_user_memory_writes` row (previous + new notes, which run). The row is created on demand. This is the only `agent_notes` writer in `src/backend` (AST-pinned).

### Database Operations
- **Table**: `public_user_memory` (schema at `src/backend/db/schema.py:1114`) — unchanged by #895
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
- **Index**: `idx_public_user_memory_lookup ON public_user_memory(agent_name, user_email)` — `src/backend/db/schema.py:2416`

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

- **The screen** (`prepare_write`) runs on both writers of the injected block: this endpoint, and the background summarizer through `prepare_summary`, which drops a tripping line and keeps the rest (no agent is in the loop to rephrase; a summary that loses every line leaves the previous one). Each chat message is clamped before the summarizer formats it, and every pass is linear, so hostile text cannot stall the event loop. It is a cheap, named, pattern-based screen at one boundary, not prompt-injection detection (that stance is ent#409). It reads three folded views of each line — NFKC, invisible characters dropped *and* read as a space, case-folded or case-preserving, line-anchored or joined — so zero-width, full-width and split-across-lines spellings of the same words are caught. Preferences, open loops, and links or addresses stated as facts are stored (an address inside an instruction counts as a link, operator ruling 2026-10-23); a paraphrase or another language gets past it by design.
- **The envelope** (`envelope`, called by `platform_prompt_service.format_user_memory_block`) is how the notes and the summary enter the prompt: a framing paragraph, then platform lines carrying a per-render nonce, every stored line (`splitlines()`) behind a `│ ` margin, and a reminder after the close. The content cannot reproduce the delimiters (envelope lines dropped, any other copy defanged by `utils/untrusted_text.defang_bracket_markers`), cannot know the nonce, and carries no control characters (a NUL in the prompt argument fails the agent's spawn).

The MCP tool description names the four refusal codes and the margin to drop, plus the two turn refusals (#4121).

## Auth / Access Control Summary

| Check | Mechanism | Failure |
|-------|-----------|---------|
| Caller is this agent's own key | agent-key self-gate (`assert_own_agent`), then `current_user.agent_name` | 403, `agent_only` for a human |
| Caller has access to agent | `assert_agent_access` | 403 |
| Execution exists | `db.get_execution(execution_id)` | 404 |
| Execution belongs to this agent | `execution.agent_name == agent_name` | 403 |
| The run is live | `execution.status == running` | 422 `memory_turn_not_live` |
| Channel turn is one-to-one | `source_channel_client` set (ent#549) | 422 `memory_not_one_to_one` |
| Execution serves a person | seat run, channel turn or `public` | 422 |
| Verified user email present | resolved address + regex | 422 |
| No other live turn resolves elsewhere | `memory_service.another_live_scope` | 422 `memory_turn_ambiguous` |
| Text passes the screen | `user_memory_guard.prepare_write` | 422 `{code, message, line}` |

The agent never provides the user's email — it supplies only `execution_id`. The backend resolves the person from the execution record: the seat a scheduled run is addressed to, ent#549's one-to-one stamp on a channel turn, or `source_user_email` on a public-link / Workspace turn.

## Side Effects
- None. No WebSocket broadcast. No audit log entry (informational `logger.info` only, including the two turn refusals: the log line is how an operator finds a note that was refused).

## Error Handling
| Error Case | HTTP Status | Detail |
|------------|-------------|--------|
| Caller is a human, the owner included | 403 | `{"code": "agent_only", ...}` |
| Caller cannot access agent | 403 | Not authorized |
| Execution not found (includes `manual`) | 404 | Execution not found |
| Execution belongs to different agent | 403 | Execution does not belong to this agent |
| The run is not running | 422 | `{"code": "memory_turn_not_live", ...}` |
| Another live turn resolves to a different scope | 422 | `{"code": "memory_turn_ambiguous", ...}` |
| Triggered by a schedule that names no one / agent / MCP | 422 | write_user_memory is only available during user-facing sessions (...) |
| A group (or unverified) channel turn | 422 | `{"code": "memory_not_one_to_one", ...}` |
| No verified email on execution | 422 | No verified user email associated with this execution |
| The text trips the screen | 422 | `{"code": "memory_<rule>", "message": "Line N …", "line": N}` |

## Security Considerations
- **Who writes**: an agent-scoped key writes only its own agent's memory (self-gate), for the person its execution serves — resolved server-side, never named by the caller (a seat run's addressee, a channel turn's one-to-one verified speaker, a public/Workspace turn's verified visitor). A group turn writes no one's memory. A human writes nothing here (`agent_only`).
- **Which turn**: only a live run, and only while no other live turn on the agent resolves to a different scope (#4121). **The cost, on a shared agent:** a write is refused whenever another person's turn is live, or a headless run unless the write is for the owner. That includes two seats briefed at the same time (ent#637's one-schedule-per-seat model, default parallelism 3: stagger the schedules) and a person's turn that has tasked its own agent (a `self_task` or `fan_out` child resolves to the agent scope). A row a crash left `running` refuses too, until the watchdog ends it. The turn may end before a retry works, so the note is lost: the search door can tolerate that as a read, a write cannot, which is why the tool description tells the agent to say the note was not saved. Nothing enforces that the agent does. The refusal is logged.
- **Scope, not person**: `scope_for` folds the agent's owner into the agent scope a headless run also resolves to, so a headless run beside the owner's live turn is not ambiguous, though notes are stored per address: a headless run can name that turn and write the owner's notes. The filed rule, pinned by `test_scope_is_not_person_the_owner_shares_the_agent_scope`.
- **What the rule does not prove**: that the caller is in the turn it names. A process holding the agent's key can name the one live run by calling the API directly; the tool edge sends the platform's header where the runtime sets one, so on those runtimes this takes a deliberate call, not a stale id.
- **What is written**: the [anti-poisoning gates](#anti-poisoning-gates-trinity-enterprise419) — a named pattern screen at the boundary (a cheap screen, not prompt-injection detection: paraphrase, other languages and homoglyphs pass) and the trust envelope at the read point, which does not depend on spelling.
- **Logs**: a refusal is logged with the rule, the line, the execution and the trigger — never the notes; the 422 body carries the rule and the line, never the text.
- **Cost**: every guard pass is linear; the summarizer clamps each chat message before formatting it on the event loop.
- **Residual limits**: the summarizer's writes are screened but not recorded in history, and there is no owner/admin memory view (trinity-enterprise#962).

## Testing
- `tests/unit/test_ent419_memory_poisoning_gates.py` — the screen through the real router (each rule, folded spellings, benign table, the hit-keyed exemption and its bypasses, copy-back normalisation, logging), AC 4 per channel, the envelope on the real `format_user_memory_block` (incl. a Hypothesis property), wiring through the seat-run and channel caller prompts, the summarizer, seat decisions, the Telegram defang, AST pins, totality and time budgets. ✅
- `tests/unit/test_ent637_schedule_seat_memory.py` — seat-run writes, history and undo. ✅
- `tests/unit/test_ent79_portal_exposure.py::test_portal_memory_reaches_the_turn_inside_the_trust_envelope` — `portal_chat` → `execute_task(system_prompt=…)`. ✅
- `tests/unit/test_4121_user_memory_write_door.py` — the door's turn and principal rules through the real route, real `get_current_user`, a real agent key and real rows. ✅
- `tests/integration/test_4121_user_memory_write_door.py` — the same rule over the network on a sibling stack, with a real agent key seeded inside the backend under test. ✅
- `tests/unit/test_4156_auto_memory_person_turns.py` — which turns are sent `disable_auto_memory`, on the push, `/api/chat` and pull wires; fail-closed. ✅
- `tests/unit/test_4156_agent_auto_memory_spawn.py` — the flag becomes `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the `claude` spawn env on both spawn paths; the owner's spawn is unchanged; `.env` cannot override it. ✅
- `src/mcp-server/src/tools/memory.test.ts` — a 422 refusal's code, message and line reach the agent; the published description names the refusal codes; the platform's header wins over a typed id on both doors. ✅

## Key Files
| File | Role |
|------|------|
| `src/mcp-server/src/tools/memory.ts` | MCP tool definition and execute handler |
| `src/mcp-server/src/client.ts:3020` | `writeUserMemory()` HTTP client method |
| `src/mcp-server/src/server.ts:680` | Tool registration |
| `src/backend/routers/public_memory.py` | FastAPI endpoint + all validation logic |
| `src/backend/services/user_memory_guard.py` | The screen (`prepare_write`, `prepare_summary`, `screen_field`) and the envelope (`envelope`, `margin_lines`) — ent#419 |
| `src/backend/utils/untrusted_text.py` | `defang_bracket_markers` — shared with the Telegram group-history block |
| `src/backend/db/public_links.py` | `get_or_create_user_memory`, `get_user_memory_agent_notes`, `write_user_memory_agent_notes` (+ history, undo) |
| `src/backend/db/schema.py:1114` | `public_user_memory` table DDL |
| `src/backend/main.py:125,1433` | Router import and mount |

## Related Flows
- [public-agent-links.md](public-agent-links.md) — web public chat sessions (read path that injects this memory into the system prompt; same split-storage rules)
- [unified-channel-access-control.md](unified-channel-access-control.md) — Slack/Telegram/WhatsApp channel sessions that also inject memory (#895), gated on `verified_email and not is_group`
- [execution-context-injection.md](execution-context-injection.md) — how `execution_id` is surfaced in the agent system prompt
