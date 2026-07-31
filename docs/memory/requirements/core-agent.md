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

### 1.3.1 Agent Display Label (ent#181)
- **Status**: 🚧 In Progress
- **Implements**: trinity-enterprise#181 (OSS-core — maintainer decision)
- **Description**: A human-readable label an owner can edit freely, with the
  agent's slug (`agent_name`) left untouched. Renaming a thing you can see is
  the common case; re-keying its identity is not.
- **FR-1 — The slug is the identity, the label is presentation**: everything
  machine-facing keeps using `agent_name` — routes, Docker container/volume
  names + labels, MCP keys, A2A cards, Redis keyspaces, every `agent_name`
  column. The label is rendered, never resolved. This is the whole point: §1.3's
  slug rename must rewrite ~20 tables, rename the container, clear every
  per-agent Redis keyspace, and *still* strands the agent's volumes under the
  old base (Docker can rename neither a volume nor its immutable
  `trinity.agent-name` label) — the root of #1664/#1665/#1667/#1669/#1671. A
  label change touches one column and nothing else.
- **FR-2 — NULL means "use the slug"**: `agent_ownership.display_label TEXT`,
  nullable, no backfill. Every existing agent renders exactly as it does today
  until someone sets a label; clearing the label reverts to the slug. Dual-track
  migration (Invariant #3).
- **FR-3 — One label everywhere a name renders**: agent detail header, dashboard
  cards, grid tiles, pickers/lists. A label applied on some surfaces and not
  others shows one agent under two names with no way to tell which is real —
  worse than no label. Resolution goes through a single helper, not per-site
  `||` chains.
- **FR-4 — The slug stays visible and copyable**: it is what URLs, MCP keys,
  containers and volumes are keyed on, so the UI shows it as secondary text
  wherever the label replaces it. A label that *hides* the identity trades one
  confusion for another.
- **FR-5 — The slug rename is demoted, not removed**: §1.3 stays available
  behind a secondary "advanced" affordance with copy that states what it
  actually does (restart, re-key, volumes stay under the old name). Owners who
  genuinely need it keep it; it stops being the default gesture for "call it
  something else".
- **API**: `GET`/`PUT /api/agents/{name}/label` — owner-only, `{label: string|null}`.
- **FR-6 — Remaining surfaces resolve the label off the agents store, not new
  payloads (#1643)**: operator queue, monitoring, executions, the collaboration
  graph, tab titles and prose/toasts render only a slug in their own payloads.
  Rather than grow a mutable `display_name` on each of those high-volume
  endpoints (staleness risk, N duplicated presentation fields), the frontend
  resolves slug → label off the loaded agents (store getters
  `displayNameForSlug` / `agentRefForSlug`, live via the `agent_label_changed`
  WS handler). An unloaded slug falls back to itself, so nothing regresses on a
  cold surface. Render rule by class: **dense operational tables** (executions,
  operator/monitoring rows, RACI matrix) keep the **slug primary** and surface
  the label as a hover tooltip (`agentNameTooltip`); **prose / toasts** use the
  label alone (`agentDisplayName`); the **collaboration graph** renders the
  label but keeps `data.label` = slug as the action key (`router.push` /
  toggles). `AgentAvatar` always receives the slug. Tab titles resolve the
  label on warm SPA nav and fall back to the slug on a cold direct load (the
  store isn't fetched yet); the next navigation self-heals. Comma-joined agent
  lists (e.g. the GitHub-PAT propagation failure list) keep the slug — long
  labels make them unreadable.
- **FR-7 — Findable by display name: pickers, search, sort (#1642)**: the
  picker surface class carries the slug **inline** — `<option>`s render
  `Display name (slug)` via `agentOptionLabel` (else the bare slug), and the
  `<option>` **value stays the slug** so filtering/selection never keys on the
  label. Six dropdowns: `ExecutionsPanel`, `ReportsPanelFleet`, operator
  `QueueList` + `NotificationsPanel`, `FileManager`, `Settings` (subscription
  assignment). `Agents.vue` name search matches **both** the slug and the
  display name (case-insensitive) — otherwise typing "TOM" against a
  `tom-marketing-ops` slug returns nothing. **Sort-key decision (AC):** the
  "Name (A-Z / Z-A)" sort orders by the **display name when set, else the slug**
  (`agentDisplayName`, in the store's `_getSortedAgents`) — sorting by the slug
  while the row renders the label would order the list by an invisible key. Every
  per-agent lookup (`getActivityState`/tags/stats/router actions) still keys on
  `agent.name`; only the option label, the search predicate, and the sort
  comparator changed. No store-shape change — the label is resolved off the
  loaded agents (FR-6 resolvers), so `agentNames`/`availableAgents` stay
  slug-string arrays.

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
- **Create-time resolution contract (#1793 + #1759)**: `local:<name>` is resolved against the curated catalog first, then the deploy-local store (`/data/deployed-templates`, #950). A well-formed but **unresolvable** id fails with a named **404 `UNKNOWN_LOCAL_TEMPLATE`** (#1793) raised **before any side effect** (no container, no MCP key, no volume, nothing to roll back) — completing the loud-reject contract #843 opened for *unprefixed* template strings. An empty / non-mapping / unparseable `template.yaml` fails in the same pre-side-effect band with **400 `LOCAL_TEMPLATE_INVALID`** (#1759), matching the strictness the listing surface (`GET /api/templates`) already applied; without it a *present* but malformed template reached the identical blank-agent-at-200 outcome through a broad `except Exception`. The traversal barrier keeps precedence: a malformed name is still 400 `INVALID_LOCAL_TEMPLATE_NAME`. `template: null` / `""` (Blank Agent) never enter this branch and are unaffected. Hidden templates (`hidden: true`) are **omitted from the listing but remain creatable by id** — the resolver never reads the flag.
  - The error is **one identical sentence whichever root missed**, carrying no filesystem path and no root name — deploy-local templates are named after *agent* names, so a root-distinguishing message would let a `creator`-role caller probe another user's agents (#186 adjacency).
  - Manifest deploys surface it per agent via the ent#125 `failed[]` report (`status_code: 400`), so one typo'd template no longer sinks a whole system.
  - The curated root falls back to the in-repo `config/agent-templates/` when the container bind mount is absent, so the gate is live in source-run backends too (aligning create with the listing surface, which has had that fallback since #843).

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

### 4.5 Library Page (trinity-enterprise#263)
- **Status**: ✅ Implemented (2026-07-31)
- **Description**: The Templates page is renamed **Library** (`/library`) — one surface for installable assets: an **Agent Templates** section (the existing Starter/GitHub/Custom card grids) and a **Skills** section (fleet-level browse over the shared skills library — see skills.md §22.3). `/templates` redirects (function-form, query **and** hash preserved) so old bookmarks and deep links keep working.
- **Key Features**:
  - Stacked sections with in-page jump anchors ("Agent templates · Skills") — deliberately NO kind-filter pills and no `?kind=` query machinery (two disjoint section shapes)
  - Per-section failure isolation: a templates fetch error never blanks the skills section and vice versa; each section owns its loading/error/empty states
  - Per-kind empty states teach the next action (templates: config hint; skills: the 4-state discriminator in skills.md §22.3)
  - Naming rule (AC#4 reading): page **identity** is Library — nav label, route path/name, `meta.title`, h1, e2e title assertions. The word "template" survives as the asset-kind noun (Starter/GitHub Templates section headers, Use Template buttons, `GET /api/templates` untouched)
  - Zero backend change — the skills half is a *view over* the skills.md §21 machinery (`GET /api/skills/library` + `/status`, admin `POST .../sync`); no new endpoints, no schema change
- **Not Built**: fleet-level assignment visibility (which agents carry each skill) — needs an aggregate read (e.g. `GET /api/skills/assignments`); cards link to the per-agent Skills tab via the agents list instead

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
- **Description**: One of three dashboard modes (Timeline / Grid / List; Timeline default — the legacy Graph mode was decommissioned in #1689, and the List mode landed in trinity-enterprise#260, §9.9) — a magnetic tile canvas: rich 384×216 landscape agent tiles snapping to a sparse, unbounded lattice the operator arranges freely, on the same pan/zoom dotted-canvas language as the graph view. Not the default (Timeline remains default for new users); selection persists to localStorage.
- **Key Features**: iPhone-style drag with live socket preview + swap-with-preview; Tidy up / Reset; keyboard arrow reorder; per-user layout (`agent → {col,row}`, localStorage v1, self-healing); five-zone tile (identity with half-out avatar, adaptive chip strip with live working timer, Activity·14d stacked-by-trigger + Context·7d trend charts, success micro-meter + stats, Run/Auto toggles); system agent keeps its purple treatment; `prefers-reduced-motion` honored.
- **Performance (first-class)**: skeleton-first render from `/api/agents`; per-tile analytics hydrate lazily (viewport-gated, concurrency-capped) into the existing `(agent, window)` cache with stale-while-revalidate; batch endpoints for chip data (sync-health, operator-queue) on a visibility-aware poll that tears down when the mode is inactive; viewport culling for 50+ fleets. **No new backend endpoints** — reads `/api/agents/{name}/analytics` (#1107), fleet context/execution/slot stats, `/api/agents/sync-health` (#389), operator-queue pending.
- **Out of scope (follow-ups)**: fleet KPI strip; "Needs your attention" + live-activity right rail.
- **Flow**: `docs/memory/feature-flows/dashboard-grid-view.md`

### 9.9 Dashboard List View — Agents-page consolidation (trinity-enterprise#260)
- **Status**: ✅ Implemented (2026-07-30)
- **Description**: Third dashboard mode **List** (Timeline / Grid / List) that replaces the standalone Agents page — the dashboard is the single canonical fleet surface. The Agents page's row list (three responsive layouts, per-row toggles, bulk tag ops, filters, empty states) is extracted into `components/AgentListPanel.vue`, mounted through the existing view-mode machinery (`VIEW_MODES` + `localStorage['trinity-dashboard-view']` — selection persists per user like the other modes). `views/Agents.vue` is deleted.
- **Key Features**:
  - **Full Agents-page parity** (28-item inventory audited, zero silent losses): name search (slug + display label, #1642) and status filter live in the List toolbar under NEW persisted keys `trinity-dashboard-list-filter-name` / `-status` (a clean break — the old page-scoped `trinity-agents-filter-*` keys are no longer read); sort dropdown bound to `agentsStore.sortBy` with the comparator extracted to `utils/agentSort.js` (system rows pinned first; `success_desc` gains a no-data-to-bottom tiebreak); row checkboxes + sticky bulk toolbar with bulk Add/Remove Tag; avatar-half-out rows with SYSTEM/GHOST/Shared/Runtime badges, activity + sync-health dots, success-rate bar, exec/schedule stats, CapacityMeter; filtered-empty ("No matching agents" + Clear all) and chassis-level true-empty ("Get started" → onboarding wizard) states; toast feedback.
  - **Filters migrated to chassis controls**: the page's single-tag dropdown and owner dropdown are superseded by the dashboard's existing quick-tag filter (multi-tag, server-side, counts) and owner filter, which apply to all three views; the List's Clear-all clears both layers (local name/status + chassis tags/owner via a `clear-chassis-filters` emit). The "X/Y" badge counts Y as the full fleet.
  - **Create Agent moved to the chassis header** — available in all three modes (previously the Agents page was the only persistent create surface); modal close refreshes the fleet.
  - **System-row Run guard adopted from the grid**: the List hides the Run toggle on system rows (the grid tile already refused it); stopping the system agent remains available on its Agent Detail page.
  - **Redirect**: `/agents` → `/?view=list` (query-preserving function redirect; `/agents/:name` and deeper untouched). The `?view=` intent is applied via a route watch as a one-shot, NON-persisting mode change, then stripped from the URL — a stale bookmark never rewrites the user's saved view selection. `?view=` doubles as a general non-persisting deep-link for all modes.
  - **NavBar consolidation**: the Agents entry is removed; the Dashboard link highlights on `/` and on `/agents/:name` pages (successor to the old `isAgentSection` highlight).
- **Performance**: zero per-row HTTP — `tags` and `read_only_enabled` ride every `GET /api/agents` row, so both Agents-page N+1 mount loops (per-agent tags + read-only fetches) are deleted (also more correct: the per-agent read-only GET 404'd on stopped containers and was coerced to `false`). One mounted-only loop: 60s visibility-aware sync-health refresh while List is active. **No new backend endpoints, zero backend changes.**
- **Seam (ent#261)**: the store-level `visibleAgents` computed in `stores/network.js` (server-side tag filter ∘ client-side owner filter) feeds Grid + List props — the type-to-filter predicate landed in that one place (§9.10), which also switched the Timeline onto the same computed via ReplayTimeline's `:agents` prop. The node-rebuild call sites deliberately do NOT read the seam (rewiring `convertAgentsToNodes` through it was rejected as timeline-mutation risk) — they read the pre-query `ownerFilteredAgents` (§9.10).
- **Flow**: `docs/memory/feature-flows/dashboard-list-view.md`

### 9.10 Dashboard Type-to-Filter (trinity-enterprise#261)
- **Status**: ✅ Implemented (2026-07-31)
- **Description**: Hotkey-activated, non-intrusive live type-to-filter across all three dashboard modes (Timeline / Grid / List). Press `/` anywhere on the Dashboard (outside editable fields and modals) → a small floating filter pill appears over the pane area; typing filters agents live in whichever view is active. An accelerator, not a takeover: **nothing is persisted** — a reload always starts unfiltered, and navigating away clears the query (Dashboard unmount). Purely client-side over the already-loaded fleet list; **zero backend changes**.
- **Key Features**:
  - **Activation**: `/` on a Dashboard-scoped document keydown listener. Guards, in order: `defaultPrevented`/`repeat` → non-`/` key (layout-produced — de-DE Shift+7 works; `shiftKey` NOT excluded) → Ctrl/Meta/Alt chords → IME composition (`isComposing`) → editable targets (INPUT / TEXTAREA / SELECT / `isContentEditable`) → open modals (onboarding wizard, System View editor, Create Agent modal). Then `preventDefault` (blocks Firefox quick-find) + open pill + focus input.
  - **Predicate**: case-insensitive substring over slug AND display label via `agentDisplayName()` (#1642 house rule, §1.3.1 FR-3) — typing `TOM` finds an agent labelled TOM whose slug is `tom-marketing-ops`. Layered inside the store `visibleAgents` seam (`stores/network.js`): `ownerFilteredAgents` (tag ∘ owner) → `visibleAgents` (∘ query), so Grid + List filter with zero pane rewiring; the Timeline joins by switching its `:agents` prop from raw `agents` to `visibleAgents` (rows, communication arrows, and schedule markers all derive from the prop). Description/tags matching is a recorded follow-up.
  - **Node invariant**: every `convertAgentsToNodes` call site reads the **pre-query** `ownerFilteredAgents` — a transient query must never degrade timeline-row node enrichment (system-first sort, purple treatment) after Esc. (The 30s refresh poll previously rebuilt nodes from the RAW list, ignoring even the owner filter — fixed to the same pre-query collection.)
  - **Honest state (pill)**: floating pill anchored to the non-scrolling chassis column, rendered whenever open OR a query is applied (an applied-but-hidden filter is the dishonest state this prevents). Live **"X of Y match"** count (X = post-query, Y = the set the view would show without the query but with tag/owner filters; secondary per-view filters — timeline "Active only", List panel name/status — may prune rendered rows below X by design: the pill claims *matching*, not *rendering*). Esc hint + × button; wrapper `role="search"`, input stays `type="text"`.
  - **Esc layering**: input-scoped Esc (clear + close + blur, `.stop` shields modal handlers) plus a document-level backstop so "Esc to clear" stays true after focus wanders — gated on filter-open/active, skipped while a modal is open, while the tag dropdown is open (that Esc closes the dropdown and KEEPS the filter), and while focus sits in another editable field (input/textarea/select/contenteditable — Esc there belongs to that control, e.g. the List panel's search box; the pill input is unaffected since its own handler stops propagation). Enter blurs the input and keeps the filter (GitHub convention).
  - **Query-empty state**: ONE chassis-level overlay ("No agents match "q"" + Esc-to-clear + Clear button) covering whichever pane is active; panes stay **MOUNTED** underneath (a transient zero-match while typing must never unmount ReplayTimeline/FleetGrid — zoom/scroll/layout state would reset). The true-empty onboarding CTA branches are guarded `&& !filterActive`, so "Get started" is unreachable while a query is active.
  - **Discoverability**: a clickable `<kbd>/</kbd>` hint button in the header controls (tooltip "Filter agents (press /)") that **toggles** — opens when closed, clears+closes when active — giving mouse/touch parity so the feature is not hotkey-only.
  - **List-mode composition**: the chassis query AND-stacks with the List panel's own persisted name/status filters; the panel's "N/M" count badge is suppressed while the chassis query is active so two disagreeing denominators never render simultaneously. The chassis query-empty overlay precedes the panel's filtered-empty state.
- **Behavior change (deliberate, release-noted)**: switching the Timeline's `:agents` prop onto `visibleAgents` makes the timeline honor the **owner filter** for the first time (previously grid-only — a latent inconsistency). `filterOwner` is persisted, so a user carrying a stale owner filter will see timeline rows narrow on upgrade day.
- **Flow**: folded into `dashboard-grid-view.md`, `dashboard-timeline-view.md`, `dashboard-list-view.md` (no standalone flow doc).

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
  - **Public source template** (#1656): provisioned via the ordinary `create_agent_internal` from
    `github:Abilityai/cornelius` — an anonymous, source-mode clone with **no PAT**, on the
    trinity-enterprise#123 tokenless public-repo path (`AgentConfig.source_mode` defaults `True`, which
    that path requires). Carries `capabilities: [brain-orb]`, `CLAUDE.md`, `.trinity/brain-orb/` hooks,
    a pre-generated `resources/agent-visualization/data.json` seed graph so the orb renders immediately,
    `resources/local-brain-search/` (so `semantic_search` is real, not a keyword fallback), and the full
    `Brain/` vault the seed graph was exported from. Was a vendored
    `config/agent-templates/cornelius/` snapshot until #1656; that snapshot drifted from its own prose
    and caused #1646 and #1656, so the bundle was deleted rather than re-vendored. **No offline
    fallback** — a fallback would only fire on a transient clone failure and would burn the durable
    `cornelius_seeded` flag on the degraded copy; leaving the flag unset to retry next boot is safer.
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
