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
2. Enter the **NVM API Key**, **Environment** (`sandbox` by default; `live`, `staging_sandbox`, `staging_live` or `custom`), **Agent ID**, and **Plan ID** from Nevermined, and the **Credits per Request** to charge (default 1; `0` for a time-based plan, below). Click **Save Configuration**.
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

### Crypto plans and card (fiat) plans

Nevermined plans are paid either in crypto or by card, and the x402 requirements
Trinity advertises must match the plan — the facilitator checks the buyer's token
against them, so a card plan described as a crypto one is simply unpayable.
Trinity reads the plan's own type from Nevermined and advertises it:

| Plan | Scheme advertised | Network |
|---|---|---|
| Crypto | `nvm:erc4337` | `eip155:8453` (live) / `eip155:84532` (sandbox) |
| Card / fiat | `nvm:card-delegation` | the plan's payment provider — `stripe`, `braintree` or `visa` |

Nothing to configure: the plan type comes from Nevermined, is cached for five
minutes, and `GET /api/paid/{agent}/info` shows the scheme a client will be asked
for. If Nevermined is unreachable when the lookup is first needed, Trinity serves
the last answer it had — or falls back to `nvm:erc4337` — and logs a warning
naming the plan.

### The URL in a 402 must be the URL you call

The 402 carries a `resource.url` that your payment token is minted and verified
against, so it has to be the origin a buyer actually reaches Trinity on. Trinity
uses the public URL you configured (**Public URL** under **Settings → General**, or the
`PUBLIC_CHAT_URL` / `FRONTEND_URL` environment variables) when it matches the
host of the incoming request, and otherwise the request's own host — upgraded to
`https` when your proxy sends `X-Forwarded-Proto: https`. If a buyer reports a
token rejected for the wrong resource URL, check that your TLS terminator
forwards that header and that the configured public URL is the hostname clients
really use.

### Paid calls over A2A

The same paywall guards an agent's A2A door (`POST /a2a/{agent_name}`) once the agent is both exposed over A2A and has payments enabled. A caller with a Trinity key still runs free; a caller without one gets `402` and pays with the same Nevermined token. A priced agent's A2A card also states the plan, the credits per call and a `paymentInfoUrl` pointing at `/api/paid/{agent}/info`, so a payment-aware client can pay on its first request. The reply carries the payment status and receipt in the Task's metadata. The details — where the token goes in an A2A message, retries and refusals — are in [A2A Protocol](a2a-protocol.md#charge-for-inbound-a2a-calls).

### Time-based plans

For a duration plan, set **Credits per Request** to `0`. The plan charges by time, so Trinity sends no per-call amount and the A2A card advertises the cost as plan-defined. A negative value is rejected.

### Operator settings

Every payment verify and settle is a call to the Nevermined facilitator. Two optional environment variables bound those calls across the whole platform; both need a backend restart:

| Variable | Default | Meaning |
|----------|---------|---------|
| `NEVERMINED_MAX_INFLIGHT` | `8` | The most facilitator calls in flight at once, across all agents |
| `NEVERMINED_FACILITATOR_WAIT_SECONDS` | `5.0` | How long a call waits for a free slot before answering "busy, retry". A refused call never reaches the facilitator and burns nothing |

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/paid/{agent_name}/chat` | POST | Paid chat: 402 without a `payment-signature` header, 403 for an invalid or exhausted token, 200 with the reply. Accepts an `Idempotency-Key` header |
| `/api/paid/{agent_name}/info` | GET | Payment requirements, including the plan's scheme and an absolute `resource.url` (no auth) |
| `/a2a/{agent_name}` | POST | The A2A task door; charges a caller without a Trinity key when the agent is exposed and priced — see [A2A Protocol](a2a-protocol.md) |
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
- The `payment-signature` header is required on every paid request to `/api/paid/{agent}/chat`; there is no session persistence. (On the A2A door the token travels in the message metadata instead.)
- The A2A paid door needs the agent's A2A exposure turned on; a price alone does not open it.

## See Also

**Trinity docs:**

- [MCP Server](mcp-server.md) — the `configure_nevermined` tool family
- [A2A Protocol](a2a-protocol.md) — exposing an agent over A2A, and paying for inbound A2A calls
- [Public Access](../guides/deploying/public-access.md) — publishing the paid and A2A doors on a public hostname
- [Chat API](../api-reference/chat-api.md) — the unpaid chat routes the paid endpoint wraps

**External references:**

- [Nevermined Documentation](https://docs.nevermined.io/) — registering agents and plans, buying credits, access tokens
