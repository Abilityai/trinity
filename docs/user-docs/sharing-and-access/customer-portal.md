# Customer Portal

A branded, signed-in web app where your external clients hold ongoing, multi-conversation chats with the agents you've shared with them — separate from the operator admin UI.

> **Enterprise-tier feature.** The Customer Portal is gated behind the paid tier. The `/portal` route and its `/api/enterprise/*` endpoints ship in the code but are inactive in a community build: the backend returns 404 and the portal is not available. This page describes user-facing behavior only.

## Concepts

- **Client** — An external person who signs in to the portal with a verified email. They have no Trinity platform account and never see the operator admin UI.
- **Portal** — The client-facing app at the `/portal` route. Standalone: no operator navigation bar, no platform chrome, and the operator help widget is hidden.
- **Conversation (thread)** — One ongoing chat between a client and one agent. Threads persist and are listed in the sidebar as history.
- **Briefing** — A per-agent welcome panel shown when a client opens a new chat, including client-visible starter prompts they can click to begin.
- **Files panel** — Where a client attaches files to send to the current agent and reviews what they've already sent.

## How It Works

### Signing in

1. Open the portal at `/portal` (for example `https://your-domain.com/portal`).
2. Enter the email an operator shared agents with. The portal emails a 6-digit code.
3. Enter the code to continue. No password, no platform account is created.

Codes expire after a few minutes. A **Resend code** action becomes available after a short cooldown, and the client can switch to a different email before verifying. If the email has no agents shared with it, sign-in still works but the portal shows an empty "No agents shared with you yet" state.

### Working in the portal

- **Pick an agent.** The sidebar lists every agent shared with the signed-in email. The client chooses which one to talk to; a name and avatar identify each agent.
- **Read the briefing.** A new chat opens with the agent's briefing and starter prompts; clicking a starter prompt begins the conversation.
- **Chat.** Messages stream back in real time, the same as any agent chat.
- **Attach files.** The client uploads a file directly in the portal — by clicking or dragging into the files panel — and sends it to the agent. Sent files are listed with their size and timestamp.
- **Browse history.** The sidebar shows past conversations grouped by date, each row tagged with the agent's avatar. Clicking a row reopens that thread.
- **Search.** A search box finds messages across all of the client's conversations.
- **Deep-link and refresh.** Each conversation has its own URL (`/portal/c/{session_id}`), so a thread is shareable and survives a page refresh; browser back and forward move between threads.
- **On mobile.** The sidebar collapses into a slide-out drawer opened from a menu button, so the chat gets the full width.

Switching agents mid-thread starts a fresh conversation with the new agent — context does not carry across agents. Signing out clears the client's session and returns to the sign-in screen.

### What an owner configures

The portal surfaces the agents that are already shared with a client's email — there is no separate portal access list. To give a client agents, share each agent with their email using the normal [agent sharing](agent-sharing.md) and [access control](access-control.md) model. Remove the share to revoke portal access. The client sees only the agents shared with them.

## For Agents

The portal reuses the platform's existing agent chat, session, and file surfaces — it is a client-facing shell over the same agent behavior, not a new agent capability. A client's portal chat reaches an agent exactly as any other chat does; uploaded files and conversation history are stored per client and per agent.

Portal traffic is served by the gated `/api/enterprise/client-portal/*` endpoints (client sign-in, agent roster, sessions, chat, search, and document upload). These are enterprise-only and return 404 in community builds. See the [Backend API Docs](http://localhost:8000/docs) for request/response schemas.

## Limitations

- **Enterprise-gated.** Not available in community builds; the route and endpoints are present but inactive (backend 404, portal shows only its sign-in / empty state).
- **Shared agents only.** A client sees an agent only if it is shared with their verified email.
- **No admin access.** Clients cannot configure, create, or manage agents; they can only chat, upload files, and browse their own history.
- **One file at a time.** Uploads are sent one file per drop or pick.
- **No cross-agent context.** Switching agents starts a new conversation rather than continuing the current one with a different agent.

## See Also

- [Public Links](public-links.md) — a single anonymous chat URL for one agent, no sign-in and no history (the portal is the signed-in, multi-conversation counterpart)
- [Agent Sharing & Access](agent-sharing.md) — sharing agents with operators and external clients (grants the portal's agent roster)
- [Cross-Channel Access Control](access-control.md) — verified-email identity and access requests across channels
