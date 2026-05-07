# Voice Chat — Gemini 2.5 Flash Native Audio

**Status**: ✅ Phase 1 + Tool Calling + Workspace Mode (BETA) Complete
**Date**: 2026-05-07
**Priority**: P1

---

## Problem Statement

Users need a fast, natural way to speak with agents via the browser. Text chat creates friction — typing is slow, reading long responses takes time, and the interaction feels robotic. A voice interface should feel like talking to a person: sub-500ms response time, natural turn-taking, interruption support.

---

## Core Concept

Use **Gemini 2.5 Flash Native Audio** (`gemini-live-2.5-flash-native-audio`) as a real-time voice proxy for the agent. Claude Code remains the agent's brain (handles complex tasks via tool calls), but the voice conversation runs on Gemini's speech-to-speech model for speed (~280ms TTFT). During voice sessions, Gemini can invoke a `run_task` function declaration to delegate work to the underlying Claude agent.

### Why Not Claude for Voice?

Anthropic has no speech-to-speech or realtime audio API. Any Claude voice pipeline would require STT → Claude text API (~500-800ms TTFT) → TTS, totaling 800ms-1.3s. Gemini's native audio model handles audio in/out natively with ~280ms latency and built-in turn-taking, barge-in, and emotion.

---

## Architecture

### Standard Mode (Chat Tab overlay)

```
┌────────────────────────────────────────────────────────────┐
│                     Browser (Agent Detail)                  │
│                                                            │
│  ┌──────────────┐    ┌──────────────────────────────────┐  │
│  │  Chat Panel  │    │  VoiceOverlay.vue (canvas orb)   │  │
│  │  (existing)  │    │  Canvas orb — value noise + curl │  │
│  │  ... msgs    │    │  State hues + tool_calling badge  │  │
│  │              │    │  [Mute] [End Call]                │  │
│  └──────────────┘    └──────────────┬───────────────────┘  │
└─────────────────────────────────────┼───────────────────────┘
                                      │ useVoiceSession.js (workspace_mode=false)
                                      │ WebSocket
                                      ▼ Trinity Backend
```

### Workspace Mode (Separate page `/agents/:name/workspace`, BETA)

```
┌────────────────────────────────────────────────────────────────────┐
│                    /agents/:name/workspace                          │
│                                                                    │
│  ┌─────────────────────────┐  ┌───────────────────────────────┐   │
│  │  Left Panel (40%)       │  │  Right Panel (60%)            │   │
│  │  bg-black               │  │  bg-gray-900 (canvas)         │   │
│  │                         │  │                               │   │
│  │  <canvas> orb (same     │  │  Panel content rendered via   │   │
│  │   particle system as    │  │  show_markdown → renderMarkdown│  │
│  │   VoiceOverlay)         │  │  update_panel → DOMPurify +   │   │
│  │                         │  │  _execScripts (Chart.js ok)   │   │
│  │  Status label           │  │  Chart.js 4 pre-loaded        │   │
│  │  Tool name badge        │  │                               │   │
│  │  [Mute] [Start/End]     │  │  Polled 300ms; updated_at     │   │
│  │  Voice selector         │  │  gate + in-flight guard       │   │
│  └─────────────────────────┘  └───────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
         │
         │ useVoiceSession.js (workspace_mode=true)
         │ WebSocket (same as standard mode)
         ▼
   Trinity Backend (routers/voice.py)
         │
         ├─ Panel tools handled in-process (_execute_panel_tool)
         │   show_markdown, update_panel, append_to_panel, clear_panel
         │   → session.panel_state (in-memory, capped at 512 KB)
         │
         └─ run_task → Agent Container (Claude Code)
```

---

## User Flow

### Starting a Voice Session (Standard Mode)

1. User is on Agent Detail page, Chat tab (authenticated)
2. User clicks **"Talk"** button (microphone icon) next to the chat input
3. `POST /api/agents/{name}/voice/start` with optional `voice_name`
4. Backend prepares the voice session:
   a. 3-level system prompt fallback: DB field → container file `voice-agent-system-prompt.md` → auto-generate from template info → generic
   b. Fetches recent chat history for context injection
5. Backend opens connection to Gemini Live API with `tools=[_RUN_TASK_TOOL]`
6. WebSocket bridge established: browser ↔ backend ↔ Gemini
7. Canvas orb overlay appears; state transitions drive hue rotation

### During the Voice Session

- User speaks; Gemini responds in real-time (~280ms TTFT)
- When Gemini calls `run_task`, backend dispatches `asyncio.create_task(_execute_and_respond())` (30s timeout)
- `tool_call` WS frame sent → orb shows amber badge; `tool_result` frame sent → orb returns to listening state
- All tool calls written to platform audit log
- Backend accumulates transcript from Gemini `serverContent` messages

### Ending a Voice Session

1. User clicks **"End"** button or closes overlay
2. Backend closes Gemini session, cancels any pending `_pending_tool_tasks`
3. Transcript saved as `ChatMessage` rows in existing `chat_messages` table
4. Chat panel refreshes showing voice conversation inline with text messages

### Starting a Voice Session (Workspace Mode)

1. User is on Agent Detail page (agent must be running)
2. **Workspace button** in AgentHeader (shown only when `voice_available=true` from feature flags)
3. Router navigates to `/agents/:name/workspace` — `AgentWorkspace.vue`
4. Same `POST /api/agents/{name}/voice/start` with `workspace_mode: true`
5. Backend appends `WORKSPACE_PANEL_INSTRUCTIONS` to the system prompt (describes 4 panel tools)
6. WebSocket established; panel poll starts at 300ms interval
7. Agent may call panel tools during conversation — panel content updated in-memory
8. Frontend polls `GET /api/agents/{name}/voice/{session_id}/panel` and re-renders canvas

### Feature Flag: voice_available

`GET /api/settings/feature-flags` returns `voice_available: VOICE_ENABLED && bool(GEMINI_API_KEY)`.
Stored in `sessions.js` Pinia store as `voiceAvailable`. Passed as prop to `AgentHeader.vue`.
Button hidden entirely when `voiceAvailable=false`.

---

## Requirements

### VOICE-001: Voice Session Initialization

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Backend endpoint | `POST /api/agents/{name}/voice/start` → returns `voice_session_id` + WebSocket URL |
| System prompt source | 3-level fallback: DB → `voice-agent-system-prompt.md` container file → auto-generate → generic |
| Context injection | Summarize last N messages of current chat session |
| Gemini connection | `google-genai` SDK, `generativelanguage.googleapis.com` Live API |
| Authentication | `GEMINI_API_KEY` platform setting |
| Audio format | PCM 16-bit, 16kHz mono |
| Voice name | Passed via `voice_name` field in `VoiceStartRequest` |

### VOICE-002: Audio Streaming Bridge

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Browser → Backend | WebSocket carrying raw audio frames from `getUserMedia()` |
| Backend → Gemini | Forward audio frames to Gemini Live API |
| Gemini → Backend | Receive audio response frames + transcript text |
| Backend → Browser | Forward audio frames for playback |
| Playback engine | AudioWorklet-first (blob URL inlined) with ScriptProcessor fallback |
| Amplitude | `createAudioPlayer()` exposes `getAmplitude()` (0–1 float) via `AnalyserNode` |

### VOICE-003: Transcript Persistence

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Transcript extraction | Captured from Gemini `serverContent` messages during session |
| Storage | Saved as `ChatMessage` rows in `chat_messages` table |
| Session linkage | Belongs to user's current `ChatSession` |
| Timing | Saved on session end |

### VOICE-004: Frontend Voice UI

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Trigger | Microphone icon button next to chat input textarea |
| Voice overlay | `VoiceOverlay.vue` — full canvas orb, pure JS (no CDN) |
| Particle system | Value noise + curl noise, 220 smoke particles in 3 layers, 9 pre-rendered sprite canvases |
| State hues | idle/connecting: 0°, listening: +90° (green), speaking: +210° (indigo), tool_calling: amber badge |
| Controls | Mute mic toggle, End call button |
| Amplitude polling | `setInterval(30ms)` via `amplitude` ref in composable |
| Audio capture | `navigator.mediaDevices.getUserMedia({ audio: true })` |
| Audio playback | AudioWorklet with ScriptProcessor fallback |

### VOICE-005: Voice System Prompt

**Status**: ✅ Implemented

| Requirement | Detail |
|-------------|--------|
| Lookup order | 1) DB field, 2) `voice-agent-system-prompt.md` in container, 3) auto-generate from template info, 4) generic fallback |
| Implementation | `_get_voice_system_prompt()` in `routers/voice.py` |

### VOICE-006: Conversation Summary for Context

**Status**: ✅ Implemented (truncation approach)

| Requirement | Detail |
|-------------|--------|
| Trigger | On voice session start |
| Method | Truncation of last N messages injected into system prompt |
| Fallback | Voice system prompt alone if no prior messages |

### VOICE-009: Multi-Worker Session Reliability (#704)

**Status**: ✅ Implemented (2026-05-07)

Uvicorn runs `--workers 2` in production. HTTP requests (REST `/voice/start`, `/voice/stop`) and WebSocket connections round-robin across OS processes. Without a cross-worker session store, the WebSocket worker would have an empty `_sessions` dict and reject with 403.

**Fix: Redis dual-write pattern**

| Layer | Store | Purpose |
|-------|-------|---------|
| In-memory `_sessions` dict | Live state | Gemini connection, asyncio tasks, panel_state (unserializable) |
| Redis key `voice_session:{id}` | Serializable metadata | Cross-worker auth lookup (agent_name, user_id, user_email, …) |

- `create_session()` (now `async`) writes JSON metadata to Redis with TTL = `VOICE_MAX_DURATION + 60` (360s). If Redis write fails, in-memory state is rolled back and `RuntimeError` is raised — the client gets 500 at `/voice/start` rather than a session ID that will intermittently 403.
- `get_session()` (now `async`) checks `_sessions` first; on cache miss, falls back to Redis, reconstructs a `VoiceSession` from the stored metadata, and registers it in the worker's `_sessions` for subsequent calls. Redis errors degrade gracefully to `None`.
- `remove_session()` (now `async`) deletes the Redis key and pops from `_sessions`.
- `_redis` client is lazy-initialized as `redis.asyncio` (async, non-blocking) reusing the existing platform Redis URL (`config.REDIS_URL`).

**Key implementation:** `src/backend/services/gemini_voice.py` — `_get_redis()`, `_REDIS_SESSION_TTL`, async `create_session`/`get_session`/`remove_session`.

---

### VOICE-007: Tool Calling (run_task)

**Status**: ✅ Implemented (Phase 3 — shipped early)

| Requirement | Detail |
|-------------|--------|
| Function declaration | `_RUN_TASK_TOOL` (`FunctionDeclaration` for `run_task`) registered in `LiveConnectConfig` |
| Execution | `_execute_and_respond()` coroutine, `asyncio.create_task` per call, 30s `wait_for` timeout |
| Agent call | `agent_client.task(prompt)` (lazy import), truncated to `_TOOL_PROMPT_MAX=2000` chars |
| Error handling | `AgentNotReachableError` → "not currently running"; `AgentRequestError` → "Task error: ..." |
| Empty prompt | Falls back to "No prompt" |
| Session tracking | `_pending_tool_tasks` dict on `VoiceSession`; all cancelled on `end_session()` |
| WS events | `{type: "tool_call", tool_name: "run_task"}` and `{type: "tool_result", ...}` frames |
| Audit | Platform audit log written on each tool call via `on_tool_call` callback; `actor_user=types.SimpleNamespace(id=..., email=...)` pattern (#705 — legacy `actor_type=`/`actor_id=`/`actor_email=` kwargs caused silent TypeError) |

### VOICE-008: Workspace Mode + Canvas Panel (BETA)

**Status**: ✅ Implemented (2026-05-07, issue #699)

| Requirement | Detail |
|-------------|--------|
| Entry point | "Workspace" button in `AgentHeader.vue` (hidden when `voice_available=false`) |
| Route | `/agents/:name/workspace` → `AgentWorkspace.vue` |
| Layout | Left 40% (orb + controls) + Right 60% (canvas panel) |
| `workspace_mode` flag | Passed in `POST /voice/start` body; appends `WORKSPACE_PANEL_INSTRUCTIONS` to system prompt |
| Panel tools | `show_markdown`, `update_panel`, `append_to_panel`, `clear_panel` — handled in-process via `_execute_panel_tool()`, never forwarded to agent container |
| Panel state | `VoiceSession.panel_state` dict (in-memory); type ∈ {empty, markdown, html}; content capped at `_PANEL_CONTENT_MAX=524288` (512 KB) |
| Panel endpoint | `GET /api/agents/{name}/voice/{session_id}/panel` — returns empty state for missing sessions (no 404 during teardown window); ownership-gated (user_id + agent_name check, admin bypass) |
| Frontend poll | `setInterval(fetchPanel, 300)` — in-flight guard (`panelFetching` flag) prevents overlapping requests; skips state update when `updated_at` unchanged (prevents 3×/sec Vue re-renders and preserves content after session ends) |
| Content preservation | Panel content preserved on session end (poll stops, state not reset); reset on new session **start** via `resetPanelState()` |
| HTML rendering | `update_panel`/`append_to_panel` → `ref="htmlPanelEl"` + `renderHtmlPanel()`: `DOMPurify.sanitize(html, {ADD_TAGS:['script']})` then `_execScripts()` re-clones `<script>` nodes as live DOM elements so Chart.js `new Chart(...)` calls execute |
| Chart.js | Chart.js 4.4.0 pre-loaded globally via `injectChartJs()` on mount (CDN `<script>` tag injected once into `document.head`, guarded by `#chartjs-cdn` id check) |
| XSS protection | `show_markdown` → `renderMarkdown()` (DOMPurify-wrapped); `update_panel` HTML — DOMPurify strips event handlers and `javascript:` hrefs; `<script>` tags are re-executed (Chart.js init) — trade-off: accepted per issue #707 security review, same trust level as agent MCP calls |
| BETA indicator | Amber "BETA" badge in header button and page header |

---

## WebSocket Message Types

**Client → Server:**
```json
{ "type": "audio", "data": "<base64 PCM audio>" }
{ "type": "end" }
```

**Server → Client:**
```json
{ "type": "audio", "data": "<base64 PCM audio>" }
{ "type": "transcript", "role": "user|assistant", "text": "..." }
{ "type": "status", "state": "listening|speaking|processing" }
{ "type": "tool_call", "tool_name": "run_task|show_markdown|..." }
{ "type": "tool_result", "tool_name": "run_task", "result": "..." }
```

Panel tool calls (`show_markdown`, `update_panel`, etc.) appear as `tool_call` WS frames but do NOT send `tool_result` frames to the browser — they're resolved in-process on the backend and Gemini is notified internally. The frontend polls the panel state separately via REST.

---

## API Design

### POST /api/agents/{name}/voice/start

**Request:**
```json
{
  "session_id": "optional - existing chat session to continue",
  "voice_name": "optional - Gemini voice name",
  "workspace_mode": false
}
```

**Response:**
```json
{
  "voice_session_id": "vs_abc123",
  "websocket_url": "/ws/voice/vs_abc123",
  "chat_session_id": "cs_xyz789"
}
```

### POST /api/agents/{name}/voice/stop

**Response:**
```json
{
  "transcript": [...],
  "messages_saved": 12,
  "duration_seconds": 45,
  "cost": 0.003
}
```

### GET /api/agents/{name}/voice/{session_id}/panel

Returns current canvas panel state. Returns empty state (not 404) for non-existent sessions to avoid poll errors during teardown. Auth: `get_authorized_agent` dep + `session.user_id == current_user.id` check (admin bypass).

**Response:**
```json
{
  "type": "empty|markdown|html",
  "content": "...",
  "title": "optional title or null",
  "updated_at": "ISO-Z timestamp or null"
}
```

---

## Configuration

### Platform-Level

| Setting | Description |
|---------|-------------|
| `GEMINI_API_KEY` | API key for Gemini Live API |
| `VOICE_ENABLED` | Global toggle |
| `VOICE_MODEL` | Model ID (default: `gemini-2.5-flash-native-audio-preview-12-2025`) |
| `VOICE_MAX_DURATION` | Max session duration in seconds (default: 300) |

### Per-Agent

| Setting | Description |
|---------|-------------|
| `voice_system_prompt` | Agent-specific voice personality prompt (DB field) |
| `voice_enabled` | Per-agent toggle |
| `voice_name` | Gemini voice selection (passed at session start) |

---

## Key Implementation Files

| Layer | File | Purpose |
|-------|------|---------|
| **Backend** | `src/backend/routers/voice.py` | Voice endpoints + WebSocket handler, `/panel` endpoint, `_get_voice_system_prompt()`, `on_tool_call`/`on_tool_result` callbacks |
| **Backend** | `src/backend/services/gemini_voice.py` | `VoiceSession` (+ `workspace_mode`, `panel_state`), `_RUN_TASK_TOOL`, `_PANEL_TOOLS`, `_execute_panel_tool()`, `WORKSPACE_PANEL_INSTRUCTIONS` |
| **Backend** | `src/backend/routers/settings.py` | `voice_available` feature flag in `GET /api/settings/feature-flags` |
| **Frontend** | `src/frontend/src/views/AgentWorkspace.vue` | Full workspace page (orb + canvas panel, particle system inlined, panel polling) |
| **Frontend** | `src/frontend/src/components/chat/VoiceOverlay.vue` | Standard mode orb overlay (unchanged) |
| **Frontend** | `src/frontend/src/components/AgentHeader.vue` | Workspace button (`goToWorkspace()`, shown when `voiceAvailable=true`) |
| **Frontend** | `src/frontend/src/composables/useVoiceSession.js` | `start(sessionId, voiceName, workspaceMode)` — passes `workspace_mode` to backend |
| **Frontend** | `src/frontend/src/stores/sessions.js` | `voiceAvailable` state from feature flags |
| **Frontend** | `src/frontend/src/utils/audio.js` | AudioWorklet-first capture/playback, `getAmplitude()` via `AnalyserNode` |
| **Tests** | `tests/unit/test_voice_tools.py` | 26 unit tests: tool execution, panel tool handlers, content cap, routing guard, Redis session fallback (#704) |
| **Tests** | `tests/unit/test_voice_auth.py` | 19 unit tests: WS auth, stop auth, panel ownership, audit attribution kwargs (#705) |

---

## Scope & Phasing

### Phase 1: MVP ✅ Complete

- Authenticated chat only (not public links)
- Single agent at a time
- Voice overlay with canvas orb visualization
- Transcript saved on session end
- 3-level voice system prompt fallback
- Gemini API key in platform settings
- `voice_name` selection at session start

### Phase 2: Polish (Partial)

- ✅ Real-time amplitude visualization (canvas orb driven by `getAmplitude()`)
- ⏳ Incremental transcript display in chat during session
- ⏳ Voice quality/latency metrics
- ⏳ Public link voice support

### Phase 3: Tool Calling ✅ Complete (shipped with Phase 1)

- ✅ `run_task` function calling (Gemini delegates to Claude agent mid-session)
- ✅ Amber badge UI state during tool execution
- ✅ 30s timeout with error recovery
- ⏳ Multi-language voice with auto-detection
- ⏳ Voice cloning / custom voice per agent

### Phase 4: Workspace Mode ✅ Complete (2026-05-07, issue #699/#707, BETA)

- ✅ Separate full-page workspace at `/agents/:name/workspace`
- ✅ Split layout: orb (left) + canvas panel (right)
- ✅ 4 panel tools: `show_markdown`, `update_panel`, `append_to_panel`, `clear_panel`
- ✅ `voice_available` feature flag gates workspace button in AgentHeader
- ✅ Panel ownership gate on REST endpoint
- ✅ 512 KB content cap on accumulated `append_to_panel` content
- ✅ Panel flicker fixed: `updated_at` change-detection gate + in-flight fetch guard (#707)
- ✅ Panel content preserved on session end; reset on new session start (#707)
- ✅ Chart.js 4 pre-loaded; `update_panel` HTML with `<script>` tags executes correctly (#707)
- ⏳ Export panel content as PDF/markdown
- ⏳ Multi-page / tabbed canvas

---

## Related Documentation

- [Authenticated Chat Tab](./authenticated-chat-tab.md) — Existing chat implementation
- [Persistent Chat Tracking](./persistent-chat-tracking.md) — Message storage
- [Gemini Runtime](./gemini-runtime.md) — Existing Gemini integration
- [Gemini Live API Docs](https://ai.google.dev/gemini-api/docs/live-api) — Official API reference
