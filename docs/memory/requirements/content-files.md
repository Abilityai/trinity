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

## 42. Agent Plugin Manifest — declared, committed, self-healing (#1704)

### 42.1 Reframe — the real gap is git-reconstitution, not recreate

Claude Code records installed marketplace plugins in `~/.claude.json` (identity)
and copies each plugin's files into `~/.claude/plugins/cache/…` (the cache).
Both are gitignored — `.claude.json` correctly (session state + secrets),
`.claude/plugins/` by #1705 (repo bloat, the #1596 class). The literal issue
premise ("a container recreate loses plugins") is **not reproducible**: HOME
(`/home/developer`) IS the durable `agent-{name}-workspace` volume, no recreate
path removes it, and startup.sh preserves untracked files — so a plain recreate
keeps both manifest and cache. The genuinely-unprotected surface is a **git-based
reconstitution** into a fresh/empty volume or a **new host** (the #1169
"move an agent" model exports `data/` only; the #834/#1581 hard-purge removes the
volume), where the gitignored files are exactly what a clone drops. #1705 removed
the last incidental crutch (the cache used to be auto-committed, so a fresh clone
accidentally restored plugins), so on that path plugin loss is now complete.

The mechanism is therefore: make the plugin selection a **first-class, declared,
committed, secret-free, self-healing** piece of agent config — which also
trivially covers recreate/reset/move. This is the **agent-local half** of the
incubating global plugin-management model (trinity-enterprise#192): the same
normalized shape a future per-agent assignment surface materializes, reconciled
on start. This PR ships the **template-declared** half; capturing plugins
installed at **runtime** (a distill of Claude's own settings) is a deferred
follow-up (fragile, couples to Claude's moving internal files, may be subsumed by
#192).

### 42.2 Functional requirements

- **FR-1 — Declaration:** a template may declare a `plugins:` block in
  `template.yaml`:
  ```yaml
  plugins:
    marketplaces:
      - name: abilityai
        source: abilityai/abilities        # owner/repo shorthand or an https:// URL
    installed:
      - trinity@abilityai                    # plugin@marketplace
    # enabledPlugins: { trinity@abilityai: true }   # Claude settings.json shape, also accepted
  ```
  The reader (`services/template_plugins.py`) is **total** — never raises,
  degrades to named errors — mirroring the ent#89 `schedules:` reader; both
  catalog builders surface normalized `plugins` + `plugin_errors`. Opt-in: an
  absent/empty block is a full no-op.
- **FR-2 — Materialization:** at creation the backend writes a nested
  `~/.trinity/plugins.yaml` (`git_service.materialize_plugins`, the shared
  injection-safe heredoc writer, `sort_keys=True`) from a `_TemplateResolution.
  declared_plugins` carrier fed by all three resolver branches (github source
  metadata, local `template_data`, copy snapshot). Ghost-skipped and non-fatal
  (sits inside the creation rollback fence). **Deterministic** — a stable set
  produces a byte-identical file, so the 15-min auto-sync loop never re-commits a
  churning manifest.
- **FR-3 — Committed (the portability divergence):** `.trinity/plugins.yaml` is
  in `_TRINITY_AUTHORED_PATHS`, so it rides the #2070 contents-only `!`
  re-include AND the `git rm --cached` exemption — it is COMMITTED (unlike the
  volume-local `persistent-state.yaml` / `data-paths.yaml`), which is what lets it
  survive a git-based reconstitution. `.claude.json` and `.claude/plugins/` stay
  gitignored (#1705 intact — the manifest is a distilled, plugin-only,
  secret-free declaration, never the cache or the raw manifest).
- **FR-4 — Self-heal at boot:** startup.sh runs
  `python3 -m agent_server.plugins_reinstall` (after credential injection — a
  private marketplace needs a git credential at install time, resolved from the
  agent's `GITHUB_PAT` env, never the manifest). It reads current state via
  `claude plugin [marketplace] list --json`, adds missing marketplaces
  (`marketplace add`) and installs missing plugins (`install`, with `--yes`
  passed only when the CLI's `--help` advertises it — #2305), and runs
  **zero** subprocesses when the declared set is already present (volume-persisting
  restart). Non-fatal; each action logged (`installed`/`skipped`/`withheld:<reason>`).

### 42.3 Security

- The `plugins.yaml` on the agent-writable volume is **untrusted**: parsed with
  the ent#314 hardened loader (size cap + `AliasPolicy.REJECT`) on the agent side
  too, and every marketplace/plugin name AND the marketplace `source` are
  charset-validated at both the backend boundary and the boot hook.
- The marketplace `source` is the dangerous argument (it points where
  `marketplace add` fetches from): it must be `owner/repo` or an `https://` URL
  with **no `user:token@` userinfo** (refused, run through `redact_url_userinfo`),
  no traversal, no leading `-` (argument injection). Names/sources are passed as
  subprocess **arg lists**, never a shell string; every call is `timeout`-bounded
  with `stdin=DEVNULL` so a no-TTY prompt cannot hang.

### 42.4 Honest scope / known limitations

- **Runtime-install distill deferred** — plugins installed after creation
  (runtime `/plugin install`, not in the template) are not captured yet; that
  needs an agent-side distill of Claude's own `known_marketplaces.json` +
  `enabledPlugins`, whose shapes are undocumented and version-drifting.
- **Cornelius / tokenless source-mode agents** cannot push a materialized
  `.trinity/plugins.yaml` back to git, so the boot hook **falls back to reading
  the `template.yaml plugins:` block** the re-cloned template carries (same
  nested shape; BUDGET alias policy since template.yaml may legitimately anchor).
  startup.sh's guard fires on the manifest OR a top-level `plugins:` key, so the
  fallback is reachable; their plugins otherwise survive by volume + boot re-install.
- **Supply chain:** `plugin@marketplace` pins identity, not a commit — a
  re-install re-fetches the marketplace's current content (the #192
  `auto_update: on` behaviour); a pinned mode is a documented follow-up.

### 42.5 Platform-provided plugin set — deploy-as-is, onboard-in-place (ent#411)

- **Status**: ✅ Implemented (2026-08-18)
- **Problem**: §42 reads the plugin set from a declaration, which is chicken-and-egg
  for the agent that most needs it. A bare `github:owner/repo` with no
  `template.yaml` declares nothing → nothing installs → `trinity@abilityai`, whose
  `/trinity:onboard` would *write* that `template.yaml`, is absent. The only escape
  was a prose instruction telling the agent to run the CLI itself — the
  prose-dispatch anti-pattern the playbook-call rule exists to remove. `create_agent`
  already tolerates a missing `template.yaml`, so *deploy-as-is* worked and only
  *onboard-in-place* was blocked.
- **Pre-install**: `docker/base-image/Dockerfile` registers the `abilityai`
  marketplace and installs `trinity@abilityai` at build (`ARG
  TRINITY_PREINSTALL_PLUGINS=1`). Never fatal — an unreachable marketplace at build
  time logs and the image still builds, because the boot hook is the reconciler.
  Docker populates an empty named volume from the image on first mount, so a NEW
  agent inherits the pre-install and boots with **zero subprocesses**; an agent whose
  volume predates the image self-heals through the hook instead.
- **Ensured every boot**: `plugins_reinstall.merge_platform_defaults` unions the
  platform set into whatever is declared, so an undeclared agent still gets it
  (`status: platform_defaults_only`).
- **Additive, never subtractive**: a `plugins:` block that omits `trinity@abilityai`
  does not uninstall it. Nothing in this module ever uninstalls anything — reconcile
  means "install what is missing", not "make the set match".
- **The platform marketplace name is pinned to its source**: the manifest is on the
  agent-writable volume, so a declaration that re-points `abilityai` at another repo
  is ignored (with a log). A redefinable platform marketplace would turn a
  self-healing boot step into an arbitrary-code-fetch primitive.
- **Operator opt-out**: `TRINITY_PLATFORM_PLUGINS=0` at runtime (status stays
  `no_manifest`, distinct from a failure) and `--build-arg TRINITY_PREINSTALL_PLUGINS=0`
  for an air-gapped build.
- **Honest status**: each reconcile is recorded to `~/.trinity/plugins-state.json`
  (`status`, `platform_defaults_enabled`, installed / skipped / withheld-with-reason),
  surfaced by compatibility check **I-006** (INFO). "The marketplace was unreachable"
  and "the operator never wanted it" are different facts, and a bare presence flag
  cannot separate them. The file is agent-writable, so I-006 cross-checks the claim
  against the recorded lists rather than trusting a free-text status, and a missing
  file is a SKIP (image/boot predates the mechanism), not a failure.
- **Ordering caveat**: an agent on a base image built before this change lacks the
  pre-install and pays one install at next boot — the same caveat as #1704's hook.
- **Out of scope here** (owned by the marketplace, `abilityai/abilities`):
  `/trinity:onboard`'s in-place mode itself — detect-in-container, write the files,
  push back or emit a patch, verify via `get_agent_compatibility_report`.
- **Guide**: `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` → "Deploy as-is, then onboard
  in place".
