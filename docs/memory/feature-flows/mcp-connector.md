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

## Deferred — Part B (email-auth onboarding, #848)

An external user on the agent's `agent_sharing` allow-list authenticates from their
MCP client via the email 6-digit-code flow (`request_login`/`verify_login` MCP
tools) and uses the connector **without a pre-minted key**, seeing only the exposed
playbooks of agents shared with their email. Blocked on #848's open design questions
(persistent key vs transient session; post-login tool visibility; whitelist-only vs
open signup). Not built.
