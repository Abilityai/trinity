# Voice Chat

Real-time voice conversations with agents, inside a Workspace chat. Audio streams bidirectionally through a backend WebSocket proxy to the Gemini Live API (~280ms latency). Gemini handles speech-to-speech; the agent itself remains the reasoning engine and is invoked on demand via tool calling — in a Workspace call, as the agent, in the chat the call belongs to.

> 📺 **Watch:** [I Gave My AI Three Years of My Notes — Then Interviewed It](https://youtu.be/xflQTzarEBQ) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Voice Session** — A live audio session bridged between the browser, Trinity backend, and Gemini Live API. It runs in the **Workspace**, inside the chat you started it from, and its transcript lands in that chat.
- **Animated Orb** — Canvas-rendered visualization that reflects session state via color and particle movement. During a call it takes the conversation column.
- **Tool Calling (`run_task`)** — During a call, Gemini can hand real work to the agent. In a Workspace call the task runs *as the agent*, in that chat — with its skills, files, memory and mid-work state — in the background: the conversation goes on, and the answer lands in the chat as a turn and is voiced when it arrives. The orb shows a pill naming each task while it runs.
- **Voice System Prompt** — Controls Gemini's persona for the session. Looked up in order: DB setting → `voice-agent-system-prompt.md` in the container → auto-generated from template info → generic fallback.
- **Talk** — The button in an agent's Agent Detail header that opens the Workspace with the call already starting. See [Voice mode in the Workspace](#voice-mode-in-the-workspace).

## How It Works

**The Workspace is the only place a voice call runs.** There are two ways in, and they end up in the same call:

- **From Agent Detail** — click **Talk** in the header, beside **Workspace**. Trinity opens the Workspace on that agent and starts the call. Talk is always there; if the instance cannot run a call, the Workspace tells you why.
- **From the Workspace** — click the call button, the leftmost control in the composer (**Start a voice call**), in whichever chat you want the call to belong to.

Once the call is up:

1. The orb takes the conversation column, with the agent's canvas beside it. The columns slide into place rather than snapping.
2. Speak — audio is captured as PCM 16 kHz and streamed to the backend WebSocket.
3. The backend proxies audio to the Gemini Live API in real-time.
4. Agent response audio (PCM 24 kHz) plays back immediately (~280ms TTFT).
5. When Gemini needs to perform real work, it says so out loud — once — and calls `run_task`:
   - The task is handed off at once and runs as the agent, in this chat, in the background. The orb shows a pill naming the task while it runs; the status line reads **Working: run task** for the hand-off itself.
   - You keep talking. Up to three tasks can be in flight at a time, and they run one after another in the chat (each pill shows *queued* or *running*); at the cap the agent says so instead of queueing silently, and the same request repeated while it runs is refused rather than run twice.
   - When a task finishes (or fails, with its reason), the agent brings it up at the next natural pause, says which request it answers, and tells you what is new. Its reply is a turn in the chat (and on the canvas, if the agent drew).
   - Ending the call cancels nothing — a task still running finishes and lands in the chat.
6. Click **End call** in the status line, the orb's End button, or press **Esc** to finish. The spoken turns are already in the chat, as one collapsed **Voice call · N min** block.

The agent's **Chat** tab on its own page is text only. Voice calls you had there before this change are still in that chat, marked as spoken; new calls belong to the Workspace chat you start them in.

### Orb State Reference

| State | Orb color | Trigger |
|---|---|---|
| Idle / Connecting | Base hue (0°) | Before audio starts |
| Listening | +90° shift (green) | Microphone active, user speaking |
| Speaking | +210° shift (indigo) | Gemini responding |
| Tool calling | Amber badge overlay | `run_task` dispatched to the agent |

### Muting

Click **Mute** on the orb, or press **M**, to silence your microphone mid-session; the status line reads **Muted**. Gemini continues speaking. Click or press again to unmute. Talking over the agent interrupts it.

## Requirements

- A Gemini key in **Settings → Integrations** ([Platform Keys](../credentials/platform-keys.md#gemini)), or `GEMINI_API_KEY` in `.env`.
- `VOICE_ENABLED` must be on (default: on when API key is present).
- Browser microphone permission granted, on a secure (https) page.
- A signed-in platform user. External clients signed in with an email code do not get a call button.

The composer's **Speak your message** microphone is a different feature. It only types what you say into the message field, runs on ElevenLabs or the browser's own speech engine rather than Gemini, and is available to clients too. Its requirements, and why it may be missing, are in [Workspace → Dictation](../sharing-and-access/workspace.md#dictation).

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | API key for Gemini Live API. A key saved in **Settings → Integrations** takes precedence and applies without a restart | — (required) |
| `VOICE_ENABLED` | Global toggle | `true` |
| `VOICE_MODEL` | Gemini model ID (leave unset to use the built-in default) | `models/gemini-3.1-flash-live-preview` |
| `VOICE_MAX_DURATION` | Max session duration in seconds for the legacy per-agent start route | `300` |
| `WORKSPACE_VOICE_MAX_DURATION` | Max call duration in seconds (Workspace voice mode) | `1800` |

### Per-Agent Voice Prompt

Set a custom voice system prompt for an agent by placing a file named `voice-agent-system-prompt.md` in the agent's workspace (`/home/developer/`). This controls Gemini's persona — tone, focus, and response style — independently of the agent's main `CLAUDE.md`.

If no file is present, Trinity auto-generates a prompt from the agent's template info and falls back to a generic prompt.

### Per-Agent Voice

Each agent has a persisted Gemini voice (default **Kore**) that applies to both the browser voice call and outbound [VoIP calls](voip-telephony.md#the-agents-voice) — the full selectable list and the UI picker are documented there. Read or set it via `GET`/`PUT /api/agents/{name}/voice/name` (PUT is owner-only; an empty value reverts to the default).

### Per-Agent Tool Surface

The tools a call may use are fixed when the session starts and cannot grow afterwards. The platform offers `run_task` plus the six canvas verbs below, and nothing fleet-wide — a call cannot list agents, message other agents or fan work out. An agent's `template.yaml` may **narrow** that set, never widen it:

```yaml
voice:
  tools: [run_task, show_markdown]   # only these; an empty list means no tools at all
```

A name the platform does not offer is dropped with a warning; declaring nothing keeps the platform default.

## Tool Calling

When Gemini encounters a request that requires complex reasoning, file access, or external actions, it calls the `run_task` function:

1. Gemini says what it is starting ("let me check that for you") and formulates a task prompt (max 2000 characters). It says it once; if it has not said anything within a few seconds of the hand-off, the platform prompts it to.
2. In a **Workspace call**, Trinity runs the prompt as a turn in the chat the call is bound to — the same resumable path a typed message takes, so the agent has its own skills, files, memory and mid-work state. The reply is a turn in that chat, captioned as asked during a voice call.
3. The task runs in the background and the call continues. Up to three tasks can be in flight, run one at a time in the chat. When one lands, Gemini is given the result and brings it up at a natural pause — after both of you have been quiet for a moment, and never more than about 20 seconds after the task finished. It reports a failure once, with its reason.
4. A call with no chat behind it (a phone call) runs the prompt against the agent container instead, waits for it with a 30-second limit, and notes it in the transcript.
5. If the agent is unreachable, Gemini says so. An empty prompt does nothing.

All `run_task` invocations are written to the platform audit log.

## Voice mode in the Workspace

The call starts in the chat you are in — modal, the way ChatGPT's voice mode is: you are either in the chat or in the call. Pressing **Talk** on Agent Detail brings you here with the call already starting.

### What happens

1. The orb takes the conversation column. The header, the chat tabs and the composer stay visible but are inert until the call ends; the call button stays live, because it is also how you end the call.
2. The agent's **canvas** takes the right column (orb left, canvas right). When the agent shows something while it talks — a summary, a diagram, an image, a table — it appears there live, and it stays on the rail's **Canvas** tab after the call.
3. A status line under the tabs says what the orb is doing (**Connecting…**, **Listening**, **Speaking**, **Working: run task**, **Muted**) and how to leave: *End the call to switch chats · Esc ends*, with an **End call** link.
4. Ending the call returns you to the chat exactly where it was. The spoken turns are in the chat as one collapsed **Voice call · N min** block, marked as spoken, and the agent's next typed turn knows what was said.

The call knows the recent turns of the chat it started in, and its transcript belongs to that chat — a call started from **Main** lands in Main like any other message. Starting a call from a brand-new chat creates the chat first, so the transcript has a home before the first word.

### When the control is disabled

The call button is shown to signed-in platform users. When the instance cannot run a call, the button is disabled and its tooltip says why: voice is turned off, or no voice provider key is configured. External clients signed in with an email code do not see it.

**Talk**, on Agent Detail, is never hidden — that is deliberate. Hiding it would mean a working feature disappears whenever the flag has not loaded yet or the request to fetch it failed, and you would have no way to tell that from "this instance has no voice". So Talk always takes you to the Workspace, and the Workspace tells you in words if a call cannot start. A pasted or bookmarked link with `voice=1` in it opens the chat and starts nothing — only the button starts a call.

### Limits, and what you hear

- A Workspace call lasts at most **30 minutes** by default (`WORKSPACE_VOICE_MAX_DURATION`). Thirty seconds before the limit the agent is told to wrap up out loud; at the limit the call ends and the chat records *ended at the 30-minute limit*.
- A call cannot start while a reply to a typed message is still being written (*A reply is still being written — wait for it, then start the call.*), and a typed message is refused while a call is live in that chat, so a reply never lands in the middle of a call.
- Microphone denied, an insecure (non-https) page, a provider error, a dropped connection — each ends or refuses the call with a sentence in the status line. The chat is never blocked by a failed call. The connection to the provider is renewed silently during a long call; the orb shows *Connecting…* for the swap.
- Switching chats, New chat, ⌘J or opening a room mid-call asks first (**End the call?** — *End call and leave* or *Stay on the call*); only **End call** itself never asks. Leaving the page or pressing the browser's back button ends the call; the transcript is kept.
- **Esc** ends the call — unless something is open on top of it. A file preview or a confirm dialog opened mid-call takes the first Esc for itself; the call ends on the next.

### Canvas tools

While you talk, the agent draws on its own **`main`** canvas — the same one it writes with `set_canvas` — through these in-session tools (resolved inside Trinity; they never run in the agent container):

| Tool | Effect on the canvas |
|------|----------------------|
| `show_markdown` | Replace what the call drew with one formatted-text block (headings, lists, tables, and chart / KPI / table / Mermaid fences) |
| `show_diagram` | … with one **Mermaid** diagram block |
| `show_image` | … with one image block — a web URL or a file from the agent's workspace |
| `update_panel` | … with one HTML block, styled with the [design kit](../agents/agent-canvas.md) |
| `append_to_panel` | Add HTML to the block the call is drawing |
| `clear_panel` | Remove what the call drew |

The call owns only the blocks it draws: everything the agent put on the canvas itself survives a call untouched, and a refused verb (a bad image source, an empty diagram) writes nothing and tells the model why. All canvas content is sanitized before display — scripts never execute, images from the workspace load through an authenticated route, and paths are confined to the workspace. See [Agent Canvas](../agents/agent-canvas.md) for the block vocabulary and who can see the result.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/enterprise/client-portal/agents/{name}/voice/start` | POST | Start a Workspace voice call bound to a chat (`portal_session_id`). Platform users only. Returns `voice_session_id` and the WebSocket URL; 409 while a reply is in flight |
| `/api/agents/{name}/voice/start` | POST | The original per-agent start route; pass `workspace_mode: true` for canvas tools. Retained for API clients — Trinity's own UI no longer calls it |
| `/api/agents/{name}/voice/stop` | POST | End session; returns transcript and cost. Retained; the Workspace call closes on the socket instead |
| `/api/agents/{name}/voice/status` | GET | Get current session state. Retained |
| `/api/agents/{name}/voice/{session_id}/panel` | GET | The agent's `main` canvas, as the call draws on it; read by the Workspace call's canvas column |
| `/api/agents/{name}/voice/prompt` | GET / PUT | Read or set the per-agent voice system prompt |
| `/api/agents/{name}/voice/name` | GET / PUT | Read (with `available_voices`) or set the persisted per-agent Gemini voice |
| `/ws/voice/{session_id}` | WebSocket | Bidirectional audio bridge |

### WebSocket Message Types

**Client → Server:**
```json
{ "type": "audio", "data": "<base64 PCM 16kHz audio>" }
{ "type": "end" }
```

**Server → Client:**
```json
{ "type": "audio",      "data": "<base64 PCM 24kHz audio>" }
{ "type": "transcript", "role": "user|assistant", "text": "..." }
{ "type": "status",     "state": "connecting|listening|speaking|ended", "reason": "cap|error|provider_closed|null", "message": "..." }
{ "type": "tool_call",  "tool": "run_task" }
{ "type": "tool_result","tool": "run_task", "result_preview": "..." }
{ "type": "task",       "state": "started|finished|failed", "task_id": "t1", "label": "...", "running": 1 }
{ "type": "saved",      "messages_saved": 12, "duration_seconds": 245.0 }
```

`task` frames track each background task on a Workspace call (the orb's per-task pills). `saved` arrives once, after the transcript is persisted and before the socket closes; the Workspace reloads the chat on it.

## Limitations

- Voice is available only to signed-in platform users, in the Workspace (not public links, and not to external clients signed in with an email code).
- Maximum duration: 30 minutes in the Workspace (configurable). The call ends with a spoken and written notice.
- The transcript appears in the chat when the call ends. It is saved turn by turn on the server, so a dropped connection or a restart mid-call loses nothing already said.
- A call is one chat with one agent: rooms (chats with several agents) have no voice mode, and switching agents ends the call.
- On a phone the orb has the whole stage and the canvas stays behind the strip's Canvas tab rather than beside the orb.
- HTML canvas blocks are static (no JavaScript execution). To keep what the call drew, use the canvas's own **PDF** and **Share** controls — see [Agent Canvas](../agents/agent-canvas.md#sharing-a-canvas-and-saving-it-as-a-pdf).

## See Also

- [Workspace](../sharing-and-access/workspace.md) — the chat the call lives in, and [dictation](../sharing-and-access/workspace.md#dictation) in its composer
- [Agent Canvas](../agents/agent-canvas.md) — the canvas the call draws on
- [Agent Chat](../agents/agent-chat.md) — the text-only Chat tab on Agent Detail
- [VoIP Telephony](voip-telephony.md) — the same voice engine on a phone line
- Backend API Docs: http://localhost:8000/docs
