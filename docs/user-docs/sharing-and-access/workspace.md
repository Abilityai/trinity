# Workspace

A signed-in chat app where you and the people you share agents with hold ongoing conversations — separate from the operator admin UI, and the one surface in Trinity where an agent keeps its working memory from one message to the next.

Workspace lives at `/workspace`. It ships in **every** build; the older `/portal` path still redirects here, and the `client-portal` naming that appears in API paths is retained history, not a licence boundary.

## Concepts

- **Workspace** — The chat app at `/workspace`. Standalone: no operator navigation, no platform chrome.
- **Client** — An external person who signs in with a verified email. They have no Trinity account and never see the admin UI.
- **Chat** — One ongoing conversation. A **1:1** chat is with a single agent; a **room** is a chat with two or more.
- **Agent page** — A per-agent summary at `/workspace/a/{agent}`: what it's been doing, what it's waiting on you for, and what you can ask it.
- **Briefing** — The panel on an empty chat listing things you can ask, drawn from the agent's exposed playbooks or its template.

> **Not to be confused with Workspace Mode**, the voice canvas at `/agents/{name}/workspace`. Different feature, different flag — see [Voice Chat](../advanced/voice-chat.md).

## How It Works

### Signing in

If you are already signed in to Trinity, open **Workspace** from the nav — your platform session *is* your Workspace session, and your roster includes both the agents you own and the ones shared with you.

An external client signs in with email instead:

1. Open `/workspace` (for example `https://your-domain.com/workspace`).
2. Enter the email an operator shared agents with. Workspace emails a 6-digit code.
3. Enter the code. No password, and no platform account is created.

Codes expire after a few minutes. **Resend code** becomes available after a short cooldown, and you can switch to a different email before verifying. Signing in with an email that has no agents shared with it still works — you land on an empty roster.

### The sidebar

Top to bottom:

- **Waiting on you** — an aggregate badge on the wordmark totalling unread replies across every chat.
- **New chat** — opens the agent picker.
- **Search** — finds messages across all your chats (two characters or more).
- **Agents** — your roster, showing the five most recent with a **Show all** toggle. Each row carries the agent's display name (with its slug underneath when they differ) and its own unread badge. Clicking a row opens that agent's **page**, not a chat.
- **Starred** — chats you've starred, lifted out of the date groups so each chat appears exactly once.
- **Today / Yesterday / Previous 7 days / Older** — everything else.

### Starting a chat

Three ways, all deliberate:

- **New chat** → pick one agent for a 1:1, or two or more to open a room.
- **Start a chat** on an agent's page, optionally seeded by one of its capability cards.
- A direct link — `/workspace?agent=<name>` opens your most recent thread with that agent, and `?new=1` forces a fresh one. Linking to an agent you can't reach says so plainly rather than quietly opening a different one.

### Conversations keep their memory

A Workspace chat resumes. Each turn reattaches to the same underlying session, so the agent keeps its tool results, mid-task state, and reasoning between messages — not merely the text of what was said. Existing chats run one cold turn and resume from then on.

Replies stream as they happen, and while the agent works you see what it's actually doing ("Using *ripgrep*…", "Thinking…") rather than a spinner. Because turns on one chat are serialized to protect that memory, sending a second message while one is still running is refused with *"This conversation is already handling a message"* — wait, or start another chat for parallel work. If a send fails you get the real reason (busy, timed out, too large, too many messages) instead of a bare failure.

Closing the tab doesn't stop anything: the turn keeps running on the server and the reply is waiting when you come back.

### Bringing in another agent

Mention another agent from an existing 1:1 — type `@` and its name — and Workspace opens a **room** containing both and posts your message there. The original 1:1 is left as it was. Inside a room, mentioning an agent that isn't a participant adds it; only a person can recruit an agent this way, never another agent.

An `@name` that isn't one of your agents stays ordinary text. Rooms carry participant avatars, a star, **+ Add agent**, and a budget warning as the conversation approaches its cap.

Multi-agent chat ships in every Trinity build. (It used to be an optional capability an instance might not have; where it is somehow absent — an older backend behind a newer interface — the picker falls back to single-select, `@mentions` stay plain text, and a link to a room says so instead of failing obscurely.)

### The agent page

`/workspace/a/{agent}` answers "what has this agent been doing, and what does it need from me?" without exposing any operator controls. It opens with the agent's description, health, and when it was last active, plus a window selector (7, 14, or 30 days) over its task count, completion rate, and first-try rate.

Five tabs:

| Tab | Shows |
|-----|-------|
| **Overview** | *Waiting on you* — the agent's open questions and approval requests — then recent work and your chats with it |
| **Reports** | Structured reports the agent has published; expand one to read it |
| **Files** | What the agent shared with you, and what you sent it |
| **What it can do** | Capability cards; clicking one starts a chat pre-filled with that prompt |
| **Activity** | The full recent-work list |

It reports; it does not configure. There are no schedules, skills, logs, costs, or model details here, and an open question is answered by replying in chat rather than from the card. Everything is read from stored data, so a **stopped** agent still renders — degraded, not blank.

### Files and history

Attach a file by clicking or dragging into the files panel; sent files are listed with size and timestamp. Each chat has its own URL (`/workspace/c/{id}`, `/workspace/r/{id}`), so threads are linkable and survive a refresh, and browser back and forward move between them. On mobile the sidebar collapses into a drawer.

### What an owner configures

Workspace surfaces the agents already shared with a person's email — there is no separate access list. To give a client agents, share each one with their email using the normal [agent sharing](agent-sharing.md) and [access control](access-control.md) model; remove the share to revoke access.

Capability cards come from the agent's exposed playbooks where an operator has configured them, and otherwise from the `use_cases` in its template — so what a client sees you can shape without touching Workspace itself.

## For Agents

Workspace is a client-facing shell over the platform's existing agent behavior, not a new agent capability. It is served by `/api/enterprise/client-portal/*` (the prefix is historical), authenticated by either a Workspace session token or a platform JWT, with every per-agent route scoped to the caller's roster.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/my-agents` | GET | The caller's roster, each agent with its label, description, and capability cards |
| `/agents/{name}/page` | GET | The whole agent page in one call. `window=7d\|14d\|30d` |
| `/agents/{name}/reports` | GET | Report metadata for the Reports tab |
| `/agents/{name}/reports/{id}` | GET | One report's payload |
| `/agents/{name}/chat` | POST | Send a turn and wait for the reply — the synchronous integration surface |
| `/agents/{name}/chat/stream` | POST | Begin a turn, returning an execution id to watch |
| `/agents/{name}/executions/{id}/stream` | GET | Live activity for one of your own turns (SSE) |
| `/chat-state` | GET | Star and unread state for every chat |
| `/chat-state/{kind}/{id}/star` | PUT/DELETE | Star or unstar a chat |
| `/chat-state/{kind}/{id}/read` | POST | Advance the read cursor |

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## Limitations

- **Shared agents only.** A client sees an agent only if it is shared with their verified email.
- **No admin access.** Clients can chat, upload files, star chats, and read reports — never configure, create, or manage agents.
- **Multi-agent chat is available in every build.** Against an older backend that does not serve rooms, `@mention` escalation is unavailable and Workspace says so rather than failing obscurely.
- **Rooms show no unread count.** Stars work for rooms; unread badges currently count 1:1 chats only.
- **Room settings are not editable here.** Name, topic, budget, and scribe are set through the API, not the Workspace UI.
- **One file at a time.** Uploads are sent one file per drop or pick.
- **Report payloads are read-only** and render as formatted JSON.
- **The agent page has no ratings** and deliberately shows no cost or model information.

## See Also

- [Public Links](public-links.md) — a single anonymous chat URL for one agent, no sign-in and no history (Workspace is the signed-in, multi-conversation counterpart)
- [Agent Sharing & Access](agent-sharing.md) — sharing agents with operators and external clients (grants the Workspace roster)
- [Cross-Channel Access Control](access-control.md) — verified-email identity and access requests across channels
- [Shared Sessions (Rooms)](../collaboration/rooms.md) — how multi-agent conversations work underneath
- [Agent Chat](../agents/agent-chat.md) — the stateless chat surface on Agent Detail
