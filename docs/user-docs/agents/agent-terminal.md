# Agent Terminal

Direct shell access to an agent container is by SSH with a short-lived key you supply. The browser terminal tab that once lived on the agent page has been retired; the WebSocket it used remains available to API clients.

## Concepts

- **Ephemeral SSH access** — Trinity injects your public key into the agent container's `authorized_keys` for a fixed time, then removes it. The server never generates or sees a private key.
- **Key-based only** — Password SSH authentication is not supported. Generate a keypair locally (`ssh-keygen -t ed25519`) and hand Trinity the public half.

## How It Works

### Enable SSH access (admin)

1. Open **Settings → Access**.
2. Under **SSH Access**, switch on **Enable SSH Access**. While it is off, every request for SSH credentials is refused.

### Get credentials

SSH credentials are issued through the API or an MCP tool, not from a button on the agent page. The agent must be running.

```bash
# 1. Generate a keypair once, locally
ssh-keygen -t ed25519 -f ~/.ssh/trinity_agent

# 2. Ask Trinity to inject the public key (admin token)
curl -s -X POST http://localhost:8000/api/agents/my-agent/ssh-access \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d "{\"ttl_hours\": 4, \"public_key\": \"$(cat ~/.ssh/trinity_agent.pub)\"}"
```

The response carries the connection details — host, port, user `developer`, a ready-to-run `ssh` command, and `expires_at`:

```bash
ssh -i ~/.ssh/trinity_agent -p 2222 developer@<host>
```

- **TTL** — `ttl_hours` defaults to 4; the minimum is 0.1 (six minutes) and the maximum is 24 (`SSH_ACCESS_MAX_TTL_HOURS` on the backend). The key is removed when it expires.
- **Host** — taken from `SSH_HOST` in `.env` when set; otherwise Trinity derives it from `FRONTEND_URL`, a Tailscale address, `host.docker.internal`, or the Docker gateway, in that order.
- **Port** — each agent has its own SSH port from the 2222–2262 range, recorded on the container.

### System agent

The system agent (`trinity-system`) exposes the same WebSocket terminal to API clients on its own route; it has no browser tab either.

## For Agents

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/ssh-access` | POST | Inject a public key for `ttl_hours` and return connection details (admin only, agent must be running) |
| `/api/agents/{name}/terminal` | WebSocket | Interactive PTY over WebSocket; `?mode=claude\|gemini\|bash`, optional `&model=` for the Claude Code TUI |
| `/api/system-agent/terminal` | WebSocket | The same PTY for the system agent (admin only) |

**MCP tool**: `get_agent_ssh_access(agent_name, public_key, ttl_hours=4)` — the same operation for MCP clients; admin only.

## Limitations

- No browser terminal in the UI. Use SSH, or drive the WebSocket route from your own client.
- SSH access is admin-only and off by default.
- An agent that is stopped cannot be reached; start it first.

## See Also

- [Managing Agents](managing-agents.md) — start, stop, and health
- [Agent Chat](agent-chat.md) — the stateless per-turn chat on the agent page
- [Workspace](../sharing-and-access/workspace.md) — the continuous conversation
- [Authentication](../api-reference/authentication.md) — obtaining an admin token
