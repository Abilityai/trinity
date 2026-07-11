# Trinity FAQ — MCP & API

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What is the Trinity MCP server?

The MCP server exposes Trinity's agent orchestration as callable tools over the Model Context Protocol, at `http://localhost:8080/mcp` (Streamable HTTP). Any MCP client — Claude Code, another agent, or your own tooling — can use it to create and manage agents, chat with them, run schedules and loops, check fleet health, and more. Authentication is an API key sent as a Bearer token. See [MCP Server](../integrations/mcp-server.md).

## How do I create an MCP API key?

Open the **API Keys** page in the web UI, click **Create Key**, and optionally scope the key to a specific agent. The generated key is prefixed `trinity_mcp_` and is shown only once at creation — copy and store it securely. You then send it as `Authorization: Bearer trinity_mcp_your-key`. See [MCP Server](../integrations/mcp-server.md).

## What is the difference between user, agent, and system key scopes?

A **user-scoped** key acts as you: it can reach any agent you own, any agent shared with you, and everything if you are an admin. An **agent-scoped** key belongs to one agent and is restricted to that agent plus the agents it has been explicitly granted permission to call — Trinity auto-generates one per agent for agent-to-agent communication. The **system** scope is reserved for the built-in system agent, which bypasses permission checks for platform operations; you don't create system keys yourself. See [MCP Server](../integrations/mcp-server.md).

## How do I connect Claude Code to Trinity?

Add Trinity as an MCP server in your Claude Code configuration (`.mcp.json`) with your API key in the `Authorization` header:

```json
{
  "mcpServers": {
    "trinity": {
      "type": "url",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer trinity_mcp_your-key"
      }
    }
  }
}
```

Once connected, Trinity's tools appear in your client's tool list. See [MCP Server](../integrations/mcp-server.md).

## How many MCP tools are there, and what can they do?

The MCP server exposes 93 tools grouped into families: agent lifecycle and credentials (22 tools), chat and fan-out, schedules, execution queries, skills, tags, system manifests, subscriptions, fleet health monitoring, git operations, sequential loops, operator-queue reading and responses, plus single-purpose tools like `share_file`, `send_message`, and `report`. The largest family covers agent management — create, start/stop, rename, delete, credential injection, GitHub sync, and data export/import. See [MCP Server](../integrations/mcp-server.md).

## Can I expose one of my agents as its own MCP tool?

Yes. On the agent's **Settings** tab, the **Expose via MCP** section has an owner-only toggle; when enabled, the MCP server registers a dedicated `chat_with_<slug>` tool (the slug is derived from the agent name, and the resolved tool name is shown next to the toggle). No restart is needed — the MCP server picks up the change on its next poll, and connected clients see the tool appear or disappear within a few seconds. Exposure publishes the tool, not access: callers still need ownership or a share to actually chat with the agent. See [MCP Server](../integrations/mcp-server.md).

## Why can't my agent call another agent over MCP?

Agent-to-agent access is deny-by-default: an agent's own key only reaches itself plus agents it has been explicitly granted. Grants live in the agent's Permissions tab — until one exists, `list_agents` won't even show the other agent to the caller, and `chat_with_agent` blocks the target. Add a permission from the source agent to the target agent and the call goes through. See [Agent Permissions](../collaboration/agent-permissions.md).

## How do I revoke an MCP API key?

Delete the key from the **API Keys** page in the web UI, or call `DELETE /api/mcp/keys/{key_id}` with your JWT. Revocation deactivates the key so future validations fail. You can only manage your own keys (admins can list everyone's). See [Authentication](../api-reference/authentication.md).

## How do I authenticate against the REST API?

Send a form-encoded (not JSON) POST to the token endpoint, then use the returned JWT as a Bearer token:

```bash
curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password'

curl -H "Authorization: Bearer <token>" http://localhost:8000/api/agents
```

The `username` field accepts `admin` or the admin's registered email; regular users log in with an emailed 6-digit code instead. Tokens are valid for 7 days, and `POST /api/auth/logout` revokes one immediately. See [Authentication](../api-reference/authentication.md).

## Can I use an MCP key instead of a JWT on the REST API?

Yes. Keys prefixed `trinity_mcp_` work as Bearer tokens on authenticated REST endpoints, exactly like a JWT: `Authorization: Bearer trinity_mcp_your-key`. This is handy for scripts and long-lived automations, since MCP keys don't expire after 7 days and survive backend restarts — revoke them from the API Keys page when you're done. See [Authentication](../api-reference/authentication.md).

## Why did my API calls start returning 401 after I restarted Trinity?

JWT tokens are invalidated when the backend restarts, so every logged-in session and stored token dies — log in again via `POST /api/token` (or the email flow) to get a fresh one. MCP API keys are unaffected: they're validated against the database, so they keep working across restarts. If a call fails with a key, check that the key wasn't revoked. See [Authentication](../api-reference/authentication.md).

## What is the Idempotency-Key header for?

It makes retries safe on endpoints that trigger an execution (`/chat`, `/task`, `/fan-out`, VoIP calls, webhook triggers). Send any unique string per logical request; if you resend the same key within 24 hours, Trinity returns the original result with the header `X-Idempotent-Replay: true` instead of creating a second execution. A duplicate sent while the first request is still running returns 409 with the original `execution_id` to poll. The header is optional and fail-open — omitting it preserves normal behavior. See [Chat API](../api-reference/chat-api.md#idempotency).

## What happens when a chat call from an MCP client takes too long?

Synchronous `chat_with_agent` calls cap at `MCP_CHAT_TIMEOUT_MS` (default 25 seconds). If the agent hasn't finished by then, the tool returns `{status: "queued_timeout", execution_id, message}` instead of an answer — the task keeps running on the agent. Poll `get_execution_result` with that `execution_id` to fetch the result when it completes; the call is not duplicated. See [MCP Server](../integrations/mcp-server.md).

## How do I run a long task via the API and check its result later?

Submit it with `POST /api/agents/{name}/task`, then poll `GET /api/agents/{name}/executions` and `GET /api/agents/{name}/executions/{id}` for status, response, cost, and duration. The agent's configured execution timeout is authoritative — omit the deprecated per-task `timeout_seconds` field and raise the agent's timeout cap if you need longer runs. For hands-off recurring triggers, use schedules or webhooks instead. See [Chat API](../api-reference/chat-api.md) and, for webhooks, [Webhook Triggers](../api-reference/webhook-triggers.md).

## Is there a command-line tool for Trinity?

Yes — install it with `pip install trinity-cli` (or `brew install abilityai/tap/trinity-cli`), then run `trinity init` to connect: it prompts for your instance URL and email, verifies a 6-digit code, and auto-provisions an MCP API key into `~/.trinity/config.json`. Named profiles (`trinity profile list` / `use` / `remove`) let you switch between local, staging, and production instances, with `TRINITY_URL` / `TRINITY_API_KEY` environment variables as overrides. Current coverage includes agents, deploy, chat, logs, health, skills, schedules, and tags, with `--format json` for scripting. See [Trinity CLI](../cli/trinity-cli.md).

## Where can I browse the full REST API?

The backend serves interactive Swagger documentation at `http://localhost:8000/docs` (adjust the host for your deployment). It lists every endpoint with request/response schemas, and you can authorize with your Bearer token and try calls directly from the browser. The user docs cover the most-used endpoints in the API reference section. See [Authentication](../api-reference/authentication.md) and [Agent API](../api-reference/agent-api.md).

## Why doesn't my MCP client see Trinity's tools after a backend restart?

MCP clients must be reconnected manually after the backend restarts — the session doesn't recover on its own. In Claude Code, run `/mcp` and reconnect the Trinity server, or restart the client. Your API key is still valid; only the connection needs re-establishing. See [MCP Server](../integrations/mcp-server.md).
