# Voice Chat

Real-time voice conversations with agents via Gemini 2.5 Flash Native Audio model (~280ms latency). Audio streams bidirectionally through a backend WebSocket proxy. Gemini handles speech-to-speech; Claude Code remains the agent's reasoning engine and is invoked on demand via tool calling.

> 📺 **Watch:** [I Gave My AI Three Years of My Notes — Then Interviewed It](https://youtu.be/xflQTzarEBQ) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Voice Session** — A live audio session bridged between the browser, Trinity backend, and Gemini Live API. It runs in the **Workspace**, inside the chat you started it from, and its transcript lands in that chat.
- **Animated Orb** — Canvas-rendered visualization that reflects session state via color and particle movement.
- **Tool Calling (`run_task`)** — During a voice session, Gemini can delegate complex tasks to the underlying Claude agent. The orb shows an amber badge while the task runs.
- **Voice System Prompt** — Controls Gemini's persona for the session. Looked up in order: DB setting → `voice-agent-system-prompt.md` in the container → auto-generated from template info → generic fallback.
- **Talk** — The button on an agent's page that opens the Workspace with the call already starting. See [Voice mode in the Workspace](#voice-mode-in-the-workspace).

## How It Works

**The Workspace is the only place a voice call runs.** There are two ways in, and they end up in the same call:

- **From the agent's page** — click **Talk** in the header, beside **Workspace**. Trinity opens the Workspace on that agent and starts the call. Talk is always there; if the instance cannot run a call, the Workspace tells you why.
- **From the Workspace** — click the **Voice** control in the conversation header, in whichever chat you want the call to belong to.

Once the call is up:

1. The orb takes the conversation column, with the agent's canvas beside it.
2. Speak — audio is captured as PCM 16 kHz and streamed to the backend WebSocket.
3. The backend proxies audio to the Gemini Live API in real-time.
4. Agent response audio (PCM 24 kHz) plays back immediately (~280ms TTFT).
5. When Gemini needs to perform a complex task, it calls `run_task`:
   - The orb shifts to an **amber badge** state.
   - Trinity sends the prompt to the Claude agent (up to 30 seconds).
   - Gemini speaks the result when done; the orb returns to listening state.
6. Click **End call** (or the orb's End button, or **Esc**) to finish. The spoken turns are already in the chat, as one collapsed **Voice call · N min** block.

The agent's **Chat** tab on its own page is text only. Voice calls you had there before this change are still in that chat, marked as spoken; new calls belong to the Workspace chat you start them in.

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
| `WORKSPACE_ENABLED` | Legacy flag for the retired per-agent workspace page; it gates nothing in the voice path today | `false` |
| `VOICE_MODEL` | Gemini model ID (leave unset to use the built-in default) | `models/gemini-3.1-flash-live-preview` |
| `VOICE_MAX_DURATION` | Max session duration in seconds (the shared default; the Workspace overrides it below) | `300` |
| `WORKSPACE_VOICE_MAX_DURATION` | Max call duration in seconds (Workspace voice mode) | `1800` |

### Per-Agent Voice Prompt

Set a custom voice system prompt for an agent by placing a file named `voice-agent-system-prompt.md` in the agent's workspace (`/home/developer/`). This controls Gemini's persona — tone, focus, and response style — independently of the agent's main `CLAUDE.md`.

If no file is present, Trinity auto-generates a prompt from the agent's template info and falls back to a generic prompt.

### Per-Agent Voice

Each agent has a persisted Gemini voice (default **Kore**) that applies to both the browser voice call and outbound [VoIP calls](voip-telephony.md#the-agents-voice) — the full selectable list and the UI picker are documented there. Read or set it via `GET`/`PUT /api/agents/{name}/voice/name` (PUT is owner-only; an empty value reverts to the default).

## Tool Calling

When Gemini encounters a request that requires complex reasoning, file access, or external actions, it calls the `run_task` function:

1. Gemini formulates a task prompt (max 2000 characters).
2. Trinity dispatches the prompt to the Claude agent via the existing chat/task path.
3. The agent runs with full tool access (read/write files, web search, MCP tools, etc.).
4. The result is returned to Gemini, which incorporates it into its spoken response.
5. If the agent is unreachable or the task times out (30s), Gemini recovers gracefully.

All `run_task` invocations are written to the platform audit log.

## Voice mode in the Workspace

The Workspace conversation has a **Voice** control in its header. Press it and the call starts in the chat you are in — modal, the way ChatGPT's voice mode is: you are either in the chat or in the call. Pressing **Talk** on an agent's page brings you here with the call already starting.

### What happens

1. The orb takes the conversation column. The header, the chat tabs and the composer stay visible but are inert until the call ends.
2. The agent's **canvas** takes the right column (orb left, canvas right). When the agent shows something while it talks — a summary, a diagram, an image, a table — it appears there live, and it stays on the rail's **Canvas** tab after the call.
3. A status line under the tabs says what the orb is doing (**Listening**, **Speaking**, **Working: run task**) and how to leave: **End call**, the orb's End button, or **Esc**.
4. Ending the call returns you to the chat exactly where it was. The spoken turns are in the chat as one collapsed **Voice call · N min** block, marked as spoken.

The call knows the recent turns of the chat it started in, and its transcript belongs to that chat — a call started from **Main** lands in Main like any other message. The agent's own Chat tab is untouched.

### When the control is disabled

The Voice control is shown to signed-in platform users. When the instance cannot run a call, the control is disabled and its tooltip says why: voice is turned off, or no voice provider key is configured. External clients signed in with a portal code do not see the control.

**Talk**, on the agent's page, is never hidden — that is deliberate. Hiding it would mean a working feature disappears whenever the flag has not loaded yet or the request to fetch it failed, and you would have no way to tell that from "this instance has no voice". So Talk always takes you to the Workspace, and the Workspace tells you in words if a call cannot start.

### Limits, and what you hear

- A Workspace call lasts at most **30 minutes** by default (`WORKSPACE_VOICE_MAX_DURATION`). Thirty seconds before the limit the agent is told to wrap up out loud; at the limit the call ends and the chat records "ended at the 30-minute limit".
- Microphone denied, an insecure (non-https) page, a provider error, a dropped connection — each ends or refuses the call with a sentence in the status line. The chat is never blocked by a failed call.
- Switching chats, New chat and ⌘J wait until the call ends. Leaving the page ends the call; the transcript is kept.
- **Mute** silences your microphone; the agent keeps talking. Talking over the agent interrupts it.

### Canvas tools

While you talk, the agent can draw on its canvas with these in-session tools (resolved inside Trinity — they never run in the agent container):

| Tool | Effect on the canvas |
|------|----------------------|
| `show_markdown` | Render formatted text (headings, lists, tables, and chart / KPI / table fences) |
| `show_diagram` | Render a **Mermaid** diagram (flowcharts, sequence diagrams, etc.) |
| `show_image` | Show an image — a web URL or a file from the agent's workspace |
| `update_panel` | Replace the agent's voice block with an HTML layout |
| `append_to_panel` | Add content to the current block |
| `clear_panel` | Remove what the call drew (the agent's own canvas blocks stay) |

All canvas content is sanitized before display (DOMPurify, the same trust model as every other markdown surface on the platform): scripts never execute, images from the workspace load through an authenticated route, and paths are confined to the workspace.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/enterprise/client-portal/agents/{name}/voice/start` | POST | Start a Workspace voice call bound to a chat (`portal_session_id`). Platform users only. Returns `voice_session_id` and the WebSocket URL |
| `/api/agents/{name}/voice/start` | POST | The original per-agent start route; pass `workspace_mode: true` for canvas tools. Retained for API clients — Trinity's own UI no longer calls it |
| `/api/agents/{name}/voice/stop` | POST | End session; returns transcript and cost. Retained; the Workspace call closes on the socket instead |
| `/api/agents/{name}/voice/status` | GET | Get current session state. Retained |
| `/api/agents/{name}/voice/{session_id}/panel` | GET | The agent's canvas as the call draws it (the same shape as the Canvas tab); read by the Workspace call's canvas column |
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
{ "type": "status",     "state": "connecting|listening|speaking|ended", "reason": "cap|error|provider_closed|null", "message": "..." }
{ "type": "tool_call",  "tool": "run_task" }
{ "type": "tool_result","tool": "run_task", "result_preview": "..." }
{ "type": "saved",      "messages_saved": 12, "duration_seconds": 245.0 }
```

## Limitations

- Voice is available only to signed-in platform users, in the Workspace (not public links, and not to external clients signed in with a portal code).
- One voice session per agent at a time.
- Maximum duration: 30 minutes in the Workspace (configurable). The call ends with a spoken and written notice.
- `run_task` tool calls time out after 30 seconds.
- Incremental transcript display during the session is not yet implemented — transcripts appear in the chat after the session ends.
- In the Workspace, rooms (chats with several agents) have no voice mode, and on a phone the canvas stays behind the Canvas tab rather than beside the orb.
- HTML canvas blocks are static (no JavaScript execution). Exporting canvas content (PDF/markdown) is not yet available.

## See Also

- [Agent Chat](../agents/agent-chat.md)
- Backend API Docs: http://localhost:8000/docs
