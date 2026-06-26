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
         ├─ verify_payment() → log: action=verify
         ├─ TaskExecutionService.execute_task(triggered_by="paid")
         ├─ Execution failed → skip settle → return error (no charge)
         └─ Execution success
              ├─ settle_payment() (3 retries, exponential backoff)
              ├─ Settle OK → log: action=settle → return response + receipt
              └─ Settle failed → log: action=settle_failed → return response + warning
```

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
| `POST` | `/api/paid/{agent_name}/chat` | x402 | Paid chat (402/403/200) |
| `GET` | `/api/paid/{agent_name}/info` | None | Payment info |
| `POST` | `/api/nevermined/agents/{name}/config` | JWT (owner) | Configure |
| `GET` | `/api/nevermined/agents/{name}/config` | JWT (shared+) | Read config |
| `DELETE` | `/api/nevermined/agents/{name}/config` | JWT (owner) | Remove config |
| `PUT` | `/api/nevermined/agents/{name}/config/toggle` | JWT (owner) | Enable/disable |
| `GET` | `/api/nevermined/agents/{name}/payments` | JWT (shared+) | Payment history |
| `GET` | `/api/nevermined/settlement-failures` | Admin | Failed settlements |
| `POST` | `/api/nevermined/retry-settlement/{log_id}` | Admin | Retry settlement |

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
| Invalid payment token | 403 | `/api/paid/{name}/chat` |
| SDK not installed | 501 | `_check_sdk()` |

## Isolation Guarantees

1. All changes are additive — no existing code paths modified
2. Lazy SDK imports — `payments-py` never imported at module level
3. Graceful degradation — 501 if SDK not installed
4. No foreign key constraints to existing tables
5. Independent failure domain — bugs affect only `/api/paid/` and `/api/nevermined/`

## Related Flows

- **Guards**: [effect-idempotency.md](effect-idempotency.md) — `settle_payment_once` is wired through `effect_guard` on the `payment:{agent_request_id}` scope (the Nevermined native exactly-once token) so a re-delivered turn cannot double-charge a settlement; preserves the existing terminal-turn no-settle guard (#1084).
