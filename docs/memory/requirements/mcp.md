# Requirements — MCP Server & Agent Interop (A2A, Per-Agent Exposure)

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 7. MCP Server

### 7.1 Trinity MCP Server
- **Status**: ✅ Implemented
- **Description**: Agent orchestration via Model Context Protocol
- **Key Features**: FastMCP with Streamable HTTP, 62 tools, API key authentication
- **Flow**: `docs/memory/feature-flows/mcp-orchestration.md`

### 7.2 Per-User API Keys
- **Status**: ✅ Implemented
- **Description**: Generate, revoke, and track usage per key

### 7.3 MCP Execution Query Tools (MCP-007)
- **Status**: ✅ Implemented (2026-03-25)
- **Requirement ID**: MCP-007
- **GitHub Issue**: #19
- **Description**: MCP tools for querying execution history, polling async results, and monitoring agent activity
- **Key Features**: `list_recent_executions`, `get_execution_result`, `get_agent_activity_summary`; enables async polling pattern for agent-to-agent collaboration beyond 60s MCP timeout
- **Spec**: `docs/requirements/MCP_EXECUTION_QUERY_TOOLS.md`

### 7.4 Configurable MCP Server URL (MCP-URL-001)
- **Status**: ✅ Implemented (2026-03-25)
- **Requirement ID**: MCP-URL-001
- **GitHub Issue**: #76
- **Description**: Admin-configurable MCP server URL displayed on the API Keys page connection snippets. Replaces hardcoded `http://{hostname}:8080/mcp` which is wrong for production deployments where MCP is proxied through nginx.
- **Key Features**: `GET/PUT/DELETE /api/settings/mcp-url` endpoints, URL validation (requires `http(s)://` and `/mcp` suffix), Settings UI section with save/reset, auto-detect fallback when not configured
- **Flow**: `docs/memory/feature-flows/platform-settings.md`

### 7.5 Per-Agent MCP Connector (ent#46; OSS-core since #118)
- **Status**: ✅ Implemented — OSS-core (relocated from the enterprise submodule by #118; originally ent#46/#55/#51)
- **GitHub Issue**: trinity-enterprise#118 (OSS-core move); ent#46 / ent#55 (original)
- **Description**: Expose a single agent as a per-agent MCP connector — an end user adds it to their AI client (Claude Code, Cursor, Claude Desktop) in one line, turning the agent's `user_invocable` playbooks into MCP tools. The agent holds all credentials server-side; only a scoped, revocable key reaches the client. Available in **every edition** (no `mcp_connector` entitlement).
- **Key Features**:
  - Owner CRUD under `/api/agents/{name}/connector*`: `GET`/`PUT` config (enable toggle + exposed-playbook allow-list), `POST /connector/key` (mint/regenerate — secret returned once, auto-enables), `DELETE /connector/key` (revoke). `GET /connector/playbooks` is connector-key-readable.
  - Scoped key = a row in the OSS `mcp_api_keys` table with `scope='connector'`, bound to the agent; validated by the existing OSS auth fence (`dependencies._enforce_connector_scope`) which fences a connector key to exactly `POST /{agent}/chat` + `GET /{agent}/connector/playbooks`.
  - Per-client copy-paste setup snippets (`services/connector_service.build_snippets`): Claude Code CLI + `.mcp.json`, Cursor, Claude Desktop.
  - Exposed-playbook allow-list (ent#55): `enterprise_connectors.exposed_playbooks` JSON array (NULL ⇒ all `user_invocable`); `user_invocable:false` playbooks are **never** exposed even if listed; `automation:gated` passed through as advisory metadata.
  - MCP proxy tools (`src/mcp-server/src/tools/connector.ts`, already OSS): `list_playbooks`, `run_playbook`, `ask` — visible only to `scope='connector'` sessions.
  - UI: `ConnectorChannelPanel.vue` + `ExposedToolsPanel.vue` in the Sharing tab, shown to all agent owners (un-gated).
- **Schema**: `enterprise_connectors` (name kept for zero-migration adoption of existing enterprise installs) — dual-track (SQLite `db/migrations.py:enterprise_connectors_table` + Alembic `0015_enterprise_connectors`).
- **Deferred (Part B, blocked on #848 design sign-off)**: email-auth onboarding — inline `request_login`/`verify_login` MCP tools so an external user on the agent's sharing allow-list connects with just their email (no pre-minted key). Tracked separately.
- **Flow**: `docs/memory/feature-flows/mcp-connector.md`

---

## 32. A2A Agent Discoverability (#737)

### 32.1 A2A v1.0 Agent Card Endpoint (#737 — Phase 1)
- **Status**: 🚧 In Progress (Phase 1)
- **Implements**: Issue #737
- **Description**: Each Trinity agent exposes an A2A-protocol Agent
  Card so external orchestrators (AWS Bedrock, Azure Copilot, Google
  ADK) can discover its identity, skills, and auth requirements
  without knowing Trinity's internal API. A2A is Google's open
  agent-interoperability protocol (https://google.github.io/A2A/).
- **Endpoint**: `GET /api/agents/{name}/a2a/agent-card` — returns a
  valid A2A v1.0 card built from the agent's `template.yaml`
  (`name`, `description`, `version`, `skills[]` mapped from
  `capabilities[]` with `use_cases[]` as examples) plus declared
  `securitySchemes.bearerAuth` (Trinity MCP API key) and
  `capabilities.streaming = true`. Auth-gated by `AuthorizedAgentByName`.
- **Behavior**: card data fetched from the agent-server's
  `/api/template/info`; falls back to Docker labels when the agent
  is stopped or unreachable (never 5xx's). The `url` field points at
  the public chat endpoint as a working placeholder until the A2A
  JSON-RPC endpoint ships.
- **Phase 2 (deferred)**: Redis caching of the card; auth-gated
  extended card with internal endpoint URLs + full skill schemas;
  host-root `/.well-known/agent-card.json` proxy convention; MCP
  `get_agent_card` tool; the A2A JSON-RPC server the card's `url`
  should ultimately address.

---

## 45. Per-Agent MCP Exposure — Dedicated Dynamic Tools (#846)

**Description**: A per-agent owner-toggled flag (`mcp_exposed`, default off) that publishes
an agent as a first-class MCP tool. When enabled, the Trinity MCP server dynamically
registers a dedicated `chat_with_<slug>` tool — functionally identical to `chat_with_agent`
with the agent name pre-filled — so a curated, well-known agent surfaces as a named tool
instead of requiring `list_agents` + `chat_with_agent`. Toggling adds/removes the tool at
runtime with **no MCP-server restart**. The flag publishes a surface only; execution always
runs the same access gate, so ownership/sharing is never bypassed.

- **FR-1 — Toggle**: `agent_ownership.mcp_exposed INTEGER DEFAULT 0`; owner-only `GET`/`PUT
  /api/agents/{name}/mcp-exposed`. PUT refuses the system agent (403). Getter/setter both guard
  `deleted_at IS NULL` (a soft-deleted agent can never be flipped exposed). Dual-track migration
  (SQLite `agent_ownership_mcp_exposed` + Alembic `0009`).
- **FR-2 — Canonical slug (single backend source of truth)**: the backend computes the
  deterministic, collision-free `tool_name` over the full exposed set (sorted; sanitized
  `chat_with_<slug>`; `_<sha1(name)[:4]>` suffix on agent-vs-agent base-slug collision). The
  per-agent GET and the internal poll endpoint use the same helper, so UI and MCP never
  diverge.
- **FR-3 — Internal poll endpoint**: `GET /api/internal/mcp-exposed-agents` (`X-Internal-Secret`)
  returns `{agent_name, tool_name, description}` per exposed agent. `description` is generated
  from cheap Docker `trinity.template` label metadata (no container read; works for stopped
  agents).
- **FR-4 — Refresh = poll**: the MCP server polls the internal endpoint (~20s), diffs an
  `agentName→toolName` map, and calls FastMCP `addTool`/`removeTool`; FastMCP fans
  `notifications/tools/list_changed` to live sessions. The reconciler is **fail-open** (mutate
  only on a valid 200; keep last-known set otherwise) and holds an in-flight mutex. A final
  guard skips any `tool_name` colliding with a built-in tool.
- **FR-5 — No logic fork**: the `chat_with_agent` body is extracted into a shared
  `runAgentChat`, reused by `chat_with_agent` and every dedicated tool (preserves #946 pull
  routing, parallel/self-task paths, idempotency tokens, #914 gateway-timeout recovery, access
  denial). Dedicated tools register with the `connectorDenied` visibility gate and bind their
  audit target (no `agent_name` param).
- **FR-6 — Surfacing**: `mcp_exposed` is exposed on `GET /api/agents` / MCP `list_agents`. A
  Settings-tab toggle ("Expose via MCP") shows the computed tool name and up-to-poll-interval
  latency copy.

**Deferred**: WS push (poll latency ≤20s is fine for an owner-toggled flag); a partial index on
`mcp_exposed`; tool-name stability across an agent rename (rename re-slugs); multi-replica MCP
servers (each replica polls + reconciles independently).

---
