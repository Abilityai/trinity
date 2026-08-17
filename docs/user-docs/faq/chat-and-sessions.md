# Trinity FAQ — Chat & Sessions

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I start chatting with an agent?

Two places. The **Workspace** (`/workspace`) is the main one: pick the agent, type, and the conversation keeps its memory from turn to turn. The **Chat** tab on the agent's detail page is a quick stateless surface — fine for one-off questions, but each message starts fresh. Either way the agent must be running. See [Workspace](../sharing-and-access/workspace.md) and [Agent Chat](../agents/agent-chat.md).

## What's the difference between the Workspace and the Chat tab?

Memory. A **Workspace** conversation resumes: every message reattaches to the same underlying session, so the agent keeps its working memory between turns. The **Chat** tab on Agent Detail is stateless — each turn replays the visible transcript as plain text and the agent starts cold every time. They are separate surfaces with separate history; the Chat tab carries a **Continue in Workspace →** link when you want continuity. (The old Session-mode toggle has been retired in the Workspace's favour.) See [Continuous Conversations](../agents/agent-session.md).

## What does the agent actually remember between turns in the Workspace?

It preserves the agent's full working memory: tool results (files it read, commands it ran), mid-skill state, and reasoning state — not just the text of the conversation. The stateless Chat tab only re-sends the visible message log as text, so the agent loses tool outputs and internal state between turns. Use the Workspace for long multi-turn reasoning or multi-step work; the Chat tab is fine for one-shot questions. See [Continuous Conversations](../agents/agent-session.md).

## Why doesn't my agent seem to remember anything between turns?

Most likely you're on the **Chat** tab rather than the Workspace — the Chat tab is stateless by design. If you are in the Workspace, the other cause is the runtime: a Codex agent has no resume primitive, so its turns replay the visible history as text instead of carrying working memory forward. The conversation stays coherent either way; what's lost is the agent's tool results and mid-task state. See [Continuous Conversations](../agents/agent-session.md).

## Why does a long conversation suddenly take much longer on one turn?

That's auto-compact. When the agent's internal history approaches roughly 85% of the model's context window, it summarizes that history mid-turn and continues — which adds a couple of minutes to that one turn and is entirely normal. Your visible message log is untouched. After several compacts in one conversation the summary loses fidelity and answers get vaguer; that's the point to start a fresh chat. See [Continuous Conversations](../agents/agent-session.md).

## Is the context window always 200K tokens?

No — the denominator is model-specific. Trinity prefers the context window the runtime itself reports for the model that actually ran; when that's unavailable it falls back to a per-model catalog (for example, Gemini and 1M-context Claude models such as Sonnet 5 report a 1M window, Codex around 272K, and plain Claude models default to 200K as a safe floor). So the percentage-used bar rescales to whichever model ran, and the same percentage can mean very different absolute token counts on different agents. See [Agent Runtimes](../agents/agent-runtimes.md).

## How do I make the agent forget the conversation and start fresh?

Start a new chat. In the Workspace, **New chat** gives you an empty conversation with fresh cost tracking and no memory of the previous one. Reach for it when the agent is going in circles, when you're switching topic and don't want bleed-over, or when repeated auto-compaction has degraded its answers. On the Chat tab every message already starts fresh, so there is nothing to clear. See [Continuous Conversations](../agents/agent-session.md).

## Who can see my chat history?

Chat messages are saved to the platform database and survive container restarts and even agent deletion. You see only your own messages; platform admins can see all messages. Workspace conversations are strictly per-person — even the agent's owner cannot open someone else's conversations with the same agent. See [Agent Chat](../agents/agent-chat.md).

## Where can I see what a chat message cost?

Every assistant reply is recorded with its cost, token usage, and execution time, and each session tracks cumulative cost across the conversation. For a per-run breakdown, the agent's **Tasks** tab lists each execution with its cost and a context-usage bar, plus a Total Cost rollup, and the Execution Detail page shows dedicated Cost and Context cards. The agent header also shows today's spend with a 7-day trend. See [Executions](../operations/executions.md).

## Can several chats run on the same agent at once?

Yes, up to the agent's parallel-capacity limit (`max_parallel_tasks`, default 3), which chat shares with scheduled and background tasks. When all slots are busy, additional chat requests queue (up to 3 waiting); beyond that the request is rejected with a 429 "too many requests" error. Owners can raise the limit in the agent's Settings tab under **Parallel Capacity**, up to the fleet ceiling set by an admin. See [Agent Configuration](../agents/agent-configuration.md).

## Why am I told the conversation is already handling a message?

Turns on one conversation are serialized on purpose — two simultaneous resumes of the same session could corrupt its state. Send a second message while one is still running and you get a busy response with a retry hint rather than a queue. Wait for the current turn to finish, or start a separate chat for the parallel line of work. See [Continuous Conversations](../agents/agent-session.md).

## How do I stop a turn that's stuck or running too long?

Open the agent's **Tasks** tab: every running execution has a **Stop execution** button that terminates it on the agent and marks it cancelled. If the work is still queued and hasn't started, cancelling removes it from the queue immediately without touching the container. The Execution Detail page offers the same termination for a running execution. See [Executions](../operations/executions.md).

## Can the agent keep working on something in the background while I chat?

Yes. An agent can dispatch a task to itself in parallel ("self-execute"), tell you it's working on it in the background, and keep the chat responsive. When the background task finishes, the result can be injected into the chat as a collapsed "Background Task Result" card you click to expand. Note there's no cancellation control for self-tasks from within the chat yet. See [Self-Execute](../agents/self-execute.md).

## What happens if I close my browser while the agent is still working?

The turn keeps running on the server — the backend persists both your message and the agent's reply, so nothing is lost. When you come back, the UI checks whether a turn is still in progress on that conversation and reattaches, waiting for the reply instead of showing a false failure. Very long turns may take a moment to reconcile after the tab wakes up. See [Continuous Conversations](../agents/agent-session.md).

## Can I pick a different model for a chat?

Yes. Both chat surfaces have a model selector next to the chat controls (placeholder "Default model"): pick a Claude model from the list or type any model id. The choice is saved in your browser and applies to your chat turns only — it doesn't change the agent's default model or affect schedules, which have their own per-schedule override. See [Agent Chat](../agents/agent-chat.md).

## What are the Fable 5 and Sonnet 5 models?

Fable 5 is the most capable model — reach for it on the longest, hardest, most involved tasks where quality matters more than speed. Sonnet 5 is the fast, smart everyday model, and it carries a 1M-token context window, so it holds far more of a long conversation or large codebase before compaction. Both appear in the model picker next to the chat controls (and in the per-schedule and per-loop model overrides), so you can match the model to the task. See [Agent Configuration](../agents/agent-configuration.md).

## Does chat render markdown, and can I attach files?

Agent replies render as markdown (headings, lists, code blocks), sanitized before display. You can attach files with the paperclip button or by dragging them onto the input: images (JPEG, PNG, GIF, WebP), plain text, CSV, and JSON are supported — up to 3 files per message, 5 MB each, 10 MB total for images. PDF, ZIP, video, and audio are not supported. See [Agent Chat](../agents/agent-chat.md).

## Can I talk to my agent with voice?

Yes — click the microphone button next to the chat input on the agent's Chat tab to open a full-screen voice overlay with real-time speech in both directions; transcripts are saved to the chat session when you end the call. It requires a Gemini API key configured on the platform, and it's available only in authenticated chat, not public links. See [Voice Chat](../advanced/voice-chat.md).
