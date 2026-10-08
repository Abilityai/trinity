# Public Links

Shareable URLs that let unauthenticated users chat with agents. Supports optional email verification, session persistence, per-user memory, and rate limiting.

## Concepts

- **Public Link** -- A unique URL (e.g., `https://your-domain.com/chat/{token}`) that allows anyone to chat with an agent without logging in.
- **Email Verification** -- Set by the agent's access policy, not per link. When the agent requires a verified email (the default), users verify before chatting. See [Access Control](access-control.md).
- **Session Persistence** -- Multi-turn conversations persist across page refreshes. Sessions are email-based (verified) or anonymous.
- **Per-User Memory** -- Email-verified sessions maintain persistent per-user memory scoped to `(agent_name, user_email)`. Updated via background summarization every 5 messages.
- **Dynamic Thinking Status** -- Real-time status labels showing agent activity (same as authenticated chat).

## How It Works

1. Open the agent detail page and go to the **Sharing** tab. Public links live under **Distribution → Public links**.
2. Click **Create Link**. The **Create Public Link** dialog asks for a **Name (optional)** and an **Expiration (optional)**. Email verification and the allow-list come from the agent's access policy on the same tab, and apply across web, Telegram and Slack alike.
3. Copy the URL from the link's card (the copy icon) and share it. Editing a link later adds a **Link enabled** checkbox to switch it off without deleting it.
4. Recipients open the URL. If the agent requires a verified email, they enter their email, receive a code and verify; then they chat. A verification lasts 24 hours in that browser. After that, the link shows the email form again with **Session expired. Please verify your email again.** If a message was being sent when the session ran out, its text comes back in the input after the visitor verifies. Visitors are never sent to the Trinity sign-in page, and a logged-in Trinity user who opens their own link stays signed in.
5. Conversations persist -- the user can return later and continue where they left off.
6. **New** in the chat header starts a fresh session.

**The link shown is always a full URL.** It uses the **Public URL** under **Settings → General** when one is set (the `external_url`), otherwise `FRONTEND_URL`. When neither is set, the Sharing tab completes the path with the address you opened Trinity on, so what you copy is a working `https://…/chat/{token}` URL rather than a bare `/chat/{token}` path. Set a public URL when the address you use internally is not the one outsiders can reach — see [Public Access](../guides/deploying/public-access.md).

### Model and Instructions for Public Chats

Two per-agent settings on the Sharing tab shape public-link conversations (and channel chats) without touching the agent's core configuration: the **Public chat model** override and the **Additional instructions — public & channel chats only** field. Both apply to public links, Slack/Telegram/WhatsApp, and paid chat — never to the owner's own chats or schedules. See [Agent Sharing & Access](agent-sharing.md#public-chat-model).

### Connect Slack from a Public Link

Each public link card has a **Slack** row with a **Connect Slack** button (owner or admin only). It is a shortcut into the Slack channel binding described in [Slack Integration](../integrations/slack-integration.md):

- If the platform's Slack workspace is already connected, Trinity creates a Slack channel named after the agent, binds the agent to it, and makes the agent the DM default when it is the first agent bound in that workspace.
- If no workspace is connected yet, the button opens Slack's authorization page; the platform Slack app must be configured under **Settings → Integrations → Slack** first, or the button reports that Slack is not configured.

Once connected, the row shows the workspace name and who connected it, with an enable/disable toggle and **Disconnect Slack**. The Slack conversation follows the same access rules as the link itself.

### Chat History for Logged-In Users

When a Trinity user who is logged in opens a public chat link, a **history dropdown** appears at the top of the chat interface showing their previous conversations with that agent.

- Sessions are identified by the user's verified email, so history is consistent across devices.
- Clicking a session in the dropdown loads its full message history, read-only (*Viewing past session — read only*). **Return to current chat** goes back to the live conversation.
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

- Rate limits are fixed, not set per link: 30 messages a minute per IP address and 60 a minute per link, plus a cap on link lookups per IP. Verification codes are limited to 3 requests per email every 10 minutes.
- Anonymous sessions have no cross-session continuity.
- Per-user memory requires email verification to be enabled.

## See Also

- [Agent Sharing](agent-sharing.md)
- [Access Control](access-control.md) — the access policy that decides whether a link asks for an email
- [Workspace](workspace.md) — the signed-in, multi-conversation counterpart to a public link
