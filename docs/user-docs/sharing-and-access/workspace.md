# Workspace

A signed-in chat app where you and the people you share agents with hold ongoing conversations — separate from the operator admin UI, and the one surface in Trinity where an agent keeps its working memory from one message to the next.

Workspace lives at `/workspace` and ships in **every** build. The older `/portal` path redirects here, and the `client-portal` naming in API paths is retained history, not a licence boundary. There is one Workspace: the per-agent voice-and-canvas page that used to live at `/agents/{name}/workspace` is gone, and that address now redirects to `/workspace?agent={name}`.

## Concepts

- **Workspace** — The chat app at `/workspace`. Standalone: no operator navigation, no platform chrome. The top-left corner carries Trinity's mark and the name **Trinity Workspace**.
- **Inbox** — The Workspace's landing page at `/workspace/inbox`: what your agents need from you and what came back, across every agent on your roster.
- **Client** — An external person who signs in with a verified email. They have no Trinity account and never see the admin UI.
- **Chat** — One ongoing conversation. A **1:1** chat is with a single agent; a **room** is a chat with two or more.
- **Main** — The pinned chat every (you, agent) pair has. It is the first tab for every agent and the place the agent reaches you when nothing else names a chat: an agent-initiated message, a question it raises outside a conversation, or a scheduled brief.
- **Rail** — The right-hand column beside the conversation. Its tabs are **Work**, **Loops**, **Canvas**, **Files** and **Info**.
- **Briefing** — The panel on an empty chat headed **Things you can ask**, drawn from the agent's exposed skills or its template.
- **Suggestions** — A short list headed **Suggested for you**, computed for you and the agent in front of you: what is waiting on you, and what you could do next. Platform users only.
- **Seat** — You and one agent together. The agent's memory of you, and the decisions you and it record, belong to the seat.

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

The main app and every Workspace tab in the same browser share one platform sign-in, and open tabs follow it. Sign out and back in on one tab, and a Workspace tab left open from the old session picks up the new one instead of ending it. Sign out on one tab, and the other tabs drop the session too, without a background tab jumping to the sign-in page. A client signed in with an email code is not sent to the operator sign-in page just because the same browser also holds an expired operator login.

### The layout

Three columns on a desktop screen: the **sidebar** on the left, the **conversation** in the middle, and a **side panel** on the right (the rail, or the agent's canvas during a voice call). The seam beside the sidebar, and the one beside the rail while it is open, are drag handles — drag to resize, double-click to reset. The conversation takes whatever is left, and the panel's maximum follows the viewport, so a width set on a large screen is clamped on a smaller one. On a phone the sidebar becomes a drawer behind the **Menu** button, and the rail becomes a strip above the composer that opens a bottom sheet.

**Theme.** The right end of every chat and room header carries the theme switch. It names what is on screen — **Light**, **Dark**, or **System · dark** / **System · light** when the choice follows your operating system — and on a phone it shows only the icon. Click it and pick **Light**, **Dark** or **System** (arrow keys move between them; Esc closes the menu without touching a running turn or call). It is the same setting as the theme control in the main app, remembered in this browser, and switching keeps your place, your draft and your thread. External clients get it too.

### The sidebar

Top to bottom:

- **Trinity Workspace** — the wordmark. It links to the Inbox.
- **Inbox** — a pinned row with two badges: the number of asks agents are waiting on you to answer, and the number of chats with something you haven't read. See [The Inbox](#the-inbox).
- **New chat** — opens the agent picker.
- **Search agents and chats…** — two characters or more. **⌘/** / **Ctrl+/** puts the cursor there from anywhere; pressed again from the field, it returns you to the message field. Agents filter in place; chat results replace the chat list below, and each half says separately when it matched nothing.
- **Agents** — your roster, ordered by the agents you worked with most recently, then by name. Five rows show, with **Show all** to expand. A row carries the agent's display name, the title of your newest chat with it (or its slug when the name is a label), when you last heard from it, an availability chip when the agent is stopped or unreachable, and its own ask and unread badges. **Clicking an agent opens a new, empty chat with it**, with the cursor in the message field — or returns you to a chat with that agent where you left unsent text. Your earlier chats are its tabs, one click away.
- **Starred** — chats you've starred, lifted out of the date groups so each chat appears exactly once.
- **Today / Yesterday / Previous 7 days / Older** — everything else. A Main you have never used is not listed here; the agent's row is the way into it.
- **Keyboard shortcuts** — at the bottom, beside sign-out. It opens the list of keys (also **⌥/** / **Alt+/**).

### The Inbox

Signing in, or opening bare `/workspace`, lands you on the **Inbox** — *What needs you, and what came back, across your agents.* A link that names a chat, a room or an agent opens that instead. Three tabs:

| Tab | Shows |
|-----|-------|
| **Action** | Asks addressed to you that are still waiting, most urgent first: asks that expire within 24 hours (soonest first), then by priority, then oldest first. While asks are waiting, a second strip narrows the list to one agent (**All agents** or an agent's name, with its count) |
| **Unread** | Chats where something came back since you last read them — replies, completed runs delivered to you, deliverables addressed to you |
| **All** | Every chat, read or not, plus waiting asks and asks that ended in the last 7 days |

Click a row to open it in the reading pane beside the list (on a narrow screen the pane replaces the list, with **Back**). An ask is answered right there, with the same controls as in a chat, and below it the ask's context: **Where it came from** (the chat and the few messages before the ask, with a link to open it there), **Delivered in that chat**, and **Your recent answers** to this agent. A chat shows what arrived since you last read it and its deliverables, and is marked read once it has loaded; **Mark read**, **Reply in chat** and **Open in chat** sit in the pane's header, plus **Open canvas** when the agent has one. On **Unread** and **All**, **Mark N chats read** clears them in one go, after a confirmation when it covers more than one chat.

Rows keep their place while you stay on a tab: a chat you read stays listed, drawn as read, and a poll never re-sorts the list under you. A tab shows 50 rows, then **Show more**. Rooms are not in the Inbox yet.

**Discuss and Dismiss.** A question or approval card also offers **Discuss** — a chat with the asking agent about that ask, where for a question **Send as answer** answers it from the message field — and **Dismiss**, which ends the ask without answering after a 5-second **Undo**. Alerts offer neither. Who asks reach, the controls, and how long ended asks stay are all in [Approvals](../automation/approvals.md).

### Chats as tabs, and Main

Every chat you have with the active agent is a tab above the thread. **Main** is always first; the rest follow most recent first, and the ones that don't fit collect under **N more**. Press **New chat** and a provisional **New chat** tab appears at once; the chat itself is created when you send the first message.

Main is named by its role and cannot be renamed. **Reset**, in the header on Main only, archives the current Main and starts the agent cold — no confirmation, because nothing is lost: the archived chat stays in your list as an ordinary chat (named and dated if it had no title), a line in the new Main names it, and it becomes the newest tab. Reset is refused while a reply is in flight, and resetting a Main you have never used does nothing.

Every other chat can be renamed in place — from the pencil beside its title in the header, or on its sidebar row. Enter or clicking away saves, Esc abandons. A title is one line of up to 100 characters. Otherwise Trinity names a chat for you from your first message, and takes one more pass on the second exchange if the first was only a greeting. A name you typed always stands.

### Starting a chat

- **New chat** in the sidebar opens the picker — **Start a chat**: pick one agent for a 1:1, or several to put them in the same conversation.
- **New chat** in the conversation header (or **⌘J** / **Ctrl+J**) starts a fresh chat with the agent in front of you.
- Click an agent in the sidebar to start a new chat with it. Nothing is created until you send, so opening an agent and leaving again leaves no empty chats behind. The address stays `/workspace/a/<name>` until the first send, so a reload or a copied link still names the agent. On a desktop the cursor lands in the message field; on a touch screen the on-screen keyboard is not raised until you tap the field.
- **⌥↓** / **⌥↑** (**Alt+↓** / **Alt+↑**) steps to the next or previous agent in the sidebar. Walking the roster returns you to the chat you last had open with each agent in this browser tab, while it still exists; a reload starts fresh.
- A direct link — `/workspace?agent=<name>` (or `/workspace/a/<name>`) opens a new chat with that agent the same way, and `?new=1` still works. A link to an agent that isn't shared with you shows *You don't have access to …* with **Back to your chats**, and a link to a chat that isn't yours shows *This chat isn't available* — neither offers a message field, and neither quietly opens a different chat.
- On an empty chat, click a card under **Things you can ask** — it pre-fills the composer and never sends on its own. A platform user also sees up to three **Suggested for you** rows below the hints (see [Suggestions](#suggestions)).

### The composer

One box: the message field on top, the controls in a row inside it. Enter sends; Shift+Enter adds a line.

- **`/` and `@`** — type `/` at the start of a word for the agent's skills, or `@` for another agent. ↓ then Enter (or Tab for the top row) inserts the pick; Esc dismisses the list. Enter alone always sends. A `/` pick splices the skill's starter prompt into the field without sending.
- **Model** — platform users on a Claude-runtime agent get a dropdown beside Send. The default option reads **Agent's default (…)** and names what the agent would use; the other three are plain-language tiers — **Most capable**, **Balanced — fast and smart**, **Fastest**. The choice is remembered per agent on your account. Resolution is your explicit choice → the model the owner set for the agent's public channels → the platform default. If the agent can't complete a turn on a model you chose, the reply says so and the choice reverts to the agent's default.
- **Attach** — the paperclip picks files, dropping files anywhere on the conversation sends them (*Drop files to send to …*), and pasting a copied image or file into the message field attaches it the same way. A paste that also carries text still types the text. Up to 20 files per drop, 25 MB each, uploaded one after another; each shows as a chip with its own progress, and a refused file names itself and the limit. Sent files land in the agent's inbox and appear under **Files you sent** in the rail.
- **Speak your message** — the microphone dictates into the field. It is separate from the voice call, and it appears only where dictation can work; see [Dictation](#dictation).
- **Voice call** — the leftmost button starts the real-time call in this chat; see [Voice Chat](../advanced/voice-chat.md).
- **Speak replies aloud** — a speaker button above the composer, shown only when the agent has a voice configured, reads each reply in the agent's ElevenLabs voice on your side. Click again to mute. It is hidden during a call, when the orb owns playback. See [Voice Replies](../advanced/voice-replies.md).
- **Send** becomes **Stop** while a turn runs.

An agent that is stopped or unreachable is labelled above the field before you type; the message still sends, and the server's refusal is the answer.

**Drafts.** Text you type but don't send survives switching to another agent, another chat tab or a room, and comes back exactly as you left it. Every chat and room that holds unsent text is marked **Draft** in the sidebar and on its tab — a draft typed into a new chat keeps its provisional **New chat** tab. Sending clears the draft; a stopped turn or a failed send that hands your words back makes them a draft again. **Reset** carries the draft onto the new Main. Drafts live in this browser, per person, and are shared between Workspace tabs. Signing out deletes them; a session that times out keeps them for when you sign back in.

### While the agent works

Replies stream as they happen. Under your message a live card shows the agent's status, the elapsed time and one line saying what the agent is doing right now, with **Stop** and **Open in Work** — the same card the rail's [Work tab](../operations/executions.md) lists afterwards. When you send, the chat scrolls down to the card, unless you had scrolled up to read something.

The activity line reads the same way everywhere: *Reading .../src/app.py*, *Running pytest -q*, *Searching for "pattern"*, *Fetching example.com*, *Using github*, *Delegating to researcher*, *Writing a reply*, or *Thinking* between steps. A new line slides up over the old one and stays long enough to read; a burst of quick steps shows only the latest. On a quiet stretch the last line stays, and the line clears when the run ends. Your own turn's line comes from its live stream. Every other live run — a delegated job, a scheduled run, a room turn — takes its line from the agent's regular heartbeat, which a platform user sees on the Work tab's cards and on a room's cards. A heartbeat line older than 30 seconds is dropped rather than shown as current, and a card with nothing to report leaves the line empty rather than guessing.

**Stop**, or **Esc** with nothing else open, cancels the in-flight turn and puts your words back in the composer (in front of anything you typed while waiting). A cancelled turn shows as cancelled, not as an error. Esc does nothing when no turn is running, and yields to whatever is open on top — the typeahead, the agent picker, dictation, a file preview.

Turns on one chat are serialized to protect the agent's memory, so a second message while one is running is refused with *This conversation is already handling a message* — wait, or start another chat for parallel work. If a send fails you get the real reason (busy, timed out, too large, too many messages, the agent's usage limit) instead of a bare failure, and **Retry** is offered only when nothing reached the agent — a turn the agent already ran is not silently billed twice.

Closing the tab doesn't stop anything: the turn keeps running on the server and the reply is waiting when you come back.

### Reading replies

- Headings, lists and tables in a reply render as such. A code block is its own object with a language label and an always-visible **Copy**; it wraps at the edge rather than scrolling sideways.
- Under every reply: **Copy message**, **Reply to this message**, and **Helpful** / **Not helpful** thumbs. A thumbs-down opens a comment box; your rating feeds the tally in the agent's band.
- **Reply to this message** puts a *Replying to …* chip above the message field, and your next message tells the agent which of its messages you are answering. The **×** on the chip, or **Esc** in the message field, drops it. Reply is offered in 1:1 chats, and is disabled during a voice call.
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
| **Canvas** | The agent's canvases (below) — see [Agent Canvas](../agents/agent-canvas.md). While there is none, **Ask for a canvas** pre-fills a request | Everyone; a client sees only canvases the agent published to its roster |
| **Files** | Files you sent and files the agent shared | Everyone |
| **Info** | The agent's context (below). Its dot lights when there are suggestions for you (*Info · 2 suggestions*) | Everyone, in a 1:1 chat |

**Files.** Drop a file on the tab, or click to send one. In a room, **Send to** defaults to **Everyone in this chat (N agents)** — one copy per agent, the same as dropping the file on the room itself — and still lists each agent for a single recipient. The receipt names who got it (*Sent “shot.png” to analyst and sidekick.*). A file counts as sent only when every recipient got it; otherwise the error line names the file and the agents it missed. The list is grouped **Files you sent** / **Files from {agent}**. Click a name to preview it — images, Markdown and text up to 256 KB; ← and → step through the previewable files, Esc closes — or **Download** to save it. The bin icon deletes a file you sent, or removes a shared file from your list without touching the share. An agent's owner, signed in as a platform user, additionally gets **Delete for everyone**.

**Canvas.** One canvas shows at a time; pick another from the dropdown in the control row above it, where a pinned canvas carries 📌 and comes first. Long titles are shortened in the list; the full title shows in the canvas header. With one canvas the dropdown is disabled rather than hidden, so the row looks the same for one canvas and for forty. Once an agent has more than six, **Search canvases…** filters them by title or id. The header states two facts and draws no conclusion from them — *Updated 2h ago · agent last ran 40m ago*. **PDF** prints the open canvas through the browser's own print dialog. An agent's owner (or an admin), signed in as a platform user, also gets **Manage** at the end of the row: it opens a list below the row, where each canvas shows its age, a pin toggle and **Delete**, and a checkbox per row feeds **Delete selected** with one confirmation naming the count. **Done** closes the list and returns to the canvas you had open. Everyone else has a read-only panel. Sharing a canvas at a link is done from the agent's Canvas tab on Agent Detail, not from the rail — see [Agent Canvas](../agents/agent-canvas.md). The canvas you have open travels with each message you send from that chat, so *add a column to this* names the right one. It is context, not permission: the agent still reaches only the canvases it could already reach, and a canvas deleted mid-conversation resolves to nothing.

### The agent's page is the conversation

Clicking an agent no longer opens a report about it. Its numbers sit in a band under the header of every chat with it — tasks in the last 7 days, the percentage completed, the percentage that succeeded first try (a rate with nothing to measure reads —), a **Helpful / Not helpful** tally where people have rated it, and a small activity chart. Everything else is the rail's **Info** tab:

| Section | Shows |
|---------|-------|
| Header | The agent's name and description, with health and availability as two separate facts |
| **Suggested for you** | What is waiting on you and what you could do next — see [Suggestions](#suggestions). Platform users only |
| **Role** | Shown only when the agent's template declares a role — see [The role card](#the-role-card) |
| **Your chats** | The full list, unread counts included — Main shows as **Current conversation**, archived chats in grey |
| **What it can do** | Capability cards; clicking one pre-fills the composer |
| **Reports** | Structured reports the agent has published; expand one to read it — see [Agent Reports](../operations/agent-reports.md) |
| **What it remembers about you** | The agent's notes about you, and a **Changes** list of the runs that rewrote them, with **Undo** |
| **Decisions** | The seat's decision record — see [Decisions](#decisions) |

An external client's band and Info tab count only the runs they can see: their own turns, the runs those turns started, and the agent's scheduled runs — except a scheduled brief delivered to someone else. Other people's runs of the same agent, and their timings, are never shown to a client. A platform user sees the agent's full activity.

It reports; it does not configure. There are no schedules, skills, logs, costs or model details here. A question the agent raised during a chat appears inside that chat, and answering it there tells you whether the agent is picking the work up; every question waiting on you is in the **Inbox**, and the agent's **Info** tab links there (*N asks waiting on you · Open in Inbox*) — see [Approvals](../automation/approvals.md). Everything is read from stored data, so a stopped agent still renders. The agent's address, `/workspace/a/{agent}`, opens a new chat with it.

### Suggestions

A platform user who opens an agent gets a short list headed **Suggested for you**, computed for them and that agent. It shows in full on the **Info** tab (up to five, with *Showing 5 of N* when there are more) and, on an empty chat, as up to three compact rows below **Things you can ask**. Each item names the signal it came from:

| Suggestion | Signal |
|------------|--------|
| **Answer what this agent asked you** | *N questions waiting on you* |
| **Review decisions past their date** | *N decisions past the review date* |
| **Check "*schedule*"** | *Failed N runs in a row*, or *Enabled since … · has never run* |
| **Re-enable or delete "*schedule*"** | *Disabled since … · it used to run* |
| **Turn on autonomy, or pause these schedules** | *N schedules won't run — autonomy is off* |
| **Pick up where you left off** | *Your last conversation was …* |
| A skill's title | *You haven't run /name yet* |

Schedule suggestions appear only for the agent's owner or an admin. A skill is suggested only if the agent exposes it, you have not run it with `/name`, and no enabled schedule already runs it. When you have never talked to the agent, the list says so — *You haven't talked to this agent yet — these are things it can do.* An agent with nothing to suggest says *Nothing to suggest right now.*

Each item has two buttons. The accept button never sends anything: it pre-fills the composer, opens the section it names, focuses the chat, or opens the agent's schedules page in the main app in a new tab. **Dismiss** hides the item until its state changes — a schedule that keeps failing does not come back with every new failure, but one that recovers and fails again does. The Info tab's rail dot lights when there are suggestions, even with the rail collapsed.

### The role card

When the agent's template declares a role, the Info tab shows a **Role** card: the role and its mission, the objectives it owns or supports, and each objective's metrics with value, target and age. The numbers are the ones the agent records. A metric is marked stale when the agent declared how often it records it and no new value arrived within twice that interval; it still shows beside its last value, never as current. A metric with no declared interval is never marked stale, and one that has never been recorded shows *no points yet*. A badge says whether each value is behind, ahead of, on or off its target. When an objective names a metric the agent does not measure, a line under the metric says so, and when the objectives cannot be read the card says that instead of showing an empty list. If only some of them could be read, a line under the list says it may be incomplete. The card also shows **Your relationship** to the agent and its **Readiness**: *calibrating* until the agent's owner presses **Mark ready** (and confirms), and **Back to calibrating** undoes it. Only the owner can change readiness; a template that claims `ready` on its own shows as calibrating. For an agent that declares a role, a scheduled brief addressed to a person does not fire on its schedule until the owner marks the agent ready; the run is recorded as skipped, with the reason. The card reads the agent's own files each time, so a stopped agent shows *The agent is stopped — the role card reads its files when it runs.* An agent without a role shows no card at all.

### Decisions

The **Decisions** section on the Info tab is a record of what you and the agent approved, deferred or killed, and why. Each decision records the outcome, what was decided, the alternatives that were live, the criterion that decided it, what would reverse it, and a **Review by** date. **Record a decision** opens the form; free prose belongs only in **Notes**, and a decision with no alternatives is refused as a note. Choosing **Direction — route to canon** as the scope keeps the record visible but outside the seat's evidence.

On a decision that is still active:

- **Reconfirm** moves its review date.
- **Correct** records a new decision that replaces the old one.
- **Reverse** needs a reason — what changed.
- **Close** retires it, after a confirmation.

Nothing is deleted. A decision past its review date shows as expired until someone reconfirms it. The seat's active decisions are read into every turn with that agent, so the agent reuses the criterion next time. You see and edit your own seat's decisions; the agent's owner sees and edits every seat's. The agent records decisions too, with the `record_decision` MCP tool.

### Dictation

The microphone in the composer (**Speak your message**) turns speech into text in the message field. Nothing sends until you press Enter. Clients and platform users both get it, and it uses one of two engines:

- **Platform transcription** — the browser records a clip and Trinity transcribes it through ElevenLabs. Used when the browser can record and the platform's ElevenLabs key is allowed to call speech-to-text.
- **The browser's own dictation** — used otherwise, in browsers that have a speech engine.

When neither engine can work, the composer shows no microphone at all rather than one that fails on every press. ElevenLabs grants permissions per endpoint, so a key that speaks replies aloud may still lack **Speech to Text**. That turns platform transcription off; the browser's engine still works where there is one.

A failed transcription gets a line above the message field that says why: the key lacks the speech-to-text permission, the key was rejected, the account is out of credits, too many voice messages in a short time, the recording could not be read, or the provider failed. You can always type instead.

**For admins.** **Settings → General → Voice (ElevenLabs)** shows, beside the key's **configured** badge, whether the key can transcribe:

| Badge | Means |
|-------|-------|
| **can transcribe** | Platform dictation is available |
| **cannot transcribe — *reason*** | ElevenLabs refused the key for speech-to-text. Grant the key the Speech to Text permission at ElevenLabs, then save it again |
| **transcription not verified** | ElevenLabs could not be reached to check. The microphone stays available until the check completes |

Trinity checks each key once and repeats the check every few hours, and at once when you save the key again. A transcription the provider refuses (HTTP 401 or 403) also marks the key, so the next page load stops offering platform transcription. Under the badge, **Last voice-input failure** names the most recent failed transcription: the cause, the provider's HTTP status and status word, and the time. It stays for 24 hours, or until you save the key again. The key itself is never shown.

### Voice mode

The call button in the composer starts a real-time voice call inside the chat you are in. The orb takes the conversation column, the agent's canvas takes the right column, and the header, tabs and composer stay visible but inert until you end the call (the theme switch keeps working) (**End call**, the orb's End button, or **Esc**). Switching chats and **⌘J** wait until the call ends. The spoken turns land in the chat as one collapsed **Voice call · N min** block. Everything about the call — who can start one, what the agent can do during it, the time limit — is in [Voice Chat](../advanced/voice-chat.md).

### Bringing in another agent

Mention another agent from a 1:1 — type `@` and pick it — and Workspace opens a **room** containing both and posts your message there; the original 1:1 is left as it was.

Files you attached in that 1:1 and have not yet sent with a message go along: the composer's attachment chips, and files sent from the rail's **Files** tab in the last 15 minutes. Workspace waits for uploads still in progress, then delivers each file to the agents that do not have it yet, *before* posting your message, so the mentioned agent can see what you asked about. A line under the room's composer then says what happened — *Sent with your message: shot.png — also delivered to sidekick.*, *shot.png didn't reach sidekick — attach it again here to retry.*, or that a file was not carried over because it never finished uploading — until you dismiss it or send your next message. If the room cannot be opened, your text comes back to the 1:1 composer with its attachment chips.

Inside a room, **+ Add agent** recruits another, and only a person can do that. Rooms carry participant avatars, a star and a budget warning as the conversation approaches its cap. For a platform user, each agent working on the room's message gets its own live card with **Stop**. A room's cards, files, names and rules are covered in [Shared Sessions (Rooms)](../collaboration/rooms.md). Against an older backend without rooms, the picker is single-select and an `@name` stays ordinary text.

### Keyboard shortcuts

The open rail carries a small **shortcut tips** panel at its foot with the most-used keys; close it with its **×** and this browser remembers. The collapsed rail shows a single keyboard icon instead, which opens the full list.

| Keys | Where | Does |
|------|-------|------|
| **⌘J** / **Ctrl+J** | Anywhere in the Workspace | New chat with the agent in front of you (the picker when there is none) |
| **⌥↓** / **⌥↑** (**Alt+↓** / **Alt+↑**) | Anywhere | Next / previous agent in the sidebar |
| **⌥⇧↓** / **⌥⇧↑** (**Alt+Shift+↓** / **↑**) | Anywhere | Next / previous chat with this agent |
| **⌘.** / **Ctrl+.** | Anywhere | Show or hide the rail |
| **⌥.** (**Alt+.**) | Anywhere | Next rail tab |
| **⌘/** / **Ctrl+/** | Anywhere | Cursor to the sidebar search; again, back to the message field. On a narrow screen it opens the sidebar first |
| **⌥/** (**Alt+/**) | Anywhere | The keyboard-shortcuts list |
| **Enter** / **Shift+Enter** | Composer | Send / new line |
| **↓ ↑**, **Enter**, **Tab**, **Esc** | Composer, with the `/` or `@` list open | Move, insert the selected row, insert the top row, dismiss |
| **Esc** | Composer, with a *Replying to* chip | Drop the reply |
| **Esc** | A turn is running, nothing else open | Stop the turn and restore your words |
| **Esc** | Room composer, with exactly one turn you can stop and no list or picker open | Stop that turn (with two or more, Esc does nothing — use the card's **Stop**) |
| **Esc** | Theme menu open | Close the menu |
| **Esc** | During a voice call | End the call |
| **M** | During a voice call | Mute or unmute |
| **Esc**, **←**, **→** | File preview | Close, previous file, next file |
| **Enter** / **Esc** | Renaming a chat | Save / abandon |

"Anywhere" includes the message field: the keys work while you type. They step aside while a dialog or the phone menu is open, and during a voice call only ⌘J answers (it asks whether to leave the call first); the moving and rail keys do nothing. Esc always closes the innermost thing first — a popup, the reply chip, a dialog, a sheet — before it stops a turn. **⌘K** / **Ctrl+K** is left to the browser. Agent rows, chat tabs and the rail's controls show their key in the tooltip.

### What an owner configures

Workspace surfaces the agents already shared with a person's email — there is no separate access list. To give a client agents, share each one with their email using the normal [agent sharing](agent-sharing.md) and [access control](access-control.md) model; remove the share to revoke access.

Capability cards come from the agent's exposed skills where an operator has configured them, and otherwise from the `use_cases` in its template — so what a client sees you can shape without touching Workspace itself. The model an external client's turns run on is the one set for the agent's public channels on its **Sharing** tab, falling back to the platform default.

## For Agents

Workspace is a client-facing shell over the platform's existing agent behavior, not a new agent capability — an agent reaches the people in it through its replies, its [reports](../operations/agent-reports.md), its [questions](../automation/approvals.md) and its [canvas](../agents/agent-canvas.md). The one Workspace record an agent writes directly is the seat's decision record: `record_decision` records a decision for the person the current turn is for (the seat comes from the turn's `execution_id`, never from a parameter), and `list_seat_decisions` reads that seat's record back. Neither returns an email address. It is served by `/api/enterprise/client-portal/*` (the prefix is historical), authenticated by either a Workspace session token or a platform JWT, with every per-agent route scoped to the caller's roster. Agent-scoped MCP keys are refused.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/my-agents` | GET | The caller's roster, each agent with its label, description, availability, model default, capability cards and `can_manage_canvases` (the only capability channel a Workspace principal has — absent reads as false); instance-level `model_options` and `realtime_voice` |
| `/briefings?agents=a,b` | GET | The capability hints for some or all rostered agents, loaded off the roster's critical path |
| `/sessions` | GET | Every chat the caller has, across all agents, in one call |
| `/agents/{name}/sessions` | GET / POST | This agent's chats (the read mints Main if missing) / start an empty chat |
| `/agents/{name}/sessions/main/reset` | POST | Archive Main and mint a fresh one; refused while a turn is in flight |
| `/agents/{name}/sessions/{id}` | PATCH | Rename a chat — `{title}`, one line, ≤100 characters |
| `/agents/{name}/history?session_id=&limit=` | GET | A chat's messages, the in-flight marker and the last turn's outcome. No message carries its cost |
| `/agents/{name}/chat` | POST | Send a turn and wait for the reply — the synchronous integration surface; accepts `session_id`, `new_thread`, `model` and `open_canvas_id` (the canvas on screen, passed as context; an id the caller cannot see resolves to nothing) |
| `/agents/{name}/chat/stream` | POST | Begin a turn, returning an execution id to watch; same body |
| `/agents/{name}/executions/{id}/stream` | GET | Live activity for one of your own turns (SSE) |
| `/agents/{name}/executions/{id}/terminate` | POST | Stop one of your own turns |
| `/agents/{name}/documents` | GET / POST | Files the agent shared with you / send the agent a file (25 MB) |
| `/agents/{name}/documents/{file_id}` | DELETE | `?scope=me` removes a shared file from your list; `?scope=everyone` revokes it (owner, platform session) |
| `/agents/{name}/uploads` | GET | Files you sent; `/uploads/{filename}` GET downloads one and DELETE removes it |
| `/agents/{name}/page` | GET | The band and Info payload in one call |
| `/agents/{name}/reports`, `/reports/{id}` | GET | Report metadata and one report's payload (`rows_offset`/`rows_limit` page a table) |
| `/agents/{name}/canvas`, `/canvas/{id}` | GET | The agent's canvases, narrowed to the roster audience for a client |
| `/agents/{name}/canvas/{id}` | DELETE | Remove a canvas (owner or admin, platform session) |
| `/agents/{name}/canvas/bulk-delete` | POST | Remove several — `{canvas_ids}`; reports the ids that existed |
| `/agents/{name}/canvas/{id}/pin` | PUT | Pin or unpin — `{pinned}` (owner or admin) |
| `/agents/{name}/ratings` | POST | Rate a message or a deliverable |
| `/agents/{name}/suggestions` | GET | Suggestions for the caller and this agent — platform sessions only; a client token gets `404` |
| `/agents/{name}/suggestions/feedback` | POST | `{key, action: accept \| dismiss}` for an item currently shown to the caller; `404` for any other key |
| `/agents/{name}/role` | GET | The role card, read from the agent's files |
| `/agents/{name}/role/readiness` | POST | `{status}` — `calibrating` or `ready`; the agent's owner only |
| `/agents/{name}/memory` | GET | What the agent remembers about the caller, and the writes that changed it |
| `/agents/{name}/memory/writes/{id}/undo` | POST | Revert the notes to before one write, latest first |
| `/agents/{name}/decisions` | GET / POST | The seat decision record / record a decision |
| `/agents/{name}/decisions/{id}/actions` | POST | `close`, `reverse` (with a reason), `reconfirm` (with a new date) or `supersede` an active decision; a decision that is no longer active returns `409` |
| `/agents/{name}/voice/start` | POST | Start a voice call bound to a chat (platform users) |
| `/agents/{name}/stt` | POST | Transcribe a recorded clip for dictation — returns `{text}`. `404` when dictation is unavailable; a provider error returns a sentence naming the cause (`503` key, permission or credits; `429` rate limit; `422` unreadable recording; `502` provider failure) |
| `/chat-state` | GET | Star and unread state for every chat; `?previews=true` adds each unread chat's newest arrivals (the Inbox) |
| `/chat-state/{kind}/{id}/star` | PUT / DELETE | Star or unstar a chat |
| `/chat-state/{kind}/{id}/read` | POST | Advance the read cursor |
| `/search?q=` | GET | Search the caller's chats |

Asks (`/asks`, with `/asks/{id}/answer`, `/discuss`, `/dismiss` and `/context`) and the Work tab have their own routes under the same prefix — see [Approvals](../automation/approvals.md) and [Executions](../operations/executions.md).

**API Endpoints**: See [Backend API Docs](http://localhost:8000/docs) for full schemas.

## Limitations

- **Shared agents only.** A client sees an agent only if it is shared with their verified email.
- **No admin access.** Clients can chat, send files, star and rename chats, rate replies and read reports — never configure, create, or manage agents.
- **Platform users only** for the model dropdown, voice calls, the Work and Loops tabs, and a room's live cards. A client sees none of them; in a room, a client sees *… is thinking…* instead and cannot stop a turn.
- **Stop covers only work your own messages started** — your turns, the jobs they hand on, and room turns. Nobody else can stop them from the Workspace. A loop is stopped from the **Loops** tab; a scheduled or background run from the agent's **Tasks** tab.
- **Dictation depends on the ElevenLabs key.** Without a key that may call speech-to-text, dictation falls back to the browser's own engine, and the microphone is hidden in a browser that has none.
- **Multi-agent chat is available in every build.** Against an older backend that does not serve rooms, `@mention` escalation is unavailable and Workspace says so rather than failing obscurely.
- **Rooms show no unread count.** Stars work for rooms; unread badges count 1:1 chats only. A chat that existed before you first read anything, and was never opened, reports nothing.
- **Room settings are not editable here** beyond the name. Topic, budget and scribe are set through the API.
- **`/` skills are not offered in a room.** `@` is.
- **Codex agents replay history** instead of resuming, so their continuity is text-only.
- **A very long chat is windowed.** The thread shows the newest turns and says *Earlier messages in this chat aren't shown* when older ones were cut.
- **No cost or model information** is shown on the agent's band or Info tab, and the chat history and chat reply routes return no turn cost to any caller.
- **Suggestions are for platform users.** An external client sees no **Suggested for you** list. Suggestions are not offered in a room.
- **Drafts stay in one browser.** Unsent text does not follow you to another device.
- **Rooms are not in the Inbox yet**, and **Reply to this message** is offered in 1:1 chats only.
- **Agent memory for the switch keys is per tab.** Which chat you last had open with each agent is forgotten on reload.
- **Some Workspace surfaces need an entitlement.** A few platform-user features appear only on instances licensed for them; an external client never sees them.

## See Also

- [Public Links](public-links.md) — a single anonymous chat URL for one agent, no sign-in and no history (Workspace is the signed-in, multi-conversation counterpart)
- [Agent Sharing & Access](agent-sharing.md) — sharing agents with operators and external clients (grants the Workspace roster)
- [Cross-Channel Access Control](access-control.md) — verified-email identity and access requests across channels
- [Continuous Conversations](../agents/agent-session.md) — what resuming preserves, and its limits
- [Voice Chat](../advanced/voice-chat.md) — the real-time call inside a Workspace chat
- [Agent Canvas](../agents/agent-canvas.md) — the surface the rail's Canvas tab and the call's right column show
- [Shared Sessions (Rooms)](../collaboration/rooms.md) — how multi-agent conversations work underneath
- [Agent Chat](../agents/agent-chat.md) — the stateless chat surface on Agent Detail
