# Voice Chat — Gemini 2.5 Flash Native Audio

**Status**: ✅ Phase 1 + Tool Calling + Workspace Mode Complete · **the Workspace is the ONLY front door (#2559, 2026-09-07)**
**Date**: 2026-09-07 (#2559: the Agent Detail overlay retired, Talk becomes a door; ent#534: Workspace voice mode — session lifetime, cap notice, `saved` frame; earlier: 2026-05-30 #979 prod-CSP fix)
**Priority**: P1

> **One front door (#2559).** The **Workspace conversation** is the only surface that starts this session — the modal call with the orb and the canvas column, transcript into the Workspace thread; see [workspace-voice-conversation.md](workspace-voice-conversation.md). Agent Detail offers a **door into it**, not a second surface: a `Talk` button in `AgentHeader` that navigates to `/workspace?agent=<name>&voice=1`. The chat-panel overlay that used to run a parallel call — its own start route, its own transcript home in `chat_messages`, its own per-agent availability probe — is retired. The session, the tools, the canvas verbs and the WebSocket bridge below are the shared machinery both start routes drive.

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

### Front doors

Both diagrams that used to sit here described surfaces that no longer exist: the
Agent Detail chat-tab overlay (retired by #2559) and the standalone
`/agents/:name/workspace` page (retired by #2484, and still documented here for
three releases after it was gone). What starts a call today:

```
Agent Detail (AgentHeader)                     Workspace conversation
  [ Talk ]  ── router.push ──▶  /workspace?agent=<name>&voice=1
                                        │
                                        │  armed one-shot, consumed once
                                        ▼
                          PortalConversation.startVoiceCall()
                                        │
                                        │  POST /api/enterprise/client-portal/
                                        │       agents/{name}/voice/start
                                        │       (workspace_mode=True,
                                        │        canvas_audience="operator")
                                        ▼
                       VoiceOverlay (orb)  +  PortalVoiceCanvas (40/60)
                                        │
                                        │  useVoiceSession.startWith(…, {restStop:false})
                                        │  WebSocket  /ws/voice/{session_id}
                                        ▼
                              Trinity Backend (routers/voice.py)
                                        │
                                        ├─ panel tools in-process (_execute_panel_tool)
                                        │    show_markdown, show_diagram, show_image,
                                        │    update_panel, append_to_panel, clear_panel
                                        │    → session.panel_state (capped 512 KB)
                                        │
                                        └─ run_task → Agent Container (Claude Code)
```

The Workspace header's own **Call** control is the second door onto the same
function; the Talk button differs only in that it arrives from another page and
carries the intent with it. The OSS `POST /api/agents/{name}/voice/start` route
still exists and is unchanged — it simply has no first-party caller now.

### Canvas enrichment (#979 / VOICE-009)

The workspace canvas renders five live panel types plus history:

- **markdown** — `renderMarkdown` (marked + DOMPurify) in the parent DOM.
- **mermaid** (`show_diagram`) — rendered **in-parent** via the bundled `mermaid`
  ESM lib (no iframe). `mermaid.initialize({securityLevel:'strict', theme:'dark'})`
  disables interactivity + htmlLabels; `mermaid.render()` runs off-DOM and the
  output SVG is **DOMPurify-sanitized** before `v-html` (H-005). A monotonic seq
  token drops stale async renders during live update / history navigation; invalid
  syntax shows a contained error + source. The prior `srcdoc` iframe was dropped
  because the production CSP (`script-src 'self'`) blocks its inline render script
  and CORP blocks the bundle from the iframe's opaque origin (#979).
- **image** (`show_image`) — web URLs render directly via Vue `:src`;
  workspace file paths are fetched through the authenticated `/files/preview`
  endpoint as a blob (a bare `<img src>` would 401) and bound as an objectURL.
- **html** (`update_panel`) — **DOMPurify-sanitized and rendered in-parent**
  (same trust model as markdown, H-005). Scripts are stripped, so agent JS
  (e.g. Chart.js) does **not** execute — static layout only (#979).
- **empty** — placeholder.

Frontend-only additions (no backend change): a 40-snapshot history ring buffer
(prev/next + dropdown; "live" follows the latest, navigating back pins until a
new update arrives; image blobs revoked on eviction/unmount), orb smoothing
(asymmetric attack/release energy lerp, idle breathe floor, larger core/glow),
and a `prefers-reduced-motion`-aware cross-fade on canvas updates.

---

## User Flow

### Starting a call — through the Talk door

1. User is on Agent Detail, any tab (authenticated, platform principal).
2. User clicks **Talk** in the header, beside **Workspace**. It is always there:
   not gated on the platform voice flag, not disabled for a stopped agent. The
   destination reports availability, in words (ent#438's ruling).
3. `AgentHeader.goToTalk()` **arms** the one-shot (`armVoiceAutoStart()`) and
   `router.push`es `/workspace?agent=<name>&voice=1` — same tab, so the
   navigation is same-document and the Workspace's `AudioContext` stays
   resumable from the click that started it.
4. `Portal.bootstrap()` fetches the roster and threads, `resolveAgentQuery()`
   lands on the agent's most recent thread, and — only if the intent is armed,
   the agent landed, and the principal is a platform session — records
   `pendingVoiceStart`.
5. `bootstrap()`'s `finally` strips `voice` from the URL and disarms, once, on
   every exit path. Then, after a `nextTick`, the shell calls the conversation's
   exposed `startVoiceCall()` through a template ref.
6. From there it is the Workspace's own start (below): thread created if needed,
   `POST /api/enterprise/client-portal/agents/{name}/voice/start`, WebSocket
   bridge, orb up.

A pasted, bookmarked or mailed `/workspace?agent=X&voice=1` lands on a **fresh
document**, where the armed flag is false: the conversation opens, the URL is
cleaned, and no call starts. That includes the signed-out variant — `bootstrap()`
does not run while signed out, so the link would otherwise survive until exactly
the sign-in click that satisfies a browser activation heuristic, which is why the
rule is an in-app intent rather than `navigator.userActivation`.

### Starting a call — from inside the Workspace

The conversation header's **Call** control, driven by
`portalVoiceMode.js::voiceEntryState`: rendered for platform sessions only,
enabled when the roster's `realtime_voice.available` is true, and otherwise
disabled **with the reason** ("voice is turned off", "no voice provider key").
Same `startVoiceCall()` as the door.

### During the call

- User speaks; Gemini responds in real-time (~280ms TTFT)
- When Gemini calls `run_task`, backend dispatches `asyncio.create_task(_execute_and_respond())` (30s timeout)
- `tool_call` WS frame sent → orb shows amber badge; `tool_result` frame sent → orb returns to listening state
- All tool calls written to platform audit log
- Backend accumulates transcript from Gemini `serverContent` messages
- Panel verbs redraw the canvas column beside the orb

### Ending the call

1. User clicks **End**, clicks the orb, or presses Escape (or the cap fires).
2. Backend closes the Gemini session, cancels any pending `_pending_tool_tasks`.
3. The transcript is already in the Workspace thread — written **turn by turn**
   as `enterprise_portal_messages` rows (`source='voice'`, `voice_call_id`) —
   and the call closes with one `system` label row, "Voice call · N min".
4. The conversation reloads the thread; the call renders as one collapsed block.

The OSS save-at-end path (`routers/voice.py::_save_transcript` →
`chat_messages.source='voice'`) is unchanged but no longer reached: the retired
overlay was the only caller that supplied an Agent Detail chat session id.
**Historic rows still render their badge** — `ChatBubble.vue` is their reader and
is deliberately untouched.

### Session lifetime — the session outlives the provider connection (ent#534)

Gemini Live ends an uncompressed audio-only session at ~15 min and recycles the
connection at ~10 min, announcing it with `go_away`. The 300 s Agent Detail cap
hid both; the Workspace's 30-minute cap does not. Every session therefore asks
for `context_window_compression` (sliding window) and `session_resumption`
(`_build_live_config`, types read through `getattr` so a stubbed/older SDK still
connects); `_receive_audio_loop` stores each `session_resumption_update.new_handle`
and returns on `go_away`; `connect_and_stream` reconnects with the latest handle
(≤ `MAX_RECONNECTS_PER_SESSION` = 8) while the watchdog, the browser socket and the
transcript stay session-scoped. **Cap with words**: `_timeout_watchdog` asks the
model out loud to wrap up at T-`CAP_WARNING_LEAD_SECONDS` (30 s) via
`send_realtime_input(text=…)`, then sets `end_reason="cap"` BEFORE `end_session`
(which cancels the watchdog's own task). `end_reason ∈ {None, cap, error,
provider_closed}` + `end_message` ride the `status: ended` frame; the bridge
sends a final `saved` frame after the transcript is persisted and before the
close, which is what a client reloads on. Per-session caps: `VOICE_MAX_DURATION`
(Agent Detail, 300), `WORKSPACE_VOICE_MAX_DURATION` (Workspace, 1800),
`VOIP_MAX_CALL_DURATION` (phone, 600).

### Availability — one fact, reported by the destination

`GET /api/settings/feature-flags` returns
`voice_available: VOICE_ENABLED && bool(GEMINI_API_KEY)`, and the Workspace
roster's `realtime_voice {available, reason}`
(`client_portal/voice.py::realtime_voice_capability`) computes the **same two
facts** for a platform principal, as does `gemini_voice.is_available()`. The
retired per-agent `GET /api/agents/{name}/voice/status` was a third spelling of
it.

Because the door's gate and the destination's gate are the same boolean, #2559
**gates nothing on the door**: Talk renders unconditionally, and the Workspace
disables its Call control *with the reason*. Gating the door would have bought
nothing and cost two defects — a cold-load pop-in (the flag arrives after first
paint, so the button appears a beat late) and a sticky-false hide (a failed
flags fetch sets it to `false` and no retry clears it, hiding a working feature).
`sessions.js::voiceAvailable` is still parsed off that payload but has no reader
in `src/` after #2559; it is kept with a comment and registered on the same
follow-up as the caller-less backend routes.

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

**Status**: ✅ Implemented · the Agent Detail surface is a **door** since #2559

| Requirement | Detail |
|-------------|--------|
| Trigger (Workspace) | **Call** control in the conversation header — `voiceEntryState`: platform sessions only, disabled *with the reason* when the instance cannot |
| Trigger (Agent Detail) | **Talk** button in `AgentHeader.vue`, beside Workspace. A door, not a surface: `armVoiceAutoStart()` + `router.push('/workspace?agent=<name>&voice=1')`. **Ungated** — no `v-if`, no stopped-agent disable |
| Voice overlay | `VoiceOverlay.vue` — full canvas orb, pure JS (no CDN). **One consumer**: `PortalConversation.vue` |
| Particle system | Value noise + curl noise, 220 smoke particles in 3 layers, 9 pre-rendered sprite canvases |
| State hues | idle/connecting: 0°, listening: +90° (green), speaking: +210° (indigo), tool_calling: amber badge |
| Controls | Mute mic toggle, End call button (and the orb itself, and Escape) |
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

- `create_session()` (now `async`) writes JSON metadata to Redis with TTL = `session.max_duration + 60` (browser voice: 360s; phone: `VOIP_MAX_CALL_DURATION + 60` = 660s — the TTL tracks the session's own cap so a 10-min phone session's metadata doesn't expire mid-call). If Redis write fails, in-memory state is rolled back and `RuntimeError` is raised — the client gets 500 at `/voice/start` rather than a session ID that will intermittently 403.
- `get_session()` (now `async`) checks `_sessions` first; on cache miss, falls back to Redis, reconstructs a `VoiceSession` from the stored metadata, and registers it in the worker's `_sessions` for subsequent calls. Redis errors degrade gracefully to `None`.
- `remove_session()` (now `async`) deletes the Redis key and pops from `_sessions`.
- `_redis` client is lazy-initialized as `redis.asyncio` (async, non-blocking) reusing the existing platform Redis URL (`config.REDIS_URL`).

**Key implementation:** `src/backend/services/gemini_voice.py` — `_get_redis()`, per-session Redis TTL (`session.max_duration + 60`), async `create_session`/`get_session`/`remove_session`.

---

### VOICE-007: Tool Calling (run_task)

**Status**: ✅ Implemented (Phase 3 — shipped early)

| Requirement | Detail |
|-------------|--------|
| Function declaration | `_RUN_TASK_TOOL` (`FunctionDeclaration` for `run_task`) registered in `LiveConnectConfig` |
| Spoken-filler etiquette | `run_task` is a **blocking** Gemini function call — the model emits no audio from the moment it decides to call until `send_tool_response` returns (up to ~30s), which on a phone call reads as dead air. `_TOOL_ETIQUETTE_INSTRUCTION` is appended to every session's `system_instruction` (in `connect_and_stream`, so it covers browser **and** phone) and the `run_task` description is sharpened, instructing the model to say a brief filler ("let me check that for you") before calling. Prompt-side fix; no SDK change. A future upgrade to non-blocking async function calling (`Behavior.NON_BLOCKING` + `FunctionResponseScheduling`) would remove the dead air entirely; this became available once `google-genai` was bumped `1.12.1 → 1.63.0` for the Brain Orb voice tile (trinity-enterprise#60), but the etiquette prompt-fix stays until the non-blocking path is wired. |
| Execution | `_execute_and_respond()` coroutine, `asyncio.create_task` per call, 30s `wait_for` timeout |
| Agent call | `agent_client.task(prompt)` (lazy import), truncated to `_TOOL_PROMPT_MAX=2000` chars |
| Error handling | `AgentNotReachableError` → "not currently running"; `AgentRequestError` → "Task error: ..." |
| Empty prompt | Falls back to "No prompt" |
| Session tracking | `_pending_tool_tasks` dict on `VoiceSession`; all cancelled on `end_session()` |
| WS events | `{type: "tool_call", tool_name: "run_task"}` and `{type: "tool_result", ...}` frames |
| Audit | Platform audit log written on each tool call via `on_tool_call` callback; `actor_user=types.SimpleNamespace(id=..., email=...)` pattern (#705 — legacy `actor_type=`/`actor_id=`/`actor_email=` kwargs caused silent TypeError) |

### VOICE-008: Workspace Mode + Canvas Panel (BETA)

**Status**: ⛔ **Retired (#2484)** — the page and its route are gone, and
`AgentWorkspace.vue` with them. Kept here for the panel-tool contract it
introduced, which VOICE-009 enriched and VOICE-010 inherited: the canvas column
beside the Workspace orb speaks exactly these verbs. The rows below describe the
retired page; where they name an entry point or a route, read VOICE-010.

| Requirement | Detail |
|-------------|--------|
| Entry point | *(retired)* "Workspace" button in `AgentHeader.vue`; today that button opens THE Workspace (ent#438) and its neighbour **Talk** opens it with the call starting (#2559) |
| Route | *(retired)* `/agents/:name/workspace` → `AgentWorkspace.vue` |
| Layout | Left 40% (orb + controls) + Right 60% (canvas panel) |
| `workspace_mode` flag | Passed in `POST /voice/start` body; appends `WORKSPACE_PANEL_INSTRUCTIONS` to system prompt |
| Panel tools | `show_markdown`, `show_diagram`, `show_image`, `update_panel`, `append_to_panel`, `clear_panel` — handled in-process via `_execute_panel_tool()`, never forwarded to agent container |
| Panel state | `VoiceSession.panel_state` dict (in-memory); type ∈ {empty, markdown, mermaid, image, html}; content capped at `_PANEL_CONTENT_MAX=524288` (512 KB) |
| Panel endpoint | `GET /api/agents/{name}/voice/{session_id}/panel` — returns empty state for missing sessions (no 404 during teardown window); ownership-gated (user_id + agent_name check, admin bypass) |
| Frontend poll | `setInterval(fetchPanel, 300)` — in-flight guard (`panelFetching` flag) prevents overlapping requests; skips state update when `updated_at` unchanged (prevents 3×/sec Vue re-renders and preserves content after session ends) |
| Content preservation | Panel content preserved on session end (poll stops, state not reset); reset on new session **start** via `resetPanelState()` |
| HTML rendering | `update_panel`/`append_to_panel` → `v-html="sanitizedHtml"`, where `sanitizedHtml = DOMPurify.sanitize(content)` (default profile) renders **in-parent**. `<script>` tags are stripped — agent JS does **not** execute. Replaces the prior `srcdoc` iframe, which the production CSP (`script-src 'self'`) + CORP blocked entirely (#979) |
| Mermaid rendering | `show_diagram` → `mermaid.render()` (`securityLevel:'strict'`, htmlLabels off) off-DOM → `DOMPurify.sanitize(svg)` → `v-html`. Monotonic seq token drops stale async renders during live-update / history nav |
| XSS protection | All three `v-html` sites (`show_markdown`, `show_diagram` SVG, `update_panel` HTML) are DOMPurify-sanitized in the parent DOM — same trust model as platform markdown (H-005). DOMPurify strips `<script>`, event handlers, and `javascript:` hrefs. The opaque-origin iframe boundary from #981 is dropped (it was non-functional under the prod CSP, so it protected nothing in production); residual risk is a DOMPurify bypass, identical to every other markdown surface on the platform |
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
{ "type": "status", "state": "connecting|listening|speaking|ended", "reason": "cap|error|provider_closed|null", "message": "..." }
{ "type": "tool_call", "tool": "run_task|show_markdown|...", "args": {} }
{ "type": "tool_result", "tool": "run_task", "result_preview": "..." }
{ "type": "saved", "messages_saved": 12, "duration_seconds": 245.0, "reason": null, "message": null }
```

`reason`/`message` are present on the `ended` status only (ent#534). `saved` is
sent once, after the transcript is persisted and before the close — a client
that reloads its thread on `ended` races the write. Panel-tool `tool_result`
frames are how a canvas column learns the board changed.

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
  "type": "empty|markdown|mermaid|image|html",
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
| `VOICE_ENABLED` | Global voice toggle (default `true`; effective only when `GEMINI_API_KEY` is set). Wired into backend compose `environment:` (#979) |
| `WORKSPACE_ENABLED` | Workspace canvas toggle — opt-in BETA, default `false` (#860). `workspace_available = voice_available && WORKSPACE_ENABLED`. Wired into backend compose `environment:` (#979 — previously never passed through, so the canvas couldn't be enabled via `.env`) |
| `VOICE_MODEL` | Model ID (default: `models/gemini-3.1-flash-live-preview`, set in `src/backend/config.py`). #1076: leave unset/commented — a set-but-empty value is coalesced to the default by `os.getenv("VOICE_MODEL") or …`. |
| `VOICE_MAX_DURATION` | Max **Agent Detail** voice session duration in seconds (default: 300 / 5 min). Phone calls use `VOIP_MAX_CALL_DURATION` instead (default 600 / 10 min) — both flow through the same per-session `_timeout_watchdog`, which sleeps on `session.max_duration` rather than a global. See [voip-telephony.md](voip-telephony.md). |
| `WORKSPACE_VOICE_MAX_DURATION` | Max **Workspace** voice call duration in seconds (default: 1800 / 30 min, ent#534); wired through both compose files and `.env.example`. Spoken wrap-up at T-30 s, written reason at the cap. |

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
| **Backend** | `src/backend/routers/voice.py` | Voice endpoints + WebSocket handler (JWT decoded before the session lookup), `/panel` endpoint, `on_tool_call`/`on_tool_result`/`on_turn` callbacks, the `saved` frame, the idempotent OSS `_save_transcript` (caller-less since #2559) |
| **Backend** | `src/backend/services/voice_prompt_service.py` | `get_voice_system_prompt` — the 3-level resolver, lifted out of the router (ent#534) so `client_portal` can share it |
| **Backend** | `src/backend/client_portal/voice.py` + `client_portal/router.py` | The Workspace front door: `POST /api/enterprise/client-portal/agents/{name}/voice/start`, turn-by-turn persistence into the thread (ent#534) |
| **Backend** | `src/backend/services/gemini_voice.py` | `VoiceSession` (+ `workspace_mode`, `panel_state`), `_RUN_TASK_TOOL`, `_PANEL_TOOLS`, `_execute_panel_tool()`, `WORKSPACE_PANEL_INSTRUCTIONS` |
| **Backend** | `src/backend/routers/settings.py` | `voice_available` feature flag in `GET /api/settings/feature-flags` |
| **Frontend** | `src/frontend/src/components/portal/PortalConversation.vue` | The **only** consumer of the orb: `startVoiceCall()` / `endVoiceCall()`, the modal call, `defineExpose({ startVoiceCall })` for the Talk door |
| **Frontend** | `src/frontend/src/components/portal/portalVoiceMode.js` | Every voice RULE as a pure function: `voiceEntryState`, `voicePreflight`, `voiceHeaderLine`, `groupVoiceBlocks` — plus the `?voice=1` armed one-shot (`armVoiceAutoStart` / `voiceAutoStart`, #2559) |
| **Frontend** | `src/frontend/src/views/Portal.vue` | Consumes the intent once in `bootstrap()`, strips the key in the `finally`, hands off via a template ref |
| **Frontend** | `src/frontend/src/components/chat/VoiceOverlay.vue` | The orb (unchanged). Mounted only by `PortalConversation.vue` since #2559 |
| **Frontend** | `src/frontend/src/components/AgentHeader.vue` | `goToWorkspace()` and — since #2559 — `goToTalk()`: the ungated **Talk** door that arms the one-shot and pushes `?voice=1` |
| **Frontend** | `src/frontend/src/composables/useVoiceSession.js` | `startWith(requestFn, {restStop})` / `stop()`. The Agent-Detail-shaped `start(sessionId, voiceName, workspaceMode)` was removed with its only caller (#2559) |
| **Frontend** | `src/frontend/src/stores/sessions.js` | `voiceAvailable` from feature flags — **no reader in `src/` since #2559**; kept pending the follow-up that retires the caller-less voice surface |
| **Frontend** | `src/frontend/src/utils/audio.js` | AudioWorklet-first capture/playback, `getAmplitude()` via `AnalyserNode` |
| **Tests** | `tests/unit/test_voice_tools.py` | 58 unit tests: tool execution (`run_task` reads `.response_text`, guards the #979 regression), panel tool handlers, `show_diagram`/`show_image` registration, image-src classification, content cap, routing guard, Redis session fallback (#704) |
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
- ✅ `update_panel` HTML + `show_diagram` Mermaid render **in-parent** via DOMPurify (H-005); scripts stripped, no JS execution (#979 — replaced the #981 `srcdoc` iframe that the production CSP blocked)
- ⏳ Export panel content as PDF/markdown
- ⏳ Multi-page / tabbed canvas
- ⛔ The page itself was **retired by #2484** — one Workspace, `/workspace?agent=`

### Phase 5: One front door ✅ Complete (2026-09-07, ent#534 + #2559)

- ✅ The call lives inside the Workspace conversation, modal, with the canvas column (ent#534)
- ✅ The transcript is written turn by turn into the Workspace thread, not saved at end
- ✅ The Agent Detail chat-panel overlay is retired; `AgentHeader` offers a **Talk** door instead (#2559)
- ✅ `?voice=1` is an in-app armed one-shot, stripped once on every exit — a pasted link never starts a call
- ⏳ Retire the now caller-less OSS `/voice/start|stop|status` routes, after a release

---

## Persisted per-agent voice (trinity-enterprise#28)

The Gemini voice was historically hardcoded to `"Kore"` (`routers/voice.py::_get_voice_name`).
It is now a persisted per-agent setting, `agent_ownership.voice_name` (default `Kore`,
an edition-agnostic OSS primitive like `voice_system_prompt`):

- `GET/PUT /api/agents/{name}/voice/name` — GET returns the persisted voice plus
  `available_voices`/`default_voice`; PUT is owner-only and validates against the
  canonical `GEMINI_VOICE_NAMES` (`config.py`), shared with the frontend picker
  list (`src/constants/voices.js`) and drift-guarded by a unit test.
- **Resolution at session start** (`voice_start`): per-session request override
  (`VoiceStartRequest.voice_name`) → persisted `voice_name` → `Kore`. The read
  path (`db.get_voice_name`) falls back to `Kore` for an unset or no-longer-valid
  persisted value.
- The retired page's ephemeral picker (`AgentWorkspace.vue`, deleted with the page
  in #2484) defaulted its selection to the persisted voice; the picker list is the
  shared `src/constants/voices.js` module, still the source for the per-agent
  Settings picker. The persisted voice also drives outbound VoIP calls — see
  `voip-telephony.md`.

## Related Documentation

- [Authenticated Chat Tab](./authenticated-chat-tab.md) — Existing chat implementation
- [Persistent Chat Tracking](./persistent-chat-tracking.md) — Message storage
- [Gemini Runtime](./gemini-runtime.md) — Existing Gemini integration
- [Workspace voice conversation](./workspace-voice-conversation.md) — the modal call, and the `?voice=1` entry contract the Talk door uses
- [Gemini Live API Docs](https://ai.google.dev/gemini-api/docs/live-api) — Official API reference
