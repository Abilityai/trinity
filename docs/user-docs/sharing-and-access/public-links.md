# Public Links

Shareable URLs that let unauthenticated users chat with agents. Supports optional email verification, session persistence, per-user memory, and rate limiting.

## Concepts

- **Public Link** -- A unique URL (e.g., `/public/chat/{token}`) that allows anyone to chat with an agent without logging in.
- **Email Verification** -- Optional. If enabled, users must verify their email before chatting. Rate limiting is applied per-email.
- **Session Persistence** -- Multi-turn conversations persist across page refreshes. Sessions are email-based (verified) or anonymous.
- **Per-User Memory** -- Email-verified sessions maintain persistent per-user memory scoped to `(agent_name, user_email)`. Updated via background summarization every 5 messages.
- **Dynamic Thinking Status** -- Real-time status labels showing agent activity (same as authenticated chat).

## How It Works

1. Open the agent detail page and go to the **Sharing** tab. Public links live under **Distribution → Public links**.
2. Click **Create Public Link**.
3. Configure the link: email verification on/off, rate limits, custom welcome message.
4. Copy the generated URL and share it with recipients.
5. Recipients open the URL and start chatting immediately.
6. If email verification is enabled: the user enters their email, receives a verification code, verifies, then chats.
7. Conversations persist -- the user can return later and continue where they left off.
8. Click **New Conversation** to start a fresh session.

### Model and Instructions for Public Chats

Two per-agent settings on the Sharing tab shape public-link conversations (and channel chats) without touching the agent's core configuration: the **Public chat model** override and the **Additional instructions — public & channel chats only** field. Both apply to public links, Slack/Telegram/WhatsApp, and paid chat — never to the owner's own chats or schedules. See [Agent Sharing & Access](agent-sharing.md#public-chat-model).

### Connect Slack from a Public Link

Each public link card has a **Slack** row with a **Connect Slack** button (owner or admin only). It is a shortcut into the Slack channel binding described in [Slack Integration](../integrations/slack-integration.md):

- If the platform's Slack workspace is already connected, Trinity creates a Slack channel named after the agent, binds the agent to it, and makes the agent the DM default when it is the first agent bound in that workspace.
- If no workspace is connected yet, the button opens Slack's authorization page; the platform Slack app must be configured under **Settings → Integrations → Slack** first, or the button reports that Slack is not configured.

Once connected, the row shows the workspace name and who connected it, with an enable/disable toggle and **Disconnect Slack**. The Slack conversation follows the same access rules as the link itself.

### Chat History for Logged-In Users

When a Trinity user who is logged in opens a public chat link, a **history dropdown** appears at the top of the chat interface showing their previous conversations with that agent. This lets them resume any past session rather than always starting fresh.

- Sessions are identified by the user's verified email, so history is consistent across devices.
- Clicking a session in the dropdown loads its full message history.
- Anonymous visitors (not logged into Trinity) see no history dropdown.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/public-links` | GET | List public links |
| `/api/agents/{name}/public-links` | POST | Create a public link |
| `/api/agents/{name}/public-links/{id}` | PUT | Update a public link |
| `/api/agents/{name}/public-links/{id}` | DELETE | Delete a public link |
| `/api/public/chat/{token}` | POST | Send a message via public chat |
| `/api/public/history/{token}` | GET | Retrieve chat history |
| `/api/agents/{name}/public-links/{id}/slack` | GET / PUT / DELETE | Slack connection status, settings, disconnect |
| `/api/agents/{name}/public-links/{id}/slack/connect` | POST | Connect Slack: binds a channel, or returns an OAuth URL when no workspace is connected |

## Limitations

- Rate limiting applies per user or session.
- Anonymous sessions have no cross-session continuity.
- Per-user memory requires email verification to be enabled.

## See Also

- [Agent Sharing](agent-sharing.md)
