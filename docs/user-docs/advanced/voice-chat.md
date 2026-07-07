# Voice Chat

Real-time voice conversations with agents via Gemini 2.5 Flash Native Audio model (~280ms latency). Audio streams bidirectionally through a backend WebSocket proxy. Gemini handles speech-to-speech; Claude Code remains the agent's reasoning engine and is invoked on demand via tool calling.

> 📺 **Watch:** [I Gave My AI Three Years of My Notes — Then Interviewed It](https://youtu.be/xflQTzarEBQ) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Voice Session** — A live audio session bridged between the browser, Trinity backend, and Gemini Live API. Transcripts are saved to the agent's chat session on close.
- **Animated Orb** — Canvas-rendered visualization that reflects session state via color and particle movement.
- **Tool Calling (`run_task`)** — During a voice session, Gemini can delegate complex tasks to the underlying Claude agent. The orb shows an amber badge while the task runs.
- **Voice System Prompt** — Controls Gemini's persona for the session. Looked up in order: DB setting → `voice-agent-system-prompt.md` in the container → auto-generated from template info → generic fallback.
- **Workspace Mode** — A full-page voice surface with a live canvas the agent can draw on (diagrams, images, formatted text) while you talk. Admin opt-in, BETA. See [Workspace Mode](#workspace-mode-beta).

## How It Works

1. Open an agent's **Chat** tab.
2. Click the microphone button next to the chat input.
3. A full-screen voice overlay appears with an animated canvas orb.
4. Speak — audio is captured as PCM 16 kHz and streamed to the backend WebSocket.
5. The backend proxies audio to the Gemini Live API in real-time.
6. Agent response audio (PCM 24 kHz) plays back immediately (~280ms TTFT).
7. When Gemini needs to perform a complex task, it calls `run_task`:
   - The orb shifts to an **amber badge** state.
   - Trinity sends the prompt to the Claude agent (up to 30 seconds).
   - Gemini speaks the result when done; the orb returns to listening state.
8. Click **End** to close the session. Transcripts are saved to the current chat session.

### Orb State Reference

| State | Orb color | Trigger |
|---|---|---|
| Idle / Connecting | Base hue (0°) | Before audio starts |
| Listening | +90° shift (green) | Microphone active, user speaking |
| Speaking | +210° shift (indigo) | Gemini responding |
| Tool calling | Amber badge overlay | `run_task` dispatched to Claude |

### Muting

Click **Mute** to silence your microphone mid-session. Gemini continues speaking. Click again to unmute.

## Requirements

- `GEMINI_API_KEY` configured in **Settings → AI Keys**.
- `VOICE_ENABLED` must be on (default: on when API key is present).
- Browser microphone permission granted.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | API key for Gemini Live API | — (required) |
| `VOICE_ENABLED` | Global toggle | `true` |
| `WORKSPACE_ENABLED` | Enable the Workspace Mode canvas (BETA, admin opt-in) | `false` |
| `VOICE_MODEL` | Gemini model ID (leave unset to use the built-in default) | `models/gemini-3.1-flash-live-preview` |
| `VOICE_MAX_DURATION` | Max session duration in seconds | `300` |

### Per-Agent Voice Prompt

Set a custom voice system prompt for an agent by placing a file named `voice-agent-system-prompt.md` in the agent's workspace (`/home/developer/`). This controls Gemini's persona — tone, focus, and response style — independently of the agent's main `CLAUDE.md`.

If no file is present, Trinity auto-generates a prompt from the agent's template info and falls back to a generic prompt.

### Per-Agent Voice

Each agent has a persisted Gemini voice (default **Kore**) that applies to both this browser voice overlay and outbound [VoIP calls](voip-telephony.md#the-agents-voice) — the full selectable list and the UI picker are documented there. Read or set it via `GET`/`PUT /api/agents/{name}/voice/name` (PUT is owner-only; an empty value reverts to the default).

## Tool Calling

When Gemini encounters a request that requires complex reasoning, file access, or external actions, it calls the `run_task` function:

1. Gemini formulates a task prompt (max 2000 characters).
2. Trinity dispatches the prompt to the Claude agent via the existing chat/task path.
3. The agent runs with full tool access (read/write files, web search, MCP tools, etc.).
4. The result is returned to Gemini, which incorporates it into its spoken response.
5. If the agent is unreachable or the task times out (30s), Gemini recovers gracefully.

All `run_task` invocations are written to the platform audit log.

## Workspace Mode (BETA)

Workspace Mode is a full-page voice surface with a **live canvas** beside the orb. While you talk, the agent can paint the canvas with diagrams, images, and formatted text — useful for walkthroughs, design reviews, and any conversation where a picture helps. It is **opt-in and admin-gated**, off by default.

### Enabling Workspace Mode

Workspace Mode is hidden unless an admin enables it platform-wide:

| Variable | Description | Default |
|----------|-------------|---------|
| `WORKSPACE_ENABLED` | Global toggle for the workspace canvas (BETA) | `false` |

The button only appears when `workspace_available` is true, which requires **both** voice to be available (`VOICE_ENABLED` + `GEMINI_API_KEY`) **and** `WORKSPACE_ENABLED=true`.

### How It Works

1. On the Agent Detail page (agent must be running), click **Workspace** in the header — it carries an amber **BETA** badge.
2. The browser opens the full-page workspace at `/agents/{name}/workspace`: the animated orb and controls on the left, the canvas on the right.
3. Start talking. The voice session behaves exactly like standard mode — same orb states, same `run_task` delegation to Claude.
4. When the agent decides a visual helps, it calls a **panel tool**. The canvas updates within ~300ms.

### Panel Tools

The agent drives the canvas with these in-session tools (resolved inside Trinity — they never run in the agent container):

| Tool | Effect on the canvas |
|------|----------------------|
| `show_markdown` | Render formatted text (headings, lists, tables) |
| `show_diagram` | Render a **Mermaid** diagram (flowcharts, sequence diagrams, etc.) |
| `show_image` | Show an image — a web URL or a file from the agent's workspace |
| `update_panel` | Replace the canvas with an HTML layout |
| `append_to_panel` | Add content to the current panel |
| `clear_panel` | Empty the canvas |

### Panel History

The canvas keeps a **40-snapshot history**. Use the **prev/next** controls or the dropdown to step back through what was shown earlier in the conversation. "Live" follows the newest snapshot; navigating back pins the view until a new update arrives.

### Rendering & Safety

All canvas content is sanitized before display (DOMPurify, the same trust model as every other markdown surface on the platform):

- **Markdown** and **Mermaid** diagrams render directly in the page.
- **Images** from the agent's workspace are fetched over an authenticated channel; web URLs load directly. Paths are confined to the workspace — traversal (`..`), absolute escapes, and non-`http` schemes (`data:`, etc.) are rejected.
- **HTML** panels render as **static layout only** — `<script>` tags are stripped, so agent-supplied JavaScript (e.g. Chart.js) does **not** execute. Use `show_diagram` for dynamic visuals instead.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice/start` | POST | Start a voice session; pass `workspace_mode: true` for canvas mode. Returns `voice_session_id` and WebSocket URL |
| `/api/agents/{name}/voice/stop` | POST | End session; returns transcript and cost |
| `/api/agents/{name}/voice/status` | GET | Get current session state |
| `/api/agents/{name}/voice/{session_id}/panel` | GET | Current workspace canvas state (`type`, `content`, `title`, `updated_at`); polled by the canvas |
| `/api/agents/{name}/voice/prompt` | GET / PUT | Read or set the per-agent voice system prompt |
| `/api/agents/{name}/voice/name` | GET / PUT | Read (with `available_voices`) or set the persisted per-agent Gemini voice |
| `/ws/voice/{session_id}` | WebSocket | Bidirectional audio bridge |

### WebSocket Message Types

**Client → Server:**
```json
{ "type": "audio", "data": "<base64 PCM 16kHz audio>" }
```

**Server → Client:**
```json
{ "type": "audio",      "data": "<base64 PCM 24kHz audio>" }
{ "type": "transcript", "role": "user|assistant", "text": "..." }
{ "type": "status",     "state": "listening|speaking|processing" }
{ "type": "tool_call",  "tool_name": "run_task" }
{ "type": "tool_result","tool_name": "run_task", "result": "..." }
```

## Limitations

- Voice is available only in authenticated chat (not public links).
- One voice session per agent at a time.
- Maximum session duration: 300 seconds (configurable).
- `run_task` tool calls time out after 30 seconds.
- Incremental transcript display during the session is not yet implemented — transcripts appear in the chat after the session ends.
- Workspace Mode is BETA, off by default, and HTML panels are static (no JavaScript execution). Exporting canvas content (PDF/markdown) and multi-page canvases are not yet available.

## See Also

- [Agent Chat](../agents/agent-chat.md)
- Backend API Docs: http://localhost:8000/docs
