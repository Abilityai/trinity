# Requirements — Agent Runtimes — Multi-Runtime, Codex, Voice, VoIP

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 14. Multi-Runtime Support

### 14.1 Runtime Adapter Architecture
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Abstract interface for agent execution engines
- **Key Features**: ClaudeCodeRuntime, GeminiRuntime, factory function

### 14.2 Gemini CLI Integration
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Google's Gemini CLI as alternative runtime
- **Key Features**: Free tier, 1M token context, native Google Search

### 14.3 Runtime Configuration
- **Status**: ✅ Implemented (2025-12-28)
- **Description**: Runtime selection via `template.yaml` `runtime:` field
- **Schema**: `runtime: {type: claude-code|gemini-cli, model: string}`

---

## 29. Voice Chat (VOICE-001)

### 29.1 Voice Session Initialization (VOICE-001)
- **Status**: ✅ Implemented
- **Description**: Real-time voice conversations with agents via Gemini 2.5 Flash Native Audio
- **Key Features**: `POST /api/agents/{name}/voice/start` loads voice prompt + summarizes prior chat, opens Gemini Live API WebSocket connection
- **Architecture**: Browser (mic) → WebSocket → Backend (proxy) → Gemini Live API → Backend → WebSocket → Browser (speaker)
- **Scope**: this is the **OSS start route**. The Workspace — the only front door since #2559 — starts through `client_portal/voice.py` (VOICE-010) under the portal principal. The bridge, the frames and `/ws/voice/{id}` are shared; only the start request differs. The OSS route is retained and unchanged, and has no first-party frontend caller after #2559.

### 29.2 Audio Streaming Bridge (VOICE-002)
- **Status**: ✅ Implemented
- **Description**: WebSocket proxy: browser ↔ backend ↔ Gemini, <100ms added latency
- **Key Features**: PCM 16kHz mono input, 24kHz mono output, base64 frame encoding

### 29.3 Transcript Persistence (VOICE-003)
- **Status**: ✅ Implemented · **no first-party caller since #2559**
- **Description**: Voice transcripts saved as ChatMessage rows with `source="voice"`, inline in existing chat sessions
- **Key Features**: Automatic transcript extraction from Gemini, `source` column on chat_messages table
- **Scope (#2559)**: the write path (`routers/voice.py::_save_transcript`) is **retained and unchanged**, but the only caller that supplied an Agent Detail chat session id was the retired chat-panel overlay, so no new `chat_messages` rows land here. **Historic rows keep rendering** — the `source === 'voice'` badge in `ChatBubble.vue` is their reader. The Workspace transcript is a different table written **per turn** (`enterprise_portal_messages.source` + `voice_call_id`, VOICE-010), not a save-at-end.

### 29.4 Frontend Voice UI (VOICE-004)
- **Status**: ✅ Implemented · **the Agent Detail surface is a door since #2559**
- **Description**: The voice orb (`components/chat/VoiceOverlay.vue`, driven by `composables/useVoiceSession.js`) has exactly **one** consumer: the Workspace conversation (VOICE-010). Agent Detail offers a **door**, not a surface — a **Talk** button in `AgentHeader` that navigates to `/workspace?agent=<name>&voice=1`, where the call starts.
- **Key Features**: VoiceOverlay component, pulsing status indicators, live transcript display, mute toggle — all in the Workspace
- **Visibility (#2559)**: Talk renders **unconditionally**. It is not gated on the platform voice flag, on the agent's running state, or on a per-agent probe. Availability is the destination's to report: the Workspace says in words why a call cannot start (`portalVoiceMode.js::voiceEntryState`), which is ent#438's ruling — *a dead button is a worse answer than a page that says why* — applied consistently. The per-agent `GET /api/agents/{name}/voice/status` probe the panel used is no longer called.
- **Entry contract (#2559)**: `?voice=1` is a **one-shot intent armed in the app**, never by the URL alone — see VOICE-010 and `public-access.md` §48.3 FR-1.

### 29.5 Voice System Prompt (VOICE-005)
- **Status**: ✅ Implemented
- **Description**: Per-agent `voice_system_prompt` field for voice personality
- **Key Features**: Stored on agent_ownership table, fallback to auto-generated prompt from agent name

### 29.6 Context Summary (VOICE-006)
- **Status**: ✅ Implemented
- **Description**: On voice start, summarize prior messages and inject into Gemini system prompt
- **Key Features**: Last 20 messages truncated to ~750 tokens, injected as conversation context
- **Scope**: this is the **OSS start route**'s summary (`_build_context_summary`), with the same caller-less status as VOICE-001 since #2559. The Workspace builds its own context from the portal thread (`_format_history_context`, VOICE-010).

### 29.7 Tool Calls + Canvas Orb (VOICE-007)
- **Status**: ✅ Implemented (#581)
- **Description**: Gemini voice sessions can invoke Trinity's `run_task` tool to dispatch agent tasks mid-conversation; frontend canvas orb replaces the static overlay
- **Key Features**: `run_task` declared to Gemini Live; non-blocking `asyncio.create_task` dispatch; prompt-injection mitigation (`_TOOL_PROMPT_MAX = 2000` chars); `_pending_tool_tasks` dict with cancellation on session end; canvas orb in `VoiceOverlay.vue` with `isToolCalling` state (no CDN dependencies); platform audit log on every tool call
- **Architecture — TWO paths since ent#535, chosen by `_is_workspace_bound`** (both `portal_session_id` AND `client_email` set):
  - **Workspace call** → `_dispatch_task_in_chat` → `client_portal.service.portal_chat` → the resumable-turn engine → the thread's `cached_claude_session_id`. The turn runs **as the agent, in the thread the call is bound to**, so it has that agent's skills, files, memory and mid-work state, and the answer lands in the chat (and on the canvas if it drew). **Since ent#551 the dispatch is asynchronous** (below): the model is answered at once and the turn runs in the background. (ent#535's 20 s spoken budget, which itself replaced the 30 s `wait_for` that cancelled the turn, is superseded on this path.)
  - **No thread** (VoIP, the legacy Agent Detail session) → `_execute_tool` → `agent_client.task(prompt)` under a 30s `wait_for`. Unchanged, and still the right path where there is nothing to run a turn in — and the **documented synchronous fallback**: spoken filler before the call, a stated 30 s budget, never silence.
- **Background tasks (ent#551, Workspace path)** — a long task runs while the conversation continues:
  - **Dispatch returns immediately.** `run_task` answers the model at once with an *accepted* result carrying a per-call task id (`t1`, `t2`, …) and the other tasks still running; the model keeps the floor and the person can speak, interrupt, or start something else. No tool timeout bounds the task.
  - **The user is told, always.** The etiquette block (VOICE-011) asks for a spoken line naming what was started; if no assistant speech is heard within `_ACK_WINDOW_SECONDS` (4 s) of the dispatch (allowing a filler spoken just before the call), the platform sends a text nudge on the realtime channel (`_ACK_NUDGE`) so the model speaks — the acknowledgement is structural, not a hope.
  - **Completion re-enters the call.** When the turn lands, `_TASK_DONE_NOTICE` (or `_TASK_FAILED_NOTICE` with the reason) is injected as a system notice via `send_realtime_input(text=…)` — the ent#534 cap-warning channel — at a **natural boundary**: the model is not mid-turn, the person has been quiet ≥ `_NOTICE_QUIET_SECONDS` (1.2 s), no other tool call is pending; held at most `_NOTICE_MAX_HOLD_SECONDS` (20 s). The notice names the task and its request so the model can say which ask it answers; the result rides in clipped to `_TASK_RESULT_MAX` chars (the full reply is in the chat).
  - **Bounded concurrency.** `MAX_BACKGROUND_TASKS_PER_CALL = 3`; at the cap the tool answers with a refusal naming the running tasks, which the model voices — never an invisible queue. Each task keeps its id so two results are never conflated; "is that done yet?" is answerable from the accepted results and notices already in the model's context — no new tool.
  - **The call ending loses nothing.** Background turns are strongly referenced (`_detached_turns`) and are **not** in `_pending_tool_tasks`, so `end_session` never cancels them; a task that lands after the call writes its rows into the thread as an ordinary turn. Both rows (the prompt and the reply) carry `voice_call_id` = the call's session id (`source` stays NULL — typed, not spoken), which is the attribution: they render as ordinary turns with an "asked during a voice call" caption, outside the collapsed spoken block, and the `#2694` delta logic (keyed on `source='voice'`) is untouched.
  - **The surface knows.** A `task` WebSocket frame (`{state: started|finished|failed, task_id, label, running}`) drives a persistent "N tasks running" badge on the orb, distinct from the amber per-call working badge, and a finished task bumps `panelVersion` so the canvas column refetches in the same moment.

### 29.11 Spoken Etiquette for the Tool Cycle (VOICE-011, trinity-enterprise#576)
- **Status**: ✅ Implemented (2026-09-12)
- **Description**: The voice model is told, once, how to speak around tools for the **whole** cycle — announce, execute, report — so the person is told a thing once and never has the canvas read to them. Replaces the filler-only `_TOOL_ETIQUETTE_INSTRUCTION`.
- **One block, built from the manifest**: `spoken_etiquette_instruction(manifest, background=…)` in `services/gemini_voice.py` is appended in `_build_live_config` for every session with any tool (Workspace call, Agent Detail, VoIP — one place, both front doors, both dispatch paths). It mentions `run_task` only when the manifest has it and the canvas only when a panel tool is in the manifest (ent#535 AC 6 preserved); `background` (the session is workspace-bound) selects the async wording, the synchronous filler wording otherwise. An empty manifest yields no block.
- **Rules**: *announce a wait, not an action* (a filler before `run_task`; none for a canvas write — the drawing appearing is the acknowledgement); *say it once* (after a result, add what is new; never restate the announced intention; never read the canvas aloud — point and interpret); *one narration per sequence* (one announcement, one report; speak in between only when expectations change, once); *report a failure once, with its reason* (a refusal is reported, never dressed as success); *background tasks* (the id, the cap, the natural-pause report). `_RUN_TASK_TOOL`'s description carries a one-line summary that agrees with the block; `WORKSPACE_PANEL_INSTRUCTIONS` states the division of labour (canvas = the artefact, voice = what it means) beside the "don't mirror every voice response" rule.
- **Pinned**: `tests/unit/test_ent576_spoken_etiquette.py` asserts the block is in the session's `system_instruction` for workspace and non-workspace sessions alike, that the run_task description and the block agree, and caps the block's size (the instruction rides every session).
- **The tool surface is a locked manifest (ent#535)**: `services/voice_tools.py` resolves it ONCE at session start from the platform default narrowed by the agent's own `template.yaml` `voice: tools:`; `_build_live_config` builds from it and the dispatcher refuses any name outside it before reading an argument. A per-agent declaration may only **narrow** — the file is agent-writable, so one that could ADD would let an agent grant itself a capability by editing itself. Tri-state: `None` = never resolved → platform default; `frozenset()` = a decision → refuse everything. It is **persisted in the Redis session blob**, because production runs `--workers 2` and a rebuild that reads it as unresolved silently hands back the full default (the ent#535 review finding).
- **`include_owned` travels on the session** (`VoiceSession.is_platform`, default False), written by `start_workspace_voice`, which is the gate that refuses a non-platform caller. Re-asserting it as a constant at the turn site would widen `agent_on_roster` for any future path that binds a call without going through that gate.

### 29.8 Voice Workspace (VOICE-008)
- **Status**: ⛔ **Retired (#2484)** — the page and its route are gone; recorded here for the panel-tool contract it introduced, which VOICE-009/VOICE-010 inherited. `AgentHeader`'s button now opens THE Workspace (ent#438), and since #2559 a second button beside it opens it **with the call starting**.
- **Description**: Full-page workspace at `/agents/:name/workspace` with split layout — orb + controls left, agent-controlled canvas panel right; gated behind `voice_available` feature flag
- **Key Features**: 6 in-process panel tools (`show_markdown`, `show_diagram`, `show_image`, `update_panel`, `append_to_panel`, `clear_panel`); 300ms poll via `GET /voice/{session_id}/panel`; DOMPurify sanitization; 512 KB content cap; `workspace_mode` param on `voice/start`; BETA-badged button in AgentHeader

### 29.9 Voice Workspace Canvas Enrichment (VOICE-009)
- **Status**: ✅ Implemented (#979)
- **Description**: Enriches the VOICE-008 canvas with Mermaid diagrams, image display, client-side panel history, and orb/transition polish — endpoint contract unchanged
- **Key Features**:
  - `show_diagram(diagram, title?)` → `mermaid` panel type rendered strictly inside the existing opaque-origin `sandbox="allow-scripts"` iframe via a self-contained `mermaid.min.js` IIFE bundle (no runtime chunk fetches); agent diagram text injected as a JS string (`JSON.stringify` + `<`→`<`, no `</script>` breakout) with `securityLevel:'strict'`; invalid syntax renders a contained error + source, not a broken panel
  - `show_image(src, title?, caption?)` → `image` panel type; web URLs render directly via Vue `:src`, workspace file paths fetched through the authenticated `/files/preview` endpoint as a blob (a bare `<img src>` would 401). Path confinement enforced in-process by `_classify_image_src` (rejects `..`, absolute escapes, the `/home/developer-evil` sibling, `data:`, and non-http schemes — stricter than the agent-server prefix check)
  - Client-side panel history: ring buffer of the last 40 snapshots with prev/next + dropdown selector; "live" follows the latest, navigating back pins a snapshot until a new update arrives; frontend-only, no backend change; image blob objectURLs revoked on eviction + unmount
  - Orb polish: asymmetric attack/release smoothing on energy (0.18/0.10), smoothed core size, idle "breathe" floor, larger core (58) and glow swing (32×)
  - Graceful canvas-update cross-fade + header "updated" flash, honoring `prefers-reduced-motion`
- **Security**: agent markup/diagrams render only inside the opaque-origin sandboxed iframe; images render via `:src` (no `v-html`); panel tools are backend+frontend only (not on the MCP surface — Invariant #13 N/A)

### Phase Roadmap
1. **Phase 1 (MVP)**: Authenticated chat only, basic overlay, transcript on session end, manual voice prompt ✅
2. **Phase 2 (Polish)**: Real-time waveform, incremental transcript, auto-generate voice prompt from CLAUDE.md ✅
3. **Phase 3 (Advanced)**: Tool calling (run_task), canvas orb ✅ — multi-language auto-detection, custom voice per agent (deferred)
4. **Phase 4 (Workspace)**: Full-page workspace with canvas panel tools, feature-flag gated (BETA) ✅ — page retired by #2484
5. **Phase 5 (One front door)**: the call lives in the Workspace conversation (VOICE-010, ent#534) ✅ — and since #2559 that is the **only** front door: Agent Detail offers a Talk door into it, not a second surface

---

### 29.10 Workspace Voice Mode (VOICE-010, trinity-enterprise#534)
- **Status**: ✅ Implemented (2026-09-07)
- **Description**: The real-time voice session started from — and written back into — a **Workspace thread**, as a modal call with the orb. Start: `POST /api/enterprise/client-portal/agents/{name}/voice/start` under the portal principal (platform users only; off-roster, a foreign thread and a portal token are one uniform 404; per-(user, agent) rate limit; 503 with a reason when voice is off). The session carries `portal_session_id` + `client_email`, `workspace_mode=True`, `canvas_audience="operator"`, `max_duration=WORKSPACE_VOICE_MAX_DURATION` (default **1800 s**, env, wired through compose). `/stop`, `/ws/voice/{id}` and `/panel` are the shared OSS routes.
- **Transcript**: written **turn by turn** by the worker holding the live socket (`client_portal/voice.py::persist_voice_turn`) as `enterprise_portal_messages` rows with `source='voice'` + `voice_call_id`; the call closes with one `system` row ("Voice call · N min", plus how it ended); a call with no turns writes nothing; `/stop` never writes for this surface; the OSS save-at-end path (VOICE-003, caller-less since #2559) is idempotent (in-process flag + Redis SETNX claim). Context for the call = the thread's recent turns (system rows skipped, earlier spoken rows labelled); `_format_history_context` labels spoken rows `(voice)` and budgets them under one total 24k-char budget across calls, trimmed oldest-first with every cut named (#2694 — replaced the 12-per-call counter; a whole 30-minute call fits); a resumed turn is prefixed with the spoken rows since the agent's last typed reply (`_format_voice_delta`).
- **Session lifetime**: every session requests context-window compression + session resumption; a `go_away` reconnects with the latest handle (≤ 8 per call) while the browser socket, watchdog and transcript span the call; the cap sends a spoken wrap-up notice at T-30 s (`send_realtime_input(text=…)`) and ends with `end_reason="cap"`; `status{ended, reason, message}` and a final `saved` frame tell the client why and when to reload.
- **Capability**: `PortalRoster.realtime_voice {available, reason}` — platform principals only, fail-closed, provider-neutral name (ent#354); distinct from the per-agent TTS `voice_available`.
- **Canvas**: the call's right column reads `/panel` (refetch on panel `tool_result` frames + 3 s poll) through `CanvasPanel`; a platform principal reads every canvas audience in the Workspace (`agent_page.canvas_audience_for`), a portal-token client stays `roster`.
- **Front doors (#2559)**: the Call control in the Workspace conversation header, and the **Talk** door on Agent Detail (`AgentHeader` → `/workspace?agent=<name>&voice=1`). `?voice=1` is a **one-shot intent armed in the app**: `armVoiceAutoStart()` sets a module-scoped flag in `portalVoiceMode.js` before the push, and `voiceAutoStart()` honours the param only when it is armed **and** the agent landed **and** the principal is a platform user. A pasted, bookmarked or mailed link always arrives on a fresh document where the flag is false, so it never auto-starts a billed call — including the signed-out variant, where the sign-in click would otherwise satisfy any browser activation heuristic. `bootstrap()` strips the key on **every** exit, keyed on its presence.
- **Retired**: the ent#440 hands-free loop (`voiceConversation.js`) — one voice entry point; and, by #2559, the Agent Detail chat-panel overlay. See `public-access.md` §48.3.

## 39. VoIP Telephony (VOIP-001)

### 39.1 Outbound Phone Calls over Gemini Live (#1056 — Phase 1)
- **Status**: 🚧 In Progress
- **Implements**: Issue #1056 (Phase 1 — outbound)
- **Description**: An agent places an outbound phone call to a user and
  holds a real-time, interruptible voice conversation powered by the
  existing Gemini Live voice bridge. A phone call is just a **different
  transport feeding the same Gemini queues** — `services/gemini_voice.py`
  is **not modified**. Ships as a regular OSS feature (no entitlement
  gating), gated behind a feature flag that is **OFF by default**.
- **Feature flag (default OFF)**: `voip_available` is exposed by
  `GET /api/settings/feature-flags` as
  `VOIP_ENABLED and bool(GEMINI_API_KEY)`. `VOIP_ENABLED` defaults to
  `false` (mirrors `workspace_available` opt-in, #860). All VoIP
  endpoints 404 when the flag is off. The feature is additionally
  per-agent-gated: it only functions once a `voip_bindings` row exists.
- **Transport**: Twilio Programmable Voice + bidirectional Media Streams
  (`<Connect><Stream>`), delivering raw G.711 μ-law 8kHz audio over a
  WebSocket directly into the existing Gemini queues. Explicitly **not**
  Twilio ConversationRelay (does its own STT/TTS for text LLMs) and
  **not** Pipecat/LiveKit (would re-implement the owned bridge).
- **Per-agent Twilio-voice credentials**: dedicated `voip_bindings`
  table (`account_sid`, AES-256-GCM-encrypted `auth_token`, `from_number`,
  `webhook_secret`, `enabled`, `daily_call_cap`), **separate from
  `whatsapp_bindings`** (voice and messaging are different Twilio
  products). Each agent owner brings their own Twilio account — outbound
  PSTN spend is on the owner's account, not the platform operator's.
- **Audio conversion** (stdlib `audioop`, all codec work in the adapter):
  inbound μ-law 8kHz → PCM16 16kHz (`ulaw2lin` → `ratecv(8k→16k)`);
  outbound PCM16 24kHz → μ-law 8kHz (`ratecv(24k→8k)` direct decimation
  → `lin2ulaw`), re-chunked to 160-byte/20ms frames, base64 for Twilio
  JSON. `audioop.ratecv` **state is carried per-direction per-connection**
  (no per-chunk reset → no boundary clicks). `audioop-lts` is pinned for
  Python ≥ 3.13 (stdlib `audioop` removed in 3.13).
- **Interruption**: relies on Gemini Live's native barge-in. The adapter
  flushes Twilio's buffer via a **`clear`** event + drops its local
  accumulator when the user speaks while the agent is mid-utterance, so
  buffered audio does not play over the caller.
- **Outbound trigger**:
  - `POST /api/agents/{name}/voip/call` (JWT/MCP, `AuthorizedAgent`) and
    the MCP tool `call_user`. Body: `{to_number, context?,
    process_transcript?}`.
  - The trigger creates a `chat_session` (owner identity), mints a
    single-use WSS ticket, stages session intent (agent, user, system
    prompt, chat_session_id, `process_transcript`) in Redis keyed by a
    high-entropy `call_id`, then calls Twilio
    `calls.create(to, from_, twiml="<Connect><Stream url='wss://…/api/voip/voice/{call_id}?ticket=…'/></Connect>")`.
  - **Abuse controls** (Phase 1): rate-limited per `(owner, destination)`
    via `services/rate_limiter.py`; a durable per-agent **daily call cap**
    (`voip_call_logs` count); optional `Idempotency-Key` (Invariant #18).
    A formal opt-in destination allowlist is deferred to Phase 2.
- **Cross-worker safety**: the trigger does **not** call
  `connect_and_stream`; it only stages intent in Redis. The WS handler —
  running on whichever worker Twilio's Media Streams socket actually hits —
  reads the staged intent, calls `voice_service.create_session(...)` then
  `connect_and_stream(...)`, so the live Gemini connection lives on the
  correct worker.
- **Two-id namespace**: `call_id` (chosen at trigger time; in the WSS URL
  + Redis intent key + ticket binding) is distinct from the Gemini
  `VoiceSession.session_id` (`vs_…`, minted at WS-connect inside the
  unmodified `create_session`). They are never conflated.
- **WSS auth**: the Media Streams socket (Twilio cannot send a JWT) is
  gated by a single-use, call-bound ticket via
  `services/ws_ticket_service.py`. `mint_ticket` gains an optional
  `ttl_seconds` param (VoIP mints at 180s to cover PSTN dial+ring, vs the
  30s browser default) and binds the ticket with `scope="voip:{call_id}"`,
  verified in the handler. Staged intent is consumed exactly once via
  Redis `GETDEL`.
- **Transcript persistence**: the call transcript is persisted to
  `chat_messages` with `source="voice"` via the existing `_save_transcript`
  path, guarded by a Redis SETNX sentinel so a double-teardown saves
  exactly once.
- **Post-call processing (default ON)**: after teardown, the full
  transcript is dispatched as a single turn to the **main agent** through
  `task_execution_service.execute_task(triggered_by="voip")` so the agent
  (with its real skills/memory/MCP) digests the call and takes follow-up
  actions. Per-call opt-out via `process_transcript=false`; skipped when
  the transcript is empty (no-answer / instant hangup); once-guarded.
- **Config**: `VOIP_ENABLED` (default `false`),
  `VOIP_MAX_CALL_DURATION` (default 600s — VoIP-specific, not the
  inherited 300s `VOICE_MAX_DURATION`), `VOIP_DEFAULT_DAILY_CALL_CAP`
  (default 50), `VOIP_TICKET_TTL_SECONDS` (default 180),
  `VOIP_CALL_RATE_LIMIT` / `VOIP_CALL_RATE_WINDOW`.
- **Three surfaces in sync (Invariant #13)**: backend router
  (`routers/voip.py`) + MCP tool (`src/mcp-server/src/tools/voip.ts`).
  No agent-server mirror (the bridge is backend-only).
- **Out of scope (Phase 1)**: inbound calls ("you call the agent" —
  Phase 2: Twilio voice webhook + `X-Twilio-Signature` + inbound
  number→agent resolution on the same table; an `inbound_number` column
  is shipped up-front so Phase 2 is additive); opt-in destination
  allowlist (Phase 2); schedule-action trigger and call cost/duration
  observability (Phase 3). Real PSTN path is manual-verify (needs a live
  Twilio voice number). (UI config surface delivered separately in §39.2.)

### 39.2 Per-agent VoIP config UI + persisted voice (trinity-enterprise#28)
- **Status**: 🚧 In Progress
- **Implements**: Issue trinity-enterprise#28 (the Phase-3 "UI config surface"
  deferred by #1056)
- **Description**: A per-agent VoIP config panel in the agent Settings/Sharing
  tab (`components/VoipChannelPanel.vue`) to create/update/remove the
  `voip_bindings` row over the existing OSS `GET/PUT/DELETE /api/agents/{name}/voip`
  endpoints (Twilio Account SID, write-only Auth Token, From number, optional
  daily call cap), an **enable/disable toggle**, and a **persisted voice picker**.
- **OSS, not entitlement-gated**: shipped as a plain OSS capability gated purely
  on the existing `voip_available` platform flag (the frontend reads it via
  `stores/sessions.js`). No enterprise entitlement, no `register_module`, no
  `trinity-enterprise` submodule change — a deliberate simplification over the
  issue's original "entitlement-gated" framing (a UI gate over an OSS,
  money-spending backend would be cosmetic; gate is the platform flag instead).
- **Enable/disable toggle**: `PUT /api/agents/{name}/voip/enabled`
  (`{enabled: bool}`, owner-only, 404 when no binding) flips `voip_bindings.enabled`
  without re-entering credentials; the call path already refuses disabled
  bindings. Re-saving credentials via the binding PUT **preserves** the current
  `enabled` state (the upsert no longer forces `enabled=1`).
- **Persisted per-agent voice**: new edition-agnostic OSS primitive
  `agent_ownership.voice_name` (default `Kore`, like `voice_system_prompt`) with
  `GET/PUT /api/agents/{name}/voice/name` (PUT owner-only, validated against the
  canonical `GEMINI_VOICE_NAMES`). Replaces the two hardcoded `"Kore"` sites
  (`routers/voice.py::_get_voice_name`, `services/voip_service.py`), so the chosen
  voice applies to **both** the in-app voice overlay/workspace and outbound VoIP
  calls. Resolution precedence at a voice start: per-session request override →
  persisted `voice_name` → `Kore`; the read path falls back to `Kore` for an
  unset or no-longer-valid persisted value. The Workspace ephemeral picker
  defaults to the persisted voice. Dual-track migration (SQLite `db/migrations.py`
  + Alembic `0004_agent_ownership_voice_name` + `db/schema.py`/`db/tables.py`).

---

## 40. Multi-Runtime Harnesses — OpenAI Codex (#1187)

### 40.1 Codex CLI Execution Engine (#1187 — MVP)

Trinity agents may run on the **OpenAI Codex CLI** as a third execution runtime
("harness == runtime") alongside Claude Code and Gemini. A template selects it
via `runtime: { type: codex, model: gpt-5.6-sol }`; the container is created
with `AGENT_RUNTIME=codex` and `codex_runtime.py` implements the `AgentRuntime`
ABC. Follow-up to spike #854.

**Functional requirements:**
- **FR-1 — Execution:** `/api/chat` and `/api/task` run via `codex exec --json`;
  the `-o/--output-last-message` file is the authoritative response (read-then-delete);
  JSONL `agent_message` is the fallback. Tokens/cost from `turn.completed.usage`
  (estimated cost — Codex has no native cost; `reasoning_output_tokens` is a subset
  of `output_tokens`, never double-counted).
- **FR-2 — Chat continuity:** `codex exec resume <thread_id>` continues the Chat-tab
  conversation. The Session tab's cached-UUID `--resume` model is NOT supported in
  the MVP (gated off for codex; chat continuity lives in the Chat tab).
- **FR-3 — Safety parity (blocking):** the platform system prompt reaches Codex
  (prepended per-turn; `CLAUDE.md`→`AGENTS.md` mirrored at startup for identity);
  read-only mode maps to `--sandbox read-only`; guardrails are honored where they
  map to Codex's control surface and surfaced (logged) where they don't; Codex
  output + logs pass through the credential sanitizer.
- **FR-4 — Sandbox + network:** normal (writable) agents run `--sandbox danger-full-access`,
  which DISABLES Codex's own bubblewrap sandbox — `workspace-write`/`read-only` both invoke
  `bwrap` to create a user namespace, which the hardened Trinity container forbids
  (`bwrap: No permissions to create a new namespace`), blocking every shell tool. The Trinity
  container is already the boundary (`cap_drop ALL` + AppArmor + `no-new-privileges`), the same
  posture Claude/Gemini run under, so dropping the redundant inner sandbox weakens nothing.
  Read-only agents keep `--sandbox read-only` (sandbox-native write protection) as the interim
  enforcement — a fail-closed read-only enforcement story for Codex is a fast-follow.
- **FR-5 — Credentials:** `OPENAI_API_KEY` from the agent's `.env` (CRED-002),
  loaded into the subprocess env **and materialised into `$CODEX_HOME/auth.json`**
  before the first spawn (#2208 — the CLI authenticates its
  `wss://api.openai.com/v1/responses` transport from that file, not from the
  environment, and the transport is no longer toggleable, so the env var alone
  yields `401` on every turn). Written via `codex login --with-api-key` with the
  key on stdin, never argv. A subscription `auth.json` (#1971) wins and is never
  overwritten, nor is one that cannot be parsed; a stored key that no longer
  matches `.env` is refreshed, since #1999 rebuilds the execution env per spawn.
  Codex agents are NOT assigned a Claude subscription.
- **FR-6 — MCP:** Trinity HTTP MCP + template MCP servers wired via `$CODEX_HOME/config.toml`;
  the bearer token is referenced by env var, never persisted as a literal.
- **FR-7 — Capabilities:** each runtime declares `RuntimeCapabilities`
  (`chat_continuity`, `session_tab_resume`, `mcp_support`, `cost_reporting`);
  `get_runtime()` validates `AGENT_RUNTIME` and fails loudly on unknown values.

**Non-functional:** concurrency-safe orphan cleanup (must not kill sibling
executions); `CODEX_HOME` relocated off the git-tracked workspace; error→HTTP
mapping keeps non-auth failures at 500 (never 503) so the dispatch breaker's
AUTH-only counting and the SUB-003 auth switch stay inert for Codex.

**Out of scope (fast-follow):** shared subprocess-helper DRY extraction; Session-tab
cached-UUID resume for Codex; backend reading `ExecutionMetadata.error_code`
directly; Codex SSE streaming; vision/images; a post-creation runtime-switch
endpoint. See the [Harness Authoring Guide](harness-authoring-guide.md) for adding
a fourth runtime.

---

## 41. Model-Conditional Prompt Tiers (ent#243)

### 41.1 Tier Resolution — the axis is the MODEL, not the runtime

Anthropic's Claude 5-generation context-engineering guidance (rules → judgment,
examples → interface design) applies to *frontier coding-post-trained models*,
and is **actively harmful** on smaller/older ones — Sonnet 4.5 and Haiku 4.5 need
the explicit structure it removes. Three vendors reached the same conclusion
independently (OpenAI on GPT-5-Codex over-prompting, Google on Gemini 3
over-analyzing verbose prompt engineering), so the fault line is model class, not
vendor and not harness.

This is why the tier is **not** keyed on `AGENT_RUNTIME`. MODEL-001 lets a
schedule, loop, or chat turn pin any model string — including free-text — so a
`claude-code` agent routinely runs Haiku 4.5. Gating on the runtime label would
silently degrade the majority of the selectable surface (7 of 9 `PRESET_MODELS`
entries sit in the tier where the guidance does not apply). Runtime already has
its own orthogonal job in this module: `_adapt_instructions_for_runtime` rewrites
MCP tool naming for Codex (#1187 F-MCP). The two axes stay separate.

**Functional requirements:**
- **FR-1 — Classifier:** `services/prompt_tier.py::resolve_prompt_tier(model)`
  returns `PromptTier.MINIMAL | PromptTier.VERBOSE`. Total function, never raises,
  pure-stdlib. Mirrors the `services/model_context.py` shape: family-prefix rules,
  first-match-wins, most-specific-first.
- **FR-2 — Unknown fails toward VERBOSE.** An unrecognized, empty, or `None` model
  resolves to `VERBOSE` — today's prompt. Over-instructing a Claude 5 model costs
  tokens; under-instructing a 4.5 model silently degrades it, and free-text model
  passthrough makes an unknown id routine rather than exceptional. This mirrors
  `model_context.py`'s "fail toward the conservative value" invariant and is the
  property that bounds this feature's blast radius to *status quo*.
- **FR-3 — Section-level gating, never two prompt strings.** `PLATFORM_INSTRUCTIONS`
  stays a single authored literal; sections are derived from it by splitting on
  its `###` headings and filtered by tier at render time. Maintaining two full
  prompts guarantees drift — a security instruction added to one variant and not
  the other is the failure mode this forbids by construction.
- **FR-4 — A section with no explicit tier renders always.** An unmapped or renamed
  heading falls back to always-render, and the heading set is CI-pinned so a rename
  fails loudly instead of silently changing what agents are told.
- **FR-5 — Safety and privacy sections are un-gateable.** Operator Communication
  (the #1402 fire-and-park contract, itself sentinel-locked and synced to
  `config/trinity-meta-prompt/prompt.md`) and the per-user memory-leak warning
  render at every tier. They are not tool-usage guidance and have no tool
  description to live in.

**Non-functional:**
- **Provable no-op on landing.** The MINIMAL prefix set ships **empty**, so every
  model resolves VERBOSE and the rendered prompt is byte-identical to the previous
  release. Enabling a family is a separate, evidence-gated change (ent#243 PR 2),
  which is what keeps a fleet-wide prompt change out of the same PR as its mechanism.
- **Prompt caching is unaffected.** The cache is keyed per model, so a cache entry
  is never shared across models; varying the prefix by model adds no fragmentation.
- Not vendored to the agent server (unlike `model_context.py`, Invariant #5): the
  backend composes the prompt and ships it in the turn payload, so the agent server
  never resolves a tier.

### 41.2 Model Availability at Compose Time

The prompt is composed **per turn** and is never baked into the container:
`get_platform_system_prompt()` is uncached and re-reads the operator's
`trinity_prompt` setting on every call, and the result travels in the request
payload. A model changed in the UI therefore takes effect on the next turn with no
container recreate, restart, or cache invalidation.

Composition paths and whether the model is known:

| Path | Model at compose time |
|---|---|
| `task_execution_service` (schedules, loops, webhooks, public/paid/channel via `execute_task`) | resolved to a concrete string before compose |
| `chat_execution_service` | `request.model`, **nullable** — no default-resolution step on this path |
| `pull_coordination_service` | **absent** — not passed into `ExecutionContext` at all (fixed here) |

- **FR-6 — The pull path threads its model.** `pull_coordination_service` populates
  `ExecutionContext.model`, so a pull-claimed turn is classified like any other.
- **FR-7 — An unpinned chat turn resolves VERBOSE, by design.** When the caller
  sends no model the *agent container* selects one (`claude_code.py` →
  `claude-sonnet-4-6`) **after** the prompt was composed — the decision does not
  exist backend-side at compose time and cannot be threaded. `claude-sonnet-4-6` is
  a 4.x model that wants the verbose prompt, so FR-2's default is already correct
  for this case. Resolving the platform default backend-side instead would trade a
  known-safe gap for a hidden one, since the backend default and the agent's
  `get_default_model()` can diverge silently. Making that second decision point
  explicit is a tracked follow-up, not a prerequisite (the same split underlies
  #1521's context-window "safe floor").

---
