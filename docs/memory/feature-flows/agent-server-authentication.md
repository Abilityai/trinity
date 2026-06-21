# Feature: Agent-Server Inbound Authentication (#1159)

## Overview

The in-container agent server (`docker/base-image/agent_server`, FastAPI on
port 8000) historically had **zero** inbound authentication on the shared
`trinity-agent-network` (172.28.0.0/16). Any process on that network — including
a prompt-injected or compromised sibling agent — could call another agent's
server to read its `.env` secrets (`/api/credentials/*`, `/api/files/*`) or run
arbitrary Claude code (the now-removed unauthenticated `/ws/chat` route invoked
`runtime.execute`).

This feature closes that gap with a **derived per-agent token**. Every
backend→agent HTTP/WebSocket call carries
`X-Trinity-Agent-Token = HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:" + agent_name)`;
a pure-ASGI middleware inside each container verifies it on every route. The
master secret lives only in the backend env, so a compromised agent holds its
own token but cannot compute a sibling's.

This is the canonical end-to-end trace; the one-paragraph authoritative summary
lives in `docs/memory/architecture.md` → Container Security → "Agent-server
inbound auth (#1159)".

## User Story

As a Trinity platform operator, I want the in-container agent server to reject
any request that doesn't come from the backend, so that a compromised or
prompt-injected agent on the shared agent network cannot read a sibling agent's
secrets or execute arbitrary code on it.

---

## Key Concepts

### Derive, don't store

The per-agent token is a **pure function of `(master, agent_name)`** — there is
no DB column, no encryption mixin, and no migration. `start.sh` persists one
stable `AGENT_AUTH_SECRET` (openssl-hex-32, same persist-once / never-rotate
contract as `SECRET_KEY` / `INTERNAL_API_SECRET`); each agent's token is derived
from it on demand. This is why a renamed agent's token "just works": re-deriving
from the new name produces the correct token, and the recreate-reconciliation
matcher (below) forces the running container to pick it up.

| Property | Where enforced |
|----------|----------------|
| Master held only by backend | `AGENT_AUTH_SECRET` is forwarded **only** to the backend service in compose; never injected into agents |
| Agent receives only its own token | `TRINITY_AGENT_AUTH_TOKEN = derive(self.name)` injected at create/recreate |
| Compromised agent A cannot forge token B | computing `token_B` needs the master, which never leaves the backend |
| Revocation | rotate master + recreate fleet (not an AC requirement) |

### Fail-closed backend, grace-path agent

The two halves have **opposite** failure postures, deliberately:

- **Backend (producer)** is fail-**closed**: `derive_agent_token()` /
  `_agent_auth_secret()` raise `RuntimeError` if `AGENT_AUTH_SECRET` is unset, so
  the backend hard-fails the first time it tries to talk to an agent rather than
  silently deriving a token off `""` (which would hand every agent the same
  predictable token).
- **Agent middleware (consumer)** has a **grace path**: when its injected
  `TRINITY_AGENT_AUTH_TOKEN` env is empty/unset it allows everything. This makes
  the rollout non-breaking — a fleet still on the old base image (no token
  injected) keeps working until the operator-timed recreate injects tokens and
  flips enforcement on. The only way to reach the grace path is an un-recreated
  agent, never a backend misconfig (the backend would have hard-failed).

### Pure-ASGI, not `BaseHTTPMiddleware`

The middleware is implemented as pure ASGI (`AgentAuthMiddleware.__call__(scope,
receive, send)`), **not** Starlette's `BaseHTTPMiddleware`, for two reasons:

1. **Scope-complete boundary** — `BaseHTTPMiddleware` only sees `http` scopes; a
   pure-ASGI middleware also gates `websocket` scopes. This matters because the
   removed `/ws/chat` route slipped past the original HTTP-only guard; WS
   coverage now keeps any *future* WS route authenticated by default.
2. **No SSE buffering** — the agent server serves long-lived `text/event-stream`
   responses; `BaseHTTPMiddleware` historically buffers response bodies, while a
   pure-ASGI pass-through never touches the body, so SSE keeps streaming.

---

## Entry Points

| Source | Mechanism | Purpose |
|--------|-----------|---------|
| **Any backend→agent HTTP call** | `X-Trinity-Agent-Token` header (via `services/agent_auth.py` helpers) | Authenticate to the agent server |
| **Agent middleware** (per request) | `AgentAuthMiddleware.__call__` | Verify the token on every HTTP + WS route |
| **Agent create** | `TRINITY_AGENT_AUTH_TOKEN` env injection (`crud.py`) | Hand the agent its own token |
| **Agent recreate / start** | `check_agent_auth_token_env_matches` | Force a one-pass recreate when the token is missing/stale |
| **Agent rename** | re-derive under the new name (`lifecycle.py`) | Re-inject `derive(new_name)` |

---

## Backend Producer Side (`src/backend/services/agent_auth.py`)

A single ~110-line module (NEW). No DB, no Redis — derivation only.

| Symbol | Line | Description |
|--------|------|-------------|
| `AGENT_AUTH_HEADER = "X-Trinity-Agent-Token"` | 35 | Wire header; named so the credential sanitizer's `.*TOKEN.*` pattern still redacts it in transcripts |
| `_TOKEN_DOMAIN_PREFIX = b"trinity-agent-auth:v1:"` | 40 | Versioned domain-separation prefix (bump `v1` if derivation changes) |
| `_agent_auth_secret()` | 43 | Returns the master as bytes; **raises** `RuntimeError` if unset/empty (fail-closed) |
| `derive_agent_token(name)` | 62 | `HMAC-SHA256(secret, prefix + name).hexdigest()` |
| `build_agent_auth_headers(name)` | 71 | `{AGENT_AUTH_HEADER: derive_agent_token(name)}` for a single request |
| `merge_auth_headers(name, existing)` | 76 | Merge the derived header into caller headers, overriding any supplied `X-Trinity-Agent-Token` **case-insensitively** (can't spoof or pin a stale value) |
| `agent_httpx_client(name, **kwargs)` | 95 | `httpx.AsyncClient` pre-stamped with the auth header as a *default* — so `client.stream(...)` and fallbacks all carry it; only for clients talking **exclusively** to `agent-{name}:8000` |

### Secret generation + compose forwarding

`scripts/deploy/start.sh:47` auto-generates `AGENT_AUTH_SECRET` via the shared
`ensure_hex32_secret()` helper (`start.sh:25`, same path as
`CREDENTIAL_ENCRYPTION_KEY` / `SECRET_KEY` / `INTERNAL_API_SECRET`). Both compose
files forward it **only** to the backend service (`.env` alone is inert in prod):

- `docker-compose.yml:90` — `AGENT_AUTH_SECRET=${AGENT_AUTH_SECRET:-}`
- `docker-compose.prod.yml:51` — `AGENT_AUTH_SECRET=${AGENT_AUTH_SECRET:-}`

### Caller migration (the helper layer)

20 backend files route their agent calls through the helpers. The two central
choke points stamp it once for everyone:

| Choke point | Line | How |
|-------------|------|-----|
| `services/agent_client.py` (`AgentClient._request`) | 782 | `kwargs["headers"] = merge_auth_headers(self.agent_name, kwargs.get("headers"))` — every chat/session/injection call |
| `services/agent_service/helpers.py` (`agent_http_request`) | 153 | `merge_auth_headers(agent_name, ...)` on **every retry attempt** |

Direct callers migrated to the helpers include
`routers/{a2a,agent_files,avatar,chat,credentials,public,system_agent,voice}.py`,
`services/git_service.py`, `services/github_pat_propagation_service.py`,
`services/credential_encryption.py`, and
`services/agent_service/{dashboard,metrics,stats}.py`.

> **Note:** `git_service.py` reaches into containers mostly via `docker exec`
> (not HTTP), and the standalone **scheduler** (`src/scheduler/`) never calls
> agents directly — it dispatches through the backend's
> `/api/internal/execute-task`. So the only token-carrying surface is the
> backend→agent HTTP/WS path. The static guard (below) excludes `scheduler/`.

---

## Agent Consumer Side (`docker/base-image/agent_server/middleware/`)

Two NEW files: `auth.py` (the middleware) and `__init__.py` (package export).

| Symbol | Line (`auth.py`) | Description |
|--------|------------------|-------------|
| `AGENT_AUTH_HEADER = "X-Trinity-Agent-Token"` | 44 | Must stay in lockstep with the backend constant |
| `AGENT_AUTH_TOKEN_ENV = "TRINITY_AGENT_AUTH_TOKEN"` | 45 | Per-agent token injected at create/recreate |
| `_EXEMPT_PATH = "/health"` | 46 | The only exempt HTTP path |
| `_WS_POLICY_VIOLATION = 1008` | 48 | RFC 6455 close code for a rejected WS handshake |
| `AgentAuthMiddleware.__call__` | 61 | Pure-ASGI entrypoint; reads `expected` **per request** (so a hot-reloaded token takes effect next request) |
| `_token_ok(scope, expected)` | 97 | `hmac.compare_digest(provided, expected)` — constant-time compare |
| `_deny_websocket(receive, send)` | 102 | Sends `websocket.close` code 1008 in response to `websocket.connect`, before the app can `accept()` |

### Enforcement matrix

| Scope | Condition | Outcome |
|-------|-----------|---------|
| `http` | `OPTIONS` method | **Allow** (CORS preflight carries no custom header) |
| `http` | exact path `/health` | **Allow** (Docker healthcheck / readiness probe; payload is low-sensitivity runtime counts) |
| `http` | valid token | **Allow** → app |
| `http` | missing/wrong token | **401** `{"detail": "Unauthorized"}` |
| `websocket` | valid token | **Allow** → app |
| `websocket` | missing/wrong token | **Close 1008** before `accept()` — no WS exemptions |
| any | `TRINITY_AGENT_AUTH_TOKEN` empty/unset | **Allow** (grace path / old-image rollout) |
| `lifespan` / other | — | pass through |

### Wiring (`docker/base-image/agent_server/main.py:52`)

```python
from .middleware.auth import AgentAuthMiddleware
...
app.add_middleware(AgentAuthMiddleware)
```

Added as the outermost middleware so it gates every router. The same change
**removed `CORSMiddleware`** (`allow_origins=["*"]` + `allow_credentials=True`) —
the agent server is internal-only on the Docker network and is never hit by a
browser, so the permissive CORS config was pure attack surface.

### Removed dead route

The unauthenticated `/ws/chat` route was deleted from
`docker/base-image/agent_server/routers/chat.py`. It ran `runtime.execute`
(arbitrary Claude) and slipped past the original HTTP-only middleware. Confirmed
gone — no `ws/chat` / `websocket` reference remains in that router.

---

## Re-Injection & Recreate Reconciliation

The token rides in the container's `Config.Env` (`TRINITY_AGENT_AUTH_TOKEN`), so
a name change or a missing env requires a recreate. This is handled by the same
"matcher forces a one-pass recreate" pattern as the guardrails/PAT env matchers
(deterministic → loop-safe, no infinite recreate).

| Site | File:line | Behavior |
|------|-----------|----------|
| **Create** | `services/agent_service/crud.py:595` | `env_vars['TRINITY_AGENT_AUTH_TOKEN'] = derive_agent_token(config.name)` — **unconditional** (NOT gated on the MCP key), so even MCP-less agents are protected |
| **Recreate / start** | `services/agent_service/lifecycle.py:430` | Re-derives `TRINITY_AGENT_AUTH_TOKEN = derive(agent_name)` under the **current** name |
| **Reconciliation check** | `services/agent_service/helpers.py:493` (`check_agent_auth_token_env_matches`) | Compares the container's env value to `derive(agent_name)`; returns `False` (→ recreate) when missing or stale |
| **Recreate trigger** | `lifecycle.py:269` | `check_agent_auth_token_env_matches` is OR'd into `needs_recreation` alongside the api-key / PAT / resources / capabilities / guardrails matchers |
| **Rename** | `routers/agent_rename.py:123` (comment) | No extra work — the rename leaves the agent stopped; the next `start_agent_internal` recreate is forced by the token mismatch and re-injects `derive(new_name)` |

The rename case is the **load-bearing** part of the matcher: a renamed container
still carries `derive(old_name)`, which is stale under the new name and would
401 once enforcement is on. The matcher catches the mismatch and re-derives.

---

## Data Flow Diagrams

### Flow 1: Authenticated backend→agent call (happy path)

```
Backend                              Agent container (:8000)
   |                                       |
   | merge_auth_headers(name, ...)         |
   |   X-Trinity-Agent-Token =             |
   |   HMAC(AGENT_AUTH_SECRET, prefix+name)|
   | POST /api/chat (or files/creds/...)   |
   |-------------------------------------->| AgentAuthMiddleware:
   |                                       |   expected = env[TRINITY_AGENT_AUTH_TOKEN]
   |                                       |   compare_digest(provided, expected) -> ok
   |                                       |--> router handler
   |              200 response             |
   |<--------------------------------------|
```

### Flow 2: Cross-agent attack (blocked)

```
Compromised agent A                  Agent B (:8000)
   |                                       |
   | has token_A (its own), NOT the master |
   | cannot compute token_B                |
   | POST agent-B:8000/api/credentials/read|
   |   (no token, or token_A)              |
   |-------------------------------------->| AgentAuthMiddleware:
   |                                       |   expected = token_B
   |                                       |   compare_digest(token_A, token_B) -> fail
   |              401 Unauthorized         |
   |<--------------------------------------|
```

### Flow 3: Rename → recreate reconciliation

```
Rename agent (old -> new)
   | rename leaves container stopped, env still TRINITY_AGENT_AUTH_TOKEN=derive(old)
   v
start_agent_internal(new)
   | check_agent_auth_token_env_matches(container, "new"):
   |   env value derive(old) != derive("new") -> False
   | needs_recreation = True
   v
recreate container with env TRINITY_AGENT_AUTH_TOKEN = derive("new")
   | next backend call stamps derive("new"); middleware matches -> 200
```

---

## Failure Modes & Guarantees

| Scenario | Behavior |
|----------|----------|
| `AGENT_AUTH_SECRET` unset (backend) | `derive_agent_token` raises `RuntimeError` on first agent call — fail-closed, never tokenless |
| Old-image agent (no `TRINITY_AGENT_AUTH_TOKEN`) | Grace path: middleware allows everything until operator-timed recreate injects the token |
| Compromised / prompt-injected sibling | Holds only its own token; cannot compute another agent's → 401 / WS close 1008 |
| Renamed agent | Matcher forces one recreate; re-derives under the new name (deterministic → loop-safe) |
| Hot-reloaded token env | Middleware reads `expected` per request, so the new token takes effect next request without a server restart |
| SSE / streaming response | Pure-ASGI pass-through never buffers the body — streaming unaffected |
| Docker healthcheck | `/health` (HTTP) and `OPTIONS` exempt — probe keeps working with no token |
| New WS route added later | Authenticated by default (no WS exemptions) — can't silently bypass the guard |

---

## Testing

| Test file | Covers |
|-----------|--------|
| `tests/unit/test_agent_auth.py` | Backend derivation: HMAC correctness, domain prefix, fail-closed on unset secret, `merge_auth_headers` case-insensitive override, `agent_httpx_client` default header |
| `tests/unit/test_agent_auth_middleware.py` | Agent-side middleware: HTTP 401 / WS 1008 rejection, `/health` + `OPTIONS` exemptions, grace path on empty token, constant-time compare |
| `tests/unit/test_agent_auth_matcher.py` | `check_agent_auth_token_env_matches` — missing / stale / renamed env → recreate |
| `tests/unit/test_agent_auth_header_guard.py` | **Static guard**: fails any new backend file that builds a raw `agent-{name}:8000` URL without an auth helper (and isn't a pure `/health` prober); excludes the helper layer + `scheduler/` |
| `tests/security/test_agent_auth_isolation.py` | End-to-end isolation: a sibling agent's token cannot reach another agent's server |

### Manual verification

```bash
# Inside a token-enabled agent container — sibling call must be rejected:
curl -s -o /dev/null -w '%{http_code}' \
  http://agent-other:8000/api/credentials/read          # -> 401

# /health is exempt (Docker probe):
curl -s -o /dev/null -w '%{http_code}' \
  http://agent-other:8000/health                         # -> 200

# Backend call carries the derived header and succeeds (200).
```

---

## Security Considerations

1. **Per-agent isolation**: each agent receives only `derive(self.name)`; the
   master `AGENT_AUTH_SECRET` never leaves the backend, so a compromised agent
   cannot forge a sibling's token.
2. **Constant-time compare**: `hmac.compare_digest` defeats timing oracles on
   the token.
3. **Scope-complete boundary**: pure-ASGI middleware gates **both** HTTP and
   WebSocket scopes; the dead arbitrary-execution `/ws/chat` route was removed
   and CORS attack surface eliminated.
4. **No new persisted secret**: token is derived, not stored — no DB column, no
   plaintext-credential exposure, no migration (consistent with Invariant #12,
   which forbids plaintext credential storage).
5. **Header redaction**: the header is named `X-Trinity-Agent-Token` so the
   existing credential sanitizer (`.*TOKEN.*` pattern) redacts it from any
   transcript that echoes headers.
6. **Fail-closed producer**: an operator running without `AGENT_AUTH_SECRET`
   gets a loud hard-fail, not a silent same-token-for-everyone degradation.
7. **Defense in depth**: this is the inbound layer for the agent server; agents
   still physically cannot reach Redis (network topology, #589), and
   `/api/internal/*` still requires `X-Internal-Secret` (C-003).

---

## Related Flows

- **Credential Injection** ([credential-injection.md](credential-injection.md)) —
  the `/api/credentials/*` agent-server endpoints this layer now protects.
- **Agent Push-Heartbeat Liveness** ([agent-heartbeat-liveness.md](agent-heartbeat-liveness.md)) —
  another backend↔agent auth boundary (agent→backend, Option-B MCP key); this
  feature is the inbound (backend→agent) counterpart.
- **Agent Lifecycle** ([agent-lifecycle.md](agent-lifecycle.md)) /
  **Agent Rename** ([agent-rename.md](agent-rename.md)) — the create / recreate /
  rename paths that inject and reconcile `TRINITY_AGENT_AUTH_TOKEN`.

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-06-21 | Initial documentation for #1159 — per-agent inbound auth for the in-container agent server. Derived `X-Trinity-Agent-Token = HMAC-SHA256(AGENT_AUTH_SECRET, "trinity-agent-auth:v1:"+name)` (`services/agent_auth.py`), pure-ASGI verifying middleware on every HTTP + WS route with `/health`/`OPTIONS` exemptions and an empty-token grace path (`agent_server/middleware/auth.py`), `check_agent_auth_token_env_matches` recreate reconciliation (rename-safe), 20-file caller migration through the helper layer, removed dead `/ws/chat` route + CORS, and a static header guard. |
