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

### The session id is the credential (#2035) — deploy over TLS only

"Session, not a minted key" has a consequence the original design did not state: what
identifies the session is the `Mcp-Session-Id` header, and the client resends it on every
request. MCP streamable HTTP is discrete POSTs, so the mcp-server re-authenticates each one
and — for the keyless tier, which has no credential to re-present — resolves the verified
identity from a store keyed by that header (`createAnonymousSessionStore`, `server.ts`).
**For this tier the header therefore IS a bearer credential.** Shipping #848 without that
memo is what made keyless sign-in non-functional (#2035): `verify_login` succeeded and the
next call answered `login_required`.

Operator consequences, in the order they bite:

- **Expose the MCP port over TLS only when `MCP_INLINE_AUTH_ENABLED` is on.** A plain
  header over plain HTTP is sniffable, and whoever reads it holds the signed-in session
  until it expires. Keyed connector clients are no worse off — their key is on the wire
  either way — but the keyless tier turns a routing detail into a secret.
- **It is not written to logs in full.** Trinity's own code logs an independent correlation
  id instead, and the bundled `mcp-proxy` prints the raw id on three paths, so the
  mcp-server truncates it at the console boundary (`log-redaction.ts`). Without that, Vector
  would ship every live keyless session id to `/data/logs` in plaintext.
- **Expiry is the only exit: 30 min idle, 4 h absolute.** There is no `logout` tool, and no
  usable disconnect signal to evict on — the MCP SDK's `close()` only aborts the SSE stream,
  and the DELETE that would end the server-side session is sent solely by the opt-in
  `terminateSession()`, so a client that simply quits leaves the entry to age out.
  Restarting the mcp-server drops every keyless session at once.
- **Clearing the conversation in an MCP client does not log out.** `/clear` in Claude Code
  resets the model's context, not the transport session; the connection — and the signed-in
  identity on it — survives.

What bounds the damage: the backend re-gates **authorization** on every call
(`email_has_agent_access` + connector-enabled), so a stolen session confers the asserted
identity with live permissions, not frozen ones — unshare the agent and the next call 403s.
Only authentication is remembered. Memoization is scoped to the anonymous tier and is
unreachable from the keyed path, which is pinned by a source guard
(`src/inline-auth-scope.test.ts`) rather than by comment: memoizing a keyed session would
let `Mcp-Session-Id` alone stand in for a key that was never presented.

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

### 32.1 A2A Agent Card Endpoint (#737 — Phase 1)
- **Status**: ✅ Implemented (Phase 1)
- **Implements**: Issue #737
- **Description**: Each Trinity agent exposes an A2A-protocol Agent
  Card so external orchestrators (AWS Bedrock, Azure Copilot, Google
  ADK) can discover its identity, skills, and auth requirements
  without knowing Trinity's internal API. A2A is Google's open
  agent-interoperability protocol (https://google.github.io/A2A/).
- **Endpoint**: `GET /api/agents/{name}/a2a/agent-card` — returns a
  card built from the agent's `template.yaml` (`name`, `description`,
  `version`, `skills[]` mapped from `capabilities[]` with
  `use_cases[]` as examples) plus declared
  `securitySchemes.bearerAuth` (Trinity MCP API key) and
  `capabilities.streaming = true`. Auth-gated by `AuthorizedAgentByName`.
- **Protocol version**: `protocolVersion: "0.3.0"` (ent#157). The
  earlier `"1.0"` was a placeholder with no endpoint behind it; the
  card now points at a JSON-RPC server that speaks the v0.3.x method
  set, so the advertised version has to match what answers.
- **Behavior**: card data fetched from the agent-server's
  `/api/template/info`; falls back to Docker labels when the agent
  is stopped or unreachable (never 5xx's). The `url` field points at
  `POST /a2a/{name}` — the JSON-RPC task endpoint (§32.2) — replacing
  the public-chat placeholder it carried before that endpoint existed.
- **Phase 2 (deferred)**: Redis caching of the card; auth-gated
  extended card with internal endpoint URLs + full skill schemas; MCP
  `get_agent_card` tool. (The host-root well-known route and the
  JSON-RPC server this card addresses shipped in §32.2.)

### 32.2 A2A Inbound Server — public card + JSON-RPC/SSE (ent#157)
- **Status**: 🚧 In Progress
- **Implements**: trinity-enterprise#157
- **Description**: The serving half of A2A — §32.1 describes an agent,
  this lets an external orchestrator *task* it. A spec-shaped public
  discovery route plus a JSON-RPC 2.0 task endpoint, so an A2A client
  discovers a Trinity agent and drives it with no Trinity-specific code.
- **FR-1 — Exposure is opt-in, default OFF**: `agent_ownership.a2a_exposed
  INTEGER DEFAULT 0`. Nothing is publicly reachable until an owner
  turns it on. Dual-track migration (SQLite
  `agent_ownership_a2a_exposed` + Alembic `0024`).
- **FR-2 — Public discovery route**: `GET /a2a/{name}/.well-known/agent-card.json`
  (unauthenticated, prefix-less `a2a_server_router`). Non-exposed,
  non-existent, and inaccessible agents return a **uniform 404** so the
  public surface is not an enumeration oracle (Invariant #8).
- **FR-3 — JSON-RPC task endpoint**: `POST /a2a/{name}` — `message/send`,
  `message/stream` (SSE), `tasks/get`, `tasks/cancel`;
  `tasks/resubscribe` returns an explicit unsupported error. Bearer auth
  is a Trinity MCP API key, fail-closed 401 before any envelope parse.
  Protocol errors ride the JSON-RPC envelope at HTTP 200; auth is the
  deliberate exception.
- **FR-4 — Per-caller dedup (Invariant #18)**: a repeated `messageId`
  must not double-execute, but `messageId` is **peer-controlled**,
  auto-generated by SDKs, and the spec only requires uniqueness *per
  client* — so the dedup scope is keyed on `(agent, caller principal)`,
  never the agent alone. An agent-only scope lets one caller's
  `messageId` resolve to another caller's stored snapshot, disclosing
  the agent's response text and silently skipping the second caller's
  task. The principal prefers the MCP key id over the username, because
  agent-scoped keys all resolve to a single owner user.
- **FR-5 — Public route is rate limited**: the discovery route is
  unauthenticated and its URL is published by design, while each hit
  costs a DB read, a live Docker API call, and an HTTP call into the
  container. Per-IP limit (60/min) enforced **before** that work, via
  the shared `services/rate_limiter` and the trusted-proxy-aware client
  IP. The JSON-RPC body is capped before parsing.
- **FR-6 — Cancellation is honest**: `tasks/cancel` reports what
  actually happened — a queued row cancels through the backlog CAS, a
  terminal row returns `TaskNotCancelable`, and a failed terminate is
  surfaced rather than reported as success. Telling a caller "canceled"
  for a task that later drains, runs, and bills is a correctness bug,
  not a cosmetic one (cf. #1082 status-as-projection).
- **FR-7 — Allow-list seam (open-core)**: `services/a2a_gate.py` lets a
  registered provider further restrict *which* caller identities may
  task an exposed agent. OSS registers none → any authenticated
  owner/shared caller is allowed. The gate **fails open** — a provider
  error never blocks an authenticated caller — so it is a restriction
  layered on authentication, not a security boundary. The identity
  compared is the caller's account email (username fallback).
- **Front door**: nginx proxies `/a2a/` to the backend with SSE
  buffering off, so external clients reach Trinity through the same
  door as `/api/` rather than `:8000` directly.
- **Flow**: `docs/memory/feature-flows/a2a-inbound-server.md`

### 32.3 A2A Control over MCP (ent#160)
- **Status**: 🚧 In Progress
- **Implements**: trinity-enterprise#160
- **Description**: Manage A2A exposure, inspect the card, and edit the
  inbound allow-list from an MCP client — so an operator configures
  interop without the web UI. Tools live in `src/mcp-server/src/tools/a2a.ts`
  (Invariant #13: backend router + MCP tool stay in sync).
- **Surface**: read the exposure state, card URL, advertised skills,
  allow-list, and registered outbound endpoints; toggle exposure; add
  and remove inbound identities. Mutating tools are owner-gated **and
  human-only** (`reject_agent_principal`) at the backend.
- **Agent-key gating — corrected (#736)**: an earlier revision of this
  section claimed the MCP layer applied "the same `{self} ∪ permitted`
  gate for agent-scoped keys as the rest of the tool surface". It did
  not: `tools/a2a.ts` contained no `checkAgentAccess` and relied wholly
  on the backend's owner gate. That was doc/code drift, and it was not
  harmless — the backend resolves an agent-scoped key to its OWNER, so
  the **reads** (`get_agent_a2a_config`, `list_a2a_endpoints`) let any
  agent enumerate a *sibling* agent's registered outbound endpoint URLs.
  #736 adds the `{self} ∪ permitted` gate to those two read tools, which
  is what this bullet now describes. The mutating tools need no such
  gate: `reject_agent_principal` at the backend already refuses every
  agent principal outright.
- **Note on the runtime call**: `call_a2a_agent` / `get_a2a_task` are
  **not** part of this management plane — see §32.5. Their MCP-layer gate
  is deliberately **self-only**, not `{self} ∪ permitted`, because the
  backend route is self-only (§32.5 FR-10) and a wider MCP check would be
  inert.
- **UI counterpart**: `components/A2aPanel.vue` on the agent's Sharing
  tab.

### 32.4 A2A Exposed-Skills Filter (ent#180)
- **Status**: 🚧 In Progress
- **Implements**: trinity-enterprise#180
- **Description**: Let an operator choose which of an agent's skills its
  A2A card advertises. §32.1 maps every `template.yaml capabilities[]`
  tag into the card's `skills[]` with no filter, and §32.2's discovery
  route is unauthenticated — so today an exposed agent's full capability
  list is world-readable. This narrows what the outside is told.
- **FR-1 — Disclosure control, NOT an invocation boundary**: the card's
  `skills[]` is advertisement. Inbound `message/send` dispatches
  free-form text via `execute_task(triggered_by="a2a")`; there is no
  per-skill routing, so filtering changes what an orchestrator **sees**,
  never what it may **ask for**. Documented in these words on the config
  surface too — a filter that operators mistake for an invocation gate is
  a control that looks like security and is not. Constraining what an
  external caller can actually reach is a separate concern (the
  `allowed_tools`/guardrails primitives) and is deliberately out of scope.
- **FR-2 — Default is unchanged behaviour**: no configuration ⇒ advertise
  every capability, exactly as §32.1 does today. Exposure is already an
  explicit opt-in (§32.2 FR-1), so an exposed agent's card stays
  byte-identical across the upgrade. A stored **empty list** is distinct
  from *no configuration*: it means "advertise nothing", an explicit
  operator choice, not a default.
- **FR-3 — Both card surfaces agree**: the filter applies to the public
  well-known card and the authenticated per-agent card alike. Two
  surfaces answering "what does this agent do?" differently is a bug —
  the owner's management view of what is *available to select* comes from
  the config surface, not from the card.
- **FR-4 — Stale tags are inert**: a stored selection naming a capability
  the template no longer declares is ignored, never advertised. The
  config outlives any given `template.yaml`, so the template stays the
  source of truth for what exists; the selection only ever subtracts.
- **FR-5 — Filter seam (open-core)**: extends the §32.2 FR-7 seam
  (`services/a2a_gate.py`) rather than adding a module — a registered
  provider answers "which skills may this agent advertise?"; OSS
  registers none, so the card is unfiltered and OSS behaviour is
  unchanged by construction. Consistent with FR-2, a provider error
  **fails open** (advertise all) and logs at WARNING: the seam's
  availability bias, and the honest trade for a control that is
  explicitly not a security boundary — failing closed would silently
  empty a card and break discovery invisibly.
- **Flow**: `docs/memory/feature-flows/a2a-inbound-server.md`

### 32.5 A2A Outbound Calls — `call_a2a_agent` (#736)
- **Status**: 🚧 In Progress
- **Implements**: abilityai/trinity#736 (epic trinity-enterprise#156)
- **Description**: The runtime half of A2A. §32.2 makes a Trinity agent
  *reachable*; this makes it a *caller* — a Trinity agent asks Trinity to
  task an external A2A agent (Google ADK, LangChain, AWS Bedrock, a remote
  Trinity) and gets the result back in the same tool call. Two MCP tools,
  `call_a2a_agent` and `get_a2a_task`; one backend route pair; no
  agent-server involvement (the egress is backend → internet, never
  through the agent container).
- **Open-core**: **OSS-core, by owner ruling** — the parent epic records
  *"Outbound = OSS. A Trinity agent calling out to an external A2A agent
  (#736) stays open-core."* There is therefore **no `requires_entitlement`
  anywhere on the call path**, and the OSS build ships its own target
  registry (FR-2) so the tool is functional, not merely ungated. Recorded
  here so the ruling is never re-inferred from the fact that it merged
  (the ent#326 / ent#384 discipline).

#### FR-1 — The caller never supplies a URL
The tool signature is `call_a2a_agent(agent_name, endpoint, message, …)`,
where `endpoint` is the **name or id of a pre-registered endpoint**, not a
URL. The issue's filed AC1 (`call_a2a_agent(agent_card_url, …)`) is
**rejected**: an agent's parameters are LLM-generated and prompt-injectable,
so a URL parameter turns any document the agent reads into an authenticated,
credentialed, server-side request to an attacker-chosen address — from inside
the platform network, where Redis, the Docker socket and cloud metadata live.
No IP filtering makes that safe, because filtering is a blocklist race (DNS
rebinding, CGNAT, IPv6-mapped forms, redirect chains) while a registry is a
whitelist of things a human deliberately typed. The **cost is stated**: an
agent cannot discover-and-call a novel A2A peer at runtime; an operator
registers it first.

#### FR-2 — Two target sources, one seam; OSS owns a real one
`services/a2a_outbound.py` is a provider seam mirroring `a2a_gate.py`'s
registration shape with **inverted failure semantics**: `a2a_gate` fails
OPEN and its docstring says that is acceptable *because it is not a
security boundary*; this one **is** one, so no provider, a provider that
raises, and a provider that returns a malformed object all **refuse**.
- **OSS provider (shipped)**: admin-managed named endpoints in
  `system_settings`, each credential wrapped in an AES-256-GCM envelope —
  the location Invariant #12 already blesses for
  `elevenlabs_api_key_encrypted`. **No new table, no migration, no Alembic
  revision.** Managed by one admin-only + human-only settings route.
- **Enterprise provider (future)**: a private module may register a provider
  that takes precedence and scope endpoints per agent. OSS ships a working
  source rather than only the seam because a seam with no registered provider
  resolves nothing, i.e. the tool would answer "no targets configured" on every
  install — which is not what "outbound = OSS" can mean.
- Resolution is **platform-scope** in OSS: a named endpoint is available to
  every agent on the instance. Per-agent scoping is the enterprise delta.

#### FR-3 — Every URL is SSRF-validated at CALL time, wherever it came from
Registration validates a URL with `startswith("http://") or
startswith("https://")` and a length cap — no SSRF check, and plain `http://`
is accepted. "It is in the registry" therefore does **not** mean "safe to
fetch". `utils/url_validation.validate_a2a_endpoint_url` runs on every call:
HTTPS only (an `http://` row is refused at *use*, with a message telling the
operator to re-register — never silently upgraded), ≤2048 chars, no userinfo
(refused, never stripped), IDNA/A-label normalised **once** so the parser and
the resolver cannot disagree about which host was approved, and every address
`getaddrinfo` returns must be public — one private/loopback/reserved/
link-local/multicast/unspecified/CGNAT record fails the whole endpoint. DNS
failure is fatal. Refusal messages are fixed strings and **never** contain a
resolved address (an operator-visible message that echoes an internal IP is a
topology oracle).

#### FR-4 — DNS rebinding is closed by connect-time IP pinning, not accepted
The sibling `validate_template_registry_url` records rebinding as an accepted
residual; that is right for a display-only catalog and **wrong here, because
this request carries a credential**. One resolution produces one validated IP;
both hops connect to that IP while presenting the original hostname for `Host`,
SNI **and certificate verification**, so TLS still authenticates the registered
name. Stated trade: pinning one address means a host whose selected A record is
down fails even though its siblings are up.

#### FR-5 — Credentials come from the registry, and registering is a trust decision
The credential is bound to the registry entry, decrypted server-side, and
attached as `Authorization: Bearer` on the RPC POST **only**. The card fetch is
uncredentialed. The issue's AC3 (`A2A_{DOMAIN}_TOKEN` in the calling agent's
`.env`, selected by card domain) is **rejected**: `.env` is agent-writable, so
the agent could plant a credential; domain-keyed selection lets a
remote-controlled string answer "which secret do I send?"; and reading it would
mean `docker exec`ing plaintext into backend memory on every call when Trinity
already has encrypted server-side storage for exactly this.

**Disclosed honestly**: the sanitiser removes the *literal* credential from
anything returned. A cooperating remote that base64s, splits or rot13s it
defeats that, and no programmatic control fixes it. **Registering an endpoint
grants that endpoint the ability to exfiltrate its own credential.** This is
stated on the settings surface and in the user doc, because it reframes
registration as what it actually is — a trust decision about a peer.

#### FR-5a — A reference deletes exactly what it resolves (#2174)
An endpoint reference is an id **or** a name, and nothing enforces uniqueness
*across* those two namespaces: ids are visible in the admin `GET`, so naming one
endpoint after another's id is an ordinary, permitted operation. `remove_endpoint`
filtered out **every** record matching on either field, so one single-target
`DELETE /api/settings/a2a-endpoints/{ref}` could destroy two endpoints — taking a
partner credential the operator may hold no other copy of — while the route
returned one success and the collaterally-deleted endpoint's next call failed
`endpoint_not_found`, which reads as a registration problem rather than a deletion.

Resolution and deletion therefore share ONE predicate and are both
**first-match-wins**: a ref deletes precisely the record it resolves to, and the
delete removes at most one record per call. Refusing an ambiguous ref was the
alternative and is rejected — with no rename path it strands the operator in a
state they can reach and cannot leave, and it leaves the destructive behaviour in
place for every collision already stored.

Additionally, a **new** endpoint may not take an existing endpoint's id as its
name (422), which stops the collision at the source. The guard is create-path only:
an already-stored collision stays editable and removable, or it would strand the
operator in exactly the state it exists to prevent. Note the deliberate asymmetry —
`upsert_endpoint` is update-**by-name** (its shipped contract), while resolve and
remove are by id-or-name, so for a colliding string the update edits the record
*named* that and the delete reaches the id-owning one first. Both are documented;
neither is destructive.

#### FR-4a — Port normalisation happens once (ent#398)
The port of an endpoint authority is read by validation, by connection pinning and
by the card's origin comparison. Each spelled its own normalisation, and they
disagreed on `:0`: the validator coalesced it to the scheme default
(`parsed.port or 443` — `0` is falsy), `_pinned_url` dropped it and therefore
connected to 443, and `_same_origin` compared it literally as port 0. A `:0`
endpoint validated, would have connected correctly, and was then permanently
refused `card_origin_mismatch` against any card declaring the ordinary form —
fail-closed, but permanently broken with a reason code pointing at the wrong thing.

All three now consume `utils.url_validation.effective_port(port, scheme)`: `0` and
absent both mean the scheme default (port 0 is not a connectable destination, and
browsers refuse `:0` rather than dialing it), an explicit port is preserved, and an
unknown scheme yields `None` for the caller to interpret. The default-port
equivalence FR-2 relies on — Trinity's own card emits no explicit port, so
`https://h` and `https://h:443` must be one origin — is preserved and pinned by
test. `_pinned_url` now omits a port equal to the scheme default rather than
spelling it out (same destination, consistent with that equivalence); the `Host`
header still reflects what the operator typed, which is the one place an explicit
`:443` is observable to a peer.

#### FR-4b — Origin comparison normalises IP literals (ent#399)
`_same_origin` compared `urlsplit().hostname` textually and its docstring claimed
otherwise ("IPv6 bracket forms compared after normalisation"). `canonical_host`
leaves a literal untouched — `idna.encode` rejects it and the ASCII fallback
returns it verbatim — so `[2606:4700:4700::1111]` and
`[2606:4700:4700:0:0:0:0:1111]`, one address written two ways, were refused
`card_origin_mismatch`. Fail-closed, but an IPv6-literal endpoint was unusable
whenever the peer's card and the registration spelled the address differently,
which they have no reason to agree on. The docstring asserting a property the code
lacked is the other half of the defect: it is what the next reader builds on.

`utils.url_validation.canonical_origin_host` is now the origin key: an IP literal
is parsed through `ipaddress` and compared in canonical form, a name still goes
through `canonical_host` (UTS-46 IDNA, SV-7), and anything neither path can
canonicalise falls back to the existing textual comparison so an unusual host stays
comparable to itself. A **scope id is part of the key** (`fe80::1%eth0` ≠
`fe80::1%eth1`), and the ambiguous IPv4 spellings (leading zeros, integer forms)
are *refused* by `ipaddress` rather than folded, so the normalisation can never
equate two addresses a resolver would treat differently. The card stays a hint: a
card declaring a different address, port or scheme is still refused.

#### FR-3a — A refusal's reason is set where it is raised (ent#397)
`validate_a2a_endpoint_url` reconstructed its machine-readable `reason` by
substring-matching its own human message — the fragility `A2AEndpointUrlError`'s
docstring warns about. The DNS-failure message interpolates the hostname and
`canonical_host` passes hosts containing spaces, so a host spelled
`an internal address.example` that merely failed to resolve was reported
`endpoint_private_address`. No bypass (both refusals carry the same HTTP status),
but the operator is told the opposite of what happened and a consumer branching
on `reason` branches wrongly.

`_validate_public_https_url` now raises `PublicUrlRefusal(kind, message)` at each
of its refusal sites (`invalid` / `not_https` / `credentials` / `private_address`
/ `dns_failure`), and the A2A wrapper maps kind → reason through one table. The
message is free to be reworded; the code is not free to change meaning.
`PublicUrlRefusal` subclasses `ValueError`, so the template-registry validator —
which lets it propagate — is unchanged in type and text. A `ValueError` raised
without a kind still becomes `endpoint_invalid`: honest about not knowing, rather
than guessing from prose. Messages still never echo a resolved address (the
topology-oracle property), and the DNS message still names the host the operator
typed, which is the point of interpolating it.

#### FR-6 — `message/send` only; no SSE; no fake `stream` parameter
Filed AC4 (stream token chunks back to the calling agent over SSE) is
**rejected**: an MCP tool call is one request and one response — a FastMCP
`execute()` returns a string, so there is no channel for chunks to reach the
calling agent's turn. It would also buy nothing against the primary target:
Trinity's own `message/stream` awaits the whole atomic agent turn and emits
exactly two events. A `stream` parameter is **not accepted at all** — a
parameter that is accepted and silently does not stream is a lie in a schema
agents read.

#### FR-7 — Long remote work returns a handle; no Trinity execution row
A non-terminal remote task returns `state` + `task_id` + `context_id`, and
`get_a2a_task` issues `tasks/get` against the same resolved endpoint. Filed AC6
(Redis `a2a:task:{taskId}` + an `execution_id` for `get_execution_result`) is
**rejected**: `get_execution_result` reads `schedule_executions`, i.e.
executions *of Trinity agents*. Minting a row for an external call would pollute
fleet cost/analytics (EXEC-022, §Overview analytics), capacity accounting, and
the canary invariants that reason about `running` rows (E-01/E-05). No new
`triggered_by` value is created, so `_VALID_TRIGGERS` / `_TRIGGER_BUCKETS` /
`_AUTONOMOUS_TRIGGERS` are correctly untouched — a later revision that mints a
row must update all three.

#### FR-8 — Dedup is best-effort and agent-cooperative, not at-most-once
Wired to `effect_guard` (#1084), the fifth sink, because an outbound A2A call is
an irreversible external effect in the same class as `send_message` /
`call_user` / `share_file`. Invariant #18 does not apply — no execution is
created.
- **Identity is `{endpoint_id, resolved_url, context_id, task_id}` and
  `dedup_label` is a REQUIRED tool parameter.** Keying on the endpoint alone
  (the `send_message` shape) is correct for a notification sink and **wrong for
  a request/response conversation**: a second call to the same endpoint with a
  *different message* would read as a completed replay and return the answer to
  the **first** question, with no error and no log. The message body is
  deliberately **not** in the key (#1084's rule — an LLM-generated body is
  non-deterministic across a re-run and would defeat re-delivery dedup entirely).
- `execution_id` is an agent-supplied parameter and the guard **fails open when
  it is absent**, so this is honest best-effort, not a guarantee. It is the
  fifth sink with that shape, enlarging the debt architecture.md already names
  as a blocking prerequisite for pull-mode default-ON.
- An in-flight duplicate raises → **409**, never a silent skip.

#### FR-9 — Caps, deadlines and a fleet bound
Card fetch ≤256 KiB, RPC response ≤1 MiB, outbound message ≤100 000 chars, text
returned to the agent ≤32 KiB with an explicit truncation marker (the agent's
context window is the real budget). Bodies are read with `httpx.AsyncClient` +
`aiter_raw()` under a running wire-byte total; any `Content-Encoding` other than
`identity` is **refused, not decoded** (measured: 199 KiB gzip → ~458 MB decoded
allocation); a 3xx is a **failure, not a hop**, on both hops; `trust_env=False`
so `HTTP_PROXY`/`HTTPS_PROXY` cannot make the validated target irrelevant (the
CA env vars are re-honoured explicitly, since `trust_env=False` would otherwise
silently drop `SSL_CERT_FILE`). The RPC timeout is **30 s**, well below the MCP
client's own 30–60 s gateway abort, plus a **total wall-clock deadline** —
httpx's `read` timeout is per-read, so a trickle-feeding tarpit resets it
forever and stays under the byte cap. DNS resolution runs off the event loop
(`asyncio.to_thread`): a synchronous `getaddrinfo` on a per-call agent path
freezes an entire worker's loop, which no per-agent rate limit bounds. Bounds
are **per-agent 30/60 s AND a fleet-wide Redis key** — a per-process semaphore
would be per-worker (prod runs `--workers 2`) and blocking on one recreates the
coroutine hold it prevents.
**These constants are deliberately not settings-backed** — do not "promote" them.

#### FR-10 — Authorization: use, not grant
MCP advertisement is the `operatorOnly` **allowlist** (`{user, agent, system}`),
so connector and anonymous sessions cannot see or call the tools. The backend
route is `AuthorizedAgentByName` **plus an agent-scoped self-check** — an agent
key may call only **as itself**. Note precisely what that buys under the OSS
provider, because the obvious reading is wrong: endpoints there are
**platform-scope**, so there is no "own" versus "neighbour" credential to
protect — every agent may name every registered endpoint. What the self-check
protects is **attribution**: the rate-limit key, the audit row and the
`agent_activities` row all name the agent that actually spent the call, and a
sibling cannot launder its egress through a neighbour's name. It also holds the
line for a future per-agent provider, where the obvious reading becomes the
literal one. `reject_agent_principal`
is deliberately **not** used: this is a *use* of a capability an admin already
granted by registering the endpoint, not a *grant* (the Invariant #8 line).
Registration itself stays admin + human-only. The MCP-layer check is **self-only
for `scope === "agent"`**, matching the backend exactly — a `{self} ∪ permitted`
check there would deny a strict subset of what the backend denies, i.e. block
nothing while costing a `getPermittedAgents` round-trip per call.

#### FR-11 — Kill switch, default OFF
`A2A_OUTBOUND_ENABLED` resolves `system_settings` row → env → **OFF**, the
brain-orb shape, so an admin can flip it with no restart and a compose gap can
never make it unreachable. Both routes **404** when off (Trinity's "capability
not present" answer), and `a2a_outbound_available` is surfaced in
`GET /api/settings/feature-flags`. The env leg is wired into **both** compose
files and `.env.example` in the same change — six recorded recurrences of a knob
shipping inert say that is not optional. This is the platform's first
backend-executed, credentialed, agent-triggerable outbound fetcher; every
comparable surface (`DISPATCH_ASYNC`, `CANARY_ENABLED`, `VOIP_ENABLED`,
`MCP_INLINE_AUTH_ENABLED`, `BRAIN_ORB_*`) ships default-OFF.

**Degradation under infrastructure loss, stated rather than emergent.** The two
bounds on this path do **not** share a failure domain, so the "lose Redis, lose
both" reading is wrong in both halves. `rate_limiter` fails *soft*, not open: a
Redis outage drops it to a bounded per-worker in-process sliding window
(`_check_inprocess`), so the limit survives and only its scope narrows from
fleet-wide to per-worker — `--workers 2` means an effective 2x, not unbounded.
`idempotency_service` is not on Redis at all: `effect_guard` claims rows in the
`idempotency_keys` **table**, so dedup is unaffected by a Redis outage and fails
open only when a DB write fails, at which point the platform is already down.
The genuine fail-open on this path is the one FR-8 names — `execution_id` is an
agent-supplied parameter and the guard fails open when it is absent — and that,
not a Redis outage, is where the residual lives. The kill switch remains the
control an operator reaches for, but it is not compensating for a bound that
disappears.

#### FR-12 — Dialect is card-driven, defaulting to v0.3
The issue's *"Target v1.0 only"* is **rejected with evidence**: Trinity's own
card pins `protocolVersion: 0.3.0` and its server dispatches slash method names
(`message/send`, `tasks/get`), so a v1.0-only client **cannot talk to Trinity**
— and #738 (Trinity-to-Trinity federation), the primary consumer, is downstream
of this issue. An absent or unparseable version is treated as v0.3, which is the
spec's own back-compat rule and what makes federation work with zero
configuration. A `1.x` card is **refused** (`unsupported_protocol_version`) in
this MVP rather than guessed at: no v1.0 peer exists to test against, and FR-6
already used "untestable ⇒ do not ship it" to reject SSE. The dialect table is
documented in `services/a2a_protocol.py` so the arm is one line when a peer
exists. The negotiated dialect and the resolved RPC target are cached briefly
**per registered endpoint** — keyed on the registered URL, never on the origin,
because one host can carry several separately-registered endpoints and those are
different trust relationships with different credentials. The cache is read by
`get_a2a_task` only: a poll would otherwise pay a second full egress just to
re-read one field, while `call_a2a_agent` deliberately re-reads the card every
time so the same-origin pin is re-evaluated against fresh peer state on every
credentialed send.

#### FR-13 — The card is a hint, not an authority
ent#159 (signed cards) is `status-blocked`, and #736 does not wait for it —
because the plan removes the card's authority instead of trying to verify it. A
signature scheme whose own scope is "validate when signed, warn when unsigned"
cannot be the boundary for a credentialed fetch: an attacker simply does not
sign.
- The card's declared `url` must be **same-origin** with the registered
  endpoint (scheme + host + port, default-port-equivalent, IDNA-normalised,
  trailing dot stripped, IPv6 bracket forms equal). Trinity's own card emits no
  explicit port, so getting default-port equivalence wrong would make Trinity
  unreachable by its own rule.
- `securitySchemes` **never** selects the credential. The card cannot cause a
  different credential to be chosen, nor a credential to be attached to an
  unregistered origin.
- **Card-vs-RPC normalisation**: the card is always derived from
  `{scheme}://{netloc}/.well-known/agent-card.json`. If the registered URL
  carries a path, it is accepted as the RPC target **only** when the card's
  declared `url` matches it exactly; ambiguity is refused with a named error.
- An unreachable card therefore blocks the RPC (the same-origin pin depends on
  it) — a card outage takes a healthy endpoint down, which is the fail-closed
  direction and is recorded, not hidden.
- Errors ride in the JSON-RPC body on **HTTP 200** (Trinity's own server does
  this), so the client parses the body for `error` even on 200 → **502**,
  `success: false`. A status-only check would read every remote failure as a
  success.
- **Flow**: `docs/memory/feature-flows/a2a-outbound-call.md`

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
`trinity-docs-mcp`) that exposes the public Trinity Docs Q&A service
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
