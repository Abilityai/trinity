# Authentication

Trinity supports three authentication methods: admin password login, email verification login, and MCP API keys.

## Concepts

- **JWT Token** -- All authenticated API calls require a Bearer token in the `Authorization` header. Tokens use HS256 signing, are valid for 7 days, and are invalidated when the backend restarts. Logging out revokes the token immediately (server-side blacklist until its natural expiry) — an exfiltrated token dies with the session instead of living out its 7 days.
- **MCP API Key** -- Keys prefixed with `trinity_mcp_` also work as Bearer tokens. Used for MCP server authentication and agent-to-agent communication.
- **Agent-Scoped Key** -- An MCP API key restricted to a specific agent, used for agent-to-agent calls.

## How It Works

### Admin Login

Send a form-encoded POST (not JSON) to the token endpoint. The `username` field accepts `admin` **or** the admin's registered email address:

```bash
curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password'
# Returns: {"access_token": "eyJ...", "token_type": "bearer"}
```

### Email Login (2-step)

1. Request a verification code:
   `POST /api/auth/email/request` with `{"email": "user@example.com"}`
2. Verify the code:
   `POST /api/auth/email/verify` with `{"email": "user@example.com", "code": "123456"}`
3. Returns a JWT token on success.

### Second Factor Pending

If the account requires two-factor authentication, a **correct** password
returns **HTTP 403** — the login is not complete, so no session is issued:

```json
{"detail": "mfa_required", "mfa_required": true, "mfa_enrolled": true,
 "enrollment_required": false, "challenge_token": "eyJ..."}
```

There is **no `access_token`** and **no `token_type`**. Finish the login at
`/api/enterprise/2fa/login/verify` (or `/login/enroll/start` +
`/login/enroll/confirm` if the account has not enrolled yet) using the
`challenge_token` — that is what returns the real token.

Three outcomes, three status codes:

| Status | Meaning |
|---|---|
| 200 | Session issued — `access_token` present |
| 403 | Credentials correct, second factor required — no session |
| 401 | Credentials rejected — no session, no challenge |

The email route (`/api/auth/email/verify`) is not an OAuth2 grant and keeps its
200 for the same case, simply omitting `access_token`.

**Scripts and unattended clients: check the status, and check for the token.**

```bash
resp=$(curl -s -w '\n%{http_code}' -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password')
code=$(echo "$resp" | tail -1)
body=$(echo "$resp" | sed '$d')

if [ "$code" = "403" ]; then
  # Don't echo $body — a challenge carries a short-lived challenge_token,
  # and script stderr often ends up in CI logs.
  echo "login needs a second factor this script cannot complete" >&2
  echo "use an MCP API key (Settings -> MCP Keys) instead" >&2
  exit 1
fi

token=$(echo "$body" | jq -r '.access_token // empty')
[ -n "$token" ] || { echo "login issued no session" >&2; exit 1; }
```

Two-factor authentication applies as soon as the account enrols **or** an
administrator enables the role policy — including before anyone has enrolled —
so an unattended credential can start receiving 403s without its own
configuration changing. For automation, prefer an **MCP API key**
(`trinity_mcp_*`), which is not subject to the second-factor flow.

### Using Tokens

Include the token in the `Authorization` header for all authenticated requests:

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/agents
```

MCP API keys (`trinity_mcp_*`) can be used in the same way as JWT tokens.

### What a Key Is Not

An MCP key resolves to the user who owns it, **carrying that user's role**. On a default installation where the admin owns the agents, an agent's injected key would therefore satisfy a plain "admin only" check.

Endpoints whose blast radius is operator-scale therefore require a **human** caller in addition to an admin role — API keys of any scope are rejected, regardless of the owner's role. This applies to:

- Approving an oversized data-retention deletion
- Restarting or reinitializing the system agent
- Registering, editing, or syncing a skill source
- Reading an agent's credential checklist
- Reading, verifying, or rotating an agent's own MCP key (these additionally require an interactive browser session)
- Binding an agent to a GitHub repository
- Writing an agent evaluation
- Adding or removing organizational (`dept-*` / `reports-to-*`) tags

If you hit a 403 on one of these from an automation, that is the gate working as intended — perform the action from the UI or with a user session.

### API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/token` | POST | None | Admin login (form-encoded) |
| `/api/auth/email/request` | POST | None | Request email verification code |
| `/api/auth/email/verify` | POST | None | Verify email code, returns token |
| `/api/auth/mode` | GET | None | Get auth mode configuration |
| `/api/auth/logout` | POST | JWT | Revoke the current token immediately (idempotent; no-op for MCP keys) |
| `/api/auth/validate` | GET | JWT | Validate current token (rejects revoked tokens) |
| `/api/users/me` | GET | JWT | Get current user info |
| `/api/setup/status` | GET | None | First-time setup status |
| `/api/health` | GET | None | Health check |
| `/api/mcp/keys` | POST | JWT | Create MCP API key |
| `/api/mcp/keys` | GET | JWT | List MCP API keys |
| `/api/mcp/keys/{id}` | DELETE | JWT | Revoke an MCP API key |

### Unauthenticated Endpoints

The following endpoints do not require a Bearer token:
`/api/auth/mode`, `/api/setup/status`, `/api/token`, `/api/health`

## See Also

- [Backend API docs](http://localhost:8000/docs) -- Interactive Swagger UI
- [MCP Server](../integrations/mcp-server.md) -- MCP API key usage and agent-to-agent auth
