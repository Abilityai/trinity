# Workspace

A signed-in chat app where you and the people you share agents with hold ongoing conversations — separate from the operator admin UI, and the one surface in Trinity where an agent keeps its working memory from one message to the next.

Workspace lives at `/workspace` and ships in **every** build. The older `/portal` path redirects here, and the `client-portal` naming in API paths is retained history, not a licence boundary. There is one Workspace: the per-agent voice-and-canvas page that used to live at `/agents/{name}/workspace` is gone, and that address now redirects to `/workspace?agent={name}`.

## Concepts

- **Workspace** — The chat app at `/workspace`. Standalone: no operator navigation, no platform chrome. The top-left corner carries Trinity's mark and the name **Trinity Workspace**.
- **Client** — An external person who signs in with a verified email. They have no Trinity account and never see the admin UI.
- **Chat** — One ongoing conversation. A **1:1** chat is with a single agent; a **room** is a chat with two or more.
- **Main** — The pinned chat every (you, agent) pair has. It is the first tab for every agent and the place the agent reaches you when nothing else names a chat: an agent-initiated message, a question it raises outside a conversation, or a scheduled brief.
- **Rail** — The right-hand column beside the conversation. Its tabs are **Work**, **Loops**, **Canvas**, **Files** and **Info**.
- **Briefing** — The panel on an empty chat headed **Things you can ask**, drawn from the agent's exposed playbooks or its template.

> **The Workspace is also where voice lives.** A voice call runs inside a Workspace chat, with the agent's canvas beside the orb. Start one from the call button in the composer, or from **Talk** on the agent's Agent Detail page — see [Voice Chat](../advanced/voice-chat.md).

## How It Works

### Signing in and out

If you are already signed in to Trinity, open **Workspace** from the nav — it opens in its own browser tab, your platform session *is* your Workspace session, and your roster includes both the agents you own and the ones shared with you.

An external client signs in with email instead:

1. Open `/workspace` (for example `https://your-domain.com/workspace`).
2. Enter the email an operator shared agents with. Workspace emails a 6-digit code.
3. Enter the code. No password, and no platform account is created.

Codes expire after a few minutes. **Resend code** becomes available after a short cooldown, and **← different email** lets you switch before verifying. Signing in with an email that has no agents shared with it still works — you land on an empty roster.

A client session slides: it renews while you use the Workspace, ends after the idle window, and never outlives the cap (defaults: 7 idle days, 30 days total). When it times out, the sign-in form says so — *Your session timed out* — and signing in again returns you to where you were. An administrator sees the policy under **Settings → Retention → Workspace sessions**; changing it needs an entitlement.

The button at the bottom of the sidebar signs out. For a platform user it is labelled **Sign out of Trinity**, because your Workspace session is your platform session and ending one ends the other. A client's expired session never turns into the operator's: on a browser that also holds an operator login, the form offers **Continue as user@example.com** as an explicit click rather than switching identity on its own.

### The layout

Three columns on a desktop screen: the **sidebar** on the left, the **conversation** in the middle, and a **side panel** on the right (the rail, or the agent's canvas during a voice call). The seam beside the sidebar, and the one beside the rail while it is open, are drag handles — drag to resize, double-click to reset. The conversation takes whatever is left, and the panel's maximum follows the viewport, so a width set on a large screen is clamped on a smaller one. On a phone the sidebar becomes a drawer behind the **Menu** button, and the rail becomes a strip above the composer that opens a bottom sheet.

### The sidebar

Top to bottom:

- **Trinity Workspace** — the wordmark, with two badges beside it: the number of open questions agents are waiting on you to answer, and the number of replies you haven't read.
- **New chat** — opens the agent picker.
- **Search agents and chats…** — two characters or more. Agents filter in place; chat results replace the chat list below, and each half says separately when it matched nothing.
- **Agents** — your roster, ordered by the agents you worked with most recently, then by name. Five rows show, with **Show all** to expand. A row carries the agent's display name, the title of your newest chat with it (or its slug when the name is a label), when you last heard from it, an availability chip when the agent is stopped or unreachable, and its own ask and unread badges. **Clicking an agent opens the chat you were last in with it** — Main when there is no other.
- **Starred** — chats you've starred, lifted out of the date groups so each chat appears exactly once.
- **Today / Yesterday / Previous 7 days / Older** — everything else. A Main you have never used is not listed here; the agent's row is the way into it.

### Chats as tabs, and Main

Every chat you have with the active agent is a tab above the thread. **Main** is always first; the rest follow most recent first, and the ones that don't fit collect under **N more**. Press **New chat** and a provisional **New chat** tab appears at once; the chat itself is created when you send the first message.

Main is named by its role and cannot be renamed. **Reset**, in the header on Main only, archives the current Main and starts the agent cold — no confirmation, because nothing is lost: the archived chat stays in your list as an ordinary chat (named and dated if it had no title), a line in the new Main names it, and it becomes the newest tab. Reset is refused while a reply is in flight, and resetting a Main you have never used does nothing.

Every other chat can be renamed in place — from the pencil beside its title in the header, or on its sidebar row. Enter or clicking away saves, Esc abandons. A title is one line of up to 100 characters. Otherwise Trinity names a chat for you from your first message, and takes one more pass on the second exchange if the first was only a greeting. A name you typed always stands.

### Starting a chat

- **New chat** in the sidebar opens the picker — **Start a chat**: pick one agent for a 1:1, or several to put them in the same conversation.
- **New chat** in the conversation header (or **⌘J** / **Ctrl+J**) starts a fresh chat with the agent in front of you.
- Click an agent in the sidebar to return to the chat you were last in with it.
- A direct link — `/workspace?agent=<name>` opens your most recent chat with that agent, and `?new=1` forces a fresh one. Linking to an agent you can't reach says so plainly rather than quietly opening a different one.
- On an empty chat, click a card under **Things you can ask** — it pre-fills the composer and never sends on its own.

### The composer

One box: the message field on top, the controls in a row inside it. Enter sends; Shift+Enter adds a line.

- **`/` and `@`** — type `/` at the start of a word for the agent's playbooks, or `@` for another agent. ↓ then Enter (or Tab for the top row) inserts the pick; Esc dismisses the list. Enter alone always sends. A `/` pick splices the playbook's starter prompt into the field without sending.
- **Model** — platform users on a Claude-runtime agent get a dropdown beside Send. The default option reads **Agent's default (…)** and names what the agent would use; the other three are plain-language tiers — **Most capable**, **Balanced — fast and smart**, **Fastest**. The choice is remembered per agent on your account. Resolution is your explicit choice → the model the owner set for the agent's public channels → the platform default. If the agent can't complete a turn on a model you chose, the reply says so and the choice reverts to the agent's default.
- **Attach** — the paperclip picks files, and dropping files anywhere on the conversation sends them (*Drop files to send to …*). Up to 20 files per drop, 25 MB each, uploaded one after another; each shows as a chip with its own progress, and a refused file names itself and the limit. Sent files land in the agent's inbox and appear under **Files you sent** in the rail.
- **Speak your message** — the microphone dictates into the field where the browser or platform can transcribe. It is separate from the voice call.
- **Voice call** — the leftmost button starts the real-time call in this chat; see [Voice Chat](../advanced/voice-chat.md).
- **Send** becomes **Stop** while a turn runs.

An agent that is stopped or unreachable is labelled above the field before you type; the message still sends, and the server's refusal is the answer.

### While the agent works

Replies stream as they happen. Under your message a live card shows the agent's status, the elapsed time and its current step, with **Stop** and **Open in Work** — the same card the rail's [Work tab](../operations/executions.md) lists afterwards. What the agent is doing ("Using *ripgrep*…", "Thinking…") shows rather than a spinner.

**Stop**, or **Esc** with nothing else open, cancels the in-flight turn and puts your words back in the composer (in front of anything you typed while waiting). A cancelled turn shows as cancelled, not as an error. Esc does nothing when no turn is running, and yields to whatever is open on top — the typeahead, the agent picker, dictation, a file preview.

Turns on one chat are serialized to protect the agent's memory, so a second message while one is running is refused with *This conversation is already handling a message* — wait, or start another chat for parallel work. If a send fails you get the real reason (busy, timed out, too large, too many messages, the agent's usage limit) instead of a bare failure, and **Retry** is offered only when nothing reached the agent — a turn the agent already ran is not silently billed twice.

Closing the tab doesn't stop anything: the turn keeps running on the server and the reply is waiting when you come back.

### Reading replies

- Headings, lists and tables in a reply render as such. A code block is its own object with a language label and an always-visible **Copy**; it wraps at the edge rather than scrolling sideways.
- Under every reply: **Copy message**, and **Helpful** / **Not helpful** thumbs. A thumbs-down opens a comment box; your rating feeds the tally in the agent's band.
- If you have scrolled up to re-read something, an arriving message does not pull you back down. A **N new messages** control appears instead; sending, opening a chat or clicking it returns you to the bottom.
- Reports the agent produced for this chat appear at the end of the thread under **Delivered here** — see [Agent Reports](../operations/agent-reports.md).
- While anything is unread, the browser tab's title carries the count — `(3) Trinity — Workspace` — so a reply lands even when the Workspace is behind another tab. Opening the chat clears it. Unread counts replies you haven't read; an agent's questions are counted separately.

### Conversations keep their memory

A Workspace chat resumes: each turn reattaches to the same underlying session, so the agent keeps its tool results, mid-task state and reasoning between messages — not merely the text of what was said. Existing chats run one cold turn and resume from then on. A spoken call in the same chat is folded in: the agent's next typed turn knows what was said. For what carries over, compaction and the per-turn limits, see [Continuous Conversations](../agents/agent-session.md).

### The rail

The rail sits beside the conversation, collapsed to a strip of icons by default; click one to open it, **Collapse** to close it. Its state — open or collapsed, and which tab — is remembered across chats and reloads. A dot on a tab means something happened since you last looked: a pulsing dot for work or a loop in progress, a plain dot for a canvas or file updated since you last opened that tab.

| Tab | What it holds | Who sees it |
|-----|---------------|-------------|
| **Work** | The agent's executions from this chat and its history — see [Executions](../operations/executions.md) | Platform users |
| **Loops** | Run and watch loops on the agent from here — see [Agent Loops](../automation/agent-loops.md) | Platform users |
| **Canvas** | The agent's canvas — see [Agent Canvas](../agents/agent-canvas.md). While there is none, **Ask for a canvas** pre-fills a request | Everyone; a client sees only canvases the agent published to its roster |
| **Files** | Files you sent and files the agent shared | Everyone |
| **Info** | The agent's context (below) | Everyone, in a 1:1 chat |

**Files.** Drop a file on the tab, or click to send one (in a room, pick the recipient first). The list is grouped **Files you sent** / **Files from {agent}**. Click a name to preview it — images, Markdown and text up to 256 KB; ← and → step through the previewable files, Esc closes — or **Download** to save it. The bin icon deletes a file you sent, or removes a shared file from your list without touching the share. An agent's owner, signed in as a platform user, additionally gets **Delete for everyone**.

### The agent's page is the conversation

Clicking an agent no longer opens a report about it. Its numbers sit in a band under the header of every chat with it — tasks in the last 7 days, the share completed, the share that succeeded first try, a **Helpful / Not helpful** tally where people have rated it, and a small activity chart. Everything else is the rail's **Info** tab:

| Section | Shows |
|---------|-------|
| Header | The agent's name and description, with health and availability as two separate facts |
| **Your chats** | The full list, unread counts included — Main shows as **Current conversation**, archived chats in grey |
| **What it can do** | Capability cards; clicking one pre-fills the composer |
| **Reports** | Structured reports the agent has published; expand one to read it — see [Agent Reports](../operations/agent-reports.md) |

It reports; it does not configure. There are no schedules, skills, logs, costs or model details here. A question the agent raised appears above the composer of the chat it belongs to, and answering it there tells you whether the agent is picking the work up — see [Approvals](../automation/approvals.md). Everything is read from stored data, so a stopped agent still renders. The old address `/workspace/a/{agent}` still works and lands in the chat.

### Voice mode

The call button in the composer starts a real-time voice call inside the chat you are in. The orb takes the conversation column, the agent's canvas takes the right column, and the header, tabs and composer stay visible but inert until you end the call (**End call**, the orb's End button, or **Esc**). Switching chats and **⌘J** wait until the call ends. The spoken turns land in the chat as one collapsed **Voice call · N min** block. Everything about the call — who can start one, what the agent can do during it, the time limit — is in [Voice Chat](../advanced/voice-chat.md).

### Bringing in another agent

Mention another agent from a 1:1 — type `@` and pick it — and Workspace opens a **room** containing both and posts your message there; the original 1:1 is left as it was. Inside a room, **+ Add agent** recruits another, and only a person can do that. Rooms carry participant avatars, a star and a budget warning as the conversation approaches its cap; a room's tabs, names and rules are covered in [Shared Sessions (Rooms)](../collaboration/rooms.md). Against an older backend without rooms, the picker is single-select and an `@name` stays ordinary text.

### Keyboard shortcuts

| Keys | Where | Does |
|------|-------|------|
| **⌘J** / **Ctrl+J** | Anywhere in the Workspace | New chat with the agent in front of you (the picker when there is none) |
| **Enter** / **Shift+Enter** | Composer | Send / new line |
| **↓ ↑**, **Enter**, **Tab**, **Esc** | Composer, with the `/` or `@` list open | Move, insert the selected row, insert the top row, dismiss |
| **Esc** | A turn is running, nothing else open | Stop the turn and restore your words |
| **Esc** | During a voice call | End the call |
| **Esc**, **←**, **→** | File preview | Close, previous file, next file |
| **Enter** / **Esc** | Renaming a chat | Save / abandon |

### What an owner configures

Workspace surfaces the agents already shared with a person's email — there is no separate access list. To give a client agents, share each one with their email using the normal [agent sharing](agent-sharing.md) and [access control](access-control.md) model; remove the share to revoke access.

Capability cards come from the agent's exposed playbooks where an operator has configured them, and otherwise from the `use_cases` in its template — so what a client sees you can shape without touching Workspace itself. The model an external client's turns run on is the one set for the agent's public channels on its **Sharing** tab, falling back to the platform default.

## For Agents

Workspace is a client-facing shell over the platform's existing agent behavior, not a new agent capability, and no MCP tool targets it — an agent reaches the people in it through its replies, its [reports](../operations/agent-reports.md), its [questions](../automation/approvals.md) and its [canvas](../agents/agent-canvas.md). It is served by `/api/enterprise/client-portal/*` (the prefix is historical), authenticated by either a Workspace session token or a platform JWT, with every per-agent route scoped to the caller's roster. Agent-scoped MCP keys are refused.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/my-agents` | GET | The caller's roster, each agent with its label, description, availability, model default and capability cards; instance-level `model_options` and `realtime_voice` |
| `/briefings?agents=a,b` | GET | The capability hints for some or all rostered agents, loaded off the roster's critical path |
| `/sessions` | GET | Every chat the caller has, across all agents, in one call |
| `/agents/{name}/sessions` | GET / POST | This agent's chats (the read mints Main if missing) / start an empty chat |
| `/agents/{name}/sessions/main/reset` | POST | Archive Main and mint a fresh one; refused while a turn is in flight |
| `/agents/{name}/sessions/{id}` | PATCH | Rename a chat — `{title}`, one line, ≤100 characters |
| `/agents/{name}/history?session_id=&limit=` | GET | A chat's messages, the in-flight marker and the last turn's outcome |
| `/agents/{name}/chat` | POST | Send a turn and wait for the reply — the synchronous integration surface; accepts `session_id`, `new_thread` and `model` |
| `/agents/{name}/chat/stream` | POST | Begin a turn, returning an execution id to watch; same body |
| `/agents/{name}/executions/{id}/stream` | GET | Live activity for one of your own turns (SSE) |
| `/agents/{name}/executions/{id}/terminate` | POST | Stop one of your own turns |
| `/agents/{name}/documents` | GET / POST | Files the agent shared with you / send the agent a file (25 MB) |
| `/agents/{name}/documents/{file_id}` | DELETE | `?scope=me` removes a shared file from your list; `?scope=everyone` revokes it (owner, platform session) |
| `/agents/{name}/uploads` | GET | Files you sent; `/uploads/{filename}` GET downloads one and DELETE removes it |
| `/agents/{name}/page` | GET | The band and Info payload in one call |
| `/agents/{name}/reports`, `/reports/{id}` | GET | Report metadata and one report's payload (`rows_offset`/`rows_limit` page a table) |
| `/agents/{name}/canvas`, `/canvas/{id}` | GET | The agent's canvases, narrowed to the roster audience for a client |
| `/agents/{name}/ratings` | POST | Rate a message or a deliverable |
| `/agents/{name}/voice/start` | POST | Start a voice call bound to a chat (platform users) |
| `/chat-state` | GET | Star and unread state for every chat |
| `/chat-state/{kind}/{id}/star` | PUT / DELETE | Star or unstar a chat |
| `/chat-state/{kind}/{id}/read` | POST | Advance the read cursor |
| `/search?q=` | GET | Search the caller's chats |

Asks and the Work tab have their own routes under the same prefix — see [Approvals](../automation/approvals.md) and [Executions](../operations/executions.md).

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## Limitations

- **Shared agents only.** A client sees an agent only if it is shared with their verified email.
- **No admin access.** Clients can chat, send files, star and rename chats, rate replies and read reports — never configure, create, or manage agents.
- **Platform users only** for the model dropdown, voice calls, and the Work and Loops tabs. A client sees none of them.
- **Multi-agent chat is available in every build.** Against an older backend that does not serve rooms, `@mention` escalation is unavailable and Workspace says so rather than failing obscurely.
- **Rooms show no unread count.** Stars work for rooms; unread badges count 1:1 chats only. A chat that existed before you first read anything, and was never opened, reports nothing.
- **Room settings are not editable here** beyond the name. Topic, budget and scribe are set through the API.
- **`/` playbooks are not offered in a room.** `@` is.
- **Codex agents replay history** instead of resuming, so their continuity is text-only.
- **A very long chat is windowed.** The thread shows the newest turns and says *Earlier messages in this chat aren't shown* when older ones were cut.
- **No cost or model information** is shown on the agent's band or Info tab.

## See Also

- [Public Links](public-links.md) — a single anonymous chat URL for one agent, no sign-in and no history (Workspace is the signed-in, multi-conversation counterpart)
- [Agent Sharing & Access](agent-sharing.md) — sharing agents with operators and external clients (grants the Workspace roster)
- [Cross-Channel Access Control](access-control.md) — verified-email identity and access requests across channels
- [Continuous Conversations](../agents/agent-session.md) — what resuming preserves, and its limits
- [Voice Chat](../advanced/voice-chat.md) — the real-time call inside a Workspace chat
- [Agent Canvas](../agents/agent-canvas.md) — the surface the rail's Canvas tab and the call's right column show
- [Shared Sessions (Rooms)](../collaboration/rooms.md) — how multi-agent conversations work underneath
- [Agent Chat](../agents/agent-chat.md) — the stateless chat surface on Agent Detail
