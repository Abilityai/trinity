# Nevermined x402 Payment Integration (NVM-001)

> **Status**: SDK-Validated (ready for implementation)
> **Priority**: P1
> **Author**: Claude + Eugene
> **Created**: 2026-03-04
> **Validated**: 2026-03-04 — Validated against installed `payments-py==1.2.1` SDK source and local testing
> **Research**: Nevermined agent integration notes (external)

## Overview

Integrate Nevermined's x402 payment protocol into Trinity so that individual agents can be monetized for external access. External users and agents pay per-request via a `payment-signature` HTTP header. Internal fleet traffic remains free.

This is **foundation + demo** quality: clean extensible architecture with a working demo-critical path.

## Goals

1. **Per-agent monetization**: Each agent independently configurable with its own Nevermined plan, pricing, and credentials
2. **x402 protocol**: HTTP 402 Payment Required as the discovery mechanism — self-describing, zero-config for callers
3. **Pay-for-success only**: Verify before work, settle after success. Failed requests never cost the caller.
4. **Internal fleet free**: Existing `chat_with_agent` MCP tool bypasses payment entirely
5. **Reuse existing patterns**: Follow subscription management (SUB-002) pattern for encrypted credential storage
6. **No A2A protocol**: Plain HTTP with x402 headers on existing chat-style endpoints. No Google A2A spec dependency.

## Non-Goals

- A2A protocol server per agent (may add later)
- Agent-to-agent paid calls within the fleet (internal always free)
- Fiat/Stripe payment integration (Nevermined handles this separately)
- Agent self-registration with Nevermined (done externally via Nevermined dashboard or SDK)
- Finance oversight agent (scheduled reporting — separate feature)
- MCP tool monetization (per-tool pricing — separate feature)
- Streaming or async execution modes (sync only for Phase 1)

---

## Architecture

### Payment Flow

```
External caller
        |
        v
  POST /api/paid/{agent_name}/chat
        |
  ┌─────────────────────────────────────────────┐
  │ 1. No payment-signature header?             │
  │    → 402 Payment Required                   │
  │    (response includes plan_id, pricing,     │
  │     agent_id, endpoint — self-describing)   │
  │                                             │
  │ 2. Invalid/insufficient token?              │
  │    → 403 Forbidden                          │
  │                                             │
  │ 3. Valid token?                             │
  │    → verify_permissions() [no credit burn]  │
  │    → TaskExecutionService.execute_task()    │
  │    → On success: settle_permissions()       │
  │    → On failure: skip settle (no charge)    │
  └─────────────────────────────────────────────┘
        |
        v
  Response + payment receipt:
  {
    response: "...",
    payment: {
      credits_burned: 1,
      remaining_balance: 9,
      tx_hash: "0x..."
    }
  }
```

### System Context

```
┌──────────────────────────────────────────────────────────────┐
│                     Trinity Platform                          │
│                                                              │
│  /api/paid/{agent}/chat  ──→  NeverminedPaymentService       │
│  (new router)                 (verify/settle via SDK)        │
│        │                            │                        │
│        │                     payments-py SDK                  │
│        │                            │                        │
│        ▼                            ▼                        │
│  TaskExecutionService        Nevermined API (sandbox/live)   │
│  (existing, unchanged)                                       │
│        │                                                     │
│        ▼                                                     │
│  Agent Container :8000/api/task                              │
│  (internal, unchanged)                                       │
│                                                              │
│  /api/nevermined/agents/{name}/config  (admin endpoints)     │
│  (new router, authenticated)                                 │
│                                                              │
│  chat_with_agent MCP tool → NO payment check (always free)   │
└──────────────────────────────────────────────────────────────┘
```

### Isolation Guarantees

1. **No existing code paths modified** — all changes are additive (new files, new tables, new routes, new tab)
2. **Lazy SDK imports** — `payments-py` is never imported at module level; import occurs inside service methods only
3. **Graceful degradation** — if `payments-py` is unavailable (import error, not installed), a module-level `NEVERMINED_AVAILABLE` flag is set to `False`. Nevermined endpoints return `501 Not Implemented`. All other platform functionality operates normally.
4. **No shared database state** — `nevermined_agent_config` and `nevermined_payment_log` have no foreign key constraints to existing tables. `execution_id` is a logical reference only.
5. **Independent failure domain** — a bug in any Nevermined code affects only `/api/paid/` and `/api/nevermined/` endpoints. No existing endpoint behavior changes.

### Data Model

```
┌──────────────────────────────┐     ┌──────────────────────────────┐
│  nevermined_agent_config     │     │  nevermined_payment_log      │
├──────────────────────────────┤     ├──────────────────────────────┤
│  id TEXT PK                  │     │  id TEXT PK                  │
│  agent_name TEXT UNIQUE      │     │  agent_name TEXT             │
│  encrypted_credentials TEXT  │     │  execution_id TEXT FK        │
│  nvm_environment TEXT        │     │  action TEXT                 │
│  nvm_agent_id TEXT           │     │  subscriber_address TEXT     │
│  nvm_plan_id TEXT            │     │  credits_amount INT          │
│  credits_per_request INT     │     │  tx_hash TEXT                │
│  enabled INT                 │     │  remaining_balance INT       │
│  created_at TEXT             │     │  success INT                 │
│  updated_at TEXT             │     │  error TEXT                  │
└──────────────────────────────┘     │  created_at TEXT             │
                                     └──────────────────────────────┘
```

- `encrypted_credentials`: AES-256-GCM encrypted `NVM_API_KEY` (same system as subscription tokens)
- `action`: `verify` | `settle` | `reject`
- `execution_id`: FK to `schedule_executions.id` — links payment to task execution

---

## Nevermined x402 Protocol Reference

### Two-Phase Payment Pattern

**`verify_permissions()`** — Called BEFORE processing:
- Checks subscriber has valid authorization and sufficient credits
- Does NOT consume/burn credits (simulation only)
- Returns: `is_valid`, `payer` (wallet address), `agent_request_id`

**`settle_permissions()`** — Called AFTER successful processing:
- Actually burns/consumes credits (on-chain settlement)
- Returns: `success`, `transaction` (tx hash), `credits_redeemed`, `remaining_balance`
- If work fails, don't call settle — no credits burned

### Access Token

Created externally via Nevermined's `CreatePermission` endpoint. Contains:
- `planId` — which payment plan
- `maxCredits` — credit limit
- `usedCredits` — consumption tracking
- `expiresAt` — access expiration
- `permissionHash` — unique identifier
- Cryptographic signature for verification

### 402 Response

The 402 response is the discovery mechanism. It tells callers:
- Plan ID and pricing (credits per request)
- Agent ID
- Endpoint URL
- How to obtain an access token

### Environment Variables (per agent)

```bash
NVM_API_KEY=sandbox:eyJhbGci...   # Format: "env:jwt" — environment prefix + colon + JWT
                                   # From nevermined.app → Settings → API Keys
                                   # Accepted env prefixes: sandbox, live, staging_sandbox, staging_live, custom
NVM_ENVIRONMENT=sandbox            # Must match the prefix in NVM_API_KEY
NVM_AGENT_ID=30407126359872...     # Registered agent ID
NVM_PLAN_ID=82820801405882...      # Registered plan ID
```

### SDK Details (payments-py ==1.2.1)

Critical gotchas from SDK source review and local testing:
- Most SDK methods are **sync** (use `requests` internally) — must wrap in `asyncio.to_thread()`
- `payments.a2a` is a dict, not an object: `payments.a2a["get_client"](...)`
- `Payments.get_instance()` creates a client for specific credentials
- **NVM_API_KEY format**: Must be `"env:jwt"` format, e.g. `"sandbox:eyJhbGci..."` (colon-separated environment prefix + JWT). Plain keys fail with "Invalid NVM API Key".
- Dependencies: `payments-py` pulls in `a2a-sdk`, `mcp`, `aiohttp`, `python-socketio`, `pyjwt` as transitive deps (heavy dependency tree — note for Docker image size)

#### Verified SDK API Surface

**Factory** (from `payments_py.payments`):
```python
from payments_py.payments import Payments
from payments_py.common.types import PaymentOptions

# nvm_api_key MUST be in "env:jwt" format (e.g. "sandbox:eyJhbGci...")
payments = Payments.get_instance(PaymentOptions(
    nvm_api_key="sandbox:eyJhbGci...",
    environment="sandbox"  # Accepted: "sandbox", "live", "staging_sandbox", "staging_live", "custom"
))
```

**Helper** (from `payments_py.x402.helpers`):
```python
from payments_py.x402.helpers import build_payment_required

payment_required = build_payment_required(
    plan_id="82820801405882...",                     # required
    endpoint="/api/paid/{agent_name}/chat",          # optional
    agent_id="30407126359872...",                     # optional
    http_verb="POST",                                # optional
    network="eip155:84532",                          # default: Base Sepolia testnet
    description=None,                                # optional: human-readable description
)
# Returns: X402PaymentRequired Pydantic model
```

**Note**: The helper uses `network` (CAIP-2 chain ID, e.g. `"eip155:84532"`) — NOT `scheme` or `environment`.
The scheme is always `"nvm:erc4337"` (hardcoded inside the helper). There is also a
`build_payment_required_for_plans()` variant that accepts multiple plan IDs.

**Facilitator** (from `payments_py.x402.facilitator_api`):
```python
# verify_permissions() — BEFORE work, does NOT burn credits
result: VerifyResponse = payments.facilitator.verify_permissions(
    payment_required: X402PaymentRequired,  # from build_payment_required()
    x402_access_token: str,                 # from payment-signature header
    max_amount: Optional[str] = None,       # upper bound for variable pricing
)
# Returns: VerifyResponse (see below)

# settle_permissions() — AFTER successful work, burns credits on-chain
result: SettleResponse = payments.facilitator.settle_permissions(
    payment_required: X402PaymentRequired,
    x402_access_token: str,
    max_amount: Optional[str] = None,       # credits to burn
    agent_request_id: Optional[str] = None, # from verify response, for observability
)
# Returns: SettleResponse (see below)
```

Both methods raise `PaymentsError` on HTTP/network failures.

**Types** (from `payments_py.x402.types`):
```python
class X402PaymentRequired(BaseModel):
    x402_version: int = Field(alias="x402Version")  # always 2
    error: Optional[str] = None                      # human-readable error
    resource: X402Resource                           # protected resource info
    accepts: list[X402Scheme]                        # accepted payment schemes
    extensions: dict[str, Any]                       # empty {} for nvm:erc4337

class X402Resource(BaseModel):
    url: str
    description: Optional[str] = None
    mime_type: Optional[str] = Field(None, alias="mimeType")

class X402Scheme(BaseModel):
    scheme: str                                      # "nvm:erc4337"
    network: str                                     # CAIP-2 format, e.g. "eip155:84532"
    plan_id: str = Field(alias="planId")
    extra: Optional[X402SchemeExtra] = None

class X402SchemeExtra(BaseModel):
    version: Optional[str] = None
    agent_id: Optional[str] = Field(None, alias="agentId")
    http_verb: Optional[str] = Field(None, alias="httpVerb")

class VerifyResponse(BaseModel):
    is_valid: bool = Field(alias="isValid")
    invalid_reason: Optional[str] = Field(None, alias="invalidReason")
    payer: Optional[str] = None                      # wallet address
    agent_request_id: Optional[str] = Field(None, alias="agentRequestId")
    url_matching: Optional[str] = Field(None, alias="urlMatching")
    agent_request: Optional[Any] = Field(None, alias="agentRequest")

class SettleResponse(BaseModel):
    success: bool
    error_reason: Optional[str] = Field(None, alias="errorReason")
    payer: Optional[str] = None
    transaction: str = ""                            # blockchain tx hash
    network: str = ""                                # CAIP-2 format
    credits_redeemed: Optional[str] = Field(None, alias="creditsRedeemed")
    remaining_balance: Optional[str] = Field(None, alias="remainingBalance")
    order_tx: Optional[str] = Field(None, alias="orderTx")  # auto-top-up tx
```

**Note on version**: GitHub main branch shows 1.3.4, PyPI public shows 0.3.1. Research tested 1.2.1 successfully. Pin to `payments-py==1.2.1` and install from GitHub if PyPI version lags.

**Note on FastAPI middleware**: The SDK provides `PaymentMiddleware` (`payments_py.x402.fastapi.middleware`) that handles the full x402 flow automatically. We intentionally use manual verify/settle calls instead for full control over logging, error handling, and `TaskExecutionService` integration.

---

## Implementation Plan

### Phase 1: Backend Foundation

#### 1.1 Pydantic Models
**Modify**: `src/backend/db_models.py`

New models:
- `NeverminedConfigCreate` — request model (nvm_api_key, nvm_environment, nvm_agent_id, nvm_plan_id, credits_per_request)
- `NeverminedConfig` — stored config (id, agent_name, nvm_environment, nvm_agent_id, nvm_plan_id, credits_per_request, enabled, timestamps)
- `NeverminedPaymentResult` — verify/settle result mapped from SDK's `VerifyResponse`/`SettleResponse`:
  - `success: bool` — from `is_valid` (verify) or `success` (settle)
  - `payer: Optional[str]` — subscriber wallet address
  - `agent_request_id: Optional[str]` — from verify response
  - `credits_redeemed: Optional[str]` — from settle response
  - `remaining_balance: Optional[str]` — from settle response
  - `tx_hash: Optional[str]` — from settle `transaction` field
  - `error: Optional[str]` — from `invalid_reason` (verify) or `error_reason` (settle)

#### 1.2 Database Schema
**Modify**: `src/backend/db/schema.py`

Add `nevermined_agent_config` and `nevermined_payment_log` tables (see Data Model above).

Indexes:
- `idx_nvm_config_agent` on `nevermined_agent_config(agent_name)`
- `idx_nvm_payment_log_agent` on `nevermined_payment_log(agent_name, created_at DESC)`
- `idx_nvm_payment_log_execution` on `nevermined_payment_log(execution_id)`

#### 1.3 Database Migration
**Modify**: `src/backend/db/migrations.py`

Add migration #23 `nevermined_tables` — CREATE TABLE for both tables + indexes. Idempotent.

#### 1.4 DB Operations Module
**Create**: `src/backend/db/nevermined.py`

`NeverminedOperations` class following `db/subscriptions.py` pattern:
- Lazy-init encryption service (same `CredentialEncryptionService`)
- `create_or_update_config(agent_name, nvm_api_key, ...)` — upsert, encrypt key
- `get_config(agent_name)` — returns config without decrypted key
- `get_config_with_key(agent_name)` — returns config + decrypted NVM_API_KEY (internal only)
- `delete_config(agent_name)`
- `set_enabled(agent_name, enabled)`
- `is_nevermined_enabled(agent_name)` — fast bool check
- `log_payment(agent_name, execution_id, action, success, ...)`
- `get_payment_log(agent_name, limit=50)`

#### 1.5 Wire into DatabaseManager
**Modify**: `src/backend/database.py`

Import `NeverminedOperations`, instantiate in `__init__`, add delegate methods (one-liner per method, same pattern as subscription delegates).

#### 1.6 Backend Dependency
**Modify**: `docker/backend/Dockerfile`

Add `payments-py==1.2.1` (exact pin) to the pip install block. Only needed in backend container — agent containers don't need it.

**Dependency conflict check**: `payments-py` pulls in `pyjwt` as a transitive dependency.
Trinity already uses `python-jose[cryptography]==3.3.0` for JWT operations. These two packages
can coexist but both provide `import jwt`. Verify during Docker build that:
1. `python-jose` is still importable as `from jose import jwt`
2. `pyjwt` doesn't shadow `python-jose`'s jwt module

> Historical note (abilityai/trinity-enterprise#1124): the backend itself now signs and verifies its JWTs with PyJWT; python-jose stays installed only while a private module still imports it, so the coexistence check above is no longer a build concern.

If conflict occurs: add `PyJWT` to pip install explicitly BEFORE `payments-py` to control resolution order.

**Install source**: If PyPI version lags behind GitHub (PyPI shows 0.3.1, GitHub shows 1.3.4),
install from GitHub: `pip install git+https://github.com/nevermined-io/payments-py.git@v1.2.1`

### Phase 2: Payment Service + Endpoints

#### 2.1 Payment Service
**Create**: `src/backend/services/nevermined_payment_service.py`

`NeverminedPaymentService` class:
- `_get_payments_client(nvm_api_key, nvm_environment)` — creates `Payments.get_instance()` per-call with agent's own credentials. Key must be in `"env:jwt"` format (e.g. `"sandbox:eyJ..."`). If stored key lacks the prefix, prepend `"{nvm_environment}:"` before passing to SDK.
- `build_402_response(config)` — uses `build_payment_required(plan_id, endpoint, agent_id, http_verb, network)` from `payments_py.x402.helpers` to construct `X402PaymentRequired` object, then serializes to JSON + base64 for response header. Note: uses `network` param (CAIP-2 chain ID like `"eip155:84532"`), not environment name.
- `verify_payment(nvm_api_key, env, config, access_token)` → `NeverminedPaymentResult` — calls `payments.facilitator.verify_permissions(payment_required, access_token)` wrapped in `asyncio.to_thread()`
- `settle_payment(nvm_api_key, env, config, access_token, execution_id)` → `NeverminedPaymentResult` — calls `payments.facilitator.settle_permissions(payment_required, access_token)` wrapped in `asyncio.to_thread()`
  - **Retry strategy**: 3 attempts with exponential backoff (1s, 2s, 4s)
  - On final failure: log with `action='settle_failed'` in `nevermined_payment_log`
  - Return success response to caller with `payment.settled: false` and `payment.settle_retry_needed: true`
  - Settlement failures are surfaced via admin endpoint (see §2.3)

All SDK calls wrapped in `asyncio.wait_for(asyncio.to_thread(...), timeout=N)`:
- `verify_permissions()`: 15s timeout
- `settle_permissions()`: 30s timeout (blockchain settlement is slower)
- `build_payment_required()`: 5s timeout (local computation, but guard against SDK bugs)

Timeout raises `asyncio.TimeoutError` → logged as payment error, returns 504 to caller.

Singleton via `get_nevermined_payment_service()`.

#### 2.2 Paid Chat Router
**Create**: `src/backend/routers/paid.py`

Endpoints:
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/paid/{agent_name}/chat` | x402 | Main paid endpoint — 402/403/200 flow |
| `GET` | `/api/paid/{agent_name}/info` | None | Agent info + payment requirements |

`/info` endpoint behavior:
- Returns `404` if agent doesn't exist OR Nevermined is not enabled (prevents agent name enumeration)
- Rate limited: 30 requests/minute per source IP (use FastAPI middleware or manual tracking)
- Response includes only public information: plan_id, credits_per_request, endpoint URL
- Does NOT return agent_id, API key, or any credential material

`/chat` flow:
1. Load Nevermined config for agent (404 if not configured or not enabled)
2. Extract `payment-signature` header
3. No header → 402 with `payment-required` header + JSON body
4. Has header → `verify_payment()` (403 on failure)
5. Log verification → `task_execution_service.execute_task(triggered_by="paid")`
6. Success → `settle_payment()` → return response + payment receipt
7. Failure → skip settle → return error

Request model:
```python
class PaidChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
```

Response includes:
```python
{
    "response": str,
    "execution_id": str,
    "payment": {
        "credits_burned": int,
        "remaining_balance": int,
        "tx_hash": str
    }
}
```

#### 2.3 Nevermined Admin Router
**Create**: `src/backend/routers/nevermined.py`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/nevermined/agents/{name}/config` | Configure Nevermined (owner/admin) |
| `GET` | `/api/nevermined/agents/{name}/config` | Get config (no decrypted key) |
| `DELETE` | `/api/nevermined/agents/{name}/config` | Remove config |
| `PUT` | `/api/nevermined/agents/{name}/config/toggle` | Enable/disable |
| `GET` | `/api/nevermined/agents/{name}/payments` | Payment history |
| `GET` | `/api/nevermined/settlement-failures` | List unsettled payments (admin) |
| `POST` | `/api/nevermined/retry-settlement/{log_id}` | Retry a failed settlement (admin) |

All endpoints require authentication + agent access check (owner or admin).

#### 2.4 Register Routers
**Modify**: `src/backend/main.py`

Import and `app.include_router()` for `paid_router` and `nevermined_router`.

### Phase 3: Frontend UI

#### 3.1 NeverminedPanel Component
**Create**: `src/frontend/src/components/NeverminedPanel.vue`

Sections:
- **Configuration form**: API Key (password input with eye toggle), Environment (sandbox/live dropdown), Agent ID (text), Plan ID (text), Credits per request (number)
- **Enable/disable toggle** with status indicator
- **Paid endpoint URL** display with copy button: `{backend_url}/api/paid/{agent_name}/chat`
- **Payment log table**: columns — Time, Action, Subscriber, Credits, Tx Hash, Status
- **Test connection** button (optional: validates credentials against Nevermined API)

API calls:
- `POST/GET/DELETE /api/nevermined/agents/{agentName}/config`
- `PUT /api/nevermined/agents/{agentName}/config/toggle`
- `GET /api/nevermined/agents/{agentName}/payments`

#### 3.2 Add Tab to Agent Detail
**Modify**: `src/frontend/src/views/AgentDetail.vue`

- Import `NeverminedPanel`
- Add `{ id: 'nevermined', label: 'Payments' }` to `visibleTabs` (after credentials tab)
- Add tab content: `<NeverminedPanel :agent-name="agent.name" />`

### Phase 4: MCP Tools

#### 4.1 Tool Definitions
**Create**: `src/mcp-server/src/tools/nevermined.ts`

| Tool | Description |
|------|-------------|
| `configure_nevermined` | Set up payment config for an agent |
| `get_nevermined_config` | Read payment config |
| `toggle_nevermined` | Enable/disable payments |
| `get_nevermined_payments` | Payment history |

Follow `subscriptions.ts` pattern: factory function, Zod schemas, auth context handling.

#### 4.2 Client Methods
**Modify**: `src/mcp-server/src/client.ts`

Add 4 methods:
- `configureNevermined(agentName, config)`
- `getNeverminedConfig(agentName)`
- `toggleNevermined(agentName, enabled)`
- `getNeverminedPayments(agentName, limit?)`

#### 4.3 Register Tools
**Modify**: `src/mcp-server/src/tools/index.ts` — export
**Modify**: `src/mcp-server/src/server.ts` — register with `server.addTool()`

### Phase 5: Demo + Documentation

#### 5.1 Demo Script
**Create**: `scripts/poc/nevermined_demo.py`

Buyer-side demonstration:
1. Hit paid endpoint without payment → show 402 response
2. Parse payment-required details → display plan info
3. Send authenticated request with pre-configured subscriber token
4. Show response + payment receipt

#### 5.2 Documentation
**Modify**: `docs/memory/requirements.md` — Add section 23: Nevermined Payment Integration
**Modify**: `docs/memory/architecture.md` — Add Nevermined subsection
**Create**: `docs/memory/feature-flows/nevermined-payments.md` — Feature flow
**Modify**: `docs/memory/feature-flows.md` — Add to index
**Modify**: `docs/memory/changelog.md` — Add entry

---

## Assumptions & Validation

### Validated Assumptions

All assumptions have been validated via SDK source code review (GitHub `nevermined-io/payments-py` main branch) and local testing (research notes).

| # | Assumption | Validation | Status |
|---|-----------|------------|--------|
| 1 | `payments-py` works with Python 3.11 | `pyproject.toml` requires `^3.10`. Tested with 3.14 in research. | Validated |
| 2 | `payments-py` SDK methods are sync | Confirmed — use `requests` internally, must wrap in `asyncio.to_thread()` | Validated |
| 3 | `CredentialEncryptionService` is reusable | Used by subscriptions.py for token encryption — same pattern applies | Validated |
| 4 | `TaskExecutionService.execute_task()` accepts `triggered_by` string | Confirmed: accepts arbitrary string, used for "manual", "public", "schedule", "agent", "mcp" | Validated |
| 5 | Backend Dockerfile uses pip install (can add dependency) | Confirmed: `docker/backend/Dockerfile` line 8-25, single pip install block | Validated |
| 6 | Migration system is append-only numbered list | Confirmed: `db/migrations.py` has ordered list, #22 is latest | Validated |
| 7 | FastAPI supports 402 status code | Yes: `JSONResponse(status_code=402, ...)` works | Validated |
| 8 | Agent containers don't need payments-py | Correct: all payment logic runs in the backend, not inside agent containers | Validated |
| 9 | `verify_permissions()` and `settle_permissions()` work in sandbox | Confirmed via local A2A round-trip test (research notes) — sandbox API handles verify/settle | Validated |
| 10 | `build_payment_required()` helper exists | Confirmed in `payments_py/x402/helpers.py` — signature: `(plan_id, endpoint=None, agent_id=None, http_verb=None, network="eip155:84532", description=None)`. Uses `network` (CAIP-2), not `scheme`/`environment`. | Validated |
| 11 | `Payments.get_instance()` is the correct factory method | Confirmed in `payments_py/payments.py` — takes `PaymentOptions`, requires `nvm_api_key` in `"env:jwt"` format (e.g. `"sandbox:eyJ..."`) | Validated |
| 12 | Verify/settle stateless across HTTP requests | Confirmed — x402 access token is self-contained (base64-encoded JSON with planId, credits, signature) | Validated |
| 13 | `payment-signature` is the correct header name | Confirmed in `payments_py/x402/fastapi/middleware.py` — default `token_header` value | Validated |

### Open Questions (Resolved)

| # | Question | Resolution |
|---|---------|------------|
| 1 | Should the 402 response include a purchase page link? | **No** — the `X402PaymentRequired` response body already contains `plan_id` and `accepts` array. Callers use `order_plan(plan_id)` via the SDK to purchase. No separate purchase URL needed. The 402 body IS the purchase instruction. |
| 2 | Should we support conversation persistence for paid sessions? | **Defer** — use `session_id` in request for continuity. No special handling needed in Phase 1. |
| 3 | How should rate limiting interact with payment verification? | **Payment IS the rate limit** — `verify_permissions()` checks credit balance. No additional rate limiting needed. Note: verify does NOT reserve credits, so concurrent requests could race (both verify, only one settles). Acceptable for Phase 1. |
| 4 | Should the payment log be queryable from the Dashboard timeline? | **Defer** — `triggered_by="paid"` already surfaces in execution timeline. Payment-specific analytics in a later phase. |

---

## Files Summary

### New Files (7)
| File | Purpose |
|------|---------|
| `src/backend/db/nevermined.py` | DB operations (encrypted config, payment log) |
| `src/backend/services/nevermined_payment_service.py` | Verify/settle via payments-py SDK |
| `src/backend/routers/paid.py` | Paid chat endpoint (x402 flow) |
| `src/backend/routers/nevermined.py` | Admin config endpoints |
| `src/frontend/src/components/NeverminedPanel.vue` | Agent payments UI tab |
| `src/mcp-server/src/tools/nevermined.ts` | MCP tools for payment config |
| `scripts/poc/nevermined_demo.py` | Buyer-side demo script |

### Modified Files (9)
| File | Change |
|------|--------|
| `src/backend/db_models.py` | Add 3 Nevermined Pydantic models |
| `src/backend/db/schema.py` | Add 2 table definitions + indexes |
| `src/backend/db/migrations.py` | Add migration #23 |
| `src/backend/database.py` | Wire NeverminedOperations delegates |
| `src/backend/main.py` | Register 2 new routers |
| `docker/backend/Dockerfile` | Add `payments-py==1.2.1` |
| `src/frontend/src/views/AgentDetail.vue` | Add Payments tab |
| `src/mcp-server/src/client.ts` | Add 4 API methods |
| `src/mcp-server/src/server.ts` + `tools/index.ts` | Register tools |

---

## Security Considerations

1. **NVM_API_KEY never leaves the backend** — encrypted in SQLite, decrypted only for SDK calls, never injected into agent containers
2. **Paid endpoint is unauthenticated by design** — payment IS the authentication (x402 protocol)
3. **No credential exposure in 402 response** — only plan_id, agent_id, pricing (public info)
4. **Payment log stores subscriber address** — wallet addresses, not PII
5. **Transaction hashes are on-chain** — immutable audit trail via blockchain
6. **Verify does NOT reserve credits** — `verify_permissions()` only checks balance, does not lock credits. Two concurrent requests can both pass verification but only one may settle successfully. Settlement catches this (returns error if insufficient). Acceptable for Phase 1; production mitigation: idempotent settlement retry or pre-reserve pattern.
7. **Settlement failure risk** — if work succeeds but `settle_permissions()` fails (network error, blockchain congestion), service has done unpaid work. Mitigate with retry logic and payment log tracking.

## Verification Checklist

- [ ] `docker compose build backend` succeeds with payments-py
- [ ] Backend starts, migration creates tables
- [ ] `POST /api/nevermined/agents/{name}/config` stores encrypted config
- [ ] `GET /api/nevermined/agents/{name}/config` returns config (no key)
- [ ] `GET /api/paid/{agent}/info` returns payment requirements
- [ ] `POST /api/paid/{agent}/chat` without header returns 402
- [ ] `POST /api/paid/{agent}/chat` with invalid token returns 403
- [ ] `POST /api/paid/{agent}/chat` with valid token → verify → execute → settle → receipt
- [ ] Failed execution → no settlement (caller keeps credits)
- [ ] Payment log visible in NeverminedPanel UI
- [ ] MCP tools work: `configure_nevermined`, `get_nevermined_config`, `toggle_nevermined`, `get_nevermined_payments`
- [ ] Internal `chat_with_agent` MCP calls remain free
- [ ] Demo script demonstrates full buyer flow
