# Trinity Architecture — Auth, authorization, credentials, container security

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/dependencies.py`, `src/backend/error_handlers.py`, `src/backend/services/credential_encryption.py`, `src/backend/services/secret_settings.py`, `src/backend/services/credential_paths.py`, `src/backend/utils/url_validation.py`, `src/backend/utils/safe_yaml.py`, `docker-compose.yml`, `docker-compose.prod.yml`
>
> **Read this before changing the paths above**: `require_admin` and `assert_admin` reject agent principals themselves. An agent-scoped MCP key resolves to its owner CARRYING the owner's role, so on a default admin-owned install any agent's injected key satisfied every admin gate. That was five separate incidents (trinity-ops-agent#232, #1644, #1816, ent#236, ent#293) before the gate was fixed rather than the endpoints; never write `require_role("admin")`, which is a third spelling that lets agent keys through.
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

---

## Authentication & Authorization Architecture

### 1. User Authentication (Human → Platform)

| Mode | Flow | Token |
|------|------|-------|
| **Email** (primary) | Email → 6-digit code → `POST /api/auth/email/verify` | JWT with `mode: "email"` |
| **Admin** (secondary) | Password → `POST /api/token` | JWT with `mode: "admin"` |

- Email whitelist controls who can login via email; admin login always available for 'admin'.
- **JWT revocation on logout** (#187): every access token carries a random `jti`; `POST /api/auth/logout` writes `jwt:revoked:{jti}` to Redis with a TTL equal to the token's remaining life, and `get_current_user` / `decode_token` (WS) / `/api/auth/validate` (nginx) reject a revoked `jti`. Closes the "exfiltrated 7-day token survives logout" gap (pentest 3.3.4). Fail-open (Redis down or a legacy no-`jti` token → not revoked), so the check can never lock out a valid session; backend restart still rotates `SECRET_KEY` and invalidates everything. Token-lifetime reduction + refresh tokens deferred (separate issue).
- **4-tier role hierarchy** (ROLE-001): `user` < `operator` < `creator` < `admin`. Agent creation requires `creator`+. Enforced via `require_role()` in `dependencies.py`.
- **Whitelist-driven role on first login** (#314): new email users inherit the `default_role` on their `email_whitelist` row (fallback `user`). Callsites pass explicit intent — `/share` and access-request approvals → `user` (chat-only grant); public `/api/access/request` self-signup → `user`; admin whitelist UI → caller-specified. Owners promote collaborators explicitly via `PUT /api/users/{username}/role`. Closes a privilege escalation where any access grant silently promoted the recipient to `creator`.
- **Public self-signup is default-OFF** (trinity-enterprise#10): the unauthenticated `POST /api/access/request` returns **403** unless an operator opts in via `PUBLIC_ACCESS_REQUESTS_ENABLED` (env) or the `public_access_requests_enabled` system setting. When off it never auto-whitelists, so the email whitelist stays authoritative against self-enrollment. Login-code requests for already-whitelisted emails are unaffected.

### 2. MCP API Keys (User → MCP Server)

Created via UI `/settings?tab=mcp-keys`; format `trinity_mcp_{random}` (44 chars); SHA-256 hash stored in SQLite; sent as `Authorization: Bearer trinity_mcp_...`; MCP server validates via `POST /api/mcp/validate`.

Client config (`.mcp.json`):
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": { "Authorization": "Bearer trinity_mcp_..." }
    }
  }
}
```

### 3. MCP Server → Backend (Key Passthrough)

The FastMCP `authenticate` callback validates the user's key via the backend and returns the `McpAuthContext`; MCP tools then call the backend API with the user's own key — the backend's `get_current_user()` accepts JWT OR MCP API key. In production (`MCP_REQUIRE_API_KEY=true`) the MCP server holds NO admin credentials.

### 4. Agent MCP Keys (Agent → Trinity MCP)

Each agent gets an auto-generated agent-scoped key (`scope='agent'`, `agent_name` stored for permission checks), injected as `TRINITY_MCP_API_KEY` env var and auto-added to the agent's `.mcp.json` pointing at the internal URL `http://mcp-server:8080/mcp`.

**Regenerable + self-healing (#1854).** The injection the agent server runs on every start early-returns unless **both** `TRINITY_MCP_URL` and `TRINITY_MCP_API_KEY` are set, and the creation-time mint is `try/except`-swallowed and sets them together — so a failed mint leaves an agent that never self-heals, and a Trinity-pointing `.mcp.json` entry under any name other than the literal `trinity` is never touched at all. Three surfaces close that: a **container config-truth probe** (`POST /api/agents/{name}/mcp-key/verify` — one `docker exec` returning ONLY `sha256(bearer token)` per entry, matched against `mcp_api_keys.key_hash`), a **start-time drift predicate** (`check_agent_mcp_key_matches`, the ninth `check_*_matches` in `start_agent_internal`; exempts `trinity-system` and ephemeral ghosts; fail-safe on error), and **owner-driven rotation** (`POST .../mcp-key/regenerate` — mint → reconcile `spawned_by_key_id` → deliver → DELETE the *captured* superseded ids; fail-closed lock; DB-only for a stopped agent; **returns no plaintext**). The self-heal takes the **same** per-agent lock as rotation and does nothing at all without it — the two run the identical capture→mint→DELETE sequence and there is no per-agent start lock, so an unserialised heal can delete the key a concurrent heal is about to bake in; a skipped pass mutates nothing and the next start retries. Delivery rides a new `env_overrides` kwarg on `recreate_container_with_updated_config`, applied last. No `docker/base-image/**` change — it works on every deployed agent with no rebuild. See [agent-mcp-key.md](feature-flows/agent-mcp-key.md).

### 5. Agent-to-Agent Permissions

Enforced at the **MCP server layer** (`src/mcp-server/src/tools/`), not the backend REST API: `list_agents` returns only permitted agents + self; `chat_with_agent` blocks non-permitted targets. The backend resolves agent-scoped keys to the owner user and applies standard ownership/sharing checks (`current_user.agent_name` is used only by notifications and event subscriptions). **Restrictive default**: new agents start with zero permissions; grants are explicit via the Permissions tab (`agent_permissions` table).

**Residual, stated plainly (#1854).** The enforcement is `scope === "agent"`-conditional, so it holds only while the container's `.mcp.json` actually carries the agent's own key. A **user-scoped** key pasted into that file authenticates as the owner and bypasses the matrix silently — the platform accepts it. #1854 **detects** this (the config-truth probe above, verdicts `foreign_user_key` / `foreign_agent_key` / `shadow_entry`) and **repairs** it (drift predicate + rotation); it does not **prevent** it. Prevention needs a trustworthy request-origin signal, which does not exist on the MCP path today — the MCP server forwards no origin marker, and client IP cannot substitute (port 8080 is published on all interfaces and the frontend also sits on `trinity-agent-network`). Deferred as Part 2b/3; if built it must be an explicit allowlist over **all five** scopes, since "reject non-agent" breaks the system agent, the connector and portal_delegate.

### 6. System Agent

`trinity-system` has `scope='system'`: bypasses all permission checks, can call any agent/tool, cannot be deleted via API. Purpose: platform operations (health, costs, fleet management).

| Scope | MCP Enforcement | Backend Enforcement |
|-------|-----------------|---------------------|
| `user` | Owner/admin/shared checks | Owner/admin/shared checks |
| `agent` | Explicit permission list (`agent_permissions`) | Resolves to owner user; ownership/sharing checks only |
| `system` | **Bypasses all checks** | Resolves to owner user (system agent owner) |
| `connector` | Consumption-only, bound to ONE agent (ent#46): connector-only tool set (`list_playbooks` / `run_playbook` / `ask`), operator tools hidden via `connectorOnly`; the playbook allow-list is authoritative | `_enforce_connector_scope` at the single auth entry point **fences it to two routes** — its bound agent's `/chat` + `/connector/playbooks`; every other path 403s (ent#46 → #118) |
| `anonymous` | #848 keyless pre-login tier, **only when `MCP_INLINE_AUTH_ENABLED`**: sees `request_login`/`verify_login` + the connector tools, which refuse to act until an email is verified. Never satisfies `operatorOnly` — `scope` stays `anonymous` after login | Holds no *key*, so it never authenticates to the backend directly; the MCP server relays over `/api/internal/mcp-auth/*` and the backend re-gates every call on `db.email_has_agent_access(agent, email)`. It is **not credential-free**, though: with nothing on the wire to re-present, the verified identity is resolved per request from a store keyed on `Mcp-Session-Id`, which makes that header **bearer-equivalent for this tier** — serve the MCP port over TLS only, keep it out of logs, and note the 30 min idle / 4 h absolute expiry is the only thing that ends a session (#2035) |
| `portal_delegate` | n/a (not an MCP tool principal) | **Fenced to a single route** — may only exchange an asserted end-user email for a portal session; every other path 403s (ent#163) |
| `ops` | n/a (not an MCP tool principal — `OPERATOR_SCOPES` is an allowlist, so it is excluded by construction) | **Fenced to a read-only route allowlist** — fleet health, telemetry, roster/capacity, execution reads and the live log relay, subscription reads. Every write 403s. Admin-minted and human-only to mint. Additionally kept OUT of `ADMIN_GATE_SCOPES`, so an admin-gated ops route must opt in with `assert_admin(..., allow_scopes={"ops"})` — or `Depends(require_admin_allowing("ops"))`, the `Depends` spelling added in #2389 because `require_admin` took no `allow_scopes` and an allowlisted route gated that way was dead to ops keys **with no opt-in available**. The opt-in makes the grant **per route** (a new ops route is inaccessible until added), and it is an **ADDITIONAL gate, never a substitute one**: the scope is admitted and `role == "admin"` still runs afterwards, so an ops key is a *narrowing* of its owner, not a *decoupling* from them. The tier therefore does **not** survive its owner being offboarded — demotion 403s it, and `get_current_user` rejects a suspended owner (#995) one layer up regardless; an earlier revision of this row claimed otherwise, which an operator would have acted on. Dropping the role check for the opted-in scope was refused: it would make the bounded tier harder to revoke than the unbounded `user`-scoped key it exists to displace, while still not delivering the claim. Mint ops keys under a service admin account that is not offboarded with people (#2323/#2389). **The fence and the gate must agree**: `GET /api/subscriptions/{id}/usage` shipped admitted by the fence and refused by its own bare `assert_admin`, so the subscription-pressure read the fence was measured for could not work — a guard asserting only the fence half gave false assurance. A live-handler scan now reds when any allowlisted route calls `assert_admin` without the opt-in, and a sibling scan reds on bare `require_admin` — closing the *shape* rather than the instance (#2389) |

Only `user`/`agent`/`system` are the credentialed **operator** tier — the allow-list `OPERATOR_SCOPES` in `server.ts`. Every other scope is deliberately outside it, and widening that set is a deliberate edit pinned by `tool-visibility.test.ts`: the gate was previously a `!== "connector"` deny-check, which admitted every scope it had not heard of — including a null auth context, where FastMCP skips `canAccess` filtering entirely (#848). `portal_delegate` (ent#163) is the clearest illustration of why the deny-check shape was untenable: it is not an MCP tool principal at all, yet a `!== "connector"` gate would have advertised it every operator tool the day it was introduced.

**`/ws/events` is a second auth entry point, and it is allowlisted too (#2389).** That handler
calls `db.validate_mcp_api_key` itself and never runs `get_current_user`, so **none** of the
fences above reach it — the ops fence's own "enforced at the single auth entry point" claim was
false for exactly one surface, and it is the broad one: the stream carries fleet-wide
`agent_activity` and execution events scoped by the **owner's** accessible agents, which for an
admin owner is everything. `dependencies.WS_EVENT_STREAM_SCOPES = {None, "user", "agent",
"system"}` gates it (close code 4003), so `ops` cannot read outside its allowlist, and
`connector`/`portal_delegate` — fenced to one or two routes everywhere else — lose a hole that
predates #2323 rather than keeping it out of politeness. An unknown future scope is refused by
construction, matching the allowlist rule below. `agent` is admitted but **not wholesale**: the
same absence of `get_current_user` also skips `_enforce_ephemeral_key_fence`, whose entire point
is that a ghost's key on an untrusted workspace must not be a fleet skeleton key — so the gate
takes the key's `agent_name` and refuses an `is_ephemeral` row, using the fence's own predicate.
That sub-check fails **CLOSED**, deliberately inverting the ephemeral fence's fail-open: that
fence guards heartbeats and result callbacks where a DB blip must not take the fleet down, while
losing this stream costs an observability client a reconnect. Presence of the `is_ephemeral` key
is what makes the answer real — `get_agent_ephemeral_info` coalesces the column for every live
row, so a dict without it is no row at all (or a stand-in), and a bare `.get()` would map that
onto the same falsy value a genuine durable agent gives.

The same reasoning governs the backend side. `scope` is a free-text column with **no CHECK constraint**, so this table is a snapshot of live values, not a closed set. Guards over it must be **allowlists**: `reject_agent_principal` + `_reject_connector_principal` between them cover only `agent` and `connector`, and a `scope='system'` key sets neither field, so both are no-ops for it while it still resolves to the key owner carrying the owner's role. `reject_non_interactive_principal` (#1854) inverts this — it passes only when `User.mcp_scope is None`, i.e. the caller came through the JWT branch — and gates the credential-lifecycle routes (agent MCP-key read/verify/rotate). Key revoke/delete (`/api/mcp/keys/*`) and the connector key mint additionally run the agent+connector pair, because `db.revoke_mcp_api_key`/`delete_mcp_api_key` skip the ownership check entirely for admins and an agent key inherits its owner's role (#1854).

### 7. External Credentials (Agent → External Services)

CRED-002 file-injection model (Invariant #12): `.env` (KEY=VALUE source of truth) + `.mcp.json` edited directly; encrypted backup `.credentials.enc` (AES-256-GCM, safe for git); auto-import on startup if `.credentials.enc` exists without `.env`. Flow: Quick Inject writes `.env` → Export encrypts to `.credentials.enc` → agent start decrypts and writes files. OAuth providers for agent credentials: Google, Slack, GitHub (PAT), Notion. Common MCP servers inside agents: google-workspace, slack, notion, github, n8n-mcp.

---

## Container Security

- **Non-root execution** (Invariant #17, #874): backend and scheduler as `trinity` (UID 1000), MCP server as `node` (UID 1000), frontend as `nginx` (UID 101), agents as `developer` (UID 1000). Backend needs `group_add: ${DOCKER_GID:-999}` for Docker socket access on Linux.
- `CAP_DROP: ALL` + `CAP_ADD: NET_BIND_SERVICE`; `security_opt: no-new-privileges:true`; tmpfs `/tmp` with `noexec,nosuid` (RAM-backed, default 512 MB — operator-tunable via `AGENT_TMP_SIZE` on the backend service, validated `^\d+[mg]$` with invalid→default; `noexec,nosuid` stay fixed; counts against the agent memory cgroup; creation-time, so existing agents pick up a change on recreate not restart, #1231. Heavy scratch like pip/npm/ML wheels is redirected via a default `TMPDIR=/home/developer/.tmp` on the disk-backed home volume, created at start by `startup.sh`; mount spec + TMPDIR default live in `services/agent_service/capabilities.py` so create/recreate/system-agent can't drift, #1098); no external UI port exposure; network isolation per Network Topology above.
- **Bounded container logs (#1871).** Docker's `json-file` driver has no default `max-size`/`max-file`, so every container log grew forever under `/var/lib/docker/containers/` — silently, until the Docker data root hit 100%, dockerd could no longer parse its own logs, and the whole fleet wedged at once (2026-07-27). Two halves, because compose cannot reach agents: the platform services share an `x-logging` anchor in **all three** compose files (`CONTAINER_LOG_MAX_SIZE`/`_MAX_FILE`, default `10m`×3; the #2280 hosted file's parity with prod is CI-guarded), and SDK-created agent containers use `AGENT_LOG_CONFIG` in `services/agent_service/capabilities.py` (`AGENT_LOG_MAX_SIZE`/`_MAX_FILE`, same default), threaded into the three agent-container create sites beside `AGENT_TMPFS_MOUNT`. Validation is fail-safe in **both** directions — malformed *and* out-of-range (>1g, >10 files) fall back to the bounded default, because a well-formed absurd value like `1000g` passes a format-only check while effectively removing the cap, which is the exact failure the constant prevents; an explicitly-set rejection logs a WARNING (a silently-ignored knob is the #1039 inert-by-obscurity class). Creation-time like the tmpfs spec: platform services adopt on the next `docker compose up`, existing agents on **recreate**, not restart. The raw Docker log is a *secondary* copy — Vector's aggregate at `/data/logs` is the primary queryable one and keeps its own `LOG_RETENTION_DAYS`; live streaming is lossless across rotation (only post-hoc `docker logs` history shortens, and the UI's log endpoint defaults to `tail=100`). `tests/unit/test_1871_log_config_parity.py` is the CI guard that fails when a **new** durable-container create site ships without `log_config` (the `learnings.md` 2026-07-10 "the create path is never one call site" class).
- **Internal API security (C-003)**: `/api/internal/` endpoints (scheduler, agent containers) require the `X-Internal-Secret` header; falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` unset.
- **Agent-server inbound auth (#1159)** (details in [agent-server-authentication.md](feature-flows/agent-server-authentication.md)): every backend→agent call carries a per-agent `X-Trinity-Agent-Token` = `HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:"+name)` — *derived*, not stored; the master lives only in backend env, so a compromised agent can't compute a sibling's token. A **pure-ASGI** middleware (`docker/base-image/agent_server/middleware/auth.py`) enforces it on **all** HTTP **and** WS routes via constant-time compare, exempting only exact `/health` (+ `OPTIONS`) — pure-ASGI (not `BaseHTTPMiddleware`) so it gates WS scopes too and never buffers SSE. The dead unauthenticated `/ws/chat` route (ran arbitrary Claude) was removed; CORS dropped (internal-only). Grace path: empty `TRINITY_AGENT_AUTH_TOKEN` → allow (old-image); `check_agent_auth_token_env_matches` forces a one-pass recreate so a missing/stale token re-injects. Backend fail-closed (`derive_agent_token` raises on empty secret; `start.sh` auto-generates the hex32 master, all three compose files forward it (dev, prod, and the #2280 hosted file)). Callers route through `services/agent_auth.py`; a static guard (`tests/unit/test_agent_auth_header_guard.py`) fails any raw `agent-{name}:8000` caller that skips them.
- **WebSocket security (C-002, #550)**: single-use ticket auth — see [Real-time Delivery](integrations.md#real-time-delivery-reliability-003-306).
- **Guardrail registration is root-owned (ent#345)**: the GUARD-002 hooks are registered in `/etc/claude-code/managed-settings.json` — Claude Code's admin-controlled managed-settings path, which takes precedence over user/project settings and sits **outside** the agent's HOME and the git-synced tree. It ships root:root `0444` inside a root-owned `0755` dir, so the file cannot be rewritten and the dir cannot be used to replace it or shadow it via `managed-settings.d`. It used to be `/home/developer/.claude/settings.json`, owned by `developer` at 0644 *inside the synced repo*: the guarded agent could edit its own guardrail registration (one `Edit` disables the bash deny-list, file guard, read-only guard and output-scanner for every later tool call — observed in the wild during #2036), and a push to the agent's repo could set it from outside. No platform-owned `settings.json` is shipped into `~/.claude` any more. The failure mode of the new path is **silent** (no registration ⇒ no hooks, nothing logged), so `startup.sh` asserts existence + non-writability each boot and logs `GUARDRAILS: ERROR …`; it reports and continues, since a registration fault must not become a fleet outage (a `/health` signal is the tracked follow-up). Read-only mode does not conflict: since #887 it writes a config file the baked hook reads, not a second registration.
- **Frontend XSS (H-005)**: all markdown rendering uses DOMPurify via `utils/markdown.js`; no direct `v-html` with unsanitized content.
- **Rate limiting (#1023)**: shared sliding-window limiter `services/rate_limiter.py` — Redis sorted-set rolling window (no fixed-window boundary burst), fail-open with bounded per-worker in-process fallback; `enforce(key, limit, window)` raises 429 + `Retry-After`, `check(key, limit, window)` is the non-raising variant for background loops. New request-rate limits reuse this primitive — don't hand-roll Redis counters. Current consumers: webhook trigger (#1023), agent reports (#918), operator-queue create ingestion (per-agent + fleet, `check`, #1632). Intentionally NOT unified under it: the auth login/OTP limiters in `routers/auth.py` are failure-counters (increment on failure, reset on success) — a different pattern. A global ASGI middleware with a route→policy table is a tracked follow-up.
- **Secret scanning (#1164)**: `.github/workflows/secret-scan.yml` runs the gitleaks MIT CLI on every PR (commit-range scope, `contents: read`, `--redact=100`) to block a re-landed credential (the `re_`-prefixed Resend key removed in #1158); config + custom `re_` rule + allowlists in `.gitleaks.toml`. Commit-time source scanning — complementary to GUARD-002's runtime output scanning. Non-blocking until a repo admin makes it a required check (follow-up).

---

