# Agent Sharing & Access

Two owner-only tabs on the Agent Detail page control who can reach an agent: the **Access** tab manages *Trinity operators* (platform users — your teammates), and the **Sharing** tab manages *external clients* (outside users reaching the agent through Slack, Telegram, WhatsApp, voice calls, or public links).

## Concepts

- **Operator** — A Trinity platform user. Operators log into the Trinity UI and get interact-level access to agents shared with them.
- **External client** — Someone without a Trinity account who reaches the agent through a channel (Slack, Telegram, WhatsApp, a phone call) or a public link. Identified by verified email.
- **Workspace roster** — Every email an agent is shared with can sign in to the [Workspace](workspace.md) and chat with it there. Sharing an agent to an email *is* that person's Workspace account; unsharing removes the agent from their roster.
- **Access policy** — The per-agent Restricted/Open switch controlling whether unknown verified users may chat. See [Access Control](access-control.md) for the cross-channel verification model.

## Access Tab — Trinity Operators

The **Access** tab lists platform users with access to this agent.

1. Open the agent detail page and click the **Access** tab (owner only).
2. Enter a teammate's email and click **Add operator**. The email is also added to the platform login whitelist.
3. Each row shows a status badge:
   - **Active** — the email resolved to an existing Trinity account (shows username, role, and last-active time).
   - **Pending** — no account yet ("Invited — no account yet"); the entry activates automatically once the person logs in for the first time.
4. Each operator row also carries a **proactive-messaging toggle** — "allow this operator to receive proactive DMs from the agent." It is off by default; turn it on per operator to let the agent message that person unprompted. This is a **per-recipient** DM permission (`PUT /api/agents/{name}/shares/proactive`, body `{email, allow_proactive}`).
5. Click **Remove** on a row to revoke access.

The per-recipient DM toggle above is complemented on the channel side by the per-channel Slack consent — see [Slack Integration](../integrations/slack-integration.md). Platform-wide proactive-messaging rate caps are admin-configurable in **Settings**.

### Access Levels

| Level | Permissions |
|-------|-------------|
| **Owner** | Full control -- create, delete, configure, share, manage credentials and schedules. |
| **Shared operator** | Interact only -- chat, run tasks, view files and logs. Cannot modify config, credentials, or permissions. |
| **Admin** | Full access to all agents regardless of ownership. |

Enforcement notes:

- All API endpoints check ownership or sharing status before granting access.
- Shared users see only their own chat messages. Admins see all messages.
- Deleting an agent cascades to remove all associated sharing records.

## Sharing Tab — External Clients

The **Sharing** tab is organized around "who outside the team can reach this agent, and through what." Sections, top to bottom:

### Who can chat with this agent?

A single **Restricted ↔ Open** control:

- **🔒 Restricted** — only approved people chat. Unknown verified users generate a pending access request you approve or deny right below the switch.
- **🌐 Open** — anyone with a verified email chats immediately.

This is the same cross-channel policy described in [Access Control](access-control.md) — approving a request admits that email on every channel at once.

### Public chat model

Pick the Claude model used for **public-facing** conversations only — the public link, Slack/Telegram/WhatsApp channels, and paid chat. Your own authenticated chats and scheduled runs are unaffected. The default option ("Use platform default") inherits the platform-wide model; selecting a model overrides it for this agent's public surfaces.

### Additional instructions — public & channel chats only

A free-text field (up to 4,000 characters) injected into the agent's system prompt **for outside audiences only**: public links, Slack/Telegram/WhatsApp, and paid chat. It does not affect your own authenticated chats, scheduled runs, loops, or agent-to-agent calls. Use it to set tone, disclosure rules, or scope limits for strangers without touching the agent's core instructions. Leave empty to disable. (Voice/VoIP has its own prompt — see [Voice Chat](../advanced/voice-chat.md).)

### Channels

Compact status rows for **Slack**, **Telegram**, **WhatsApp**, and **Voice calls** (when VoIP is enabled platform-wide). Each row shows a connection status dot and a **Configure** (not connected) or **Manage** (connected) button that opens the channel's full configuration in a dialog. Per-channel setup guides:

- [Slack Integration](../integrations/slack-integration.md)
- [Telegram Integration](../integrations/telegram-integration.md)
- [WhatsApp Integration](../integrations/whatsapp-integration.md)
- [VoIP Telephony](../advanced/voip-telephony.md)

Each channel dialog also carries that channel's **voice-replies allow flag** — replies are text by default and the agent opts individual replies into voice per message. See [Voice Replies](../advanced/voice-replies.md).

### Portal clients

A list of the people you've shared this agent with, as Workspace (client portal) sign-ins: each row shows when they were last seen, whether they are blocked, and when their sessions were last ended. **Refresh** reloads it.

- **Log out** — ends this person's live Workspace sessions everywhere. One sign-in covers every agent shared with them, so there is no per-agent session to end. They can sign straight back in. Any owner of an agent shared with them can do this.
- **Block** / **Unblock** — admins only. A block keeps the person out of the whole platform until an admin lifts it, and ends their sessions; their chat history and memory are kept. To remove someone from *this agent only*, unshare them instead.

Both actions are recorded in the audit log, and an agent's own API key cannot perform them.

### Client roster — who's reaching this agent

A read-only table of external channel users who have messaged the agent, aggregated across Telegram and WhatsApp (more channels to follow): client name, channel, verified email, message count, and last-active time, ordered by most recent. The roster is database-sourced, so it renders even when the agent container is stopped.

### Distribution

Two collapsible panels for distributing the agent's output rather than granting access:

- **Public links** — shareable chat URLs; see [Public Links](public-links.md).
- **File sharing** — signed download URLs for files the agent publishes.

## For Agents

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/share` | POST | Share agent with an email address |
| `/api/agents/{name}/share/{email}` | DELETE | Remove a share |
| `/api/agents/{name}/shares` | GET | List all shares for an agent |
| `/api/agents/{name}/access` | GET | Operator roster (active + pending) for the Access tab |
| `/api/agents/{name}/shares/proactive` | GET / PUT | Per-recipient proactive-DM permission; PUT body `{email, allow_proactive}` (owner/admin) |
| `/api/agents/{name}/clients` | GET | External client roster (owner-only, read-only) |
| `/api/enterprise/client-portal/agents/{name}/clients` | GET | Portal clients with last-seen, block and session state (owner/admin; the prefix is historical) |
| `/api/enterprise/client-portal/agents/{name}/clients/{email}/logout` | POST | End the client's live Workspace sessions (owner/admin, people only) |
| `/api/enterprise/client-portal/agents/{name}/clients/{email}/block` | POST / DELETE | Block (optional `{reason}`) or unblock the client platform-wide (admin, people only) |
| `/api/agents/{name}/public-prompt` | GET / PUT | Public/channel-only custom instructions (owner-only, ≤4000 chars; empty clears) |
| `/api/agents/{name}/public-channel-model` | GET / PUT | Public-channel model override (owner-only; null clears to platform default) |

See [Backend API Docs](http://localhost:8000/docs) for full request/response schemas.

## See Also

- [Access Control](access-control.md) — cross-channel email verification and access requests
- [Workspace](workspace.md) — where the people you share an agent with chat with it
- [Public Links](public-links.md) — shareable chat URLs
- [Voice Replies](../advanced/voice-replies.md) — spoken replies on Slack, Telegram, and WhatsApp
- [MCP Server](../integrations/mcp-server.md) — exposing an agent as a dedicated MCP tool
