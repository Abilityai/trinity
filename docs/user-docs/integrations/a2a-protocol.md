# A2A Agent Card

A standardized discovery endpoint that publishes each Trinity agent's capabilities in [A2A v1.0](https://a2aproject.github.io/A2A/v0.3.0/specification/) format, so external orchestrators (AWS Bedrock, Azure Copilot, Google ADK) can discover and call them without knowing Trinity's internal API.

## Concepts

- **A2A (Agent-to-Agent)** — An open spec for inter-agent interoperability. The Agent Card is its `template.yaml` analogue: a JSON document advertising what an agent does, where to reach it, and what auth it expects.
- **Agent Card** — The JSON returned by Trinity's A2A endpoint. Generated from the agent's `template.yaml` + container labels. No agent-server changes required.

## How It Works

Every Trinity agent exposes its A2A Agent Card at:

```
GET /api/agents/{name}/a2a/agent-card
```

The endpoint requires the same `AuthorizedAgentByName` gate as the rest of the per-agent API — owner, admin, or shared user.

The card includes:

- `name` — Agent name (machine ID).
- `display_name` — Human label from template.
- `description` — Template description.
- `version` — Template version.
- `url` — Resolved external URL (uses `PUBLIC_CHAT_URL` → `FRONTEND_URL` → request host, in that order).
- `capabilities` and `use_cases` — From `template.yaml`.
- `skills` — From the agent's loaded skill catalog (when running).

### Stopped Agents Still Have Cards

Card generation never depends on the agent container being up. If the agent is stopped or its agent-server is unreachable, the endpoint falls back to Docker container labels and returns a partial card with the agent's name, display name, and an empty skills list. The card endpoint never returns 5xx.

### Public `.well-known` Discovery

A2A v1.0 also defines a public `/.well-known/agent-card.json` route for unauthenticated discovery. Trinity does **not** serve that route yet — it requires a separate access-policy decision per agent. Until it ships, external orchestrators that need card data must authenticate via an MCP key with access to the agent.

## For Agents

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/agents/{name}/a2a/agent-card` | GET | JWT or MCP key with access to `{name}` | Return A2A v1.0 Agent Card |

**Example:**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/my-agent/a2a/agent-card | jq
```

## Limitations

- Phase 1 ships authenticated discovery only. Public `/.well-known/agent-card.json` is a follow-up that needs a per-agent access-policy gate.
- The card's `url` falls back to the request host if neither `PUBLIC_CHAT_URL` nor `FRONTEND_URL` is set — fine for internal callers, but external orchestrators should configure one of those env vars so the published URL is reachable from outside the local network.

## See Also

**Trinity docs:**

- [MCP Server](mcp-server.md) — Trinity's primary inter-agent protocol
- [Agent Configuration](../agents/agent-configuration.md)

**External references:**

- [A2A Project specification](https://a2aproject.github.io/A2A/v0.3.0/specification/) — The canonical v1.0 spec
- [a2aproject/A2A](https://github.com/a2aproject/A2A) — Reference implementations
