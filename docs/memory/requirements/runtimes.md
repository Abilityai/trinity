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

### 29.2 Audio Streaming Bridge (VOICE-002)
- **Status**: ✅ Implemented
- **Description**: WebSocket proxy: browser ↔ backend ↔ Gemini, <100ms added latency
- **Key Features**: PCM 16kHz mono input, 24kHz mono output, base64 frame encoding

### 29.3 Transcript Persistence (VOICE-003)
- **Status**: ✅ Implemented
- **Description**: Voice transcripts saved as ChatMessage rows with `source="voice"`, inline in existing chat sessions
- **Key Features**: Automatic transcript extraction from Gemini, `source` column on chat_messages table

### 29.4 Frontend Voice UI (VOICE-004)
- **Status**: ✅ Implemented
- **Description**: Mic button next to chat input, voice overlay with status/mute/end controls
- **Key Features**: VoiceOverlay component, pulsing status indicators, live transcript display, mute toggle

### 29.5 Voice System Prompt (VOICE-005)
- **Status**: ✅ Implemented
- **Description**: Per-agent `voice_system_prompt` field for voice personality
- **Key Features**: Stored on agent_ownership table, fallback to auto-generated prompt from agent name

### 29.6 Context Summary (VOICE-006)
- **Status**: ✅ Implemented
- **Description**: On voice start, summarize prior messages and inject into Gemini system prompt
- **Key Features**: Last 20 messages truncated to ~750 tokens, injected as conversation context

### 29.7 Tool Calls + Canvas Orb (VOICE-007)
- **Status**: ✅ Implemented (#581)
- **Description**: Gemini voice sessions can invoke Trinity's `run_task` tool to dispatch agent tasks mid-conversation; frontend canvas orb replaces the static overlay
- **Key Features**: Single `run_task` tool declaration sent to Gemini Live API; non-blocking `asyncio.create_task` dispatch with 30s timeout; prompt injection mitigation (`_TOOL_PROMPT_MAX = 2000` chars); `_pending_tool_tasks` dict with cancellation on session end; canvas orb in `VoiceOverlay.vue` with `isToolCalling` state (no CDN dependencies); platform audit log on every tool call
- **Architecture**: Gemini → `tool_call` WS message → `_execute_and_respond()` → `POST /api/agents/{name}/chat` → Gemini `tool_response`

### 29.8 Voice Workspace (VOICE-008)
- **Status**: ✅ Implemented (#699)
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
4. **Phase 4 (Workspace)**: Full-page workspace with canvas panel tools, feature-flag gated (BETA) ✅

---

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
via `runtime: { type: codex, model: gpt-5.1-codex }`; the container is created
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
  loaded into the subprocess env; Codex agents are NOT assigned a Claude subscription.
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
