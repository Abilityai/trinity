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
| `src/backend/routers/paid.py` | Public paid endpoint (`/api/paid/`) |
| `src/backend/routers/nevermined.py` | Admin config endpoints (`/api/nevermined/`), `_require_agent_exists()` guard |
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

## Isolation Guarantees

1. All changes are additive — no existing code paths modified
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
| #1018 | **Settlement-ordering / honest status.** Settle-fail → `success_unsettled` (was lying `"success"`); concurrent effect-guard settle → `settle_in_progress:true`; wired `Idempotency-Key` keyed on `(payment-signature ∥ message)` with in-flight-409 / settled-verbatim-replay / unsettled-re-drive-and-converge (`_finalize_settled` + `upgrade_snapshot`); `fail()` on 403/exception/failed paths; stop leaking the body on `failed` executions (keep it on `cancelled`); `/retry-settlement` stub → honest 501. Tier 2 durable stored-credential retry split to a follow-up. |
| #1084 | `settle_payment_once` + `effect_guard` on `payment:{agent_request_id}` (local exactly-once + receipt replay). |
| #679 | Cancelled turn must NOT settle (charge-on-cancel money bug). |
| NVM-001 | Initial x402 integration (2026-03-04). |
