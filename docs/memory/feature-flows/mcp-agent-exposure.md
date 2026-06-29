# Feature: MCP Agent Exposure — dedicated `chat_with_<slug>` tools (#846)

## Overview

A per-agent opt-in toggle (`agent_ownership.mcp_exposed`) that publishes the
agent as a **dedicated MCP tool** — a `chat_with_<slug>` tool functionally
identical to `chat_with_agent` with the agent name **pre-filled**. When an owner
flips the toggle, the Trinity MCP server (which **polls** the backend) registers
or removes the dedicated tool **at runtime — no MCP-server restart** — and
FastMCP pushes `notifications/tools/list_changed` to connected clients so their
tool list refreshes within one poll interval (~20s).

The flag publishes a **surface only**. Execution always runs the same
`checkAgentAccess` gate as `chat_with_agent` (via the shared `runAgentChat`
body), so ownership/sharing is never bypassed — exposing an agent does not grant
anyone access to it.

The **backend is the single source of truth** for the deterministic,
collision-free tool name (computed over the full exposed set), killing the
split-brain an independent client-side slug would create across MCP-server
restarts/replicas.

## User Story

As an agent owner, I want to publish a specific agent as a first-class MCP tool
(`chat_with_acme` instead of `chat_with_agent(agent_name="acme")`), so an
orchestrator or external Claude Code surfaces it as a named capability — without
restarting the MCP server or weakening the access gate.

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| **UI** | `components/McpExposedPanel.vue` (in `settings/SettingsPanel.vue`) | "Expose via MCP" toggle on the agent Settings tab; shows the deterministic tool name. |
| **API** | `GET /api/agents/{name}/mcp-exposed` | Current flag + the `tool_name` the MCP server would register (any accessor). |
| **API** | `PUT /api/agents/{name}/mcp-exposed` | Owner-only enable/disable. System agent → 403. |
| **API** (internal) | `GET /api/internal/mcp-exposed-agents` | Authoritative poll list (`X-Internal-Secret`) consumed by the MCP server. |
| **MCP tool** | `chat_with_<slug>` (dynamic) | One per exposed agent; same params as `chat_with_agent` minus `agent_name`. |

No new agent-server route — this is a backend + MCP-server feature only.

---

## Architecture Flow

```
┌────────────────────────────────────────────────────────────────┐
│ Owner toggles "Expose via MCP" (McpExposedPanel.vue)           │
│   PUT /api/agents/acme/mcp-exposed {enabled:true}              │
└───────────────────────────┬────────────────────────────────────┘
                            ▼  (OwnedAgentByName; 403 if system agent)
┌────────────────────────────────────────────────────────────────┐
│ routers/agents.py  set_mcp_exposed_endpoint                    │
│   db.set_mcp_exposed("acme", True)   ← UPDATE deleted_at IS NULL│
│   audit: CONFIGURATION / mcp_exposed_config                    │
└───────────────────────────┬────────────────────────────────────┘
                            ▼   agent_ownership.mcp_exposed = 1
        ┌───────────────────────────────────────────────┐
        │ MCP server poll (~20s, fail-open + mutex)     │
        │   GET /api/internal/mcp-exposed-agents        │
        └───────────────────────┬───────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────┐
│ routers/internal.py  mcp_exposed_agents                        │
│   names = db.get_mcp_exposed_agents()                         │
│   tool_names = compute_tool_names(names)  ← full-set, stable   │
│   description = build_tool_description(name, template_label)   │
│     (cheap Docker label read; tolerates Docker hiccups)        │
│   → {agents:[{agent_name, tool_name, description}, ...]}       │
└───────────────────────────┬────────────────────────────────────┘
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ MCP server  tools/dynamic-agents.ts                            │
│   startExposedToolsReconciler.syncOnce()                       │
│     - diff desired vs current (agentName→toolName)            │
│     - builtinToolNames collision guard (skip on shadow)       │
│     - unregister gone / re-slugged ; register new            │
│   makeDedicatedChatTool() → execute delegates to runAgentChat │
│     with agent_name BOUND (never from caller input)           │
│   registerDynamicTool() → addToolWithAudit(tool, connectorDenied, agentName)│
│     → server.addTool / removeTool fan list_changed to sessions │
└────────────────────────────────────────────────────────────────┘
```

---

## Implementation

### Data Layer

- **`db/agent_settings/mcp_exposure.py`** (new) — `McpExposureMixin` composed into
  `AgentOperations` (`db/agents.py`), exported from `db/agent_settings/__init__.py`:
  - `get_mcp_exposed(name) -> bool` — `COALESCE(mcp_exposed, 0)`, `deleted_at IS NULL`. Default `False`.
  - `set_mcp_exposed(name, enabled) -> bool` — guards `deleted_at IS NULL` so a **soft-deleted agent can never be flipped into exposed state** (deliberately modeled on the circuit-breaker setter in `resources.py`, NOT `file_sharing.py` whose setter omits that guard).
  - `get_mcp_exposed_agents() -> [{"agent_name": ...}]` — all live agents with `mcp_exposed=1`. DB-only; tool name/description are computed at the service/router layer.
- **`db/schema.py`** / **`db/tables.py`** — `mcp_exposed INTEGER DEFAULT 0` on `agent_ownership`.
- **Dual migration** (Invariant #3 / RoE #9): SQLite `db/migrations.py` `_migrate_agent_ownership_mcp_exposed` (`agent_ownership_mcp_exposed`, `_safe_add_column`) **+** Alembic `migrations/versions/0009_agent_ownership_mcp_exposed.py` (`ADD COLUMN IF NOT EXISTS`, no-op when `0001_baseline` already created it).
- **`db/agent_settings/metadata.py`** — `get_all_agent_metadata` projects `COALESCE(ao.mcp_exposed, 0) as mcp_exposed` → `bool`.

### Backend Service

- **`services/agent_service/mcp_tool_names.py`** (new) — **pure** (no Docker/DB imports), the single source of truth for the tool name:
  - `_slugify(name)` — lowercase, non-`[a-z0-9_]` → `_`, collapse runs, trim `_`.
  - `compute_tool_names(names)` — deterministic `{agent_name: chat_with_<slug>}` over the **sorted, de-duped full set**. On a base-slug collision (`my-agent` vs `my_agent`), every colliding name (and any empty-slug name) gets a stable `_<sha1(name)[:4]>` suffix.
  - `compute_tool_name(name)` — single-agent convenience for the per-agent GET (the internal poll endpoint is authoritative for the final name).
  - `build_tool_description(name, template_label)` — container-read-free description from the agent's `trinity.template` Docker label (works for stopped agents; missing → name-only).
- **`services/agent_service/helpers.py`** — `get_accessible_agents` surfaces `mcp_exposed` per agent.

### Backend Routers

- **`routers/agents.py`**:
  - `GET /{name}/mcp-exposed` (`AuthorizedAgentByName`) → `{agent_name, enabled, tool_name}`.
  - `PUT /{name}/mcp-exposed` (`OwnedAgentByName`, body `McpExposedUpdate`) — **403 on the system agent** (it already bypasses all permission checks); `db.set_mcp_exposed`; audit `CONFIGURATION / mcp_exposed_config`.
- **`routers/internal.py`** — `GET /mcp-exposed-agents` (`X-Internal-Secret`): `compute_tool_names` over the live exposed set + one cheap `list_all_agents_fast()` for template labels (any Docker hiccup → name-only descriptions, **never 5xx the poll**).
- **`models.py`** — `McpExposedUpdate {enabled: bool}`.

### MCP Server (TypeScript)

- **`tools/chat.ts`** (refactor) — extracts two module-level helpers so the dedicated tools share ONE implementation with `chat_with_agent`:
  - `resolveClient(baseClient, requireApiKey, authContext)` — per-request authed client.
  - `runAgentChat(baseClient, requireApiKey, agentChatPullEnabled, agent_name, params, context)` — the shared `chat_with_agent` execution body. **No logic fork**: preserves the #946 pull-routing branch, self-task/parallel paths, idempotency route tokens, the #914 gateway-timeout receipt, sourceAgent collaboration tagging, and `checkAgentAccess` denial.
- **`tools/dynamic-agents.ts`** (new):
  - `makeDedicatedChatTool(...)` — schema mirrors `chat_with_agent` minus `agent_name`; `execute` delegates to `runAgentChat` with the agent name **bound** (never caller input).
  - `startExposedToolsReconciler(opts)` — poll loop over `/api/internal/mcp-exposed-agents`. **Fail-open** (only mutate the tool set on a 200 with a valid `agents` array; any network/non-200/parse/shape error keeps the last-known set). **In-flight mutex** so the startup sync and interval tick can't race. Diffs desired vs current, applies the `builtinToolNames` collision guard, unregisters gone/re-slugged tools, registers new. Returns a handle (`syncOnce`/`stop`/`getCurrent`); `timer.unref()` so it doesn't keep the loop alive.
- **`server.ts`** — `createServer` now returns dynamic-tool handles: a `builtinToolNames` set (final collision guard), `registerDynamicTool(tool, canAccess, auditTargetId)` / `unregisterDynamicTool(name)` (route ALL dynamic registration through `addToolWithAudit` so audit + `canAccess` wrapping is uniform; `removeTool` is public FastMCP 4.x and triggers `list_changed`), plus `agentChatPullEnabled`, `trinityApiUrl`, `connectorDenied`. `addToolWithAudit` gains an `auditTargetId` arg.
- **`index.ts`** — starts the reconciler **after `server.start()`** (so post-start add/remove fan `list_changed` to live sessions). Skips with a warning when `INTERNAL_API_SECRET` is unset (the poll would 403).
- **`audit.ts`** — `withAudit(toolName, execute, boundTargetId?)`: dedicated tools carry no `agent_name` param, so `targetId = resolveTargetId(params) ?? boundTargetId` keeps the audit row attributed to the bound agent.
- **`types.ts`** — `Agent.mcp_exposed?: boolean`.

### Frontend

- **`components/McpExposedPanel.vue`** (new) — toggle + live tool-name display; optimistic toggle that reloads on error. Copy makes the access invariant explicit ("callers still need ownership or a share to actually chat").
- **`components/settings/SettingsPanel.vue`** — renders `McpExposedPanel` as a settings section.
- **`stores/agents.js`** — `getMcpExposedStatus(name)` / `setMcpExposed(name, enabled)` (Invariant #7, single API client).

---

## Access Control Summary

| Surface | Gate |
|---------|------|
| `GET /{name}/mcp-exposed` | `AuthorizedAgentByName` (owner / shared / admin) |
| `PUT /{name}/mcp-exposed` | `OwnedAgentByName` (owner-only); system agent → 403 |
| `GET /internal/mcp-exposed-agents` | `X-Internal-Secret` (MCP server only) |
| dynamic `chat_with_<slug>` execute | `runAgentChat` → `checkAgentAccess` (identical to `chat_with_agent`) |
| dynamic tool **visibility** | `connectorDenied` gate (mirrors operator tools, ent#46) |

Exposing an agent changes only the **advertised tool surface** — the execution
access gate is unchanged.

---

## Error Handling

- Soft-deleted agent → setter no-ops (`deleted_at IS NULL` guard), `rowcount == 0`.
- System agent on PUT → 403 (`Cannot expose the system agent via MCP`).
- Internal poll Docker hiccup → name-only descriptions, never a 5xx.
- MCP reconciler poll network/non-200/parse/shape error → keep last-known set (no flap).
- `tool_name` collides with a built-in tool → reconciler skips that agent (warns).
- `INTERNAL_API_SECRET` unset → reconciler disabled with a startup warning.

---

## Testing

- **`tests/unit/test_mcp_tool_names.py`** (new) — slug normalization (case/spaces/special-char collapse+trim), collision → distinct **stable** suffixes, empty-slug → hash fallback, determinism over the full set, no-collision no-suffix, description with/without template label.
- **`tests/unit/test_mcp_exposure_mixin.py`** (new) — default `False` (no row / unset column), set→get round-trip, setter returns `False` for a missing agent, per-agent isolation, setter **refuses** a soft-deleted agent, getter ignores soft-deleted, list returns only exposed live agents.
- **`src/mcp-server/src/tools/dynamic-agents.test.ts`** (new) — `makeDedicatedChatTool` delegates to `runAgentChat` with the bound agent (no `agent_name` param), propagates a 403 denial, preserves the audit `target_id`; reconciler registers from a 200, removes gone agents, no-op on unchanged set, skips built-in collisions, fail-open on 500/network/malformed body, re-registers on a re-slug, in-flight mutex prevents map corruption.

---

## Related Flows

- [mcp-orchestration.md](mcp-orchestration.md) — the MCP server surface these dynamic tools join.
- [agent-to-agent-collaboration.md](agent-to-agent-collaboration.md) — the `runAgentChat` body (and #946 pull routing) the dedicated tools reuse verbatim.
- [mcp-api-keys.md](mcp-api-keys.md) — the key scopes that gate dynamic-tool execution.
- [mcp-git-tools.md](mcp-git-tools.md) — the prior pattern of deterministic MCP tools + bound `auditTargetId` on the `mcp_operation` row.
- [agent-lifecycle.md](agent-lifecycle.md) — the `agent_ownership` row this toggle extends.
