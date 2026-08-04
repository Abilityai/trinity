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

---

## Agent MCP Key — Detection, Self-Heal & Rotation (#1854)

- **Status**: ✅ Implemented
- **GitHub Issue**: #1854 (Parts 1 + the container-truth half of Part 2)
- **Area**: MCP key scope + per-agent credential lifecycle. The auth-fence primitive it
  adds (`reject_non_interactive_principal`) is cross-linked from
  [auth.md](auth.md) but owned here.

### Problem

Nothing validated that an agent container authenticates to the Trinity MCP server with
**its own** `scope='agent'` key. If the `trinity` entry in an agent's `.mcp.json` carries a
**user-scoped** key, the platform accepts it and the agent operates with the *owner's*
identity: `list_agents` returns the owner's whole accessible set and the `agent_permissions`
matrix is silently bypassed (both gates are `scope === "agent"`-conditional and live in the
MCP server). No surface showed which key an agent actually presents, and the platform had no
way to mint or rotate an agent-scoped key — the only recovery was hand-editing a protected
file inside the container.

The agent server re-injects the `trinity` entry from env on **every** start, but the
injection early-returns unless **both** `TRINITY_MCP_URL` **and** `TRINITY_MCP_API_KEY` are
present — and the creation-time mint is `try/except`-swallowed, so a failed mint drops both
and the self-heal never runs. Three live drift causes therefore exist: key absent from env,
container never restarted, and a *second* Trinity-pointing entry under a non-`trinity`
server name (which the injection never touches and which no credential rotation can fix).

### FR-1 — Detect: container config-truth probe

- `POST /api/agents/{name}/mcp-key/verify` asks the **running container** what it is
  configured with, rather than reporting what the platform believes.
- One `docker exec` (`execute_command_in_container`, the established primitive) runs a
  base64-injected Python script that reads `~/.mcp.json`, extracts each server entry's
  bearer token, and emits **only `sha256(token)`** — the token itself, and the file body,
  never cross the container boundary. Server names are truncated/sanitised.
- The digest is matched against `mcp_api_keys.key_hash` (plain unsalted SHA-256), yielding a
  per-entry verdict: `ok` · `foreign_user_key` ("this agent authenticates as user X — the
  permissions matrix is not in effect") · `foreign_agent_key` · `unknown_key` ·
  `not_configured` · `shadow_entry` (a second Trinity-pointing entry under a non-`trinity`
  name) · `unavailable` (stopped container / exec failure — degrade, never 500).
- Deliberately a separate explicit route, not folded into the GET: a `docker exec` per panel
  load would be slow. The panel calls it on mount when the agent is running.

### FR-2 — Self-heal: start-time drift predicate

- `check_agent_mcp_key_matches(container, agent_name)` joins the eight existing
  `check_*_matches` predicates in `start_agent_internal`. Drift when `TRINITY_MCP_API_KEY`
  **or** `TRINITY_MCP_URL` is absent from the container env, or when `sha256(env value)`
  matches no **active** `scope='agent'` row for the agent.
- On drift the recreate mints a fresh key and bakes **both** `TRINITY_MCP_API_KEY` and
  `TRINITY_MCP_URL` (plus `TRINITY_BACKEND_URL`) so the agent-server injection actually
  runs. A successful mint satisfies the predicate on the next evaluation, so the recreate
  converges in one pass; starts are manual, never timed, so a failed mint costs at most one
  extra recreate per start.
- `trinity-system` and ephemeral ghosts are **exempt** (the system key is `scope='system'` —
  minting an agent-scoped replacement is an irreversible privilege downgrade; ghosts are
  volume-less and "never recreate").
- The heal takes the **same** `agent:mcp_key_regen:{name}` lock as FR-3 and does **nothing**
  when it cannot hold it. It runs the identical capture→mint→DELETE sequence, and there is no
  per-agent start lock, so two concurrent starts both observe the drift; if the second
  captures after the first's mint but before its delete, it removes the key the first is
  about to bake in and the surviving container 401s all four readers. A mint taken outside
  the lock can still be captured-and-deleted by the holder, so the three steps are atomic as
  a unit. Failure is **silent and inert** here (nothing minted, nothing deleted, the recreate
  proceeds on the existing env, the next start retries) — the opposite of FR-3's loud
  503/409, because nobody asked for a heal.
- The heal writes an `agent_key_self_heal` audit row (metadata only, actor `system`): it
  mints and deletes credentials fleet-wide with no human in the loop, and an INFO log is not
  an audit trail for a feature premised on the platform not knowing what its agents
  authenticate with.
- The env plaintext is hashed and discarded — never logged.

### FR-3 — Rotate: owner-visible key + deliberate regeneration

- `GET /api/agents/{name}/mcp-key` → `{exists, key_id, key_prefix, scope, created_at,
  last_used_at, usage_count, health, health_detail}`. Never the secret.
- `POST /api/agents/{name}/mcp-key/regenerate` → mint → reconcile → deliver → delete
  superseded, in that order, returning **metadata only — no plaintext**. The agent-key
  plaintext has never been exposed over HTTP and nobody outside the container has any use
  for it; returning it would create a credential-exfiltration primitive on an
  owner-reachable route.
- Ordering and concurrency:
  1. refuse `trinity-system` / ephemeral ghosts (**409**), before any mutation;
  2. acquire `agent:mcp_key_regen:{name}` — **fail-CLOSED**: Redis unavailable ⇒ **503**
     (two interleaved rotations under a failed-open lock end at "container holds K1, the
     only active row is K2", permanently 401-ing the heartbeat, result callback, pull
     worker and MCP client, with the surviving plaintext unrecoverable);
  3. **capture** the active `scope='agent'` id set *before* the mint;
  4. mint (`is_active=1` set explicitly);
  5. reconcile `spawned_by_key_id` (FR-4) — before delivery;
  6. deliver — **running** agent: `clear_agent_breakers` then container recreate with an
     `env_overrides` payload carrying key + URL + backend URL; **stopped** agent: DB-only,
     no recreate (a recreate would silently start a deliberately-stopped, possibly
     quarantined agent — the drift predicate bakes the key on its next start);
  7. delete the **captured** superseded ids (not "everything except the new id" — there is
     no per-agent start lock, so a concurrent `recreate_missing_container` mint must not
     become collateral damage);
  8. audit; return metadata.
- Superseded keys are **DELETEd, not deactivated**: `recover_agent_ownership` reactivates
  every inactive per-agent row, so a deactivated rotated-out key would come back alive after
  a soft-delete/recover cycle — rotation would not be durable.
- Deletion is `scope='agent'`-only. The existing `deactivate_agent_mcp_keys` /
  `set_agent_keys_active` helpers span `('agent','connector')` and must **not** be reused —
  they would silently revoke the owner's MCP **connector** key.
- 409-adoption post-condition: `recreate_container_with_updated_config` can adopt a
  container someone else created on a name conflict. After the recreate the container is
  reloaded and its `TRINITY_MCP_API_KEY` compared **in full, constant-time** against the
  plaintext just minted; if it differs, **nothing is deleted** and the call fails. Not a
  `key_prefix` comparison — the prefix is `api_key[:20]` and its first 12 characters are the
  constant `trinity_mcp_`, so that would put an eight-character assertion between a
  stranger's container and the DELETE of every superseded key.
- Honest failure semantics: the old container is stopped and removed *before* the
  replacement is created, so a post-removal failure leaves the agent with no container. The
  response says exactly that ("the container was replaced and failed to start; press Start
  to rebuild") and claims no continuity; superseded keys are left in place so the drift
  predicate / `recreate_missing_container` heal on the next start.

### FR-4 — `spawned_by_key_id` reconcile (idempotent)

`enforce_agent_spawn_scope` 403s unless `get_agent_mcp_api_key(parent).id ==
child.spawned_by_key_id`, so rotation would silently and unrecoverably sever parenthood for
every child. `reconcile_spawn_key_id(agent, current_id)` re-points children with
`spawned_by_agent = :agent AND spawned_by_key_id IS NOT NULL AND spawned_by_key_id !=
:current_id`. Keyed on `!= current`, **not** `= old_id`: `get_agent_mcp_api_key` is
`ORDER BY created_at DESC LIMIT 1`, so an `= old_id` form is a no-op the instant the mint
commits, and children stranded by an earlier crashed rotation could never be repaired. Runs
*before* delivery — otherwise a 403 window spans the whole recreate and a crash mid-flight
makes it permanent. The gate itself is **not** relaxed (that would widen a security
boundary whose purpose is a precise single-identity match); the predicate is scoped to rows
whose provenance already names this parent, so it cannot grant parenthood over an agent this
parent never spawned.

### FR-5 — Auth

- Path auth is `OwnedAgentByName` (uniform 404, Invariant #8/#186 — no 404-then-403 split).
- **Allowlist, not denylist**: `User.mcp_scope` is populated for every MCP-key principal and
  `reject_non_interactive_principal` 403s whenever it is not `None`. A two-item denylist
  (`reject_agent_principal` + `_reject_connector_principal`) leaves `scope='system'` walking
  through both (`agent_name` and `connector_agent` are both `None` for it) — and
  `can_user_share_agent` returns `True` fleet-wide on an admin-owned install. `scope` is
  free-text with no CHECK constraint, so only an allowlist is fail-closed against a sixth
  scope.
- Rate-limited per agent and per actor (`services/rate_limiter.enforce`) — for an admin,
  ownership is fleet-wide, so an unthrottled loop is a scripted fleet-wide
  container-recreate storm.
- Audited (`AuditEventType.MCP_OPERATION`, success **and** failure) with
  `{new_key_id, new_key_prefix, superseded_key_ids, delivery, children_repointed}` plus the
  actor's `mcp_key_id`/`mcp_scope`. **Never** the plaintext, the key hash, `env_vars`, the
  probe output, or a raw exception string — `audit_log` is append-only with a 365-day
  no-delete trigger and an unsanitised `details` column, so anything written there is
  permanent.

### FR-6 — Health signal

| State | Predicate | Tone |
|---|---|---|
| `missing` | no active `scope='agent'` row | Warning |
| `env_absent` | the container env has no `TRINITY_MCP_API_KEY` / `TRINITY_MCP_URL` | Warning |
| `env_mismatch` | the env key matches no active `scope='agent'` row for this agent | Warning |
| `never_used` | row exists, `last_used_at IS NULL` | Neutral (Warning when corroborated) |
| `stale` | `last_used_at` materially older than the agent's most recent execution | Warning |
| `active` | recently used | Info |

The two `env_*` states **outrank** every usage-derived state: a key row that exists and was
used last week says nothing about what the container runs with *now*, and reporting `active`
over an env with no key at all is the green-during-the-incident failure this feature exists to
remove. They cost a Docker **inspect**, not the FR-1 docker **exec**, so the panel is honest on
load; the match verdict is delegated to `check_agent_mcp_key_matches` so the panel and the
start-time recreate decision cannot disagree; and any error falls back to the usage-derived
state rather than claiming unobserved drift.

`stale` is load-bearing: the motivating incident's signature is literally *"the agent-scoped
key sat unused for months"* — non-NULL but old — which a binary used/unused predicate renders
as green. `last_used_at` is a genuine signal because the high-frequency agent paths
(heartbeat, result callback, internal) deliberately pass `track_usage=False`, so the field
tracks real MCP tool use.

Wording is **discriminating, not quiet**: bare `never_used` with no corroboration renders
neutral (a legitimately non-collaborating agent must not be accused), but `never_used`/`stale`
**plus** executed turns **plus** a non-`ok` FR-1 verdict renders as a warning.
`trinity-system` is exempt from the health status entirely (its key is `scope='system'`, so
it would report a permanent false `missing` on the platform orchestrator).

### FR-7 — Adjacent principal guards (in blast radius)

`db.revoke_mcp_api_key` / `db.delete_mcp_api_key` skip the ownership check entirely for
admins, so on a default admin-owned install *any* agent-scoped key could delete **every** MCP
key in the instance — a one-request fleet-wide auth wipe. `POST /connector/key` likewise
returns a sibling's plaintext to an agent principal. All three now run
`reject_agent_principal` + `_reject_connector_principal`.

### Still exploitable after this change (stated plainly)

- An agent whose `.mcp.json` carries a user-scoped key still authenticates as the owner and
  still bypasses `agent_permissions`. This **detects and repairs**; it does not **prevent**.
- The foreign user key itself is not revoked by rotation — FR-1 surfaces *which* key row the
  container presents so the owner can revoke it in Settings → MCP Keys.
- A compromised agent presenting a sibling's key is still undetectable at request time
  (needs request-origin attribution, deferred).

### Deferred

- **Part 2b — request-origin attribution.** No reliable "this request came from agent
  container X" signal reaches the backend on the MCP path (the MCP server forwards no origin
  marker, and client IP cannot substitute: port 8080 is published on all interfaces and the
  frontend also lives on `trinity-agent-network`). Best candidate is the #1159 derived
  `X-Trinity-Agent-Token`. Explicitly **not** the "unpublished second listener" trick — that
  is a self-declared origin, bypassed by editing the same `.mcp.json` line above the header.
- **Part 3 — enforcement flag** (default OFF), strictly downstream of 2b. Must be an
  explicit **allowlist over all five scopes** (`user`, `agent`, `system`, `connector`,
  `portal_delegate`) — "reject non-agent" breaks the system agent, the connector and
  portal_delegate.
- **`.mcp.json` re-clobber by git sync**: gitignore does not untrack an already-committed
  file, so an auto-sync `git pull` can restore a bad `.mcp.json` after startup.
  `git rm --cached` on the repair path is a follow-up.

**Schema**: no change — `agent_name`, `scope` and `spawned_by_key_id` all exist, health is
derived, and `User.mcp_scope` is a Pydantic field, not a column.

**Flow**: `docs/memory/feature-flows/agent-mcp-key.md`
