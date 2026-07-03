# Requirements — Content & File Management, Image Gen, Avatars, Runtime Data

> Part of Trinity's requirements set. Index & write-path rule: [requirements.md](../requirements.md).

---

## 13. Content & File Management

### 13.1 Per-Agent File Manager
- **Status**: ✅ Implemented (Updated 2026-05-19, Issues #51, #37)
- **Description**: Full-featured file manager in AgentDetail Files tab with two-panel layout (tree + preview)
- **Key Features**: Tree view with search, image/video/audio/PDF/text preview, inline text editing, create folder (into selected directory or workspace root; nested via `/`), delete with protected path warnings, show hidden files toggle
- **Components**: Reuses `file-manager/FileTreeNode.vue` and `file-manager/FilePreview.vue`
- **Flow**: `docs/memory/feature-flows/file-browser.md`

### 13.2 File Manager Page (Standalone - Deprecated)
- **Status**: 🗄️ Deprecated (2026-03-03, Issue #51)
- **Description**: Former dedicated `/files` page replaced by per-agent Files tab. Route removed, component preserved.
- **Flow**: `docs/memory/feature-flows/file-manager.md`

### 13.3 Content Folder Convention
- **Status**: ✅ Implemented (2025-12-27)
- **Description**: `content/` directory gitignored by default for large generated assets

### 13.4 Agent Dashboard
- **Status**: ✅ Implemented (2026-01-12, Updated 2026-02-23)
- **Description**: Agent-defined dashboard via `dashboard.yaml` with widget system
- **Key Features**: 11 widget types (metric, status, progress, table, etc.), auto-refresh, historical tracking with sparklines (DASH-001), platform metrics injection
- **DASH-001 Enhancements** (2026-02-23):
  - Historical value tracking in `agent_dashboard_values` table
  - Sparkline charts showing metric trends
  - Trend indicators (up/down/stable with percentage)
  - Auto-injected platform metrics section (Tasks 24h, Success Rate, Cost, Health)
  - Query params: `include_history`, `history_hours`, `include_platform_metrics`
- **Flow**: `docs/memory/feature-flows/agent-dashboard.md`

### 13.5 Tasks Tab
- **Status**: ✅ Implemented
- **Description**: Unified task execution UI in Agent Detail page
- **Key Features**: Trigger manual tasks, monitor queue, view history, stop running tasks, make repeatable
- **Flow**: `docs/memory/feature-flows/tasks-tab.md`

### 13.6 Execution Log Viewer
- **Status**: ✅ Implemented
- **Description**: Modal for viewing Claude Code execution transcripts
- **Flow**: `docs/memory/feature-flows/execution-log-viewer.md`

### 13.7 Execution Detail Page
- **Status**: ✅ Implemented (2026-01-10)
- **Description**: Dedicated page for execution details with metadata, timestamps, transcript
- **Flow**: `docs/memory/feature-flows/execution-detail-page.md`

### 13.8 Live Execution Streaming
- **Status**: ✅ Implemented (2026-01-13), hardened (2026-03-13)
- **Description**: Real-time streaming of Claude Code execution logs to the Execution Detail page
- **Key Features**:
  - SSE streaming from agent server through backend proxy
  - Live log display with auto-scroll
  - "Live" indicator for running executions
  - "Live" button in TasksPanel (green pulsing badge) for running tasks
  - Stop button integration
  - Late joiner support (buffered entries)
  - Polling fallback when stream ends prematurely (race condition recovery)
  - Connect timeout on backend SSE proxy (prevents indefinite hang)
  - User-visible stream error banner with retry button
- **Spec**: `docs/requirements/LIVE_EXECUTION_STREAMING.md`

### 13.9 Continue Execution as Chat (EXEC-023)
- **Status**: ✅ Implemented (2026-02-20)
- **Priority**: MEDIUM
- **Description**: Resume failed or completed executions as interactive chat conversations with full context preservation
- **Key Features**:
  - Store Claude Code `session_id` in execution records
  - "Continue as Chat" button on Execution Detail page
  - Uses `--resume {session_id}` for native Claude Code session continuity
  - Full 150K+ token context available without copying/injection
  - Resume banner in Chat tab showing execution context
- **Spec**: `docs/requirements/CONTINUE_EXECUTION_AS_CHAT.md`

### 13.10 Outbound File Sharing (FILES-001)
- **Status**: ✅ Implemented (2026-04-24)
- **Requirement ID**: FILES-001
- **GitHub Issue**: #295
- **Priority**: P1
- **Description**: Agents publish files to a public download URL with token-based auth, 7-day default expiration, and inheritance of the agent's channel-access policy. The URL is a universal delivery mechanism that works across web, Slack, Telegram, WhatsApp, and email — replacing fragile per-channel workarounds.
- **Key Features**:
  - Per-agent opt-in toggle + Docker volume `agent-{name}-public` mounted at `/home/developer/public/`
  - `share_file` MCP tool (agent-scoped) — publishes a file and returns a download URL
  - Internal endpoint `POST /api/internal/agent-files/share` (agent-server path, `X-Internal-Secret` auth)
  - MCP-path endpoint `POST /api/agents/{name}/shared-files` (owner/admin or agent-scoped key)
  - Public download endpoint `GET /api/files/{file_id}?sig={token}` — 192-bit signed token, constant-time compare, streaming, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, audit logged as `file_share_download`
  - List / revoke endpoints for the owner (`GET` / `DELETE /api/agents/{name}/shared-files[/{id}]`)
  - UI panel in Agent Detail → Sharing tab (toggle, quota, table, copy URL, revoke)
  - File validation: relative path only, no `..` escapes, 50 MB per file, 500 MB per-agent quota, magic-byte MIME detection with executable blocklist (PE/ELF/Mach-O/shebang)
  - Agent delete cascades: DB rows + on-disk files + Docker volume all removed
  - Agent rename cascades: `rename_agent()` in `db/agent_settings/metadata.py` updates our table
- **Database**: `agent_shared_files` table + `agent_ownership.file_sharing_enabled` column (FK `ON DELETE CASCADE ON UPDATE CASCADE`, though enforcement is via the manual-cascade pattern used platform-wide)
- **Security (audited)**: path traversal rejection, filesystem isolation (backend never mounts agent workspace; `docker get_archive` only pulls the agent-named file), agent-scope defense (agent-scoped MCP keys can't share files for a different agent), no `download_token` param name (renamed to `sig` to avoid credential-sanitizer redaction)
- **Deferred (tracked for future)**: one-time download links (schema columns retained), platform-wide storage cap, streaming tar extraction, UUID-prefix directory sharding, dedicated rate-limit bucket
- **Design doc**: `docs/drafts/amazing-file-outbound.md`
- **Flow**: `docs/memory/feature-flows/file-sharing-outbound.md`

---

## 24. Platform Image Generation (IMG-001)

> **Design**: Platform-level image generation service using Gemini. Two-step pipeline:
> prompt refinement (Gemini 2.5 Flash text) + image generation (Gemini 2.5 Flash Image).

### 24.1 Image Generation Service
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: IMG-001
- **Description**: Core service for generating images from text prompts
- **Key Features**:
  - Two-step pipeline: prompt refinement → image generation
  - Use-case-specific best practices (general, thumbnail, diagram, social)
  - Configurable aspect ratios (1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3)
  - Optional prompt refinement bypass
  - Singleton pattern, httpx async client
- **Config**: `GEMINI_API_KEY` environment variable

### 24.2 REST Endpoints
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: IMG-001
- **Description**: REST API for image generation
- **Endpoints**:
  - `POST /api/images/generate` — Generate image from prompt (JWT required)
  - `GET /api/images/models` — List available models and options (JWT required)

### 24.3 Future: MCP Tools
- **Status**: ⏳ Not Started
- **Description**: MCP tools for agents to generate images

### 24.4 Future: Frontend UI
- **Status**: ⏳ Not Started
- **Description**: UI for image generation in agent detail or standalone page

---

## 25. AI-Generated Agent Avatars (AVATAR-001)

> **Design**: AI-generated circular avatars for agents using the existing Gemini image generation service.
> Users provide an identity prompt, the platform generates a consistent avatar cached on disk.

### 25.1 Avatar Generation
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Generate agent avatars from identity prompts using Gemini image service
- **Key Features**:
  - Identity prompt stored in DB (avatar_identity_prompt column)
  - Avatar use case in image generation prompts (optimized for circular crop, bold colors, digital illustration)
  - PNG cached at /data/avatars/{agent_name}.png
  - Cache-busting via avatar_updated_at timestamp

### 25.2 Avatar REST API
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Endpoints**:
  - `GET /api/agents/{name}/avatar` — Serve cached PNG (JWT, access check)
  - `GET /api/agents/{name}/avatar/identity` — Get identity prompt + metadata (JWT, access check)
  - `POST /api/agents/{name}/avatar/generate` — Generate avatar (JWT, owner only)
  - `DELETE /api/agents/{name}/avatar` — Remove avatar (JWT, owner only)

### 25.3 Avatar UI Components
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Reusable avatar component with fallback, shown across all agent surfaces
- **Components**:
  - `AgentAvatar.vue` — Circular avatar with gradient+initials fallback (sm/md/lg/xl sizes)
  - `AvatarGenerateModal.vue` — Modal for generating/removing avatars
- **Integration**: AgentHeader, AgentNode (dashboard), Agents list (3 layouts)

### 25.4 Avatar Lifecycle
- **Status**: ✅ Implemented (2026-03-07)
- **Requirement ID**: AVATAR-001
- **Description**: Avatar files cleaned up on agent delete, renamed on agent rename

---

## 41. Agent Runtime Data — `data_paths` + Snapshot/Export (#1169)

### 41.1 Declared Data Paths over the Existing Home Volume (#1169 — PR1)

Agents accumulate runtime data (SQLite DBs, datasets) that can't live in the
git-synced template repo (bloat) yet must survive container lifecycle events and
be portable to another Trinity instance. The agent home directory
(`/home/developer`) is **already** a persistent named Docker volume
(`agent-{name}-workspace`) that survives recreate, image upgrade, template
re-pull, and subscription auto-switch — so data dropped under
`/home/developer/data` is already durable today. This feature therefore reduces
to a **declaration** (`data_paths`) plus a real **snapshot/export/import**
capability over that existing volume — **no separate volume, no platform schema
change** (snapshots are filesystem artifacts; audit rides the existing
`audit_log`). Schema-free by design, to stay decoupled from the in-flight
SQLite→PostgreSQL migration (#1183).

**Functional requirements:**
- **FR-1 — Declaration:** a template may declare `data_paths:` (a list of globs
  under `data/`) in `template.yaml`. At creation, the backend materializes
  `~/.trinity/data-paths.yaml` inside the agent (quoted-heredoc write, glob-safe),
  mirroring the S4 persistent-state pattern. Opt-in — default `[]` (no file, no
  side effects when undeclared).
- **FR-2 — Durability:** declared data lives under `/home/developer/data` on the
  existing persistent home volume; no new volume is created. (Met by reuse.)
- **FR-3 — Gitignore:** when `data_paths` is non-empty, the declared paths and the
  `data/` root are appended to the **agent's own** `.gitignore` (idempotent
  `grep -qxF` merge) so runtime data is never committed. The fleet-wide ignore
  list is untouched.
- **FR-4 — On-demand export:** `POST /api/agents/{name}/data/export` (owner/admin)
  streams a tar of `/home/developer/data` (via `get_archive`, no workspace mount)
  to a temp file under `/data`, then returns it as a `StreamingResponse`. A
  configurable size cap (`AGENT_DATA_EXPORT_MAX_BYTES`) returns **413** on
  overflow. The tar embeds a self-describing **manifest** (`data-paths.yaml` +
  agent/version metadata). Accepts `Idempotency-Key` (Invariant #18); audited as
  `data_export`.
- **FR-5 — On-demand import/restore:** `POST /api/agents/{name}/data/import`
  (owner/admin) restores an uploaded tar into `/home/developer/data` via the
  existing agent-server `POST /api/agent-server/restore` primitive, whose
  `restore_from_tar` enforces the `data/**` allowlist and rejects absolute / `..`
  traversal. Audited as `data_import`.
- **FR-6 — Concurrency:** export and import are serialized per agent by a Redis
  operation lock (409 on contention).
- **FR-7 — Portability (MCP):** `export_agent_data` / `import_agent_data` MCP tools
  expose the capability so "move an agent" = template URL + `.credentials.enc` +
  data tar.

**Non-functional:** export never loads the full dataset into memory (stream →
temp → stream); the temp file is removed after the response is sent
(`BackgroundTask`). System agents are out of scope (no public/shared volumes;
`.trinity` is reset on their reset path).

**Out of scope (PR2 / follow-ups):** scheduled background snapshots,
`~/.trinity/pre-snapshot` SQLite-quiesce hook (`sqlite3 .backup` staging copy to
eliminate the hook-vs-tar race), snapshot retention, and the rename/purge
snapshot-dir cascade — all deferred to PR2. The pre-existing
home/public/shared **volume leak-on-purge** + **strand-on-rename** is a separate
fleet-wide bug filed independently.
