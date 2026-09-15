# Nevermined x402 Payments

Monetize agents with per-request payments using the Nevermined x402 payment protocol. Users pay per chat message; agents earn credits.

## Concepts

- **x402 Protocol** -- HTTP-native payment protocol. Unauthenticated requests to a paid endpoint return HTTP 402 (Payment Required) with payment instructions.
- **NVM API Key** -- Nevermined platform API key used to verify payments and settle credits.
- **Agent ID** -- The Nevermined-assigned identifier for the agent being monetized.
- **Plan ID** -- The Nevermined plan that defines credit pricing and allocation.
- **Access Token** -- A payment-signature header value that proves the caller has purchased credits.

## How It Works

### Setup (Admin)

1. Open the agent detail page and select the **Payments** tab. Anyone with access to the agent can read it; changing it needs the owner or an admin.
2. Enter the **NVM API Key**, **Environment** (`sandbox` by default; `live`, `staging_sandbox`, `staging_live` or `custom`), **Agent ID**, and **Plan ID** from Nevermined, and the **Credits per Request** to charge (default 1). Click **Save Configuration**.
3. Flip the **Enabled** toggle to switch payments on; **Remove** deletes the configuration and disables paid access.
4. The agent now has a paid chat endpoint, shown as **Paid Endpoint** on the tab: `POST /api/paid/{agent_name}/chat`. The **Payment Log** below lists settled and failed payments.

### Payment Flow

```
Client -> POST /api/paid/{agent}/chat (no token)
  <- 402 Payment Required (includes payment info)

Client -> Purchases credits via Nevermined checkout page
Client -> POST /api/paid/{agent}/chat (payment-signature header)
  <- 200 OK (agent response, 1 credit deducted)
```

| Step | Action |
|------|--------|
| 1 | Client sends a chat request without payment credentials |
| 2 | Trinity returns HTTP 402 with Nevermined payment details |
| 3 | Client purchases credits via the Nevermined checkout page |
| 4 | Client retries the request with `payment-signature` header containing the access token |
| 5 | Trinity verifies payment, deducts the configured credits, routes to agent |
| 6 | Trinity settles the transaction with Nevermined |

### Payment Info (Public)

Call `GET /api/paid/{agent_name}/info` to retrieve payment requirements without authentication. Returns the Agent ID, Plan ID, checkout URL, and credits per request.

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/paid/{agent_name}/chat` | POST | Paid chat: 402 without a `payment-signature` header, 403 for an invalid or exhausted token, 200 with the reply. Accepts an `Idempotency-Key` header |
| `/api/paid/{agent_name}/info` | GET | Payment requirements (no auth) |
| `/api/nevermined/agents/{name}/config` | POST | Configure payment settings |
| `/api/nevermined/agents/{name}/config` | GET | Get payment configuration |
| `/api/nevermined/agents/{name}/config` | DELETE | Remove payment configuration |
| `/api/nevermined/agents/{name}/config/toggle` | PUT | Enable or disable payments |
| `/api/nevermined/agents/{name}/payments` | GET | Payment history |
| `/api/nevermined/settlement-failures` | GET | Failed settlements (admin) |
| `/api/nevermined/retry-settlement/{log_id}` | POST | Retry failed settlement (admin) |

### MCP Tools

| Tool | Description |
|------|-------------|
| `configure_nevermined` | Set NVM API Key, environment, Agent ID, Plan ID and credits per request |
| `get_nevermined_config` | Retrieve current payment configuration |
| `toggle_nevermined` | Enable or disable payments |
| `get_nevermined_payments` | View payment history |

## Limitations

- Only one Nevermined plan per agent.
- Settlement failures must be retried manually via the admin endpoint.
- The `payment-signature` header is required on every paid request; there is no session persistence.

## See Also

**Trinity docs:**

- [MCP Server](mcp-server.md) — the `configure_nevermined` tool family
- [Chat API](../api-reference/chat-api.md) — the unpaid chat routes the paid endpoint wraps

**External references:**

- [Nevermined Documentation](https://docs.nevermined.io/) — registering agents and plans, buying credits, access tokens
