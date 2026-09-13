# Trinity FAQ — Chat & Sessions

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I start chatting with an agent?

Two places. The **Workspace** (`/workspace`, opened from the nav in its own browser tab) is the main one: click an agent in the sidebar and you land in the chat you were last in with it — its pinned **Main** chat the first time — and the conversation keeps its memory from turn to turn. The **Chat** tab on the agent's detail page is a quick stateless surface — fine for one-off questions, but each message starts fresh. Either way the agent must be running. See [Workspace](../sharing-and-access/workspace.md) and [Agent Chat](../agents/agent-chat.md).

## What's the difference between the Workspace and the Chat tab?

Memory. A **Workspace** conversation resumes: every message reattaches to the same underlying session, so the agent keeps its working memory between turns. The **Chat** tab on Agent Detail is stateless — each turn replays the visible transcript as plain text and the agent starts cold every time. They are separate surfaces with separate history; the Chat tab carries a **Continue in Workspace →** link when you want continuity. (The old Session-mode toggle has been retired in the Workspace's favour.) See [Continuous Conversations](../agents/agent-session.md).

## What does the agent actually remember between turns in the Workspace?

It preserves the agent's full working memory: tool results (files it read, commands it ran), mid-skill state, and reasoning state — not just the text of the conversation. The stateless Chat tab only re-sends the visible message log as text, so the agent loses tool outputs and internal state between turns. Use the Workspace for long multi-turn reasoning or multi-step work; the Chat tab is fine for one-shot questions. See [Continuous Conversations](../agents/agent-session.md).

## Why doesn't my agent seem to remember anything between turns?

Most likely you're on the **Chat** tab rather than the Workspace — the Chat tab is stateless by design. If you are in the Workspace, the other cause is the runtime: a Codex agent has no resume primitive, so its turns replay the visible history as text instead of carrying working memory forward. The conversation stays coherent either way; what's lost is the agent's tool results and mid-task state. See [Continuous Conversations](../agents/agent-session.md).

## Why does a long conversation suddenly take much longer on one turn?

That's auto-compact. When the agent's internal history approaches roughly 85% of the model's context window, it summarizes that history mid-turn and continues — which adds a couple of minutes to that one turn and is entirely normal. Your visible message log is untouched. After several compacts in one conversation the summary loses fidelity and answers get vaguer; that's the point to start a fresh chat. See [Continuous Conversations](../agents/agent-session.md).

## Is the context window always 200K tokens?

No — the denominator is model-specific. Trinity prefers the context window the runtime itself reports for the model that actually ran; when that's unavailable it falls back to a per-model catalog (for example, Gemini and 1M-context Claude models such as Sonnet 5 report a 1M window, Codex around 1.05M on the gpt-5.6 family and around 272K on older ones, and plain Claude models default to 200K as a safe floor). So the percentage-used bar rescales to whichever model ran, and the same percentage can mean very different absolute token counts on different agents. See [Agent Runtimes](../agents/agent-runtimes.md).

## What is the Main chat, and what does Reset do?

Every (you, agent) pair has one pinned **Main** chat: it is always the first tab above the thread, it can't be renamed, and it's where the agent reaches you when nothing else names a chat — an agent-initiated message, a question it raises outside a conversation, or a scheduled brief. **Reset**, in the header on Main only, archives the current Main and starts the agent cold; there's no confirmation because nothing is lost — the archived chat stays in your list as an ordinary, still-resumable chat, and a line in the new Main names it. Reset is refused while a reply is in flight, and resetting a Main you have never used does nothing. See [Workspace](../sharing-and-access/workspace.md#chats-as-tabs-and-main).

## How do I start a new chat with an agent, and can I rename it?

**New chat** in the conversation header (or **⌘J** / **Ctrl+J**) starts a fresh chat with the agent in front of you; **New chat** in the sidebar opens the agent picker instead. A provisional tab appears at once and the chat itself is created when you send the first message. Trinity names it from that message (and takes a second pass if your opener was only a greeting); to rename it, click the pencil beside its title or edit it on the sidebar row — one line of up to 100 characters, Enter saves, Esc abandons, and a name you typed always stands. Only Main can't be renamed. A direct link `/workspace?agent=<name>` opens your most recent chat with that agent, and `?new=1` forces a fresh one. See [Workspace](../sharing-and-access/workspace.md#starting-a-chat).

## How do I make the agent forget the conversation and start fresh?

Start a new chat (**New chat** or **⌘J** / **Ctrl+J**): it has no memory of the previous one. If you're in the agent's **Main**, use **Reset** in the header instead — it archives the current Main and starts the agent cold, keeping the old conversation in your list as an ordinary chat. Reach for either when the agent is going in circles, when you're switching topic and don't want bleed-over, or when repeated auto-compaction has degraded its answers. On the Chat tab every message already starts fresh, so there is nothing to clear. See [Continuous Conversations](../agents/agent-session.md#clearing-working-memory).

## What do `/` and `@` do in the Workspace composer?

They open a typeahead. Type `/` at the start of a word for the agent's playbooks — picking one splices its starter prompt into the field without sending — and `@` to bring another agent in. ↓ then Enter (or Tab for the top row) inserts the pick, Esc dismisses the list, and Enter on its own always sends. Picking an `@` agent from a 1:1 opens a **room** containing both agents and posts your message there; rooms are covered in the [Collaboration FAQ](collaboration.md), and `/` playbooks are not offered inside one. See [Workspace](../sharing-and-access/workspace.md#the-composer).

## Who can see my chat history?

Chat messages are saved to the platform database and survive container restarts and even agent deletion. You see only your own messages; platform admins can see all messages. Workspace conversations are strictly per-person — even the agent's owner cannot open someone else's conversations with the same agent. See [Agent Chat](../agents/agent-chat.md).

## Where can I see what a chat message cost?

Every assistant reply is recorded with its cost, token usage, and execution time, and each session tracks cumulative cost across the conversation. For a per-run breakdown, the agent's **Tasks** tab lists each execution with its cost and a context-usage bar, plus a Total Cost rollup, and the Execution Detail page shows dedicated Cost and Context cards. The agent header also shows today's spend with a 7-day trend. The Workspace deliberately shows no cost or model figures — its band, Info tab and Work tab speak in outcomes — so for the numbers go to the agent's Tasks tab or the Executions page. See [Executions](../operations/executions.md).

## Can several chats run on the same agent at once?

Yes, up to the agent's parallel-capacity limit (`max_parallel_tasks`, default 3), which chat shares with scheduled and background tasks. When all slots are busy, additional chat requests queue (up to 3 waiting); beyond that the request is rejected with a 429 "too many requests" error. Owners can raise the limit in the agent's Settings tab under **Parallel Capacity**, up to the fleet ceiling set by an admin. See [Agent Configuration](../agents/agent-configuration.md).

## Why am I told the conversation is already handling a message?

Turns on one conversation are serialized on purpose — two simultaneous resumes of the same session could corrupt its state. Send a second message while one is still running and you get a busy response with a retry hint rather than a queue. Wait for the current turn to finish, or start a separate chat for the parallel line of work. See [Continuous Conversations](../agents/agent-session.md).

## How do I stop a turn that's stuck or running too long?

In the Workspace and on the Chat tab, **Send** becomes **Stop** while a turn runs: click it, or press **Esc** with nothing else open, and the in-flight turn ends as cancelled (not failed) with your words put back in the composer, in front of anything you typed while waiting. The live card under your message in the Workspace carries the same **Stop**. Outside chat, the agent's **Tasks** tab and the Execution Detail page offer **Stop execution** for any running execution; if the work is still queued, cancelling removes it from the queue without touching the container. See [Agent Chat](../agents/agent-chat.md#stopping-a-turn) and [Executions](../operations/executions.md).

## Why did my message fail, and why isn't there always a Retry button?

A failed send tells you the real reason — the agent is busy, the turn timed out, the message was too large, there were too many messages, or the agent hit its usage limit — rather than a bare failure. **Retry** is offered only when nothing reached the agent: a turn the agent already ran is not silently sent and billed twice, so in that case ask again in your own words. An agent that is stopped or unreachable is labelled above the field before you type; the message still sends and the server's refusal is the answer. See [Workspace](../sharing-and-access/workspace.md#while-the-agent-works).

## Can the agent keep working on something in the background while I chat?

Yes. An agent can dispatch a task to itself in parallel ("self-execute"), tell you it's working on it in the background, and keep the chat responsive. When the background task finishes, the result can be injected into the chat as a collapsed "Background Task Result" card you click to expand. Note there's no cancellation control for self-tasks from within the chat yet. See [Self-Execute](../agents/self-execute.md).

## What happens if I close my browser while the agent is still working?

The turn keeps running on the server — the backend persists both your message and the agent's reply, so nothing is lost. When you come back, the UI checks whether a turn is still in progress on that conversation and reattaches, waiting for the reply instead of showing a false failure. Very long turns may take a moment to reconcile after the tab wakes up. See [Continuous Conversations](../agents/agent-session.md).

## Can I pick a different model for a chat?

Yes, on both surfaces, though the pickers differ. In the **Workspace**, platform users on a Claude-runtime agent get a dropdown beside **Send**: **Agent's default (…)** names what the agent would use, and the other three options are plain-language tiers — **Most capable**, **Balanced — fast and smart**, **Fastest**. That choice is remembered per agent on your account, and if the agent can't complete a turn on the model you chose the reply says so and the choice reverts to the default; a Workspace turn resolves your explicit choice → the model the owner set for the agent's public channels → the platform default. On the **Chat** tab, the **Default model** dropdown above the input lists Claude models by name (or type any model id) and is remembered in that browser. Either way it applies to your chat turns only — not the agent's default model, and not its schedules, which have their own per-schedule override. See [Workspace](../sharing-and-access/workspace.md#the-composer) and [Agent Chat](../agents/agent-chat.md).

## What are the Fable 5 and Sonnet 5 models?

Fable 5 is the most capable model — reach for it on the longest, hardest, most involved tasks where quality matters more than speed. Sonnet 5 is the fast, smart everyday model, and it carries a 1M-token context window, so it holds far more of a long conversation or large codebase before compaction. Both appear by name in the Chat tab's model picker, in the agent's Model Selection setting, and in the per-schedule and per-loop overrides. The Workspace dropdown speaks in tiers instead: **Balanced — fast and smart** is Sonnet 5, **Most capable** is Opus 5, and **Fastest** is Haiku. See [Agent Configuration](../agents/agent-configuration.md#model-selection).

## Does chat render markdown, and can I attach files?

Replies render as markdown on both surfaces — headings, lists, tables and code blocks — sanitized before display. In the Workspace a code block is its own object with a language label and an always-visible **Copy**, and every reply carries **Copy message** plus Helpful / Not helpful thumbs. On the **Chat** tab, attach with the paperclip or by dropping onto the input: images (JPEG, PNG, GIF, WebP) reach the agent as vision content, while plain text, CSV, JSON and ZIP are written to `/home/developer/uploads/` inside the container (a ZIP is stored as-is, not extracted — the paperclip picker filters it out, so drop it instead). Limits are 3 files per message, 5 MB each, 10 MB of images in total; PDF, tar, gzip, video and audio are refused. Workspace uploads work differently — see the next question. See [Agent Chat](../agents/agent-chat.md#file-attachments).

## How do I send files to my agent in the Workspace, and where do they end up?

Click the paperclip, or drop files anywhere on the conversation (*Drop files to send to …*) — up to 20 files per drop, 25 MB each, uploaded one after another with a progress chip apiece; a refused file names itself and the limit. Sent files land in the agent's inbox inside its workspace and appear under **Files you sent** in the rail's **Files** tab, alongside **Files from {agent}** for anything it shared back. Click a name to preview it (images, Markdown and text up to 256 KB; ← and → step through the previewable files) or **Download** to save it. The bin icon deletes a file you sent or removes a shared file from your list without touching the share; an agent's owner signed in as a platform user also gets **Delete for everyone**. See [Workspace](../sharing-and-access/workspace.md#the-rail).

## How do I know a reply has arrived when I'm not looking at the chat?

Three ways. While anything is unread, the browser tab's title carries the count — `(3) Trinity — Workspace` — and the agent's row in the sidebar shows its own unread badge (questions the agent is waiting on you to answer are counted separately). Inside a chat, if you've scrolled up to re-read something, an arriving message doesn't pull you back down: a **N new messages** control appears, and sending, opening a chat or clicking it returns you to the bottom. Opening the chat clears the count; unread badges count 1:1 chats only, not rooms. See [Workspace](../sharing-and-access/workspace.md#reading-replies).

## What are the tabs in the rail beside the conversation?

The rail is the right-hand column, collapsed to a strip of icons by default; its state is remembered across chats and reloads, and a dot on a tab means something happened since you last looked. **Work** lists the agent's executions from this chat and its history; **Loops** runs and watches loops on the agent (see the [Scheduling FAQ](scheduling-and-automation.md)); **Canvas** shows the agent's canvas (see the [Advanced Features FAQ](advanced-features.md)); **Files** holds files you sent and files the agent shared; **Info** is the agent's context — your chats with it, what it can do, and its published reports. Work and Loops are for platform users only; an external client sees the other three, with only the canvases the agent published to its roster. See [Workspace](../sharing-and-access/workspace.md#the-rail).

## Why does the Workspace say earlier messages in this chat aren't shown?

The chat is very long and the thread is windowed: the Workspace renders the newest turns and says *Earlier messages in this chat aren't shown* when older ones were cut (a 30-minute voice call alone adds around 180 rows). Nothing is lost — the messages are still stored and the agent's own memory is unaffected; only how much the page draws is trimmed. If the length means the conversation has run its course, start a new chat or Reset Main. See [Continuous Conversations](../agents/agent-session.md#known-limitations).

## Can I talk to my agent with voice?

Yes. Click **Talk** in the agent's header, or the call button — the leftmost control in the Workspace composer — in the chat you want the call to belong to. The orb takes the conversation column with the agent's canvas beside it, speech runs both ways, and when the call ends the spoken turns sit in that chat as one collapsed **Voice call · N min** block, so the agent's next typed turn knows what was said. The separate microphone control in the composer only dictates into the message field. Voice needs a Gemini API key on the platform and is for signed-in platform users only — the Chat tab is text only, and the [Advanced Features FAQ](advanced-features.md) covers what the agent can do during a call. See [Voice Chat](../advanced/voice-chat.md).
