# Trinity FAQ — Chat & Sessions

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I start chatting with an agent?

Open the agent's detail page and click the **Chat** tab. Pick an existing conversation from the dropdown or start a new one, type your message, and press Enter. While the agent works, the status label updates in real time (for example "Reading files..."), and the reply appears as a chat bubble. The agent must be running — if it's stopped, the chat surface asks you to start it first. See [Agent Chat](../agents/agent-chat.md).

## What's the difference between Session mode and classic chat?

The Chat tab has a **Session mode** toggle at the top-right, on by default. With Session mode on, every message reattaches to the same Claude Code session, so the agent keeps its working memory between turns. With it off, you get classic stateless chat: each turn replays the visible transcript as plain text, and the agent starts cold every time. Your choice persists in your browser across all agents, and the two surfaces share nothing — flipping the toggle does not transfer state. See [Agent Session](../agents/agent-session.md).

## What does the agent actually remember between turns in Session mode?

Session mode preserves the agent's full working memory: tool results (files it read, commands it ran), mid-skill state, and reasoning state — not just the text of the conversation. Classic chat only re-sends the visible message log as text, so the agent loses tool outputs and internal state between turns. Use Session mode for long multi-turn reasoning or multi-step work; classic chat is fine for one-shot questions. See [Agent Session](../agents/agent-session.md).

## Why is the Session mode toggle missing for my agent?

The toggle hides — and the tab falls back to classic chat — in two cases: the platform's `session_tab_enabled` feature flag is off, or the agent runs on a runtime without resume support (currently Codex). In both cases you still have a working chat surface; you just lose cross-turn working memory. See [Agent Session](../agents/agent-session.md).

## What does the "X% last cache" number under my session mean?

It's the size of the most recent assistant turn's cache as a fraction of the model's context window — a per-turn reading, not a session-wide "fullness" meter. It bounces by design: a heavy turn climbs, an auto-compact resets the underlying history to a small summary so the next reading drops sharply, and a light turn reads low. A high value means the last turn loaded a lot into the model's cache, not that the session is about to fail. See [Agent Session](../agents/agent-session.md).

## Is the context window always 200K tokens?

No — the denominator is model-specific. Trinity prefers the context window the runtime itself reports for the model that actually ran; when that's unavailable it falls back to a per-model catalog (for example, Gemini and 1M-variant Claude models report a 1M window, Codex around 272K, and plain Claude models default to 200K as a safe floor). So the same percentage can mean very different absolute token counts on different agents. See [Agent Runtimes](../agents/agent-runtimes.md).

## How do I make the agent forget the conversation and start fresh?

In Session mode you have two buttons that do different things. **Clear working memory** wipes the agent's Claude working memory but keeps your visible message log — the next turn starts cold in the same session; use it when the agent is stuck or going in circles. **+ New Session** starts a brand-new conversation with an empty log and fresh cost tracking. In classic chat, just start a **New Chat**. See [Agent Session](../agents/agent-session.md).

## Who can see my chat history?

Chat messages are saved to the platform database and survive container restarts and even agent deletion. You see only your own messages; platform admins can see all messages. Session-mode sessions are strictly per-user — even the agent's owner cannot open another user's sessions on the same agent. See [Agent Chat](../agents/agent-chat.md).

## Where can I see what a chat message cost?

Every assistant reply is recorded with its cost, token usage, and execution time, and each session tracks cumulative cost across the conversation. For a per-run breakdown, the agent's **Tasks** tab lists each execution with its cost and a context-usage bar, plus a Total Cost rollup, and the Execution Detail page shows dedicated Cost and Context cards. The agent header also shows today's spend with a 7-day trend. See [Executions](../operations/executions.md).

## Can several chats run on the same agent at once?

Yes, up to the agent's parallel-capacity limit (`max_parallel_tasks`, default 3), which chat shares with scheduled and background tasks. When all slots are busy, additional chat requests queue (up to 3 waiting); beyond that the request is rejected with a 429 "too many requests" error. Owners can raise the limit in the agent's Settings tab under **Parallel Capacity**, up to the fleet ceiling set by an admin. See [Agent Configuration](../agents/agent-configuration.md).

## Why do I see "Another turn on this session is in progress"?

Session-mode turns on the same session are serialized on purpose — two simultaneous resumes of one Claude session could corrupt its state. If you send a second message (or a teammate script hits the same session) while a turn is still running, the API answers 429 with a retry hint. Wait for the current turn to finish, or start a separate session for the parallel line of work. See [Agent Session](../agents/agent-session.md).

## How do I stop a turn that's stuck or running too long?

Open the agent's **Tasks** tab: every running execution has a **Stop execution** button that terminates it on the agent and marks it cancelled. If the work is still queued and hasn't started, cancelling removes it from the queue immediately without touching the container. The Execution Detail page offers the same termination for a running execution. See [Executions](../operations/executions.md).

## Can the agent keep working on something in the background while I chat?

Yes. An agent can dispatch a task to itself in parallel ("self-execute"), tell you it's working on it in the background, and keep the chat responsive. When the background task finishes, the result can be injected into the chat as a collapsed "Background Task Result" card you click to expand. Note there's no cancellation control for self-tasks from within the chat yet. See [Self-Execute](../agents/self-execute.md).

## What happens if I close my browser while the agent is still working?

The turn keeps running on the server — the backend persists both your message and the agent's reply itself, so nothing is lost. When you come back to the Chat tab, the UI checks whether a turn is still in progress on the session and reattaches, polling until the reply lands instead of showing a false failure. Very long turns may take a moment to reconcile after the tab wakes up. See [Agent Session](../agents/agent-session.md).

## Can I pick a different model for a chat?

Yes. Both chat surfaces have a model selector next to the chat controls (placeholder "Default model"): pick a Claude model from the list or type any model id. The choice is saved in your browser and applies to your chat turns only — it doesn't change the agent's default model or affect schedules, which have their own per-schedule override. See [Agent Chat](../agents/agent-chat.md).

## Does chat render markdown, and can I attach files?

Agent replies render as markdown (headings, lists, code blocks), sanitized before display. You can attach files with the paperclip button or by dragging them onto the input: images (JPEG, PNG, GIF, WebP), plain text, CSV, and JSON are supported — up to 3 files per message, 5 MB each, 10 MB total for images. PDF, ZIP, video, and audio are not supported. See [Agent Chat](../agents/agent-chat.md).

## Can I talk to my agent with voice?

Yes — click the microphone button next to the chat input to open a full-screen voice overlay with real-time speech in both directions; transcripts are saved to the chat session when you end the call. It requires a Gemini API key configured on the platform, and it's available only in authenticated chat (not public links). The mic is hidden while Session mode is on — switch the toggle off for voice. See [Voice Chat](../advanced/voice-chat.md).
