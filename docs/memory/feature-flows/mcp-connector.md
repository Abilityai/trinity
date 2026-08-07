# Per-Agent MCP Connector (ent#46; OSS-core since #118)

Expose one agent as a per-agent MCP connector: an end user adds it to their AI
client (Claude Code, Cursor, Claude Desktop) in one line, and the agent's
`user_invocable` playbooks become MCP tools. The agent holds all credentials
server-side; only a scoped, revocable key reaches the client.

Shipped entitlement-gated in v0.8.0 (`mcp_connector`); **relocated into OSS core
by #118** — the router/service/db moved from the private enterprise submodule into
the public repo and the entitlement gate was dropped front and back. Part B
(email-auth onboarding, #848) is deferred pending design sign-off.

## Layers

| Layer | File | Notes |
|-------|------|-------|
| Router | `src/backend/routers/connector.py` | `/api/agents/{name}/connector*`, mounted unconditionally in `main.py` (no `requires_entitlement`) |
| Service (pure) | `src/backend/services/connector_service.py` | `build_snippets`, `resolve_exposed_playbooks`, `connector_name` |
| DB | `src/backend/db/connector.py` (`ConnectorOperations`) | config CRUD + key mint/regenerate/revoke; facade delegators on `database.py` (`db.get_connector_config`, …) |
| Models | `src/backend/models.py` | `ConnectorConfigUpdate/Status/KeySecret/Playbook/ClientSnippet` (Invariant #14) |
| MCP tools | `src/mcp-server/src/tools/connector.ts` | `list_playbooks` / `run_playbook` / `ask` — `connectorOnly` `canAccess` (already OSS) |
| Auth fence | `src/backend/dependencies.py` | `_enforce_connector_scope` / `_reject_connector_principal` — edition-agnostic (already OSS) |
| UI | `components/ConnectorChannelPanel.vue` + `ExposedToolsPanel.vue` | in `SharingPanel.vue`, un-gated (#118) |
| UI (2nd surface) | `components/McpExposedPanel.vue` "Connect an external client" section | #1575: one-click **Copy connection config** on the #846 Expose-via-MCP panel (Settings tab), shown when `mcp_exposed` is on. Reuses the SAME connector endpoints (`GET/POST/DELETE /connector[/key]`) + `ExposedToolsPanel` picker — mint-or-reuse the scoped key, copy the `.mcp.json` (with the live key embedded, via `utils/clipboard.copyToClipboard`), regenerate/revoke. No new backend/key/endpoint. Copy-once secret: an existing key re-copies the placeholder config and offers "Regenerate & copy" for a fresh live key |

## Endpoints (all under `/api/agents`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/{name}/connector` | `OwnedAgentByName` | Owner status: enabled, allow-list, `has_key`, `key_prefix`, `mcp_url`, snippets (placeholder key) |
| PUT | `/{name}/connector` | `OwnedAgentByName` | Set enable + `exposed_playbooks` (or `expose_all_playbooks` to clear the allow-list) |
| POST | `/{name}/connector/key` | `OwnedAgentByName` | Mint/regenerate the scoped key — **secret returned once**; auto-enables; snippets embed the real key. Atomic delete-old + insert-new (single-active-key invariant) |
| DELETE | `/{name}/connector/key` | `OwnedAgentByName` | Revoke all connector-scoped keys (idempotent) |
| GET | `/{name}/connector/playbooks` | `AuthorizedAgentByName` (accepts a `scope='connector'` key) | Effective exposed playbooks the connector advertises |

## Key + policy

- **Scoped key**: a row in the OSS `mcp_api_keys` table with `scope='connector'`,
  `agent_name` bound. Generated/hashed via `McpKeyOperations` so
  `validate_mcp_api_key` recognizes it. The OSS auth fence fences a connector key
  to exactly `POST /{agent}/chat` + `GET /{agent}/connector/playbooks` — it cannot
  reach owner ops or any other agent.
- **Allow-list** (`enterprise_connectors.exposed_playbooks`, JSON array; NULL ⇒ all
  `user_invocable`): `resolve_exposed_playbooks` drops `user_invocable:false`
  unconditionally (even if explicitly listed); `automation:gated` is passed through
  as advisory metadata (not a filter).

## Schema / migration

`enterprise_connectors` (one row per agent: `enabled`, `exposed_playbooks`,
`created_at`, `updated_at`). The name is **kept from the enterprise era** so an
existing enterprise install adopts its data with zero migration (`CREATE TABLE IF
NOT EXISTS` on the same name — no data copy, no duplicate-table drift). Dual-track:
SQLite `db/migrations.py:enterprise_connectors_table` + Alembic
`0015_enterprise_connectors`; DDL in `db/schema.py`, MetaData in `db/tables.py`;
delete/rename cascade via the `enterprise_connectors` `AGENT_REF`. The enterprise
Alembic `0006_mcp_connector` is neutralized to a no-op (its descendant
`0007` keeps the chain).

## Part B — inline email auth (#848)

An external user on the agent's `agent_sharing` allow-list signs in from their MCP
client with the 6-digit email code and uses the connector **without a pre-minted
key**. Flag-gated `MCP_INLINE_AUTH_ENABLED`, **default OFF**. Requirements: `docs/memory/requirements/mcp.md` §7.6.

```
MCP client (no key in config)
  │  no Authorization header
  ▼
mcp-server authenticate()            server.ts
  │  flag ON → truthy anonymous sentinel {scope:"anonymous", sessionId}
  │  flag OFF → throw (pre-#848 behaviour)
  │  INVALID key → throw either way
  ▼
tools advertised: request_login, verify_login, list_playbooks, run_playbook, ask
  │  (identical before AND after login — NOT because the list is frozen: FastMCP
  │   re-filters live sessions on addTool/removeTool, which the #846 reconciler
  │   fires every ~20s, so a login-keyed gate would flip non-deterministically)
  ▼
request_login({email})               tools/auth.ts
  │  POST /api/internal/mcp-auth/request   (X-Internal-Secret)
  │  backend: known-address check → create_login_code → fire-and-forget email
  │  ALWAYS one constant 202 body; no audit row
  ▼
verify_login({code})                 tools/auth.ts
  │  POST /api/internal/mcp-auth/verify
  │  backend: rate limits → verify_login_code → get_or_create_email_user (role=user, #314)
  │           → resolve_accessible_agents → audit login_success/failed
  │  returns {verified, username, agents[]} — NO credential
  ▼
session upgraded IN PLACE (same object FastMCP holds)
  verifiedEmail / userEmail / userId / agents set; scope STAYS "anonymous"
  ▼
list_playbooks / run_playbook / ask  tools/connector.ts
  │  agent = explicit arg, or the sole available one
  │  POST /api/internal/mcp-auth/{playbooks,chat}
  ▼
backend re-gates EVERY call: email_has_agent_access(agent, email) AND connector enabled
  → uniform 403 otherwise; chat runs through TaskExecutionService (triggered_by="mcp")
```

**Keyless setup (AC6).** With the flag on, the owner shares the agent WITHOUT minting
a key — the collaborator drops in a keyless config and signs in by email. The connector
panel surfaces this ("Share without a key — sign in by email") whenever
`ConnectorStatus.inline_auth_available` is true, alongside the keyed setup; the same
`connector_service` builds both variants (`build_keyless_snippets` → `_client_snippets`
with no key) so they cannot drift. The `.mcp.json` a collaborator pastes is simply the
keyed block minus the `Authorization` header:

```json
{ "mcpServers": { "trinity-<agent>": { "type": "http", "url": "<mcp_url>" } } }
```

Then `request_login("me@example.com")` → `verify_login("123456")` in the client. The
keyless config is offered ONLY when `MCP_INLINE_AUTH_ENABLED` is on — an anonymous MCP
session is rejected otherwise, so a keyless config would not connect.

**Why a session and not a minted key** (§7.6): every other channel binds a verified
email server-side and hands the user nothing. The cost is that a FastMCP session is
per-connection, so a client restart requires signing in again.

**Two invariants worth not breaking:**

- **`scope` stays `"anonymous"` after login.** The session still holds no API key
  and must never satisfy `operatorOnly`. Pinned by `src/inline-auth.test.ts`.
- **`session.agents` is not the authorization boundary.** It exists to default the
  single-agent case and to name alternatives in errors. The backend re-gates every
  call, so a stale or tampered list cannot widen access.

**Enumeration safety.** `request_login` returns one byte-identical body across
known / unknown / malformed / rate-limited / backend-threw, and writes no audit row
— wording, status, latency or an audit entry would each be an oracle for "is this
address registered" (#186). `verify_login` failures are uniform; the three denial
reasons at the data gate (no access / connector disabled / no such agent) share one
403 body (Invariant #8).

**Backend surface** (`routers/mcp_auth.py` → `services/mcp_auth_service.py`; no new
tables, reuses `email_login_codes`). The internal secret authenticates the *caller*,
never the action — which does mean `INTERNAL_API_SECRET` can act as any verified
email, so it stays as sensitive as the rest of the internal surface.

**Deferred:** an explicit "request access to agent X" affordance. The per-call gate
returns a flat 403 rather than writing an `access_requests` row, because that gate
runs per tool call and would otherwise be a spam vector (§7.6).
