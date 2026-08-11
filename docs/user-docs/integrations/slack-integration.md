# Slack Integration

Connect agents to Slack workspaces. Supports DMs, @mentions in channels, multi-agent routing, and thread continuity.

## Concepts

- **Channel Adapter** -- Pluggable abstraction for external messaging platforms. Slack, Telegram, and WhatsApp are implemented on the same interface.
- **Socket Mode** -- Default transport using a WebSocket connection. No public URL required. Configured via a Slack App Token (`xapp-...`).
- **Webhook Mode** -- HTTP webhook transport for production environments (fallback option).
- **Multi-Agent Routing** -- Multiple agents can share one Slack workspace. Each agent is bound to a dedicated channel. DMs are routed to a default agent; @mentions in channels route to the bound agent.
- **Thread Tracking** -- The bot automatically responds to thread replies without requiring an @mention.

## How It Works

### Platform Setup (Admin -- Settings Page)

1. Go to **Settings > Slack** section.
2. Enter your Slack App Token (`xapp-...`) for Socket Mode.
3. Click **Connect** to start the Socket Mode transport.
4. Click **Install to Workspace** to perform platform-level OAuth and obtain a bot token.
5. The connection status badge shows connected or disconnected.

### Per-Agent Channel Binding (Agent Sharing Tab)

1. Open the agent detail page and select the **Sharing** tab.
2. Under **Channels**, click **Configure** on the **Slack** row — the Slack configuration opens in a dialog.
3. Click **Create Channel**. A dedicated Slack channel is created and bound to this agent.
4. All messages in that channel are routed to the bound agent.
5. To disconnect, click **Unbind**.

### Changing the DM-Default Agent

DMs to the Slack bot are routed to the workspace's **DM-default agent**. By default the first agent bound to a workspace becomes the DM default, but you can reassign it at any time.

1. Open the agent detail page for the agent you want to receive DMs.
2. Go to the **Sharing** tab → Slack Channel section.
3. Click **Set as DM Default**.
4. The previous DM-default agent retains its channel binding but no longer receives DMs.

**Rules:**
- Only one agent per workspace can be the DM default at a time.
- You cannot unbind the current DM-default agent while other agents are still bound to the workspace. Reassign the DM default to another agent first, then unbind.
- Changing the DM default takes effect immediately — no restart required.

### Message Flow

```
Transport -> Adapter -> Router -> Agent -> Response
```

| Message Type | Routing |
|--------------|---------|
| DM to bot | Default agent |
| @mention in channel | Bound agent for that channel |
| Thread reply (no @mention) | Same agent that was originally mentioned |

**Inbound files.** Files attached to a Slack message are downloaded through a host allow-list: the authenticated first request may only go to Slack's own API host, and a redirect is followed only to Slack's file and edge-CDN hosts, over HTTPS, with each hop re-checked and a bounded number of hops. A download to any other host is refused and logged as an error — so a file that fails to reach your agent is visible in the logs rather than silently dropped.

### Agent Identity in Channels

- **The agent sees who's talking and where.** Channel (non-DM) messages reach the agent with an identity prefix such as `[Channel: #engineering]` / `[From: John Smith (@johndoe)]`, so it can address people by name and adapt to the room. DMs stay clean — no prefix.
- **The agent replies as itself.** Replies post with the agent's name and its avatar as the per-message bot icon (via the `chat:write.customize` scope), so multiple agents in one workspace are visually distinct.

By default, every agent shares one workspace bot and is told apart by that per-message name and avatar — so a channel with several agents already reads as distinct voices.

### Per-Agent Dedicated Bots (enterprise)

On the enterprise tier, an agent can have its **own dedicated Slack bot identity** instead of sharing the workspace bot: its own name and avatar, and — the part a shared bot can't do — a bot users can **DM directly** and **`@mention` by name**, right alongside other agents in the same channel. Configure it per agent from the **Dedicated Slack bot** panel on the agent's Sharing tab (enable/disable, replace tokens, remove). In a community build this panel is hidden and the shared-workspace-bot model above applies.

### Voice Replies (Outbound)

The agent can speak its replies as inline MP3 voice clips uploaded into the thread (Slack renders MP3 with a built-in player). Enable the shared **Voice replies** toggle inside the Slack dialog — see [Voice Replies](../advanced/voice-replies.md).

### Proactive Channel Messages

Agents can post to their bound Slack channels without waiting to be mentioned — for scheduled digests, alerts, or follow-ups:

- MCP tools: `list_channel_groups(channel_type: "slack")` discovers the agent's bound channels; `send_group_message(channel_type: "slack", chat_id, message, thread_ts?)` posts to one, optionally into an existing thread via `thread_ts`.
- REST: `GET /api/agents/{name}/slack/channels` lists bound channels; `POST /api/agents/{name}/slack/channels/{channel_id}/messages` posts (owner-gated).
- Proactive posts carry the agent's identity (name + avatar icon), same as replies.

#### Per-Channel Proactive Consent

Posting **proactively** to a Slack channel requires a per-channel consent toggle — distinct from the per-recipient consent that governs proactive DMs. **Binding an agent to a channel is not itself consent**: a newly bound channel *denies* proactive posts by default. The owner enables it in the Slack channel panel, or via `PUT /api/agents/{name}/slack/channels/{channel_id}/proactive`.

- A denied proactive attempt returns a named "not allowed" error (403), kept distinct from *not bound* (404) and *rate-capped* (429), so the agent can relay **why** the post did not land.
- Replying inline to a user's own message never needs consent — this gate applies only to agent-initiated posts.

#### Completion Report-Back

When an agent starts a long-running or delegated job that inherited Slack channel context, it posts a short note back to the **originating channel/thread** on completion — so the room sees the outcome without re-asking.

- Gated on the same per-channel proactive consent above; a channel that denies proactive posts gets no completion note.
- The completion text is credential-sanitized before it is posted.
- A normal inline Slack reply is never double-reported — the report-back fires only for jobs that finish out of band.

#### Proactive Rate Limits

Proactive channel posts are rate-limited over a rolling **1-hour window**. The caps are **defaults, admin-configurable in Settings** (`0` = unlimited):

| Cap | Default |
|-----|---------|
| Messages per channel per hour | 10 |
| Messages per agent per hour | 100 |

Message body cap: 4,000 characters. Read or update the caps via `GET /api/settings/proactive-rate-limits` and `PUT /api/settings/proactive-rate-limits` (per-key integer, `0..max`).

### Rate Limiting

| Setting | Default |
|---------|---------|
| Messages per window per Slack user | 30 |
| Window duration | 60 seconds |
| Execution timeout | 120 seconds |
| Allowed tools | WebSearch, WebFetch |

Rate limit and timeout values are configurable via settings (`channel_rate_limit_max`, `channel_rate_limit_window`).

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/settings/slack/status` | GET | Connection state |
| `/api/settings/slack/connect` | POST | Start Socket Mode |
| `/api/settings/slack/disconnect` | POST | Stop transport |
| `/api/settings/slack/install` | POST | OAuth install |
| `/api/agents/{name}/slack/channel` | GET | Channel binding status |
| `/api/agents/{name}/slack/channel` | POST | Create and bind channel |
| `/api/agents/{name}/slack/channel` | DELETE | Unbind channel |
| `/api/agents/{name}/slack/channel/dm-default` | PUT | Set this agent as the DM default for its workspace |
| `/api/agents/{name}/slack/channels` | GET | List channels bound to this agent (for proactive messaging) |
| `/api/agents/{name}/slack/channels/{channel_id}/messages` | POST | Post a proactive message to a bound channel (owner-gated, rate-limited) |
| `/api/agents/{name}/slack/channels/{channel_id}/proactive` | PUT | Toggle per-channel proactive consent (owner-gated) |
| `/api/settings/proactive-rate-limits` | GET | Read proactive rate-limit caps |
| `/api/settings/proactive-rate-limits` | PUT | Update proactive rate-limit caps (admin; per-key integer `0..max`, `0` = unlimited) |

Full request/response schemas are in the [Swagger docs](http://localhost:8000/docs).

## Limitations

- Only one Slack workspace can be connected per Trinity instance.
- Webhook Mode requires a publicly accessible URL.
- Thread tracking applies only to threads started by a bot message or @mention.
- Rate limits are per Slack user, not per agent.

## See Also

**Trinity docs:**

- [Agent Sharing & Access](../sharing-and-access/agent-sharing.md) — the Sharing tab that hosts the Slack dialog
- [Voice Replies](../advanced/voice-replies.md) — spoken replies across channels
- [Telegram Integration](telegram-integration.md) · [WhatsApp Integration](whatsapp-integration.md)

**External references:**

- [Slack: Socket Mode](https://api.slack.com/apis/socket-mode) — the default transport (no public URL needed)
- [Slack: chat.postMessage](https://api.slack.com/methods/chat.postMessage) — the message-posting primitive, including `username`/`icon_url` customization
