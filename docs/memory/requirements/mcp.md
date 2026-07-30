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
- **Part B (email-auth onboarding)**: see §7.6 — inline `request_login`/`verify_login` so an external user on the agent's sharing allow-list connects with just their email (no pre-minted key).
- **Flow**: `docs/memory/feature-flows/mcp-connector.md`

---

## 7.6 MCP Inline Email Auth (#848)

- **Status**: 🚧 In Progress
- **GitHub Issue**: #848 (public) — Part B of trinity-enterprise#118
- **Description**: Let an external user authenticate to Trinity **from inside their MCP
  client** using the existing 6-digit email-code flow, with no pre-minted API key and no
  web-UI visit. They install a keyless connector config, call `request_login(email)`,
  receive a code, call `verify_login(code)`, and can then use the exposed playbooks of
  every agent shared with that email. Mirrors Telegram's inline `/login` (§Channels), the
  established Trinity pattern for binding a verified email to a channel identity.

### Credential model — session, not a minted key

Per the #848 design sign-off, inline login credentials the **session**; it never returns a
`trinity_mcp_*` key to the client. This matches every other channel (Telegram/Slack/WhatsApp
bind a verified email server-side and hand the user nothing).

Consequence to design around: **FastMCP sessions are per-connection**, so an inline login
lasts only as long as the MCP client's connection — a client restart requires logging in
again. Accepted as the cost of the no-credential-on-disk model.

### Anonymous session tier

- `MCP_INLINE_AUTH_ENABLED` (env, **default OFF**) gates the whole feature. With it off,
  behaviour is byte-identical to today: a request with no `Authorization` header is
  rejected at `authenticate` and no session is created.
- **TWO processes read this one key**, so it must be wired into **both** the `backend` and
  `mcp-server` services in **both** `docker-compose.yml` and `docker-compose.prod.yml` —
  four wirings. The mcp-server read (`server.ts`) is the session-tier gate; the backend read
  (`config.py`) gates `/api/internal/mcp-auth/*` (404 when off) and the keyless connector
  snippet. Neither compose file carried it when the feature was first written, which fails
  *safe* but renders the whole feature un-switchable: the surface 404s, keyless connections
  are refused and `keyless_snippets` never appears, with no operator lever. Neither CI nor
  `/verify-local` can catch that class — both boot at defaults, so a flag that can never be
  turned on boots clean and goes green. `GET /api/settings/feature-flags` surfaces
  `mcp_inline_auth_enabled` (observability-only) so an operator can confirm the two halves
  agree after a deploy.
- **Operational prerequisite:** set `INTERNAL_API_SECRET` **explicitly** in production
  rather than relying on its `→ SECRET_KEY` fallback. This feature widens that secret — a
  holder can assert any verified email over the internal surface. It grants no privilege
  beyond internal-secret compromise (already backend god-mode over `/api/internal/*`), but
  the value now deserves its own rotation lifecycle rather than `SECRET_KEY`'s. (CSO N1.)
- With it on, a request carrying **no** `Authorization` header yields a truthy
  **anonymous sentinel** auth context (`scope: "anonymous"`), not `undefined`. A request
  carrying an **invalid** key still throws — a wrong credential is an error, an absent one
  is an invitation to log in.
- The sentinel must be truthy and must not carry an `authenticated: false` key: fastmcp
  skips `canAccess` filtering entirely for a falsy auth (`#createSession`, giving a session
  EVERY registered tool) and rejects `{authenticated: false}` outright. Verified against
  `fastmcp@4.12.1`, the version `package-lock.json` resolves; the behavioural test
  `tool-visibility.test.ts` pins it so a future bump cannot quietly change it.
- Tool visibility is enforced by the §7.5 allow-list gates (`operatorOnly` /
  `connectorOnly` / anonymous), hardened for this feature — operator tools are never
  visible to an anonymous or unknown scope.

### Static tool surface (no reconnect)

The anonymous session is advertised the **same** tool set before and after login —
`request_login`, `verify_login`, and the connector tools. Login flips tool *behaviour*, not
tool *visibility*: before verification the connector tools return a "log in first" error.
This is deliberate, but NOT because the list cannot change. FastMCP *does* re-filter a
live session against its current auth (`FastMCPSession.toolsListChanged`), and Trinity
triggers exactly that every ~20s through the #846 exposed-agents reconciler. That makes a
login-state-dependent gate **non-deterministic**: visibility would flip whenever the
reconciler happened to fire, unrelated to the login itself. A static surface is
predictable. (A second hazard points the same way: `updateAuth` *replaces* the session's
auth object rather than mutating it, which would silently discard the in-place upgrade.)

### Keyless setup surface

The owner shares the agent **without minting a key**: the connector panel shows a
"Share without a key — sign in by email" block whenever `ConnectorStatus.inline_auth_available`
is true (i.e. `MCP_INLINE_AUTH_ENABLED` is on), carrying `keyless_snippets` — the same
per-client config blocks as the keyed setup but with no `Authorization` header, so the
client connects as an anonymous session and signs in via `request_login`/`verify_login`.
`services/connector_service.build_keyless_snippets` and `build_snippets` share one
`_client_snippets` builder so the keyed and keyless variants cannot drift. The keyless
config is offered **only** when the flag is on — an anonymous MCP session is rejected
otherwise, so the config would not connect (no dead setup instructions).

### Backend access path

An email-verified session holds no API key, so the MCP server reaches Trinity over a
dedicated **internal** surface authenticated with `X-Internal-Secret` and carrying the
verified email. The backend gates every such call on `db.email_has_agent_access(agent, email)`
(the same primitive the channel access gate uses, #311) — the internal secret authenticates
the *caller*, never the *authorization*. Alternatives rejected: minting a session-scoped key
cannot be revoked on disconnect (FastMCP's session `#auth` is private, so a `disconnect`
event cannot be correlated back to the minted credential) and would leak rows into
`mcp_api_keys` indefinitely.

### Whitelist and access gate

Inline login **bypasses the email whitelist**, matching Telegram's inline `/login`. This is
required for the feature to serve its purpose: `POST /api/auth/email/request` silently no-ops
for a non-whitelisted email, so a whitelist-gated flow could never onboard the external users
this exists for. First login creates a `user`-role account per #314 (chat-only grant, no
silent promotion).

Authorization is `db.email_has_agent_access` (the same primitive the channel gate uses),
re-checked on **every** data call. A denial is a **flat, uniform 403** — it does NOT write an
`access_requests` row, deliberately diverging from the channel gate:

- The channel gate runs **once per inbound conversation**, so an access request there is a
  bounded, user-initiated act. The inline gate runs **per tool call**, where the same write
  would let any caller spam `access_requests` rows for arbitrary agent names.
- There is also no natural trigger: inline login has no per-agent request step, and `verify`
  returning an empty `agents` list is already the normal "nothing shared with you yet"
  outcome.

`no-access`, `connector-disabled` and `no-such-agent` all return the **same** 403 body —
splitting them would enumerate the fleet (Invariant #8, self-uniform handlers).

**Deferred:** a deliberate "request access to agent X" affordance (an explicit tool, rate-limited
per session, writing one `access_requests` row) is the right home for the #311 request path and
is out of scope here.

**Known-address gate.** `request_login` is reachable unauthenticated, so it must not become an
open email relay: a code is generated only for an address Trinity already knows (a `users` row,
or an `agent_sharing` entry). This lookup **fails closed** — an inability to answer "do we know
this address" must not degrade into "email anyone who asks". It is a send-worthiness test only,
never authorization.

Because the whitelist is bypassed, the OTP primitives (`db.create_login_code` /
`db.verify_login_code`) are used directly rather than the whitelist-gated
`/api/auth/email/*` endpoints. **Telegram's path has no rate limiting** — acceptable behind
Telegram's bot API, not on an open MCP port — so inline auth additionally applies the
existing `check_login_rate_limit` / `check_otp_rate_limit` limiters.

### Enumeration safety

`request_login` returns a **generic, branch-independent** response whether or not the email
is known (mirrors `auth.py`'s generic response, #186) and emits **no** audit event — any
per-branch signal (message, timing, audit row) is an enumeration oracle. `verify_login`
outcomes ARE audit-logged (`AuditEventType.AUTHENTICATION`, `login_success`/`login_failed`,
`source="mcp"`), matching the web email path.

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

## Trinity Helper MCP Server (#1459)

**Description**: A standalone, dependency-light MCP server (`src/helper-mcp/`, npm
`@abilityai/trinity-docs-mcp`) that exposes the public Trinity Docs Q&A service
(DOCS-QA-001, `docs/memory/feature-flows/trinity-docs-qa.md`) as MCP tools, so anyone can
add a grounded "ask Trinity anything" assistant to Claude Code / Claude Desktop / any MCP
client **without running a Trinity instance**. Pure protocol adapter over the existing
`ask-trinity` Cloud Function — no new backend/QA logic, no authentication, no credentials.
Distinct from the main Trinity MCP server (`src/mcp-server/`, requires a Trinity API key).

- **FR-1 — `ask_trinity` tool**: `{question (required, ≤4,000 chars), session_id?
  (opaque string)}` → POSTs the public endpoint; returns the answer plus the response
  `session_id` for multi-turn follow-ups. Session expiry is **silent** server-side (an
  expired/invalid id yields a NEW session with HTTP 200/`SUCCEEDED`) — the tool always
  returns the effective session_id and appends a context-lost warning when it differs
  from the input.
- **FR-2 — `get_agent_requirements` tool**: fetches `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
  from raw.githubusercontent.com at call time (living doc, no bundling staleness); on fetch
  failure returns a static quick-reference fallback + the GitHub URL, never an error-only
  response. Same tool name/shape as the main MCP server's (per-server namespacing).
- **FR-3 — Robustness**: 50s abort timeout (under the 60s MCP client default), no
  auto-retry, `redirect: "error"`, non-JSON response guard, structured error text for
  non-200 / `state != SUCCEEDED` / empty answer — a tool call never crashes the server.
  `session_id` is an opaque string end-to-end (live values exceed 2^53; numeric handling
  would corrupt them).
- **FR-4 — Distribution**: npx-runnable stdio package; runtime deps = official
  `@modelcontextprotocol/sdk` + `zod` only (deliberately NOT fastmcp — smaller
  supply-chain surface); `console.error`-only logging (stdout is the JSON-RPC channel);
  Node ≥18 guarded at startup. Publish via `.github/workflows/publish-helper-mcp.yml`
  (npm provenance; one-time manual first publish creates the package, then trusted
  publishing takes over).
- **FR-5 — Endpoint override**: `ASK_TRINITY_ENDPOINT` env var (default: the public Cloud
  Function URL) for self-hosted mirrors and the CI smoke test; logged to stderr when set.
- **FR-6 — Corpus**: the docs-sync workflow indexes `docs/onboarding/**`,
  `docs/user-docs/**` (incl. the 264-Q&A FAQ) and `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
  so answers cover evaluator/operator questions, not just onboarding.

**Deferred**: hosted remote Streamable-HTTP endpoint + vanity URL + MCP registry listing
(fast-follow; the official SDK keeps the transport option open); Cloud Function citations
passthrough (the endpoint returns no citations today — the adapter forwards a `citations`
field if it ever appears); #1460 (`ask_trinity` inside the main Trinity MCP server —
shares the same tool name/schema and endpoint-client contract).
