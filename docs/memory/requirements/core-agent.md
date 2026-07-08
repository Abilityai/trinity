# Requirements — Core Agent — Management, Templates, Chat/Terminal, Activity, Collaboration

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 1. Core Agent Management

### 1.1 Agent Creation
- **Status**: ✅ Implemented
- **Description**: Create agents from templates (GitHub or local) or from scratch
- **Key Features**: Web UI, REST API, GitHub templates (`github:Org/repo`), local templates, credential schema auto-detection

### 1.2 Agent Start/Stop Toggle
- **Status**: ✅ Implemented (Updated 2026-01-26)
- **Description**: Start and stop agent containers via unified toggle control
- **Key Features**: Toggle switch shows Running/Stopped state, loading spinner during action, consistent UI across Dashboard, Agents page, and Agent Detail page
- **Components**: `RunningStateToggle.vue` - Reusable toggle component with size variants (sm/md/lg)

### 1.3 Agent Rename (RENAME-001)
- **Status**: ✅ Implemented (2026-03-01)
- **Description**: Rename agents via UI or MCP without deleting and recreating
- **Key Features**: Inline editing with pencil icon, `rename_agent` MCP tool, atomic DB updates, Docker container rename, WebSocket broadcast
- **Restrictions**: System agents cannot be renamed, only owners/admins can rename
- **API**: `PUT /api/agents/{name}/rename` with `{new_name: string}`

### 1.4 Agent Deletion
- **Status**: ✅ Implemented
- **Description**: Delete agents and cleanup resources
- **Key Features**: Container cleanup, network cleanup, cascade delete sharing records

### 1.5 Agent Logs Viewing
- **Status**: ✅ Implemented
- **Description**: View container logs for debugging
- **Key Features**: Logs tab, fixed-height scrollable container, auto-refresh, smart auto-scroll

### 1.6 Agent Live Telemetry
- **Status**: ✅ Implemented
- **Description**: Real-time container metrics in agent header
- **Key Features**: CPU/memory usage, network I/O, uptime display, auto-refresh every 10 seconds

---

## 4. Template System

### 4.1 Local Templates
- **Status**: ✅ Implemented
- **Description**: Auto-discovery from `config/agent-templates/`

### 4.2 GitHub Templates
- **Status**: ✅ Implemented
- **Description**: Clone via `github:Org/repo` format with PAT authentication

### 4.2.1 Admin-Configurable GitHub Templates (TMPL-001)
- **Status**: ✅ Implemented
- **Description**: Admin can configure which GitHub repos appear as agent templates via Settings UI. All metadata (display name, description, resources, MCP servers) is fetched from each repo's `template.yaml` via GitHub API (cached 10 min).
- **Key Features**: `config.py` holds default repo list (no metadata), `system_settings` table (`github_templates` key) stores admin overrides, `GET/PUT/DELETE /api/settings/github-templates` endpoints, Settings UI with add/remove/save/reset
- **Behavior**: `None` (key missing) = use defaults, `[]` = no GitHub templates, `[{...}]` = custom list. Admin-provided display_name overrides repo's template.yaml value.

### 4.3 Template Metadata
- **Status**: ✅ Implemented
- **Description**: Read template.yaml for display name, description, resources, credentials

### 4.4 Fork-to-Own Templates (trinity-enterprise#93)
- **Status**: ✅ Implemented (2026-07-06)
- **Description**: A GitHub template can declare `fork_to_own: required` in its `template.yaml`; creating an agent from it copies the template into a repo the **user owns** (private by default) and the agent's `origin` points there — captures, operator Push, and auto-sync write to the user's repo, never the shared upstream template. Cornelius is the first user; the mechanism is template-generic.
- **Key Features**:
  - `POST /api/agents` accepts an optional `fork_to_own` block: `{destination_repo: "owner/name", github_pat (SecretStr), private: true}`. The copy (repo creation + push of the template's default branch with full history) runs under the **user's PAT** — the platform PAT is read-only for the template clone.
  - Backend enforces `fork_to_own: required` (400 `FORK_TO_OWN_REQUIRED` without the block) so MCP/CLI paths can't silently create upstream-pointed agents. `@branch` template syntax and `local:` templates are rejected with the block (400).
  - Privacy: destination repo is **private by default**; public requires an explicit `private: false` the UI gates behind a loud warning.
  - The user PAT is persisted as the agent's per-agent PAT (#347, AES-256-GCM) so recreates re-bake it — the agent never falls back to the platform PAT.
  - Destination collision handling: non-empty repo → 409 `FORK_DESTINATION_EXISTS`, unless its only branch head matches the template tip (retry-safe reuse); repo already bound to a live agent → 409 `FORK_DESTINATION_IN_USE`; empty repo (incl. pre-created without README) is reused.
  - `upstream` remote auto-added in the agent workspace (credential-less, public templates) so `git pull upstream main` adopts template improvements; `GIT_UPSTREAM_REPO` env var baked at creation.
  - Fork-to-own agents are pinned to source mode (origin main = the brain) with the 15-min auto-sync heartbeat enabled (pushing to your own main is the point).
  - Create Agent modal renders templates carrying `fork_to_own` as **featured cards** (tagline surfaced from template.yaml) with destination/PAT/visibility fields.
- **Out of scope (v1)**: MCP `create_agent` tool does not accept `fork_to_own` (tool args are audit-logged — a PAT arg would persist in plaintext); PAT expiry/rotation UX (sync-health alerts detect push failures); upstream-update UI affordance.

---

## 5. Agent Chat & Terminal

### 5.1 Agent Terminal
- **Status**: ✅ Implemented (2025-12-25)
- **Description**: Browser-based xterm.js terminal with Claude Code TUI
- **Key Features**: PTY forwarding, mode toggle (Claude/Gemini/Bash), resize support
- **Flow**: `docs/memory/feature-flows/agent-terminal.md`

### 5.2 Chat via Backend API
- **Status**: ✅ Implemented
- **Description**: `/api/agents/{name}/chat` endpoint with stream-json output parsing

### 5.3 Conversation History
- **Status**: ✅ Implemented
- **Description**: Persistent chat history per agent stored in database

### 5.4 Context Window Tracking
- **Status**: ✅ Implemented
- **Description**: Token usage display (e.g., "45.5K / 200K") with color-coded progress bar

### 5.5 Session Cost Tracking
- **Status**: ✅ Implemented
- **Description**: Cumulative cost display across conversation

### 5.6 Authenticated Chat Tab
- **Status**: ✅ Implemented (2026-02-19)
- **Description**: Dedicated Chat tab in Agent Detail with simple bubble UI for authenticated users
- **Key Features**: Session selector dropdown, New Chat button, Dashboard activity tracking (uses `/task` endpoint), shared components with PublicChat
- **Spec**: `docs/requirements/AUTHENTICATED_CHAT_TAB.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

### 5.7 Dynamic Thinking Status (THINK-001)
- **Status**: ✅ Implemented (2026-03-03, extended 2026-03-04)
- **Description**: Real-time status labels in Chat tab and Public Chat reflecting agent activity (replaces static "Thinking...")
- **Key Features**: SSE stream subscription, tool-name-to-label mapping, 500ms anti-flicker, 10s heartbeat timeout, async_mode task execution with session persistence
- **Scope**: Authenticated Chat tab + Public Chat links (both use async_mode + SSE streaming)
- **Persistence hardening (#1444)**: `async_mode` + `save_to_session` chat-session persistence is **fail-loud** (a write error logs at ERROR with a stack trace and a `chat_persist_failed` marker on the sync response; never silently swallowed, never 500s a billed turn) and **owner-checks** a caller-supplied `chat_session_id` (IDOR fix). Guarded on a SUCCESS terminal only (FAILED/CANCELLED turns write no session). Covered by a **fast unit regression guard** (`tests/unit/test_1444_chat_session_persistence.py`) — the slow `requires_agent` integration tests (`test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence`) now also assert the execution reached `success` before demanding a session, disambiguating an execution failure from a persistence failure.
- **Spec**: `docs/requirements/DYNAMIC_THINKING_STATUS.md`
- **Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

### 5.8 Session Tab — `--resume`-default Chat Surface (SESSION_TAB_2026-04)
- **Status**: ✅ Implemented (2026-05-01), GA (2026-05-04)
- **Requirement ID**: SESSION_TAB_2026-04
- **GitHub Issue**: #651
- **Description**: New Agent Detail tab that lives alongside the existing Chat tab. Each turn reattaches to the same Claude Code session via `claude --print --resume <uuid>`, preserving tool-result memory, mid-skill state, and reasoning state across messages — strictly more capable than Chat's stateless text-replay model.
- **Key Features**:
  - New `agent_sessions` and `agent_session_messages` tables, strictly parallel to `chat_sessions`/`chat_messages` (no shared state, no FK between them)
  - Six endpoints under `/api/agents/{name}/sessions*` (create, list, get, message, reset, delete)
  - `SessionPanel.vue` + `stores/sessions.js` reuse Chat sub-components for visual parity
  - Stream-json parser fix recognises `{"type":"system","subtype":"init"}` (Phase 1.3)
  - `persist_session` flag plumbed through `ParallelTaskRequest → AgentRuntime → ClaudeCodeRuntime`
  - Resume-failure fallback: clears cache, retries cold once on missing JSONL (Anthropic upstream #39667 / #53417)
  - Per-`(agent, claude_uuid)` Redis lock (`SET NX EX 300s`, 30s wait ceiling) prevents JSONL corruption (Anthropic #20992)
  - Per-user ownership returns 404 on mismatch (does not leak session-id existence — E6)
  - JSONL cleanup service: synchronous best-effort reap on reset/delete + 6h periodic sweep with 1h race guard
  - JSONL-side fallback recovery for stdout pipe race + JSONL-side compact event capture
  - Cross-session contamination empirical gate (`test_session_cross_contamination.py`, Anthropic #26964)
- **Default**: ON (`session_tab_enabled` flag flipped to True for GA on 2026-05-04, PR #652)
- **Spec**: `docs/planning/SESSION_TAB_2026-04.md`
- **Flow**: `docs/memory/feature-flows/session-tab.md`
- **Unified Chat tab (#1112)**: the separate Session tab is collapsed into the single
  **Chat** tab, which carries a **Session-mode toggle** (default ON, persisted
  per-user in `localStorage['trinity.chatMode']`). ON → `SessionPanel`; OFF →
  legacy `ChatPanel`. The toggle is hidden and the tab falls back to legacy when
  `session_tab_enabled` is off or the runtime lacks `--resume` (Codex) — never
  zero chat surfaces. `?tab=session` aliases to the Chat tab; execution-resume
  (`resumeSessionId`) forces legacy for that landing without changing the saved
  preference. See architecture → Session Tab.

---

## 6. Activity Monitoring

### 6.1 Unified Activity Panel
- **Status**: ✅ Implemented
- **Description**: Real-time tool execution tracking with `--output-format stream-json --verbose`

### 6.2 Tool Chips with Counts
- **Status**: ✅ Implemented
- **Description**: Visual counts per tool type, sorted by frequency

### 6.3 Expandable Timeline
- **Status**: ✅ Implemented
- **Description**: List of all tool calls with timestamps and durations

### 6.4 Unified Activity Stream
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Centralized `agent_activities` table for all runtime activities
- **Flow**: `docs/memory/feature-flows/activity-stream.md`

---

## 9. Agent Collaboration

### 9.1 Agent-to-Agent Communication
- **Status**: ✅ Implemented (2025-11-29)
- **Description**: Agents communicate via Trinity MCP with agent-scoped API keys
- **Flow**: `docs/memory/feature-flows/agent-to-agent-collaboration.md`

### 9.2 Agent Permissions
- **Status**: ✅ Implemented (2025-12-10, Updated 2026-02-19)
- **Description**: Explicit permission model controlling which agents can call which
- **Key Features**: Permissions tab in UI, restrictive default (no auto-grant), explicit opt-in
- **Flow**: `docs/memory/feature-flows/agent-permissions.md`

### 9.3 Agent Shared Folders
- **Status**: ✅ Implemented (2025-12-13)
- **Description**: File-based collaboration via shared Docker volumes
- **Key Features**: Expose/consume toggles, permission-gated mounting
- **Flow**: `docs/memory/feature-flows/agent-shared-folders.md`

### 9.4 Collaboration Dashboard
- **Status**: ✅ Implemented (2025-12-02)
- **Description**: Real-time visual graph showing agents and animated connections
- **Key Features**: Vue Flow, draggable nodes, context progress bars, replay mode
- **Flow**: `docs/memory/feature-flows/agent-network.md`

### 9.5 Dashboard Timeline View
- **Status**: ✅ Implemented (2026-01-10)
- **Description**: Graph/Timeline mode toggle with execution visualization
- **Key Features**: Execution boxes (color-coded by trigger), collaboration arrows, live streaming
- **Flow**: `docs/memory/feature-flows/dashboard-timeline-view.md`

### 9.6 Replay Timeline Component
- **Status**: ✅ Implemented (2026-01-04)
- **Description**: Waterfall-style timeline visualization of agent activities
- **Key Features**: Zoom controls (50%-2000%), agent rows, activity bars, communication arrows
- **Flow**: `docs/memory/feature-flows/replay-timeline.md`

### 9.7 Task DAG System
- **Status**: ❌ Removed (2025-12-23)
- **Reason**: Individual agent planning deferred to orchestrator-level. Claude Code handles task management internally.

### 9.8 Dashboard Grid View (trinity-enterprise#47)
- **Status**: ✅ Implemented (2026-07-06)
- **Description**: Third dashboard mode (Grid / Graph / Timeline) — a magnetic tile canvas: rich 384×216 landscape agent tiles snapping to a sparse, unbounded lattice the operator arranges freely, on the same pan/zoom dotted-canvas language as the graph view. Not the default (Timeline remains default for new users); selection persists to localStorage.
- **Key Features**: iPhone-style drag with live socket preview + swap-with-preview; Tidy up / Reset; keyboard arrow reorder; per-user layout (`agent → {col,row}`, localStorage v1, self-healing); five-zone tile (identity with half-out avatar, adaptive chip strip with live working timer, Activity·14d stacked-by-trigger + Context·7d trend charts, success micro-meter + stats, Run/Auto toggles); system agent keeps its purple treatment; `prefers-reduced-motion` honored.
- **Performance (first-class)**: skeleton-first render from `/api/agents`; per-tile analytics hydrate lazily (viewport-gated, concurrency-capped) into the existing `(agent, window)` cache with stale-while-revalidate; batch endpoints for chip data (sync-health, operator-queue) on a visibility-aware poll that tears down when the mode is inactive; viewport culling for 50+ fleets. **No new backend endpoints** — reads `/api/agents/{name}/analytics` (#1107), fleet context/execution/slot stats, `/api/agents/sync-health` (#389), operator-queue pending.
- **Out of scope (follow-ups)**: fleet KPI strip; "Needs your attention" + live-activity right rail.
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md`

---

---

## Brain Orb — The Self-Rendering Mind (trinity-enterprise#58)

**Description**: A capability-gated per-agent page that renders a Cornelius-class agent's live
3D knowledge-graph orb from data the agent produces in its own container, with live scope control
and a client-held voice tile. **Shipped: static render (Phase 1, FR-1…5) + scope mount/unmount →
re-export → live rebuild (Phase 2, FR-6) + client-held Gemini Live voice tile + read-only KB search
(Phase 3, FR-7) + owner-gated KB-write actions capture/link (Phase 4a, FR-8) + voice-transcript
capture & configurable post-session processing (Phase 4b, FR-9, #66).** Only `run_skill` (arbitrary
headless exec from the orb) remains out of scope. Default OFF — no impact on other agents or the UI.
See [feature-flows/brain-orb.md](../feature-flows/brain-orb.md).

- **FR-1 — First-party CSP-clean assets**: the orb ships as verbatim first-party frontend assets
  (`public/brain-orb/`), with `three`/`marked`/`DOMPurify`/font vendored locally and the inline
  module externalized, so it runs under prod `script-src 'self'`/`font-src 'self'` with no nginx
  change. Only mechanical orb edits (externalize, vendor, repoint data fetch, neutralize the
  deferred voice proxy, hide deferred panels). Note bodies are DOMPurify-sanitized (H-005).
- **FR-2 — Capability gating**: a `/agents/:name/brain` route (lazy + `beforeEnter` platform-flag
  guard) and a Brain tab shown only when `brain_orb_available` (runtime-resolved platform flag —
  admin setting → `BRAIN_ORB_ENABLED` env fallback, default OFF; FR-11) **AND** the agent's
  `template.yaml capabilities` list contains the generalizable
  `brain-orb` token (surfaced by `/api/agents/{name}/info`) — never a hardcoded agent name.
- **FR-3 — Same-origin iframe host**: `views/AgentBrainOrb.vue` embeds the first-party page in a
  same-origin iframe (not agent-origin → avoids the #979 CSP trap, no Vue rewrite of the renderer).
- **FR-4 — Auth via postMessage, standard Bearer**: the host hands the user's JWT to the iframe via
  origin-pinned `postMessage` (never in a URL); the data route uses standard `AuthorizedAgentByName`
  Bearer auth — no new ticket primitive. A `brain-orb:error` message shows an empty state.
- **FR-5 — Read-only proxy (agent owns generation)**: `GET /api/agents/{name}/brain-orb/data`
  (`AuthorizedAgentByName`) proxies via `agent_httpx_client` (#1159) to the agent-server
  `GET /api/brain-orb/data`, which streams `~/resources/agent-visualization/data.json`. Byte
  pass-through (no re-serialize of the multi-MB JSON); 404 when the flag is off / no export,
  503/504 unreachable, 502 agent error. Trinity never runs `export_data.py` (Invariant #8).
- **FR-6 — Live scope control (Phase 2)**: the orb's scope panel mounts/unmounts vault scopes,
  driving an agent re-export → live in-place rebuild (no reload). `GET /api/agents/{name}/brain-orb/scopes`
  (`AuthorizedAgentByName`, read) lists selectable + active scopes; **`POST .../brain-orb/scope`
  (`OwnedAgentByName` — owner/admin)** mutates the set. The agent provides two executable convention
  hooks (`~/.trinity/brain-orb/{scopes,scope}`, mirrors `~/.trinity/pre-check`); the agent-server runs
  them via hardened async subprocess (timeout-kill, output cap, JSON-parse + non-zero-exit guards) and
  404s when absent. The agent owns scope state + the re-export (Invariant #8); Trinity only brokers.
  Replaces the local voice proxy's per-start `X-Orb-Token` with the platform JWT + owner gate.
- **FR-7 — Client-held Gemini Live voice tile + read-only KB search (Phase 3, #60)**: the orb's voice
  tile holds its own Gemini Live session **client-side** — the browser connects DIRECTLY to Gemini
  Live (mic capture + playback in the same-origin iframe), Trinity never proxies the audio.
  Deliberately distinct from Trinity's backend-proxied workspace voice (VOICE-001), to keep the
  voice→tool→orb loop in-browser. **Ephemeral-credential broker**: `POST /api/agents/{name}/brain-orb/
  voice-token` (`AuthorizedAgentByName`; per-(user,agent) rate-limited) mints a short-lived,
  **config-locked** Gemini Live ephemeral token via `auth_tokens.create` (`live_connect_constraints`
  pins model + the whole config incl. the tool surface; `uses=1`; ~60s new-session window; expiry =
  `VOICE_MAX_DURATION`). Built with a dedicated **v1alpha** genai client (NOT the cached voice
  singleton). The token is minted by the orb page (which holds the JWT) and relayed to the nested
  voice iframe over `postMessage` — the JWT never enters the voice iframe or a URL; the voice iframe
  only ever sees the single-use Google token. Response field is `ephemeral_token` (never `token`, which
  would flip the deferred write surface on). **Visual-only tools** (`highlight_related_notes`,
  `navigate_to_note`, `list_converged_topics`, …) run in-browser via the existing `orb-tool`
  postMessage bridge. **Scope-by-voice reuses Phase 2** (`mount_scope`/`unmount_scope` → the FR-6
  `/scope` broker — no new mutation surface). **Read-only KB search**: `POST /api/agents/{name}/
  brain-orb/tool` (`AuthorizedAgentByName`) → agent-server runs the agent's `~/.trinity/brain-orb/
  search` convention hook (scope-aware, read-only; 404 when absent). **Writes stay off by
  construction**: the locked tool manifest declares only read/visual/scope tools; the browser cannot
  widen it, and orb.js's `ACTIONS` write surface stays disabled (no `/session` route). **Gating**: a
  new `brain_orb_voice_available` flag (`BRAIN_ORB_VOICE_ENABLED && GEMINI_API_KEY`, default OFF) —
  distinct from the static `brain_orb_available` — AND the agent's `brain-orb` capability, enforced by
  BOTH the route guard and the tab (the orb is never launchable on a non-Cornelius agent, even via a
  raw URL — the `beforeEnter` guard reads `/info` capabilities and redirects otherwise, #60). CSP-clean:
  `connect-src` already allows `wss:`; the Gemini JS client is hand-rolled (no SDK), the voice logic
  and mic worklet are externalized same-origin files (script-src 'self'); the standalone page's
  hardcoded key is stripped; its p5.js audio-reactive voice orb is **vendored locally** (not CDN) so
  the speech animation is retained CSP-clean. The outer host iframe carries `allow="microphone"`.

- **FR-8 — Owner-gated KB-write actions: capture + link (Phase 4a, #61)**: the orb's action panel
  (`#actions`, `A` key) + inspector connect are un-hidden and rewired from the dead standalone voice
  proxy to the platform broker. Two owner/admin-only write verbs — **capture** (a note into the
  agent's inbox) and **link** (`[[wikilink]]` two notes). `POST /api/agents/{name}/brain-orb/action`
  (`OwnedAgentByName`) enum-validates the verb (run_skill/capture_transcript → 400, Phase 4b), body-caps
  (413), rate-limits per (user, agent, action), audit-logs (`brain_orb_capture`/`brain_orb_link`), and
  dedups via `Idempotency-Key` (Invariant #18, key folded per verb — NOT the #1084 effect_guard, which is
  execution_id-scoped and has no execution here); `GET .../brain-orb/actions` (`OwnedAgentByName`) reports
  `{enabled, skills}` so the orb un-hides the panel only for owners (403/404 otherwise). Both proxy to the
  agent-server, which runs the agent's `~/.trinity/brain-orb/action` convention hook via the hardened
  `_run_hook` (agent owns the write, Invariant #8; 404 when absent). **Voice write tools are owner-gated**:
  the mint route computes `can_write` (owner + flag) and only then folds `capture_note`/`link_notes` into
  the **locked** manifest — shared-user sessions keep the read-only Phase-3 manifest, and the `/action`
  route is the hard gate regardless. Own kill-switch `BRAIN_ORB_WRITE_ENABLED` (env, default OFF; distinct
  from `BRAIN_ORB_ENABLED` so writes disable without downing read/voice) → `brain_orb_write_available` in
  feature-flags. No DB change, no migration.
- **FR-9 — Voice-transcript capture + configurable post-session processing (Phase 4b, #66)**: mirrors the
  original `cornelius-internal/resources/agent-visualization/voice/` (client captures, agent renders/saves).
  The mint adds `input_audio_transcription`/`output_audio_transcription` to the **locked** `LiveConnectConfig`,
  so the constrained ephemeral token returns per-turn transcription. `voice.js` buffers input/output
  transcription into conversation events (`session_start`/`user_turn`/`model_turn`/`tool_call`/`session_end`)
  and, on `endConversation` (the correct flush seam — `onclose` early-returns on `wsClosedByUs`), relays them
  to `orb.js`, which POSTs `capture_transcript {session_id, events, process}` (session-id = `Idempotency-Key`
  → a double session-end saves one transcript). The `action` hook renders a markdown transcript into
  `resources/inbox/Voice Conversations/` (ported `transcript_io`). **Post-session processing** (`process_transcript`,
  or `capture_transcript {process:true}`): if the agent ships `~/.trinity/brain-orb/voice-postprocess.md` (the
  "formulated prompt config" — configuring it is the opt-in), the hook runs that prompt over the transcript via
  a **detached** `claude -p` (transcript piped on **stdin** — no shell string → no command injection), writing a
  processed note. Owner-only (`OwnedAgentByName` + `ACTIONS.enabled`), body cap raised to 1 MiB (backend +
  agent-server) for whole conversations. No DB change. **Confirmed on localhost**: constrained-token mint accepts
  the transcription config, and synthetic voice events render + save; full live-audio transcription streaming is a
  manual voice-session check.
- **FR-10 — Write → graph refresh loop + visible integration (#67, #68)**: closes the gap where captured notes /
  links landed in the inbox but never appeared on the orb. `POST /api/agents/{name}/brain-orb/refresh`
  (`OwnedAgentByName`, 200s timeout mirroring `/scope`, audited `brain_orb_refresh`) → agent-server
  `POST /api/brain-orb/refresh` → the `action` hook's `refresh` verb reindexes + re-exports `data.json` (folds inbox
  notes + `_links.md` edges into the graph; the agent owns generation, Invariant #8). `orb.js` `refreshGraph()`
  refetches `/data` and rebuilds **in place** (same machinery as `setScope`), auto-triggered after capture/link
  (voice writes debounced ~4s so a burst coalesces into one rebuild), plus a visible **"↻ integrate & refresh"**
  control, an "integrating…" state, and a "graph updated · +N notes, +M links" confirmation toast (#68). No DB
  change. **Confirmed on localhost**: capture → refresh folds the note in as a real graph node (`1072 → 1079`),
  and the UI control rebuilds with the confirmation toast.
- **FR-11 — Admin-configurable platform flags (trinity-enterprise#85)**: the three platform flags
  (`brain_orb_enabled`, `brain_orb_voice_enabled`, `brain_orb_write_enabled`) are **runtime-resolved**,
  not import-time env constants: `system_settings` row ("true"/"false", wins in both directions) →
  `BRAIN_ORB_*` env var honored as **opt-in** fallback → default OFF (the `workspace_enabled` idiom via
  one shared `_resolve_bool_flag` helper). Resolvers are fail-open (a settings-read failure falls back
  to the env/default leg — a raise would 500 `feature-flags` and zero every flag in the frontend store)
  and deliberately uncached (`--workers 2` cross-worker consistency, #506 rationale). All route gates in
  `routers/agent_brain_orb.py` and the three `feature-flags` values read the resolvers, so an admin flip
  applies without restart; the voice-token mint additionally composes with the base flag
  (`base ∧ voice`, closing the base-OFF mint gap) and `brain_orb_voice_available = base ∧ voice ∧
  GEMINI_API_KEY`. **Admin surface**: `GET/PUT /api/settings/brain-orb` (admin-only, registered before
  the `/{key}` catch-all) — GET returns per-flag `{value, source: override|env|default}` +
  `gemini_key_configured`; PUT takes partial booleans and/or `clear: [flag,…]` to **revert a flag to its
  env/default** (the env var is otherwise dead once a DB override exists), audit-logged with per-flag
  old→new values. Settings → General hosts the panel (per-flag source display, write-surface warning,
  post-save `loadFeatureFlags(force)`; other open sessions pick the change up on next page load).
  GEMINI_API_KEY stays env-only (secret). No migration (`system_settings` KV).

**Still out of scope**: `run_skill` (arbitrary allow-listed headless exec from the orb) — the full exec surface
with a `template.yaml` allow-list ceiling + #1083 detached-execution integration remains unbuilt; open a fresh
issue if it's ever wanted. Also deferred: `data.json` caching/streaming.

---

## Default Cornelius Agent — Auto-Seed on Fresh Install (trinity-enterprise#107)

- **Status**: ✅ Implemented (2026-07-07)
- **Description**: A fresh Trinity install auto-seeds a default "Cornelius" second-brain agent with the
  Brain Orb enabled, so a first-run operator lands on a working knowledge-graph agent out-of-the-box
  (no manual create/clone). Provisioned by
  `services/cornelius_agent_service.py::CorneliusAgentService.ensure_seeded()`.
- **Key Features**:
  - **Bundled local template**: a new `config/agent-templates/cornelius/` LOCAL template
    (`capabilities: [brain-orb]`, `CLAUDE.md`, `.trinity/brain-orb/` hooks, a pre-generated
    `resources/agent-visualization/data.json` seed graph so the orb renders immediately, and a minimal
    seed `Brain/` vault). Sourced from the public `github.com/Abilityai/cornelius`; provisioned via the
    ordinary `create_agent_internal` from `local:cornelius` (no PAT / network / clone).
  - **First-run-only**: a durable `cornelius_seeded` system-setting flag gates the seed — an operator who
    deletes Cornelius is **not** re-provisioned.
  - **Fresh-install-scoped**: skipped when any non-system agent already exists (`db.count_non_system_agents()`),
    so upgrades of established fleets aren't surprised by a new agent.
  - **Existence-guarded flag enable**: turns on the `brain_orb_enabled` platform flag only when unset —
    never clobbers an admin who set it OFF.
  - **Triggers**: the setup-completion handler (`routers/setup.py`, fresh installs, FastAPI BackgroundTask)
    + a `main.py` lifespan safety-net gated on `setup_completed && !cornelius_seeded` (upgrades). A Redis
    SETNX lock (`cornelius:provision`, fail-open, mirrors the #1464 leader-lock) guards the `--workers 2` race.
- **Known deviation (local bundle)**: the default Cornelius is a LOCAL bundle, not github-native, so it has
  **no git origin** — it won't auto-`git pull` upstream template updates. Durable ownership is deferred to
  fork-to-own (trinity-enterprise#109). No DB migration (`system_settings` is free-form KV). The Brain Orb was
  already fully OSS (flag-gated, not entitlement-gated), so no de-gating was needed.
- **Flow**: `docs/memory/feature-flows/cornelius-default-agent.md`
