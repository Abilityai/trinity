# Agent Chat

The Chat tab in Agent Detail provides a bubble UI for conversing with agents, with persistent history and real-time status updates.

> 📺 **Watch:** [I Gave My AI Three Years of My Notes — Then Interviewed It (voice chat)](https://youtu.be/xflQTzarEBQ) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Chat Session** -- A conversation thread stored in the database. Each agent can have multiple sessions.
- **Dynamic Thinking Status** -- Real-time labels showing what the agent is doing (replaces static "Thinking..."). Maps tool names to human-readable labels with 500ms anti-flicker.
- **Playbook Autocomplete** -- Type `/` in the chat input to trigger a dropdown of available playbooks. Ghost text shows command syntax with argument hints.
- **Continue as Chat** -- Resume a completed or failed execution as an interactive chat, preserving the full context (150K+ tokens) via Claude Code's `--resume` flag.

## How It Works

1. Open an agent's detail page and click the **Chat** tab.
2. Select an existing session from the dropdown or click **New Chat**.
3. Type a message and press Enter.
4. The agent processes the message -- the status label updates in real-time (e.g., "Reading files...", "Running tests...").
5. The response appears as a chat bubble with cost and token tracking.
6. Type `/` to autocomplete playbook commands.

### File Attachments

Attach files to any chat message using the paperclip button or by dragging and dropping onto the chat input.

**Supported types:** images (JPEG, PNG, GIF, WebP), plain text, CSV, JSON. Images are passed to the agent as vision content blocks. Text files are written to `/home/developer/uploads/` inside the agent container and are readable by name.

**Unsupported:** PDF, ZIP, archives, video, audio.

**Limits per message:**
| | |
|---|---|
| Max files | 3 |
| Max size per file | 5 MB |
| Max total image size | 10 MB |

Oversized files are rejected client-side with an alert. Files that exceed the total image limit or exceed the per-message count are skipped and a note is appended to the message context. File uploads work in both authenticated chat and public chat.

### Voice Chat

Voice chat is available directly from the Chat tab.

- Click the microphone button to start a voice session.
- Audio streams bidirectionally through the backend WebSocket proxy to Gemini 2.5 Flash Native Audio (~280ms latency).
- Transcripts are auto-saved to the chat session with `source="voice"` markers.
- Requires `GEMINI_API_KEY` configured on the platform.
- Controls: mute, end session, status indicator.
- **Tool calling**: during a voice session, Gemini can invoke `run_task` to delegate complex work to the Claude agent. The orb shows an amber badge while the task runs (up to 30 seconds) and returns to the listening state when done.

### Continue as Chat

- From the Execution Detail page, click **Continue as Chat**.
- This opens the Chat tab with a resume banner showing execution context.
- Uses `--resume {session_id}` for native session continuity.

### Session Management

- Sessions persist across container restarts.
- Context window tracking: token usage display (e.g., "45.5K / 200K").
- Session cost tracking: cumulative cost across the conversation.
- Close a session: `POST /api/agents/{name}/chat/sessions/{id}/close`.

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/chat` | POST | Send chat message (stream-json output) |
| `/api/agents/{name}/chat/sessions` | GET | List all sessions |
| `/api/agents/{name}/chat/sessions/{id}` | GET | Get session with messages |
| `/api/agents/{name}/chat/sessions/{id}/close` | POST | Close session |
| `/api/agents/{name}/chat/history/persistent` | GET | Get persistent history |
| `/api/agents/{name}/chat/history` | DELETE | Reset session |

### MCP Tools

- `chat_with_agent(agent_name, message)` -- Send a message to an agent.
- `get_chat_history(agent_name)` -- Retrieve chat history for an agent.

## See Also

- Backend API Docs: http://localhost:8000/docs (full request/response schemas)
- [Creating Agents](creating-agents.md)
- [Managing Agents](managing-agents.md)
