# Feature Flow: Nevermined x402 Payment Integration (NVM-001)

> **Status**: Implemented (2026-03-04)
> **Spec**: `docs/requirements/NEVERMINED_PAYMENT_INTEGRATION.md`

## Overview

Per-agent monetization via Nevermined's x402 payment protocol. External callers pay per-request through a `payment-signature` HTTP header. Internal fleet traffic (MCP `chat_with_agent`) bypasses payment entirely.

## Flow: Paid Chat Request

```
External Caller
    |
    POST /api/paid/{agent_name}/chat
    + Header: payment-signature: <access_token>
    |
    ├─ No config/not enabled → 404
    ├─ No payment-signature → 402 Payment Required
    │     (includes plan_id, pricing, endpoint)
    ├─ Invalid token → verify_payment() fails → 403
    │     (log: action=reject)
    └─ Valid token
         ├─ verify_payment() → log: action=verify   (NO credit burn — settle burns)
         ├─ Idempotency gate (Invariant #18, #1018) — AFTER verify so a 403 never
         │    consumes a key. key = sha256(payment-signature ∥ message); a client
         │    `Idempotency-Key` header is accepted but ignored for derivation.
         │    ├─ in-flight duplicate → 409
         │    ├─ completed + settled snapshot → replay verbatim (X-Idempotent-Replay)
         │    └─ completed + UNSETTLED snapshot → re-drive settle_payment_once
         │         (idempotent via payment:{agent_request_id}); success → _finalize_settled
         │         (log settle + upgrade_snapshot → settled); a THIRD request replays settled
         ├─ TaskExecutionService.execute_task(triggered_by="paid")
         ├─ Execution raised → fail(idem) → 500 (no charge, retryable)
         ├─ Execution failed → fail(idem) → 200, NO response body (#1018), no settle
         ├─ Execution cancelled → fail(idem) → 200, response kept (#679), no settle
         └─ Execution success
              ├─ attach_execution(idem)
              ├─ settle_payment_once (effect-guarded, 3 retries, exp. backoff)
              ├─ Settle OK → _finalize_settled: log settle + complete snapshot → status "success"
              ├─ Settle FAILED → log settle_failed → complete(unsettled snapshot)
              │      → 200 status "success_unsettled", payment{settled:false, settle_retry_needed:true}
              └─ Concurrent settle in-flight (effect guard) → 200 status "success_unsettled",
                     payment{settled:false, settle_in_progress:true}  (no settle_failed log)
```

> **#1018 (settlement ordering).** The settle-failed branch used to lie with top-level
> `status:"success"` — releasing the paid resource without a confirmed transfer. It now returns
> honest `success_unsettled` while still delivering the completed work (deliver-then-reconcile).
> Because `verify_payment` doesn't burn credits, a client that re-presents the same
> `payment-signature` replays the completed work (via the `(token+body)` trigger key, so **no double
> LLM run**) and re-attempts settle. The re-settle is **not** provider-idempotent — Nevermined's
> `agent_request_id` is an observability id and the facilitator burns on every successful settle — so
> a re-drive is safe only because the prior settle did not complete; a false-negative burn (settled
> on-chain but reported failed) can still double-charge (at-least-once residual, tracked by #1408).
> Durable server-side (stored-credential) retry is a Tier 2 follow-up.

## Flow: Admin Configuration

```
Admin/Owner
    |
    POST /api/nevermined/agents/{name}/config
    + Body: { nvm_api_key, nvm_environment, nvm_agent_id, nvm_plan_id, credits_per_request }
    |
    ├─ _require_write_access()
    │     ├─ _require_agent_exists() → checks Docker + DB → 404 if not found
    │     └─ owner or admin only → 403 otherwise
    ├─ NeverminedOperations.create_or_update_config()
    │     ├─ Encrypt nvm_api_key via CredentialEncryptionService (AES-256-GCM)
    │     └─ Upsert nevermined_agent_config row
    |
    PUT /api/nevermined/agents/{name}/config/toggle?enabled=true
    |
    └─ Agent is now accepting paid requests

Shared User (view-only)
    |
    GET /api/nevermined/agents/{name}/config
    GET /api/nevermined/agents/{name}/payments
    |
    ├─ _require_read_access()
    │     ├─ _require_agent_exists() → checks Docker + DB → 404 if not found
    │     └─ owner, shared, or admin → 403 otherwise
    └─ Returns config (no decrypted key) / payment log
    |
    POST/PUT/DELETE → 403 "Owner access required"
    Frontend shows read-only view with disabled form controls
```

## Files

### Backend
| File | Purpose |
|------|---------|
| `src/backend/db/nevermined.py` | `NeverminedOperations` — config CRUD + payment log |
| `src/backend/services/nevermined_payment_service.py` | `NeverminedPaymentService` — SDK verify/settle |
| `src/backend/routers/paid.py` | Public paid endpoint (`/api/paid/`) — the HTTP shape over `paid_turn_service` since ent#679 |
| `src/backend/services/paid_turn_service.py` | **The one paid-turn orchestrator** (ent#679): verify → dedup gate → execute → settle, with the #1018 branches. Shared by the paid door and the A2A inbound door; takes every collaborator as a parameter and imports none |
| `src/backend/services/a2a_payment_gate.py` | The A2A-shaped adapter over it (ent#679) — token extraction, the 402/403 bodies, the payer's Task. Flow: [a2a-inbound-server.md](a2a-inbound-server.md) |
| `src/backend/services/a2a_card_service.py` | `with_payment_extension` — a priced agent's A2A card declares its plan (ent#679) |
| `src/backend/routers/nevermined.py` | Admin config endpoints (`/api/nevermined/`), `_require_agent_exists()` guard |
| `src/backend/utils/public_url.py` | `public_base_url` — the ONE externally-reachable origin for the card and both 402 doors (#3215) |
| `src/backend/db_models.py` | Pydantic models for config, payment result, payment log |
| `src/backend/db/schema.py` | Table definitions |
| `src/backend/db/migrations.py` | Migration #23 |
| `src/backend/database.py` | Delegate methods |

### Frontend
| File | Purpose |
|------|---------|
| `src/frontend/src/components/NeverminedPanel.vue` | Payments tab component |
| `src/frontend/src/views/AgentDetail.vue` | Tab registration |

### MCP
| File | Purpose |
|------|---------|
| `src/mcp-server/src/tools/nevermined.ts` | 4 MCP tools |
| `src/mcp-server/src/client.ts` | API methods |
| `src/mcp-server/src/server.ts` | Tool registration |

## Database Tables

- **nevermined_agent_config** — Per-agent config with encrypted `NVM_API_KEY`
- **nevermined_payment_log** — Audit trail of verify/settle/reject/settle_failed actions

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/paid/{agent_name}/chat` | x402 | Paid chat (402/403/200/409). Accepts `Idempotency-Key` (#1018); settle-fail → `success_unsettled` |
| `GET` | `/api/paid/{agent_name}/info` | None | Payment info |
| `POST` | `/a2a/{agent_name}` | x402 **or** Trinity key | The A2A inbound door (ent#679). A resolved Trinity principal runs free, exactly as before; an anonymous caller is charged when the agent is both A2A-exposed and payments-enabled — same 402 bytes, same settle logic, `resource.url` on this door. See `requirements/mcp.md` §32.6 |
| `POST` | `/api/nevermined/agents/{name}/config` | JWT (owner) | Configure |
| `GET` | `/api/nevermined/agents/{name}/config` | JWT (shared+) | Read config |
| `DELETE` | `/api/nevermined/agents/{name}/config` | JWT (owner) | Remove config |
| `PUT` | `/api/nevermined/agents/{name}/config/toggle` | JWT (owner) | Enable/disable |
| `GET` | `/api/nevermined/agents/{name}/payments` | JWT (shared+) | Payment history |
| `GET` | `/api/nevermined/settlement-failures` | Admin | Failed settlements |
| `POST` | `/api/nevermined/retry-settlement/{log_id}` | Admin | Honest **501** (#1018) — server-side retry unsupported (token not stored); client re-presents `payment-signature`. Durable retry = Tier 2 follow-up |

## MCP Tools

| Tool | Description |
|------|-------------|
| `configure_nevermined` | Set up payment config |
| `get_nevermined_config` | Read config (no key) |
| `toggle_nevermined` | Enable/disable |
| `get_nevermined_payments` | Payment history |

## Error Handling

| Error Case | HTTP Status | Where |
|------------|-------------|-------|
| Agent not found (Docker + DB) | 404 | `_require_agent_exists()` in all config/payment endpoints |
| No access to agent | 403 | `_require_read_access()` |
| Not owner/admin | 403 | `_require_write_access()` |
| Config not found | 404 | GET/DELETE/toggle config endpoints |
| Missing payment-signature | 402 | `/api/paid/{name}/chat` |
| Invalid payment token | 403 | `/api/paid/{name}/chat` (before the idempotency gate — a 403 never consumes a key) |
| In-flight duplicate paid request | 409 | `/api/paid/{name}/chat` idempotency gate (#1018) |
| Settle failed after retries | 200 `success_unsettled` | `/api/paid/{name}/chat` — honest status, work delivered (#1018) |
| Concurrent settle in progress | 200 `success_unsettled` + `settle_in_progress` | effect guard (#1084/#1018) |
| Server-side settlement retry | 501 | `/api/nevermined/retry-settlement/{log_id}` — token not stored (#1018) |
| SDK not installed | 501 | `_check_sdk()` |

## Plan scheme resolution (#3215)

A Nevermined plan is paid in crypto or by card, and the x402 requirements must
say which: the facilitator is POSTed the requirements document verbatim, so
`accepts[0].scheme` is the only channel the scheme travels through. Passing none
meant the SDK's `nvm:erc4337` default, and every fiat plan's token was rejected.

| Call site | Scheme comes from | Why |
|---|---|---|
| 402 body (paid door, A2A door), `GET /api/paid/{name}/info` | `resolve_plan_scheme` → `plans.get_plan` | No token exists yet. `/info` is the card's `paymentInfoUrl`, so it must advertise the same scheme as the 402 it replaces |
| `verify_payment`, `settle_payment` (incl. the detached re-settle and the `tasks/get` re-verify) | `scheme_from_token` → the token's own `accepted.{scheme,network,planId}` | Zero network calls on the money path, and a settle re-driven hours after its verify is byte-stable with it. Allow-listed (SDK scheme + `SupportedNetworks`) and plan-id-checked, because it is caller-supplied |

Fallback chain, in order: token → plan → `default_plan_scheme(environment)` (the
pre-#3215 document). Trinity parses the plan itself with the SDK's own keys
(`registry.price.isCrypto`, `metadata.plan.fiatPaymentProvider`), parity-tested
against `payments_py.x402.resolve_scheme` — the SDK swallows every lookup failure
at DEBUG and returns crypto, so a card plan silently stayed unpayable with nothing
in Trinity's logs. Trinity logs a WARNING once per negative window instead.

The lookup is cached per `(environment, plan_id)` — not `plan_id` alone, which is
the SDK's cache and would let a sandbox answer serve a live request — positive
300 s, negative 30 s, with per-key single-flight futures (50 cold 402s cost one
`get_plan`) and stale-while-revalidate. The erc4337 environment→network map stays
an explicit frozen literal pinned by a golden test, so a payments-py bump (#3216)
cannot move a live agent's network silently.

## Public origin for `resource.url` (#3215)

`utils/public_url.py::public_base_url(request, configured=, frontend_url=)` is the
single owner, shared by the agent card, the paid door and the A2A door — a card
advertising one origin while the 402 mints a token for another sends a buyer to a
URL it never verifies against. Precedence: the configured public origin (Settings
`public_chat_url` → `PUBLIC_CHAT_URL` → `FRONTEND_URL`) **only when its host
equals the request host** (a caller on a private host must not be redirected to a
public one whose narrow tunnel may not route the path), else the request host with
an https **upgrade** — never a downgrade — from the **raw** `X-Forwarded-Proto`
header.

Raw, not `request.url.scheme`: `docker-compose.prod.yml` / `.hosted.yml` override
the image `command:` and drop the Dockerfile CMD's `--proxy-headers
--forwarded-allow-ips=*`, so uvicorn applies no forwarded headers there at all.
The frontend `nginx.conf` `$fwd_proto` map stops that hop clobbering an upstream
`https` with its own always-`http` `$scheme`. Restoring the uvicorn flags is a
trust change (`--forwarded-allow-ips=*` would let any agent on the agent network
spoof `X-Forwarded-For` into every per-IP limiter) and is deliberately deferred.

## Configuration (operator knobs)

All four are env-only and read at import, so a change needs a backend restart.

| Variable | Default | What it bounds |
|----------|---------|----------------|
| `NEVERMINED_MAX_INFLIGHT` | `8` | Fleet-wide ceiling on **concurrent** facilitator calls. Every verify and settle attempt runs on the default thread executor, so a slow facilitator would otherwise hold backend threads for the whole fleet. Deliberately fleet-wide rather than per agent — the thread pool is a platform resource — and a priced agent's public URL needs no credential to make the backend dial out, so per-IP rate limiting cannot supply this bound (it has to hold *across* IPs). |
| `NEVERMINED_FACILITATOR_WAIT_SECONDS` | `5.0` | How long a call waits for a free slot before giving up. Bounded because the caller is holding an HTTP request open: "busy, retry" is an honest answer, an unbounded queue is not. A refused call never reaches the facilitator, so it burns nothing. |

| `NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS` | `5.0` | How long one plan-scheme lookup may take, and how long a follower waits on an in-flight one. The 402 door is anonymous, so the caller is holding an HTTP request open on an outbound call Trinity does not control. A timeout serves the fallback chain, never a 500. |
| `NEVERMINED_PLAN_LOOKUP_MAX_INFLIGHT` | `2` | Concurrent plan lookups, on a gate of their own. Never a facilitator slot: `asyncio.wait_for` does not cancel the worker thread (and `get_plan` has a `(10, 30)` s requests timeout), so a timed-out lookup holding a facilitator slot would release it while the thread lingered — and an anonymous 402 flood could starve paying verify/settle. |

The gate is one semaphore **per event loop** (`_FACILITATOR_GATES`, keyed weakly
on the running loop): a module-level semaphore would bind whichever loop first
contended on it, which in a test suite is whichever test ran first, while in
production there is one loop per worker and the bound is per worker.

## Isolation Guarantees

1. All changes are additive — no existing code paths modified. (ent#679 is the
   one exception and deliberately behaviour-preserving: `routers/paid.py`'s
   orchestration moved into `services/paid_turn_service.py` so the A2A door
   could share it rather than grow a second copy of the #1018 branches. The
   paid door's three existing test files are the net and were not edited.)
2. Lazy SDK imports — `payments-py` never imported at module level
3. Graceful degradation — 501 if SDK not installed
4. No foreign key constraints to existing tables
5. Independent failure domain — bugs affect only `/api/paid/` and `/api/nevermined/`

## Related Flows

- **Guards**: [effect-idempotency.md](effect-idempotency.md) — `settle_payment_once` is wired through `effect_guard` on the `payment:{agent_request_id}` scope so a *concurrent same-id* settle cannot double-charge (`agent_request_id` is a Nevermined observability id, not a provider exactly-once token — a fresh-id retry's residual is tracked by #1408); preserves the existing terminal-turn no-settle guard (#1084).
- **Trigger idempotency**: [idempotency-keys.md](idempotency-keys.md) — the paid boundary is a wired `Idempotency-Key` boundary (Invariant #18, #1018). Trigger dedup (`derive_payment_key`, `(token+body)` scope `agent:{name}`) composes with the `payment:{agent_request_id}` effect guard above.

## Change History

| Issue | Change |
|-------|--------|
| [#3215](https://github.com/abilityai/trinity/issues/3215) | Card (fiat) plans are payable: the plan's x402 scheme reaches the facilitator (token-derived on the money path, plan-derived for the 402 and `/info`); one shared public origin for `resource.url` with an `X-Forwarded-Proto` upgrade and the nginx `$fwd_proto` map; the A2A reply Task mirrors its x402 metadata onto the top-level `metadata` |
| #1018 | **Settlement-ordering / honest status.** Settle-fail → `success_unsettled` (was lying `"success"`); concurrent effect-guard settle → `settle_in_progress:true`; wired `Idempotency-Key` keyed on `(payment-signature ∥ message)` with in-flight-409 / settled-verbatim-replay / unsettled-re-drive-and-converge (`_finalize_settled` + `upgrade_snapshot`); `fail()` on 403/exception/failed paths; stop leaking the body on `failed` executions (keep it on `cancelled`); `/retry-settlement` stub → honest 501. Tier 2 durable stored-credential retry split to a follow-up. |
| ent#679 | **The same paywall on the A2A door.** `paid.py`'s 402/verify/settle orchestration extracted to `services/paid_turn_service.py` (behaviour-preserving) and reused by `POST /a2a/{name}`; metadata-first token carriage with the `payment-signature` header as deprecated fallback; the priced agent's A2A card declares its plan; `credits_per_request` accepts **0** for a duration plan (a negative is still a named 422). No migration. Requirement: `requirements/mcp.md` §32.6. |
| #1084 | `settle_payment_once` + `effect_guard` on `payment:{agent_request_id}` (local exactly-once + receipt replay). |
| #679 | Cancelled turn must NOT settle (charge-on-cancel money bug). |
| NVM-001 | Initial x402 integration (2026-03-04). |
