# MCP Server

Trinity's MCP server exposes 130 tools across 33 modules for agent orchestration via the Model Context Protocol, enabling programmatic control from Claude Code, other MCP clients, or agent-to-agent communication. 125 of them are the operator tool set; three consumption-only tools are visible only to connector keys, and two sign-in tools are registered only when inline email auth is enabled. A few operator tools are enterprise-gated and return `"disabled"` (or a `not available` result) where not entitled.

> 📺 **Watch:** [From Zero to Deployed AI Agent — MCP setup](https://youtu.be/-TSZyekDS6o) *(Apr 2026)* · [all videos](../videos.md)

## Concepts

- **Model Context Protocol (MCP)** — An open standard for tool-based AI integrations. Trinity implements an MCP server that exposes agent management as callable tools.
- **FastMCP** — The server framework used, with Streamable HTTP transport on port 8080.
- **API Keys** — Authentication mechanism for MCP access. Keys are generated under **Settings → MCP Keys** and sent via `Authorization: Bearer` header.
- **Agent-Scoped Keys** — Keys Trinity mints for each agent automatically (its **Trinity access key**, visible on the agent's Settings tab) so the agent can call the platform as itself. You do not create these by hand; the keys you create yourself are `user`-scoped, `ops`, or `portal_delegate` — see [Authentication](../api-reference/authentication.md#mcp-key-scopes).

## How It Works

### Authentication

![MCP API Keys page showing auto-generated agent keys with usage stats and connection snippet](../../screenshots/mcp-api-keys.png)

1. Go to **Settings → MCP Keys**.
2. Click **Create API Key**, name it, and choose a **Key scope** — **Standard** for your own use, or one of the bounded scopes.
3. Copy the generated key (prefixed `trinity_mcp_*`).
4. Use the key as a Bearer token in the `Authorization` header.

#### Signing in with an email code instead of a key

If your admin has enabled inline authentication, you can connect with **no API key at all** and sign in from inside your MCP client:

1. Connect with a keyless connector configuration.
2. Call `request_login(email)` — a 6-digit code is emailed to you.
3. Call `verify_login(code)`.

You can then use the exposed playbooks of every agent shared with that email. This mirrors the `/login` flow that Telegram and WhatsApp already use.

Two things to know: the login binds a **session**, not a key — nothing is written to disk and you are never handed a `trinity_mcp_*` token. And MCP sessions are per-connection, so restarting your client means logging in again.

Inline authentication is **off by default** (`MCP_INLINE_AUTH_ENABLED`). Operators enabling it should set `INTERNAL_API_SECRET` explicitly rather than relying on its fallback.

### Connecting from Claude Code

Add Trinity as an MCP server in your Claude Code configuration:

```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer <your-api-key>"
      }
    }
  }
}
```

#### Which URL to use

- **Local install:** `http://localhost:8080/mcp` — the MCP server's own published port.
- **Production install behind the frontend (nginx):** `https://your-domain.com/mcp`. The frontend proxies `/mcp` to the MCP server, so an install that exposes only ports 80/443 (a firewalled host, a one-click cloud image) still serves MCP without opening port 8080.

The URL Trinity advertises — in the MCP Keys page's connection snippet and in an exposed agent's **Copy connection config** — is auto-detected from the request host as `http://<host>:8080/mcp`. On an 80/443-only install, an admin sets the real one under **Settings → MCP Keys → MCP Server URL** (it must end in `/mcp`); `Auto-detect` there shows what would be advertised otherwise.

### Tool Categories

| Module | Tools | Description |
|--------|-------|-------------|
| `agents.ts` | 22 | Agent lifecycle, credentials, SSH, local deploy, GitHub sync, per-agent PAT, runtime-data export/import, compatibility report |
| `chat.ts` | 4 | `chat_with_agent`, `get_chat_history`, `get_agent_logs`, `fan_out` — chat and parallel dispatch, all gateway-timeout safe |
| `executions.ts` | 4 | `list_recent_executions`, `get_execution_result`, `get_fan_out_result`, `get_agent_activity_summary` — execution queries, polling for async tasks and fan-out batches, activity monitoring |
| `schedules.ts` | 8 | Schedule CRUD and execution history |
| `skills.ts` | 9 | Skill management and assignment, plus the skill-runner tools `run_skill` and `list_runnable_skills` (enterprise-gated — return `"disabled"` in community builds) |
| `tags.ts` | 5 | Agent tagging |
| `systems.ts` | 4 | `deploy_system`, `list_systems`, `restart_system`, `get_system_manifest` — see [System Manifest](../collaboration/system-manifest.md) |
| `subscriptions.ts` | 6 | Subscription management |
| `monitoring.ts` | 3 | Fleet health |
| `nevermined.ts` | 4 | Payment configuration |
| `notifications.ts` | 1 | `send_notification` — agent-to-platform notifications (`alert`, `info`, `status`, `completion`, `question`) |
| `events.ts` | 4 | Agent event pub/sub |
| `docs.ts` | 2 | `get_agent_requirements`, `ask_trinity` — agent documentation and grounded Q&A about Trinity |
| `channels.ts` | 2 | Channel group discovery + proactive group messaging (Telegram and Slack) |
| `messages.ts` | 1 | Proactive user messaging by verified email |
| `voice.ts` | 1 | `send_voice_reply` — speak one reply of the current channel turn as a voice note (see [Voice Replies](../advanced/voice-replies.md)) |
| `files.ts` | 1 | `share_file` — publish file to a signed download URL |
| `memory.ts` | 1 | `write_user_memory` — per-user memory blob, isolated server-side |
| `loops.ts` | 3 | `run_agent_loop`, `get_loop_status`, `stop_loop` — sequential bounded task loops |
| `voip.ts` | 1 | `call_user` — outbound phone call (flag-gated, requires a per-agent voice binding) |
| `operator_queue.ts` | 3 | `list_operator_queue`, `get_operator_queue_item`, `respond_to_operator_queue` — read and resolve Operating Room queue items |
| `reminders.ts` | 3 | `set_reminder`, `list_reminders`, `cancel_reminder` — durable one-shot deferred self-triggers |
| `rooms.ts` | 5 | `create_room`, `list_rooms`, `read_room`, `post_to_room`, `close_room` — multi-agent rooms (see [Rooms](../collaboration/rooms.md)) |
| `canvas.ts` | 5 | `set_canvas`, `patch_canvas`, `get_canvas`, `list_canvases`, `clear_canvas` — the agent's durable render surface in the Workspace (see [Agent Canvas](../agents/agent-canvas.md)) |
| `git.ts` | 6 | Deterministic git operations — status, sync, log, pull, sync-state, and the destructive reset-to-main recovery |
| `pipelines.ts` | 2 | Read-only introspection of an agent's self-published pipelines |
| `reports.ts` | 3 | `report`, `list_reports`, `get_report` — publish a structured report and read reports back (see [Agent Reports](../operations/agent-reports.md)) |
| `a2a.ts` | 7 | A2A management plane — per-agent exposure and card, inbound allow-list, outbound endpoint registry (entitlement-gated; see [A2A Protocol](a2a-protocol.md)) |
| `a2a_call.ts` | 2 | `call_a2a_agent`, `get_a2a_task` — task a registered external A2A agent by endpoint name and poll it |
| `credential_vault.ts` | 2 | `list_available_credentials`, `fetch_credential` — pull a granted vault credential by name at runtime (see [Credential Management](../credentials/credential-management.md#credential-vault)) |
| `assignments.ts` | 1 | `get_agent_assignments` — read who an agent works for (read-only; degrades to a not-available result where unsupported) |
| `connector.ts` | 3 | `list_playbooks`, `run_playbook`, `ask` — the consumption-only set a **connector key** sees; operator tools stay hidden from connector keys |
| `auth.ts` | 2 | `request_login`, `verify_login` — registered only when inline email auth is on, advertised only to keyless sessions |

### Dedicated Agent Tools (Expose via MCP)

An owner can publish an agent as its own first-class MCP tool. On the agent's **Settings** tab, the **Expose via MCP** section has a toggle; when enabled, the MCP server registers a dedicated `chat_with_<slug>` tool (the slug is derived from the agent name, with a short suffix on name collisions — the resolved tool name is shown next to the toggle).

- No restart needed — the MCP server picks up the change on its next poll, and connected MCP clients see the tool appear (or disappear) within a few seconds.
- The tool behaves exactly like `chat_with_agent` with the agent name pre-filled, including idempotency and timeout handling.
- Exposure publishes the *tool*, not access: callers still need ownership or a share to actually chat with the agent.
- While exposure is on, the same panel shows **Connect an external client**: **Copy connection config** mints (or reuses) a connector-scoped key for this agent and copies a ready-to-paste `.mcp.json`. An existing key is never shown again — **Regenerate & copy** embeds a fresh one.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/mcp-exposed` | GET | Exposure flag + the resolved `tool_name` |
| `/api/agents/{name}/mcp-exposed` | PUT | Toggle exposure (`{"enabled": true}`, owner-only) |

### Each Agent's Own MCP Key

Every agent carries its own agent-scoped key, injected into its container so it can call Trinity's MCP server. That key is what makes the agent-to-agent permission matrix apply — a container carrying a *user*-scoped key would operate with the owner's identity and bypass the matrix entirely.

The agent's **Settings** tab surfaces this key so you can see and repair it. You never see the secret itself — only metadata and a health state:

| State | Meaning |
|-------|---------|
| `active` | Healthy and in recent use |
| `never_used` | The key exists but the agent has never authenticated with it |
| `stale` | The key hasn't been used since well before the agent's last execution — the agent is probably authenticating as something else |
| `missing` | No active agent-scoped key exists for this agent |
| `env_absent` | The container has no Trinity MCP key configured |
| `env_mismatch` | The container's key doesn't match any active key for this agent |
| `exempt` | The system agent, which uses a system-scoped key by design |

Two actions:

- **Verify** runs a one-shot probe inside the container and reports what its configuration actually contains — including whether it is carrying a foreign user key, another agent's key, or a duplicate entry. A stopped agent degrades to "unavailable" rather than erroring.
- **Regenerate** rotates the key: a new one is minted, delivered to the container, and the superseded keys are deleted. A running agent is rebuilt to pick it up; a stopped agent is updated in the database and stays stopped. **No plaintext is ever returned.**

Trinity also self-heals: if an agent starts with a missing or mismatched key, the start path re-mints and re-injects one automatically.

These routes are owner-only and reachable only from an interactive (browser) session — API keys of any scope are rejected, because rotating a credential should not be doable with the credential itself.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/mcp-key` | GET | Key metadata and health state (never the secret) |
| `/api/agents/{name}/mcp-key/verify` | POST | Probe the container's actual configuration |
| `/api/agents/{name}/mcp-key/regenerate` | POST | Rotate and deliver a new key |

### Key Tools Worth Knowing

| Tool | Why it exists |
|------|---------------|
| `chat_with_agent` | Send a message to another agent. **Gateway-timeout safe in every sync mode** — sequential chat (`parallel=false`) and the sync task route (`parallel=true, async=false`) alike: if the call exceeds `MCP_CHAT_TIMEOUT_MS` (default 25s), it returns `{status: "queued_timeout", agent, execution_id, message}` so the caller polls `get_execution_result` instead of duplicate-queueing the request. The receipt is only issued when the running execution can be attributed to *your* call unambiguously; otherwise the error says so and names `list_recent_executions`. Calls carry a deterministic idempotency key, so an identical re-send dedupes server-side and answers with the original `execution_id` — a **reworded** re-send is a new call and dispatches a second execution. For work you know will outlive the gateway, use `parallel=true, async=true` from the start. |
| `fan_out` | Dispatch N independent tasks to an agent in parallel and collect all the results. **Gateway-timeout safe**: a batch runs longer than any single task in it, so this is the tool most likely to outlive the 25s ceiling — when it does, it returns `{status: "fan_out_timeout", agent, fan_out_id, execution_ids, task_count, message}` and the batch keeps running. Poll `get_fan_out_result(agent_name, fan_out_id)`. Re-sending the *identical* call is deduplicated server-side and answers with the same batch; **rewording it dispatches all N tasks again**. See [Fan-Out](../automation/fan-out.md). |
| `get_fan_out_result` | Poll a fan-out batch: `running` while any task can still change, then `completed`, `partial` (some succeeded — normal for a best-effort batch) or `failed`, with per-task status and results. |
| `run_agent_loop` | Run the same task against an agent repeatedly (bounded, sequential), with templated messages and an optional stop signal. Poll with `get_loop_status`; stop gracefully with `stop_loop`. See [Agent Loops](../automation/agent-loops.md). |
| `list_operator_queue` | Read the Operating Room queue (approvals, questions, alerts). Agent-scoped keys see only the calling agent plus its permitted agents. Resolve an item with `respond_to_operator_queue`. |
| `set_reminder` | Schedule a durable one-shot deferred self-trigger — the agent re-invokes itself later with a message it picks. Survives restarts; list with `list_reminders`, cancel with `cancel_reminder`. |
| `run_skill` | Run a named skill headlessly (enterprise-gated; returns `"disabled"` in community builds). Discover runnable skills with `list_runnable_skills`. |
| `create_room` | Open a shared multi-agent room and post/read messages (`post_to_room` / `read_room` / `list_rooms` / `close_room`). Rooms are bounded by message, cost, and time budgets — [Rooms](../collaboration/rooms.md) owns the budget defaults. Against an older backend that does not serve rooms the tools return a `shared_sessions_not_enabled` result. |
| `set_canvas` | Render the agent's canvas in the Workspace — a block layout the person the agent works with sees beside the conversation. `patch_canvas` updates blocks in place; `get_canvas` / `list_canvases` read it back; `clear_canvas` empties it. See [Agent Canvas](../agents/agent-canvas.md). |
| `fetch_credential` | Fetch a vault credential the agent has been granted, by name; `list_available_credentials` lists the granted names (never values). The value arrives live and is scrubbed from the saved transcript. Where the vault is not available the tools degrade to a result that says so rather than failing. See [Credential Management](../credentials/credential-management.md#credential-vault). |
| `call_user` | Place an outbound phone call to a user and hold a voice conversation. Server-gated: works only when VoIP is enabled platform-wide and the agent has a voice binding; rate-limited and daily-capped. See [VoIP Telephony](../advanced/voip-telephony.md). |
| `share_file` | The agent drops a file into `/home/developer/public/` and calls this tool to mint a signed, expiring download URL (universal — works for web, Slack, Telegram, WhatsApp, email). |
| `write_user_memory` | Per-user memory blob in an isolated store. Trinity resolves the user's email from `execution_id` server-side, so an agent cannot accidentally cross-write another user's memory. |
| `send_message` | Proactive message to a specific user by verified email. Rate-limited and audit-logged. |
| `send_group_message` | Proactive message to a channel group (Slack channel, Telegram chat). Discovered via `list_channel_groups`. |
| `ask_trinity` | Grounded Q&A about Trinity itself, answered from the documentation. Pass the `session_id` it returns to ask follow-ups; the tool tells you when a session reset dropped your context. Also available standalone as the `trinity-docs-mcp` npx package, with no Trinity instance or API key required. |
| `report` | Publish a structured report (table, KPI set, markdown, timeline). Read them back with `list_reports` / `get_report`. See [Agent Reports](../operations/agent-reports.md). |

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/mcp/keys` | POST | Create API key |
| `/api/mcp/keys` | GET | List API keys |
| `/api/mcp/keys/{key_id}` | DELETE | Revoke API key |
| `/api/settings/mcp-url` | GET/PUT/DELETE | The advertised MCP URL: read the effective and auto-detected values, set an override, or clear it (admin) |

### MCP Endpoint

| Endpoint | Transport | Description |
|----------|-----------|-------------|
| `http://localhost:8080/mcp` | Streamable HTTP | MCP tool server on its own port |
| `https://your-domain.com/mcp` | Streamable HTTP | The same server, proxied by the production frontend — the URL for an install that exposes only 80/443 |

## Limitations

- Agent-scoped keys cannot access tools outside their assigned agent (plus explicitly permitted agents).
- MCP clients must be manually reconnected after a backend restart.
- `chat_with_agent` and `fan_out` sync modes cap at `MCP_CHAT_TIMEOUT_MS` (default 25s). Longer calls switch to poll-mode via the returned `execution_id` (or `fan_out_id`); a receipt is issued only when the running work can be attributed to your call unambiguously.
- Fan-out is self-only: an agent fans out to itself, not to another agent.

## See Also

- [Fan-Out](../automation/fan-out.md) — parallel dispatch and polling a batch
- [Chat API](../api-reference/chat-api.md) — the REST routes the chat and task tools call
- [Nevermined Payments](nevermined-payments.md)
- [Slack Integration](slack-integration.md)
- [A2A Protocol](a2a-protocol.md) — A2A `0.3.0` discovery and inbound tasking for external orchestrators
