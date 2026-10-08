# Agent Chat

The Chat tab in Agent Detail provides a bubble UI for conversing with agents, with persistent history and real-time status updates.

Each message on this tab runs as a new agent session, and the last 20 messages of the visible transcript are included as text for context. The tab says so in a banner (*Each message runs as a new agent session; the last 20 messages are included for context.*), with a **Continue in Workspace →** link beside it that opens the Workspace in its own browser tab.

For a conversation where the agent keeps its working memory between turns — tool results, mid-task state, reasoning — use the [Workspace](../sharing-and-access/workspace.md) instead. The Session-mode toggle that used to live here has been retired in its favour, and `?tab=session` links now redirect there.

The tab is **text only**. Voice lives in the Workspace: the **Talk** button in the agent's header opens the Workspace on this agent with a call starting — see [Voice Chat](../advanced/voice-chat.md). Calls you had here before that change are still in their sessions, badged **Voice**.

> 📺 **Watch:** [I Gave My AI Three Years of My Notes — Then Interviewed It (voice chat)](https://youtu.be/xflQTzarEBQ) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Chat Session** -- A conversation thread stored in the database. Each agent can have multiple sessions.
- **Dynamic Thinking Status** -- Real-time labels showing what the agent is doing (replaces static "Thinking..."). Each tool call becomes a short line such as *Reading …/src/app.py...* or *Running pytest -q...*; each label stays up at least 500 ms so fast tool calls don't flicker.
- **Playbook Autocomplete** -- Type `/` in the chat input to trigger a dropdown of available playbooks. Ghost text shows command syntax with argument hints.
- **Continue as Chat** -- Resume a completed or failed execution as an interactive chat, preserving the full context (150K+ tokens) via Claude Code's `--resume` flag.

## How It Works

1. Open an agent's detail page and click the **Chat** tab.
2. Select an existing session from the dropdown or click **New Chat**.
3. Optionally pick a model in the **Default model** dropdown above the input; empty means the agent's default. The choice is remembered in this browser.
4. Type a message and press Enter.
5. The agent processes the message -- the status label updates in real-time (e.g., *Reading …/src/app.py...*, *Running pytest -q...*, *Processing results...*).
6. The response appears as a chat bubble.
7. Type `/` to autocomplete playbook commands.

The agent must be running; a stopped agent shows **Agent Not Running** in place of the input.

### Stopping a turn

While a turn runs, **Send** becomes **Stop**. Click it, or press **Esc** with nothing else open, to cancel the in-flight turn: the turn ends as cancelled (not failed) and your message text returns to the input, in front of anything you typed while waiting. A cancel that loses the race to the reply says nothing — the reply is already on screen. A refused cancel leaves the input untouched and says the turn is still running. Stop is available once the turn has an execution id, which takes about a second.

### File Attachments

Attach files to any chat message using the paperclip button or by dragging and dropping onto the chat input.

**Supported types:** images (JPEG, PNG, GIF, WebP), plain text, CSV, JSON, and ZIP. Images are passed to the agent as vision content blocks. Other files are written to `/home/developer/uploads/` inside the agent container and are readable by name; a ZIP is stored as-is, not extracted. The paperclip picker filters to images and text-like files — drop a ZIP onto the input instead.

**Unsupported:** PDF, tar, gzip, rar, video, audio.

**Limits per message:**
| | |
|---|---|
| Max files | 3 |
| Max size per file | 5 MB |
| Max total image size | 10 MB |

Oversized files are rejected client-side with an alert. Files that exceed the total image limit or exceed the per-message count are skipped and a note is appended to the message context. File uploads work in both authenticated chat and public chat.

### Continue as Chat

- From the Execution Detail page, click **Continue as Chat**.
- This opens the Chat tab with a resume banner showing execution context.
- Uses `--resume {session_id}` for native session continuity.

### Session Management

- Sessions persist across container restarts.
- Close a session: `POST /api/agents/{name}/chat/sessions/{id}/close`.

## For Agents

### API Endpoints

The Chat tab itself sends each message as an asynchronous `POST /api/agents/{name}/task` with `save_to_session: true` (the transcript rides along as text), then follows the execution's stream and polls it for the result. `POST /chat` is the programmatic route that `chat_with_agent` and the `trinity` CLI use — see [Chat API](../api-reference/chat-api.md).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/chat` | POST | Send chat message (stream-json output). The response's `execution.compaction` reports an auto-compaction during the turn, or is `null` |
| `/api/agents/{name}/chat/sessions` | GET | List all sessions |
| `/api/agents/{name}/chat/sessions/{id}` | GET | Get session with messages |
| `/api/agents/{name}/chat/sessions/{id}/close` | POST | Close session |
| `/api/agents/{name}/chat/history/persistent` | GET | Get persistent history |
| `/api/agents/{name}/chat/history` | GET | The agent's current `/chat` conversation |
| `/api/agents/{name}/chat/history` | DELETE | Reset the `/chat` conversation so the next turn starts fresh (owner or admin) |
| `/api/agents/{name}/executions/{id}/terminate` | POST | Stop an in-flight turn (what **Stop** calls) |

### MCP Tools

- `chat_with_agent(agent_name, message)` -- Send a message to an agent. In its default sequential mode it continues the agent's **own** chat session, shared by every caller of that agent. It never picks up a scheduled or other headless run's session — on Claude Code and Gemini agents alike; if that session has gone missing, the turn retries once from a fresh start. The session starts fresh after a model change or a history reset (`DELETE /api/agents/{name}/chat/history`). A turn that crosses the context limit can pay a one-off auto-compaction, which is recorded as `compact_metadata` (read it with `get_execution_result`) and does not mean the agent is degraded. A turn whose run ends in an error is a failed execution with the real error, never a reply whose text is the error message. On an agent in the experimental pull-mode pilot, each caller gets their own conversation instead of the shared one — see [Chat API → Pull-mode pilot agents](../api-reference/chat-api.md#pull-mode-pilot-agents).
- `get_chat_history(agent_name)` -- Retrieve chat history for an agent.

## See Also

- [Continuous Conversations](agent-session.md) — what resuming preserves, auto-compact, and the per-turn iteration cap
- [Workspace](../sharing-and-access/workspace.md) — the chat surface that keeps working memory across turns
- [Voice Chat](../advanced/voice-chat.md) — the real-time call, reached from **Talk** in the agent's header
- Backend API Docs: http://localhost:8000/docs (full request/response schemas)
- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
