# Trinity FAQ — MCP & API

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## What is the Trinity MCP server?

The MCP server exposes Trinity's agent orchestration as callable tools over the Model Context Protocol (Streamable HTTP) — at `http://localhost:8080/mcp` on a local install, or `https://your-domain.com/mcp` behind the production frontend. Any MCP client — Claude Code, another agent, or your own tooling — can use it to create and manage agents, chat with them, run schedules and loops, check fleet health, and more. Authentication is an API key sent as a Bearer token. See [MCP Server](../integrations/mcp-server.md).

## How do I create an MCP API key?

Go to **Settings → MCP Keys**, click **Create API Key**, and give it a name. The generated key is prefixed `trinity_mcp_` and is shown only once — copy and store it securely, then send it as `Authorization: Bearer trinity_mcp_your-key`. A key created this way is a **Standard** (user-scoped) key that acts as you. Admins additionally see a **Key scope** choice with two bounded alternatives: **Ops (read-only)**, a route-fenced machine credential for monitoring dashboards, and **Portal delegate**; both are admin-only and minted from an interactive browser session, never by another key. You don't create agent-scoped keys by hand — Trinity mints one per agent automatically and injects it into the container. See [MCP Server](../integrations/mcp-server.md).

## What is the difference between user, agent, and system key scopes?

Every key carries one of six scopes, fixed at creation. A **user** key (the **Standard** choice) acts as you with your full role — any agent you own or that is shared with you, everything if you are an admin. An **agent** key belongs to one agent: its own heartbeats, reports, and result callbacks plus the agents it has been explicitly granted permission to call; it never passes an admin-only endpoint. The **system** scope is reserved for the built-in system agent, which bypasses agent permission checks. Three bounded scopes round it out: **connector** (consumption-only — one exposed agent's chat and playbooks, minted from that agent's **Expose via MCP** panel), **ops** (read-only: version, fleet status, health, schedules, alerts, costs, host and container telemetry, execution history and the live log stream — every write is refused and it gets no MCP tools), and **portal_delegate** (exactly one route, exchanging an end-user email for a Workspace session; that route requires an entitlement). The keys page badges the non-standard ones — **Agent**, **Ops (read-only)**, **System**, **Portal Delegate** — so a bounded key is never mistaken for a personal one, and an ops key still needs its owner to hold the admin role at call time. For why the gates are shaped this way, see the [Security FAQ](security.md); for the full table, see [Authentication](../api-reference/authentication.md#mcp-key-scopes).

## How do I connect Claude Code to Trinity?

Add Trinity as an MCP server in your Claude Code configuration (`.mcp.json`) using the `http` transport, with your API key in the `Authorization` header:

```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer trinity_mcp_your-key"
      }
    }
  }
}
```

The **Settings → MCP Keys** page shows a ready-made connection snippet with the URL for your install, and once connected Trinity's tools appear in your client's tool list. See [MCP Server](../integrations/mcp-server.md).

## Which URL should I point my MCP client at — port 8080 or my domain?

On a local install use `http://localhost:8080/mcp`, the MCP server's own published port. On a production install behind the frontend use `https://your-domain.com/mcp`: the frontend proxies `/mcp` to the MCP server, so a host that exposes only ports 80/443 (a firewalled server, a one-click cloud image) serves MCP without opening 8080. The URL Trinity advertises — in the MCP Keys connection snippet and in an exposed agent's **Copy connection config** — is auto-detected from the request host as `http://<host>:8080/mcp`, which is wrong for exactly those installs; an admin sets the real one under **Settings → MCP Keys → MCP Server URL** (it must end in `/mcp`, and **Auto-detect** shows what would be advertised otherwise). See [MCP Server](../integrations/mcp-server.md#which-url-to-use).

## How many MCP tools are there, and what can they do?

The MCP server exposes 154 tools across 36 modules: 149 operator tools, three consumption-only tools for connector keys, and two sign-in tools that exist only when inline email auth is on. The largest family is agent management (22 tools — create, start/stop, rename, delete, credential injection, SSH access, GitHub sync and per-agent GitHub tokens, template info, runtime-data export/import); the rest cover chat and fan-out, execution queries, schedules, skills, tags, system manifests, subscriptions, fleet health, git operations, loops, shared rooms, reminders, the operator queue, notifications and proactive messages, structured reports, event pub/sub, and outbound A2A calls, plus single-purpose tools like `share_file`, `write_user_memory`, `send_voice_reply`, and `call_user`. The metrics tools (`record_metrics`, `get_metrics`, `get_objectives`, `refresh_metric_definitions`) let an agent record and read back its declared business metrics — see [Dynamic Dashboards](../advanced/dynamic-dashboards.md#declared-metrics). Two newer families are worth knowing: the canvas tools (`set_canvas`, `patch_canvas`, `get_canvas`, `list_canvases`, `clear_canvas`) render an agent's durable block layout beside the conversation in the Workspace — see [Agent Canvas](../agents/agent-canvas.md) — and the vault tools (`list_available_credentials`, `fetch_credential`) let an agent pull a granted credential by name at runtime — see [Credential Management](../credentials/credential-management.md#credential-vault). A few tools — among them the Workspace project tools and the inbound A2A controls — answer `"disabled"` or "not available" on an instance without the matching entitlement. See [MCP Server](../integrations/mcp-server.md#tool-categories).

## How does an agent record a business metric over MCP?

Declare the metric in the agent's `template.yaml` under `metrics:`, then call `record_metrics(points=[{"metric": "revenue", "value": 1240.5}], execution_id=...)`. A batch is all-or-nothing, and each bad point comes back with a reason code. If the name is refused as `metric_undeclared`, add it to `template.yaml` and call `refresh_metric_definitions`. Read the values back with `get_metrics`, or targets vs actuals with `get_objectives`. See [Dynamic Dashboards](../advanced/dynamic-dashboards.md#declared-metrics).

## Can I expose one of my agents as its own MCP tool?

Yes. On the agent's **Settings** tab, the **Expose via MCP** section has an owner-only toggle; when enabled, the MCP server registers a dedicated `chat_with_<slug>` tool (the slug is derived from the agent name, and the resolved tool name is shown next to the toggle). No restart is needed — the MCP server picks up the change on its next poll, and connected clients see the tool appear or disappear within a few seconds. Exposure publishes the tool, not access: callers still need ownership or a share to actually chat with the agent. While exposure is on, the same panel offers **Connect an external client**: **Copy connection config** mints (or reuses) a connector-scoped key that reaches only this agent and copies a ready-to-paste `.mcp.json` for Claude Code, Cursor, or Claude Desktop. An existing key's secret is never shown again — **Regenerate & copy** embeds a fresh one — and the key can be revoked from the same panel. See [MCP Server](../integrations/mcp-server.md#dedicated-agent-tools-expose-via-mcp).

## Why can't my agent call another agent over MCP?

Agent-to-agent access is deny-by-default: an agent's own key only reaches itself plus agents it has been explicitly granted. Grants live in the agent's Permissions tab — until one exists, `list_agents` won't even show the other agent to the caller, and `chat_with_agent` and `run_agent_loop` block the target with an `Access denied` result. Add a permission from the source agent to the target agent and the call goes through. See [Agent Permissions](../collaboration/agent-permissions.md).

## What's the difference between revoking and deleting an MCP key?

Two routes, two outcomes. `POST /api/mcp/keys/{key_id}/revoke` **deactivates** the key: future validations fail, but its record stays for the audit trail, where every key-authenticated call is attributed by key id, name and scope. `DELETE /api/mcp/keys/{key_id}` **permanently deletes** it. You can only manage your own keys (admins can list everyone's), and creating or listing keys needs a signed-in browser session — **Settings → MCP Keys** is the UI for them. Two kinds of key are handled elsewhere: a connector key minted from an agent's **Expose via MCP** panel is revoked from that panel, and an agent's own injected key is rotated from the agent's Settings tab rather than deleted by hand (see the MCP key panel question below). See [Authentication → API Endpoints](../api-reference/authentication.md#api-endpoints).

## How do I authenticate against the REST API?

Send a form-encoded (not JSON) POST to the token endpoint, then use the returned JWT as a Bearer token:

```bash
curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password=your-password'

curl -H "Authorization: Bearer <token>" http://localhost:8000/api/agents
```

The `username` field accepts `admin` or the admin's registered email; regular users log in with an emailed 6-digit code instead. Tokens are valid for 7 days, and `POST /api/auth/logout` revokes one immediately. If the account has two-factor authentication turned on (an entitlement-gated feature), a correct password answers **403** with a challenge and no token — finish the login in the browser, or give scripts an MCP key, which is never subject to the second factor. See [Authentication](../api-reference/authentication.md).

## Can I use an MCP key instead of a JWT on the REST API?

Yes. Keys prefixed `trinity_mcp_` work as Bearer tokens on authenticated REST endpoints, exactly like a JWT: `Authorization: Bearer trinity_mcp_your-key`. This is handy for scripts and long-lived automations, since MCP keys don't expire after 7 days and survive backend restarts — revoke them from **Settings → MCP Keys** when you're done. A few routes refuse every key and need a signed-in browser session, including creating and listing MCP keys themselves. See [Authentication](../api-reference/authentication.md).

## Why did my API calls start returning 401 after I restarted Trinity?

JWT tokens are invalidated when the backend restarts, so every logged-in session and stored token dies — log in again via `POST /api/token` (or the email flow) to get a fresh one. MCP API keys are unaffected: they're validated against the database, so they keep working across restarts. If a call fails with a key, check that the key wasn't revoked. See [Authentication](../api-reference/authentication.md).

## What is the Idempotency-Key header for?

It makes retries safe on endpoints that trigger an execution (`/chat`, `/task`, `/fan-out`, VoIP calls, webhook triggers). Send any unique string per logical request; if you resend the same key within 24 hours, Trinity returns the original result with the header `X-Idempotent-Replay: true` instead of creating a second execution. A duplicate sent while the first request is still running returns 409 with the original `execution_id` to poll (for `/fan-out`, that field carries the batch's `fan_out_id`). A first attempt that was rejected before dispatch, or a sync `/task` that failed, timed out, or was cancelled, releases the key so a legitimate retry goes through. The header is optional and fail-open — omitting it preserves normal behavior. See [Chat API](../api-reference/chat-api.md#idempotency).

## Why is `chat_with_agent` returning `queued_timeout` instead of a reply?

Synchronous `chat_with_agent` calls — sequential chat (`parallel=false`) and the sync task route (`parallel=true, async=false`) alike — give up at `MCP_CHAT_TIMEOUT_MS` (default 25 seconds), before the MCP gateway does. Instead of a transport error such as `fetch failed`, the tool returns a receipt `{status: "queued_timeout", agent, execution_id, message}`; the task keeps running on the agent, and you poll `get_execution_result` with that `execution_id`. The receipt is issued only when the running execution can be attributed to *your* call unambiguously — otherwise the error says so and points you at `list_recent_executions`. Calls carry a deterministic idempotency key, so an identical re-send answers with the same `execution_id`, but a **reworded** re-send is a new call and dispatches a second execution. For work you know will run long, use `parallel=true, async=true` from the start. See [MCP Server](../integrations/mcp-server.md#key-tools-worth-knowing).

## How do I poll a fan-out batch that outlived the call?

A batch runs longer than any single task in it, so `fan_out` is the tool most likely to hit the 25-second ceiling. When it does, it answers with a receipt — `{status: "fan_out_timeout", agent, fan_out_id, execution_ids, task_count, message}` — and the batch keeps running; nothing is lost. Poll `get_fan_out_result(agent_name, fan_out_id)` (or `GET /api/agents/{name}/fan-out/{fan_out_id}`): the batch reads `running` while any task can still change, then `completed`, `partial` (some succeeded — normal for a best-effort batch), or `failed`, with per-task execution status and results. Every fan-out response carries the `fan_out_id`, timed out or not. Don't re-send to "try again": an identical re-send is deduplicated and answers with the same batch, but **rewording it dispatches all N tasks a second time**. See [Fan-Out](../automation/fan-out.md#polling-a-batch).

## Can I start a fan-out and collect the results later?

Yes. Pass `async_mode: true` to `fan_out` (or in the body of `POST /api/agents/{name}/fan-out`). The call returns at once with `{fan_out_id, status: "accepted"}` and the tasks run in the background. Poll `get_fan_out_result(agent_name, fan_out_id)` and match each result to your task by its `task_id`. Without `async_mode`, `timeout_seconds` only limits how long the call waits: tasks still open at the deadline report `running` and keep going, so poll the batch for their outcome. See [Fan-Out](../automation/fan-out.md#fire-and-forget-async_mode).

## How do I run a long task via the API and check its result later?

Submit it with `POST /api/agents/{name}/task` and `async_mode: true` — the call returns at once with `status: "accepted"` and an `execution_id`. Then poll `GET /api/agents/{name}/executions/{id}` for status, response, cost, and duration; `…/executions/{id}/log` holds the full transcript and `…/executions/{id}/stream` follows it live over SSE. Without `async_mode` the call holds the connection for the whole run (queuing on the same connection at capacity), and if that long-poll times out while the run is still going, a retry with the same `Idempotency-Key` answers with the execution to poll rather than starting another. The agent's configured execution timeout is authoritative — omit the deprecated per-task `timeout_seconds` and raise the agent's cap if you need longer runs. For hands-off recurring triggers, use schedules or webhooks instead. See [Chat API](../api-reference/chat-api.md#sync-task-calls-and-the-execution_id-receipt) and, for webhooks, [Webhook Triggers](../api-reference/webhook-triggers.md).

## How do I cancel a running task or chat turn through the API?

Call `POST /api/agents/{name}/executions/{id}/terminate`. It answers `terminated` for a run that was in progress, `cancelled_while_queued` or `cancelled_while_parked` for one that had not started yet (removed from the queue without touching the container), or `already_finished`. A run you stop ends as `cancelled` — a distinct state, not a failure — and releases its capacity slot. A turn started from a public link has its own `POST /api/public/executions/{token}/{execution_id}/terminate`, scoped so it stops only turns that link started, never a scheduled run or an operator chat on the same agent. See [Chat API](../api-reference/chat-api.md#task-execution).

## Can I attach files to a chat or task call through the API?

Yes. `POST /api/agents/{name}/chat` and `POST /api/agents/{name}/task` accept a `files` array of `{name, mimetype, size, data_base64}` (raw base64 or a `data:` URI). Images reach the agent as vision content; every other file lands in `/home/developer/uploads/` inside the container. Accepted types are images, plain text, CSV, JSON, and ZIP — a ZIP is stored unextracted for the agent to unpack itself. PDF, tar/gzip/rar, audio, and video are rejected. The web limits are 3 files per message, 5 MB per file, and 10 MB of images in total. See [Chat API](../api-reference/chat-api.md#file-attachments).

## Why was my chat or task call refused with "is not a model id"?

The `model` you sent can't name a model. `POST /chat`, `POST /task`, and a session turn check the value before anything runs, and answer `422` with the offending value in the message. Use a short alias (`sonnet`, `opus`, `haiku`, `fable`) or a full id that starts with a known family, such as `claude-sonnet-4-6`; leave the field empty to use the agent's default. A refused call starts no execution and does not use up its `Idempotency-Key`, so you can fix the value and retry with the same key. See [Chat API](../api-reference/chat-api.md#model-override).

## Is there a command-line tool for Trinity?

Yes — install it with `pip install trinity-cli` (or `brew install abilityai/tap/trinity-cli`), then run `trinity init` to connect: it prompts for your instance URL and email, verifies a 6-digit code, and auto-provisions an MCP API key into `~/.trinity/config.json` (`--profile <name>` saves the instance under a named profile; `--admin` logs in with the admin password instead of a code). Named profiles (`trinity profile list` / `use` / `remove`) let you switch between local, staging, and production instances, with `TRINITY_URL` / `TRINITY_API_KEY` environment variables as overrides; `trinity login` re-authenticates a profile (`--instance <url> --profile <name>` adds or repoints one), `trinity status` shows which profile and instance you are on, and `trinity logout` clears the current profile's stored credentials. Current coverage includes agents, deploy, chat, logs, health, skills, schedules, and tags, with `--format json` for scripting. See [Trinity CLI](../cli/trinity-cli.md).

## Where can I browse the full REST API?

The backend serves interactive Swagger documentation at `http://localhost:8000/docs` (adjust the host for your deployment). It lists every endpoint with request/response schemas, and you can authorize with your Bearer token and try calls directly from the browser. The user docs cover the most-used endpoints in the API reference section. See [Authentication](../api-reference/authentication.md) and [Agent API](../api-reference/agent-api.md).

## Which endpoints work without a token, and where is the health check?

The health check is `GET /health` — at the root, not under `/api` — and it answers 503 while schema migrations are incomplete, so a deploy script can wait on it. The other token-free routes are the login and setup flow — `/api/auth/mode`, `/api/auth/email/request`, `/api/auth/email/verify`, `/api/token`, `/api/setup/status` and `/api/setup/admin-password` (which provisions the *first* admin only and answers 403 once a usable admin account exists) — and `POST /api/access/request`, public self-signup that adds the submitted email to the login allow-list; it is disabled by default and answers 403 until an admin enables public access requests. Everything else needs a JWT or an MCP key. See [Authentication → Unauthenticated Endpoints](../api-reference/authentication.md#unauthenticated-endpoints).

## Why doesn't my MCP client see Trinity's tools after a backend restart?

MCP clients must be reconnected manually after the backend restarts — the session doesn't recover on its own. In Claude Code, run `/mcp` and reconnect the Trinity server, or restart the client. Your API key is still valid; only the connection needs re-establishing. See [MCP Server](../integrations/mcp-server.md).

## Can I connect an MCP client to Trinity without creating an API key first?

If your admin has enabled inline authentication, yes: connect with a keyless connector config, call `request_login(email)` to get a 6-digit code by email, then `verify_login(code)`. You can then use the exposed playbooks of every agent shared with that email. The login binds a **session**, not a key — nothing is written to disk, and because MCP sessions are per-connection, restarting your client means logging in again. Inline auth is off by default (`MCP_INLINE_AUTH_ENABLED`). See [MCP Server](../integrations/mcp-server.md).

## What is the MCP key panel on an agent's Settings tab for?

Every agent carries its own agent-scoped key so it can call Trinity's MCP server — and that key is what makes the agent-to-agent permission matrix apply. A container carrying a *user*-scoped key would operate with the owner's identity and bypass the matrix entirely. The panel shows the key's health (`active`, `never_used`, `stale`, `missing`, `env_absent`, `env_mismatch`, or `exempt` for the system agent) and two actions: **Check what the container is using** (**Re-check** afterwards) runs a one-shot probe inside the container and reports what its configuration actually contains — a foreign user key, another agent's key, a duplicate entry; a stopped agent reads "unavailable" rather than erroring — and **Regenerate key** (**Issue a key** when none exists) mints a new one, delivers it to the container and deletes the superseded keys, rebuilding a running agent to pick it up. The secret is never displayed, and these routes work only from an interactive browser session — API keys of any scope are rejected, because rotating a credential should not be doable with the credential itself. Trinity also self-heals a missing or mismatched key on the agent's next start. See [MCP Server → Each Agent's Own MCP Key](../integrations/mcp-server.md#each-agents-own-mcp-key).

## Why does my automation get a 403 on an endpoint my account is admin on?

Two gates cause this, and both are working as intended. First, admin-only endpoints are an **allowlist over key scopes**: only a browser session, a **user** (Standard) key, or the system key can pass one — an **agent**, **connector**, **ops**, or **portal delegate** key is refused even though it resolves to an admin owner, because a key carries its owner's role and an agent's injected key would otherwise satisfy every "admin only" check (an individual read route may opt the ops scope in; the ops routes do). Second, endpoints whose blast radius is operator-scale require a **human** caller and reject API keys of any scope: approving an oversized retention deletion, turning usage sharing on or off, restarting the system agent, managing skill sources, reading a credential checklist, reading or rotating an agent's MCP key, creating or listing MCP keys of any scope, minting an ops or portal-delegate key, binding an agent to a repository, writing an evaluation, and editing org tags. Perform those from the UI or with a user session. For the security reasoning behind the gates, see the [Security FAQ](security.md); the mechanics are in [Authentication](../api-reference/authentication.md#what-a-key-is-not).

## How do I receive Trinity's real-time events in my own client?

Open `/ws/events?token=trinity_mcp_…` with an MCP key in the query string — this is the stream Trinity Connect and external listeners use, and it delivers events for the agents the key's owner can access. Only **user**, **agent** (non-ephemeral agents), and **system** keys may open it; **ops**, **connector**, and **portal delegate** keys are closed with code 4003. The browser's own `/ws` socket is different: it takes a single-use, 30-second ticket from `POST /api/ws/ticket` (JWT in `Authorization`) instead of a token in the URL, and it is scoped too — an event naming agents is delivered only when every agent it names is one you may access, for live delivery and reconnect replay alike; admins see everything. See [Authentication](../api-reference/authentication.md#websocket-authentication).

## Is there a browser terminal for my agent?

No — the terminal tab that once lived on the agent page has been retired, and SSH is the only direct-shell path. An admin turns on **Enable SSH Access** under **Settings → Access**, then `POST /api/agents/{name}/ssh-access` (or the `get_agent_ssh_access` MCP tool) injects your own public key into the running container for `ttl_hours` (default 4, maximum 24) and returns a ready-to-run `ssh` command on the agent's port in the 2222–2262 range. Password SSH is not supported, and Trinity never generates or sees a private key. The interactive PTY WebSocket route remains available to API clients that drive it themselves. See [Agent Terminal](../agents/agent-terminal.md).

## Can I ask Trinity questions about Trinity from my MCP client?

Yes — the `ask_trinity` tool answers from the documentation, and returns a `session_id` you can pass back for follow-ups (it tells you when a session reset dropped your context). It is also published standalone as the `trinity-docs-mcp` npx package, which needs no Trinity instance and no API key. See [MCP Server](../integrations/mcp-server.md).

## Can an agent read or answer the Operating Room queue over MCP?

Yes. `list_operator_queue` reads the queue — approvals, questions, alerts — and `get_operator_queue_item` fetches one item; an agent-scoped key sees only the calling agent plus the agents it has permission to reach, so one agent cannot browse another's asks without a grant. `respond_to_operator_queue` resolves an item, within the same boundary — but only with a person's own key: an agent's key is refused, so an agent can never answer its own request. An agent raises an item as itself with `ask_operator`, which checks it at once and returns a receipt (the queue file still works for two releases), and reads how one of its own items ended with `get_my_ask`, by the id it chose. The human side — where an agent's question appears and how you answer it — is in the [Operations FAQ](operations-and-monitoring.md). See [MCP Server → Key Tools Worth Knowing](../integrations/mcp-server.md#key-tools-worth-knowing).

## Why does listing or creating MCP keys with an API key return 403?

Because the key inventory is human-only. `GET /api/mcp/keys` (list) and `POST /api/mcp/keys` (create) take a signed-in browser session, and every MCP key is refused — your own Standard key included. A credential that mints credentials must be at least as strict as what it produces, and the key list has no machine consumer. Manage keys under **Settings → MCP Keys**, or call the routes with a JWT from `POST /api/token`. Revoking and deleting a key still work with any authenticated caller who owns it. See [MCP Server → API Endpoints](../integrations/mcp-server.md#api-endpoints).

## Can I see what an agent can do before I call it over MCP?

Yes. `get_agent_info(name)` returns the agent's template metadata: display name, description, version, capabilities, commands, MCP servers, tools, skills and example use cases. An agent-scoped key can read only agents it has permission to call. To see which templates you can create agents from, call `list_templates`; pass one of its IDs to `create_agent`. See [MCP Server → Tool Categories](../integrations/mcp-server.md#tool-categories).

## Can I set an agent's GitHub token over MCP?

Yes. `set_agent_github_pat(agent_name, pat)` stores a per-agent token: it is checked against GitHub, encrypted at rest, and pushed into the running agent so git and `gh` use it at once. Pass an empty string to clear it and fall back to the platform token. `get_agent_github_pat_status(agent_name)` says whether the agent uses its own token or the global one, and never returns the token. Setting a token is owner-only, and the agent must already have Git sync configured. See [GitHub PAT Setup → Per-agent PAT override](../integrations/github-pat-setup.md#per-agent-pat-override).

## Can I turn git auto-sync or pull-sync on through the API?

Yes, through REST, not MCP. `PUT /api/agents/{name}/git/auto-sync`, `PUT /api/agents/{name}/git/pull-sync` and `PUT /api/agents/{name}/git/freeze-schedules-if-failing` each take `{"enabled": true}` from the agent's owner. Turning pull-sync on or off also needs a person: an agent's own key is refused. There are no MCP tools for these switches; the agent's **Settings → Git sync** section is the UI. See [GitHub Sync → For Agents](../integrations/github-sync.md#for-agents).

## Do I need an enterprise entitlement to register outbound A2A endpoints?

No. The outbound endpoint registry — `register_a2a_endpoint`, `list_a2a_endpoints`, `remove_a2a_endpoint`, and the `/api/settings/a2a-endpoints` routes behind them — works on every edition. It needs an admin acting as a person, because the list is platform-wide, and outbound calling stays off until an admin switches it on. Only the inbound side (exposing an agent, its card and the inbound allow-list) needs the A2A entitlement. See [A2A Protocol](../integrations/a2a-protocol.md).

## What happens when an external A2A agent I call wants payment?

Its `402 Payment Required` comes back as a 402 with `payment_required: true`, the remote's price and plan, and `do_not_retry: true`. Trinity never buys anything. Relay the price to a person once; they buy the token from the provider, and an admin stores it on the endpoint with `register_a2a_endpoint` (the credential kind is inferred, or pass `credential_kind: "payment_token"`). Then call again with the `task_id` from the refusal so the remote resumes the same task. A spent or expired payment token comes back as `payment_rejected`. See [A2A Protocol → When the remote charges for the call](../integrations/a2a-protocol.md#when-the-remote-charges-for-the-call).
