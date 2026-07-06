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

### Using Tokens

Include the token in the `Authorization` header for all authenticated requests:

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/agents
```

MCP API keys (`trinity_mcp_*`) can be used in the same way as JWT tokens.

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
