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

Use Gemini's Live API (`VOICE_MODEL`, default **`models/gemini-3.1-flash-live-preview`** — the newest general-purpose Live model as of 2026-09; the `gemini-3.5-*-live` ids are transcribe-only and translate-only) as a real-time voice proxy for the agent. Claude Code remains the agent's brain (handles complex tasks via tool calls), but the voice conversation runs on Gemini's speech-to-speech model for speed (~280ms TTFT). During voice sessions, Gemini can invoke a `run_task` function declaration to delegate work to the underlying Claude agent.

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
- When Gemini calls `run_task`, backend dispatches `asyncio.create_task(_execute_and_respond())`. On a Workspace call the task is **backgrounded** (ent#551): the model is answered at once with a task id and the turn runs as the agent in the thread; on a thread-less call (VoIP) it is the synchronous container path (30s timeout)
- `tool_call` WS frame sent → orb shows amber badge; `tool_result` frame sent → orb returns to listening state; a `task` frame (`started|finished|failed`) drives the persistent "N tasks running" badge and refetches the canvas when a task lands
- The model is told how to speak around tools once, for the whole cycle (ent#576): announce a wait (not a drawing), say it once, never read the canvas aloud
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
| **Spoken etiquette for the whole tool cycle (ent#576, VOICE-011)** | ONE block, `spoken_etiquette_instruction(manifest, background=…)`, appended in `_build_live_config` for every session with a tool — Workspace call, Agent Detail, VoIP; both dispatch paths. Replaced the filler-only `_TOOL_ETIQUETTE_INSTRUCTION`, which said "never call `run_task` silently" and nothing about what to say *after* a tool returned — so the model, handed a tool result as a turn, re-presented what it had announced before the call and read the canvas aloud on top. Rules: **announce a wait, not an action** (a filler before `run_task`; none for a canvas write — the drawing appearing is the acknowledgement); **say it once** (after a result add what is new, never restate the announced intention, never read the canvas aloud — point and interpret); **one narration per sequence**; **report a failure once, with its reason**; **keep it short** (an acknowledgement is one short sentence, no "anything else while we wait?"; a report is one or two sentences); **never claim a canvas you did not draw** (canvas sessions only — the third live run had the model *say* "the canvas shows the breakdown" and "I've put the weather up there" with no canvas call and the row untouched; the completion notice also states the result is in the chat only and invites drawing it with `show_markdown` first, `_TASK_DONE_CANVAS_HINT`); **background tasks** (Workspace only). Built from the manifest so a session is never told about a tool it cannot call (ent#535 AC 6); `background` = `_is_workspace_bound` picks the async or the synchronous wording. `_RUN_TASK_TOOL`'s description carries a one-line summary that agrees; `WORKSPACE_PANEL_INSTRUCTIONS` states canvas = the artefact, voice = what it means. Pinned by `tests/unit/test_ent576_spoken_etiquette.py` (size-capped: the block rides every session). |
| Execution | `_execute_and_respond()` coroutine, `asyncio.create_task` per call. The bound depends on the path (below) |
| **Where the task runs (ent#535)** | **A Workspace call runs the turn AS THE AGENT, in the thread the call is bound to** — `_dispatch_task_in_chat` → `client_portal.service.portal_chat` → the resumable-turn engine → the thread's `cached_claude_session_id`. Same pipeline a typed message takes, so the agent has its own skills, files, memory and mid-work state, and the answer lands in that chat as a turn (and on the canvas if it drew). A call with **no** thread (VoIP, the legacy Agent Detail session) keeps the container path — there is nothing to run it in. The split is `_is_workspace_bound`: **both** `portal_session_id` and `client_email`, because a thread with no email cannot be attributed and an email with no thread has nowhere to land |
| **Background dispatch (ent#551, Workspace path)** | **Dispatch returns immediately.** `_dispatch_task_in_chat` starts the turn as its own task and answers the model at once with `_TASK_ACCEPTED` — a per-call task id (`t1`, `t2`, …), the label, and what else is running — so the model keeps the floor and the person can talk, interrupt, or start something else. No clock bounds the task (the ent#535 20 s spoken budget, which itself replaced the 30 s `wait_for` that cancelled the turn, is superseded here). **Told once, always — structurally**: on a Workspace call `run_task` is declared `Behavior.NON_BLOCKING` (`run_task_tool(background=True)`) and the accepted result is returned with `FunctionResponseScheduling.SILENT` (`_send_tool_response(..., silent=True)`, keyed on `_ACCEPTED_PREFIX`), so the acceptance enters the model's context without handing it a turn. Wording alone could not do this: a blocking call's result IS a turn and the model answered it ("I'm checking on that now — anything else while we wait?") right after the filler it had already said, every time. Verified live on `gemini-3.1-flash-live-preview`: one line before the call, nothing after the SILENT acceptance. Refusals (the cap, a blank prompt) and every container-path result are the answer and stay spoken; the container path keeps the BLOCKING declaration (`_RUN_TASK_TOOL`). Both are `getattr`-guarded so an SDK without the enums (the unit stub) degrades to the blocking shape. The acceptance text still says the model holds no result yet and asks for a line only if none was given, for that fallback. `_ack_watch` waits `_ACK_WINDOW_SECONDS` (4 s); if no assistant speech is heard since `_ACK_LOOKBACK_SECONDS` (3 s) before the dispatch — a filler just before the call is the etiquette, and nudging after it would be the ent#576 double-telling — it sends `_ACK_NUDGE` on the realtime channel so the model speaks. **Completion re-enters the call**: `_task_landed` (a done-callback) builds `_TASK_DONE_NOTICE` / `_TASK_FAILED_NOTICE` (task id, label, result clipped to `_TASK_RESULT_MAX`, or the reason) and `_deliver_task_notice` sends it via `send_realtime_input(text=…)` (the ent#534 cap-warning channel) at a **natural boundary**: `_model_speaking` false (set on `model_turn`, cleared on `turn_complete` / `interrupted`), **both** the person and the model's own speech quiet ≥ `_NOTICE_QUIET_SECONDS` (2.5 s — long enough to speak into; 1.2 s on the person alone put a fast result on the heels of the acknowledgement), no other tool call pending, a provider leg up (a reconnect holds it); never held past `_NOTICE_MAX_HOLD_SECONDS` (20 s). **Platform text is never the agent's line**: every notice opens with `_NOTICE_OPEN` ("never read this aloud"), and `_record_turn` runs `_scrub_platform_notice` on assistant text — a row that starts with `[System notice` / `[Platform notice` loses the bracketed notice (the model did read one verbatim once); only what it said after it is kept. **Row order**: the dispatcher flushes the turn in progress (`_flush_partial_turn`) before starting the task, so the spoken request and the filler are rows *before* the task's ask row rather than after it; the receive loop accumulates on the session's `_partial_*` fields, never in locals, for exactly this. **Bounded**: `MAX_BACKGROUND_TASKS_PER_CALL = 3`; at the cap `_TASK_REFUSED_AT_CAP` names the running tasks and the model voices it. **The call ending loses nothing**: background turns live in `_background_tasks` + `_detached_turns` (strong refs), NOT in `_pending_tool_tasks` which `end_session` cancels; a task that lands after the call writes its rows and says nothing. `_on_task_event` → the `task` frame (below) drives the surface |
| **Attribution (ent#551)** | `_portal_turn` passes `voice_call_id=session.session_id` to `portal_chat`, which stamps it on **both** rows (the prompt via `_persist_user_turn`, the reply) with `source` **NULL** — typed rows, not spoken. They render as ordinary turns outside the collapsed block, with an "asked during a voice call" caption on the ask (`voiceTaskCaption`), and the #2694 spoken-delta / history-context logic — keyed on `source='voice'` — is untouched. Two #2694 seams recognise the id: `_refuse_turn_during_voice_call(session_id, voice_call_id=…)` lets the call's own turn through (the guard, right for a typed turn from another tab, had refused every `run_task` since #2694 landed after ent#535 — "A voice call is on in this chat"), and `get_platform_rows_since_last_reply` does not take a reply carrying the id as its cursor, so a task landing mid-call cannot erase the call's earlier spoken rows from the next delta. Only the voice dispatcher writes the id; no request can |
| Agent call (container path only) | `agent_client.task(prompt)` (lazy import), 30s `wait_for`, prompt truncated to `_TOOL_PROMPT_MAX=2000` via the shared `_tool_prompt` |
| **Locked manifest (ent#535)** | `services/voice_tools.py` owns the policy. The manifest is resolved **once at session start** (`resolve_manifest`) and locked onto the session; `_build_live_config` builds the config **from it**, and `_execute_and_respond` refuses any name outside it *before reading an argument* — defence in depth, the Brain Orb `/action` shape. A per-agent declaration may only **narrow**: `template.yaml` is agent-writable, so a declaration that could ADD would let an agent grant itself a capability by editing itself. Intersection only |
| **Manifest is tri-state** | `None` = never resolved → the platform default (the pre-ent#535 surface, the safe answer). `frozenset()` = a decision → refuse everything. Reading an empty set as "unset" would hand the strongest narrowing the widest manifest — which the first cut of this did |
| **Manifest is PERSISTED, and the tri-state survives the round trip** | It rides the Redis session blob as `sorted(...)`/`null` (`json.dumps` cannot serialize a set, and writing it raw would raise inside the try and lose the whole blob). Without it the lock is silently undone by the cross-worker rebuild: production runs `--workers 2`, the WebSocket routinely lands on a worker other than the one `/voice/start` ran on, and `get_session` reconstructing from a blob with no manifest reads `None` as "never resolved" and returns the **full** platform default — in the config AND in the dispatcher, with no log line, because from that worker's view nothing was ever narrowed. `_manifest_from_meta` restores three inputs to two answers: absent or `null` → unresolved; a list, **including `[]`** → a decision; anything else → unresolved rather than a crash in the audio loop |
| **`include_owned` is the session's, not the turn's** | `VoiceSession.is_platform` (default **False**) is written by `start_workspace_voice`, the function that refuses a non-platform caller — so the gate that authorizes the wider roster read is the one that records it. `_portal_turn` reads it instead of re-asserting `True`, which would widen `agent_on_roster` for any future path that sets `portal_session_id` + `client_email` without passing that gate, with no diff at that line (Invariant #8). `canvas_audience` already travels for exactly this reason |
| **No fleet tools (ent#535)** | No `list_agents`, `chat_with_agent`, `fan_out`. Recorded as a decision, and enforced by `PLATFORM_VOICE_TOOLS` being the only door a name can enter through |
| Transcript | A container-path task appends a `system` entry (`[ran a task] …`) — the only record that path has. A Workspace task appends nothing: the turn **is** a row in the thread, which is a better record, and a note would read as a second turn beside the real one |
| Error handling | `AgentNotReachableError` → "not currently running"; `AgentRequestError` → "Task error: ..." |
| Empty prompt | `"No prompt provided."`, with **zero side effects**, on BOTH paths. The container path has always had it; the ent#535 chat path dropped it at first, and `portal_chat` calls `_persist_user_turn` unconditionally — so a blank `run_task` would have written an empty user row into the person's Workspace thread and dispatched a real, cost-tracked execution. `required=["prompt"]` makes that unlikely, not impossible: the argument is model-generated. Stripped, not merely falsy, and worded identically on both paths so the model cannot tell which one it reached |
| Session tracking | `_pending_tool_tasks` dict on `VoiceSession` (the tool-call coroutines; all cancelled on `end_session()`) and, separately, `_background_tasks` (ent#551 — the Workspace turns, which `end_session` deliberately leaves running) |
| WS events | `{type: "tool_call", tool: "run_task"}`, `{type: "tool_result", ...}`, and `{type: "task", state: "started|finished|failed", task_id, label, running}` (ent#551) frames |
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
{ "type": "task", "state": "started|finished|failed", "task_id": "t1", "label": "...", "running": 1 }
{ "type": "saved", "messages_saved": 12, "duration_seconds": 245.0, "reason": null, "message": null }
```

`reason`/`message` are present on the `ended` status only (ent#534). `saved` is
sent once, after the transcript is persisted and before the close — a client
that reloads its thread on `ended` races the write. Panel-tool `tool_result`
frames are how a canvas column learns the board changed; a `finished`/`failed`
`task` frame (ent#551) bumps the same `panelVersion`, since a background task may
have drawn. `task` frames drive the persistent work-in-flight badge, kept as a
list by id (`applyTaskFrame`) so the first of two to land never clears the other;
the badge and the header line say *what* is running (`backgroundTasksLabel`: one
task is its own one-liner, several are the count then the labels), not how many.
While the call is on, the conversation advances the thread's read cursor on
every spoken turn and task landing (`store.markChatRead`), so the sidebar's
unread badge does not count a conversation the person is in.

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
- ✅ 30s timeout with error recovery (container path only since ent#551 — a Workspace task is backgrounded, unbounded, and re-enters the call when it lands)
- ✅ Spoken etiquette for the whole tool cycle (ent#576)
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
