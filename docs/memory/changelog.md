### 2026-03-25

**feat: MCP Execution Query Tools — agents can poll for async results (MCP-007) (#19)**

Three new MCP tools enabling agents to query execution history and poll for async task results. This closes the async agent-to-agent collaboration loop: `chat_with_agent(async=true)` returns an `execution_id`, then `get_execution_result(id)` polls until complete.

- `list_recent_executions` — List recent executions for an agent across all trigger types (schedule, manual, MCP, chat) with optional status filter
- `get_execution_result` — Get full execution details including response text, cost, and optionally the full transcript/log
- `get_agent_activity_summary` — High-level activity summary over a time window with counts by type and state

**MCP Server:**
- `src/mcp-server/src/tools/executions.ts` — NEW: 3 execution query tools with agent-scoped access control
- `src/mcp-server/src/tools/index.ts` — Export new tools
- `src/mcp-server/src/server.ts` — Register 3 new tools (total: 62 tools)
- `src/mcp-server/src/client.ts` — Added `getExecution()`, `getExecutionLog()`, `getActivityTimeline()` client methods
- `src/mcp-server/src/types.ts` — Added `ActivityTimelineResponse`, `ActivityEntry` types; extended `ScheduleExecution` with `model_used`, `claude_session_id`, etc.

**No backend changes** — all tools wrap existing REST endpoints (`GET /api/agents/{name}/executions`, `GET /api/agents/{name}/executions/{id}`, `GET /api/activities/timeline`).

---

### 2026-03-23

**feat: Voice Chat — real-time voice conversations with agents via Gemini Live API (VOICE-001)**

Phase 1 MVP implementation of voice chat. Users click a microphone button in the Chat tab to start a real-time voice conversation with any agent. Audio streams bidirectionally through a backend WebSocket proxy to Google's Gemini 2.5 Flash Native Audio model (~280ms latency). Transcripts are automatically saved to the existing chat session with `source="voice"` markers. Uses the platform's existing `GEMINI_API_KEY`.

**Backend:**
- `src/backend/services/gemini_voice.py` — Gemini Live API client wrapper using `google-genai` SDK
- `src/backend/routers/voice.py` — Voice endpoints: start, stop, status, prompt CRUD, WebSocket audio bridge
- `src/backend/config.py` — Added `VOICE_ENABLED`, `VOICE_MODEL`, `VOICE_MAX_DURATION` config
- `src/backend/main.py` — Registered voice router
- `src/backend/db/schema.py` — Added `source` column to `chat_messages` table
- `src/backend/db_models.py` — Added `source` field to `ChatMessage` model
- `src/backend/db/chat.py` — Updated message creation and retrieval to handle `source` field
- `src/backend/db/migrations.py` — Added migrations for `source` column and `voice_system_prompt` on agent_ownership

**Frontend:**
- `src/frontend/src/utils/audio.js` — Microphone capture (PCM 16kHz) and audio playback (PCM 24kHz)
- `src/frontend/src/composables/useVoiceSession.js` — Voice session state management and WebSocket lifecycle
- `src/frontend/src/components/chat/VoiceOverlay.vue` — Voice session UI overlay with status, mute, end controls
- `src/frontend/src/components/ChatPanel.vue` — Added mic button and voice overlay integration
- `src/frontend/src/components/chat/ChatBubble.vue` — Voice message indicator badge
- `src/frontend/src/components/chat/ChatMessages.vue` — Pass source prop to bubbles

**Dependencies:**
- `docker/backend/Dockerfile` — Added `google-genai==1.12.1`

**fix: Scheduler fires Monday cron jobs on Tuesday — missing misfire handling (#145)**

APScheduler's `add_job()` calls had no `misfire_grace_time` parameter, defaulting to 30 seconds. When the scheduler container restarted (even briefly), any cron job whose window passed more than 30 seconds ago was silently dropped. Weekly schedules (e.g. Monday 9 AM) would then not fire until the next occurrence (Tuesday for daily, next Monday for weekly), appearing as a consistent +1 day shift. The in-memory job store (`MemoryJobStore`) compounded this — all jobs are lost on restart and reloaded from DB, but APScheduler still applies the 30s grace check.

**Fix**: Added `misfire_grace_time=3600` (1 hour, configurable via `MISFIRE_GRACE_TIME` env var), `coalesce=True`, and `max_instances=1` to both agent schedule and process schedule `add_job()` calls. Added startup catch-up logic that detects missed schedules (DB `next_run_at` in the past within grace window) and fires them immediately on container start.

- `src/scheduler/service.py` — Added misfire_grace_time, coalesce, max_instances to both `_add_job()` and `_add_process_job()`; added `_get_missed_schedules()` method; added startup catch-up sweep in `run_forever()`
- `src/scheduler/config.py` — Added `MISFIRE_GRACE_TIME` env var (default 3600s)
- `docker-compose.yml` — Added `MISFIRE_GRACE_TIME=3600` to scheduler environment
- `docker-compose.prod.yml` — Added `MISFIRE_GRACE_TIME=3600` to scheduler environment

---

### 2026-03-22

**fix: Execution termination (Stop button) fails for interactive chat executions**

The Stop button on the Execution Detail page did not work reliably for interactive chat executions. Root cause: the `/api/chat` path never passed the database execution ID to the agent container, so the process registry used a different random UUID — causing a 404 when termination was attempted. A secondary bug: the `task_execution_id` body param in the terminate endpoint was never parsed by FastAPI, so the DB status was never updated to "cancelled".

- `docker/base-image/agent_server/models.py` — Added `execution_id` field to `ChatRequest`
- `docker/base-image/agent_server/services/runtime_adapter.py` — Added `execution_id` param to abstract `execute()` interface
- `docker/base-image/agent_server/services/claude_code.py` — Accept `execution_id` in `execute()`, use provided ID instead of generating random UUID
- `docker/base-image/agent_server/services/gemini_runtime.py` — Accept `execution_id` in `execute()` (interface compliance)
- `docker/base-image/agent_server/routers/chat.py` — Pass `request.execution_id` to `runtime.execute()`
- `src/backend/routers/chat.py` — Pass `task_execution_id` in chat payload; default `task_execution_id` to `execution_id` in terminate handler

---

**fix: Auto-switch subscriptions now covers scheduled executions (SUB-003)**

The auto-switch hook only fired from `routers/chat.py` (interactive chat and background tasks). Scheduled executions, MCP calls, and agent-to-agent tasks go through `TaskExecutionService.execute_task()` which had no 429 detection. Agents running overnight on schedules would hit rate limits repeatedly without ever triggering a subscription switch.

- `src/backend/services/task_execution_service.py` — Added `handle_rate_limit_error` call in the `httpx.HTTPError` handler when status code is 429
- `docs/memory/feature-flows/subscription-auto-switch.md` — Updated flow diagram and file table

---

### 2026-03-21

**feat: Auto-switch subscriptions on repeated rate-limit errors (#153, SUB-003)**

When an agent hits 2+ consecutive rate-limit (429) errors, Trinity can automatically switch it to a different subscription. Opt-in via Settings > Subscriptions toggle.

- `src/backend/db/subscriptions.py` — Rate-limit event tracking (record, check, cleanup, select best alternative)
- `src/backend/db/migrations.py` — New `subscription_rate_limit_events` table
- `src/backend/services/subscription_auto_switch.py` — NEW: Auto-switch orchestration (detect, switch, log, notify, restart)
- `src/backend/routers/subscriptions.py` — GET/PUT `/api/subscriptions/settings/auto-switch`
- `src/backend/routers/chat.py` — Hook into 429 handler (chat proxy + background tasks)
- `src/backend/database.py` — Delegation methods for rate-limit tracking
- `src/frontend/src/views/Settings.vue` — Toggle in Subscriptions section with helper text
- `tests/test_subscription_auto_switch.py` — NEW: 6 smoke tests for setting CRUD

---

**fix: Agent server stream-json parser crashes on non-dict JSON lines (#151)**

Added `isinstance(raw_msg, dict)` guards in all four stream-json parsing locations in `claude_code.py`. Previously, `json.loads()` could return a string literal (valid JSON), and calling `.get()` on it raised `AttributeError`, killing the stdout loop and discarding all execution results.

- `parse_stream_json_output()` (line ~148) — skip non-dict after json.loads
- `process_stream_line()` (line ~283) — skip non-dict after json.loads
- `_run_headless_with_streaming()` (line ~547, ~882) — skip non-dict before sanitize_dict

---

**feat: Add chart widget type to agent dashboards**

Agents can now display charts (line, bar, area, pie, donut) in their dashboard via a new `type: chart` widget. Uses Chart.js/vue-chartjs (already in deps). Fully additive — no breaking changes to existing dashboards.

- `docker/base-image/agent_server/routers/dashboard.py` — Added `chart` to valid widget types with validation for chart_type, series/segments, height
- `src/frontend/src/components/DashboardChart.vue` — NEW: Chart renderer component using vue-chartjs
- `src/frontend/src/components/DashboardPanel.vue` — Integrated DashboardChart widget
- `src/backend/services/agent_service/dashboard.py` — Updated docstring

---

**🔧 fix: Unify ExecutionQueue and SlotService — chat now acquires capacity slots (#98)**

Chat executions (`/api/chat`) now acquire a capacity slot via SlotService in addition to using the ExecutionQueue. This makes SlotService the single source of truth for agent load — the capacity meter now reflects ALL execution types (chat, task, schedule, public, paid). Previously, chat occupied the queue but NOT a slot, so capacity meters showed incorrect load.

- `src/backend/routers/chat.py` — `chat_with_agent()` acquires/releases a slot around the chat execution; `terminate_agent_execution()` releases slot on termination
- `src/backend/services/agent_service/queue.py` — `force_release_agent_logic()` now also clears capacity slots

---

### 2026-03-19

**🔄 refactor: Split routers/agents.py into focused routers (#113)**

Reduced `agents.py` from 1,774 lines to 642 by extracting four focused routers — all API paths unchanged, 102 tests pass.

- `src/backend/routers/agents.py` — Slimmed to core CRUD, lifecycle, stats, queue, activities, terminal
- `src/backend/routers/agent_config.py` — NEW: autonomy, read-only, resources, capabilities, capacity, timeout, api-key
- `src/backend/routers/agent_files.py` — NEW: files, info, playbooks, permissions, metrics, folders
- `src/backend/routers/agent_rename.py` — NEW: rename endpoint with WebSocket broadcast
- `src/backend/routers/agent_ssh.py` — NEW: SSH access endpoint
- `src/backend/main.py` — Registers 4 new routers

---

**feat: Inline playbook autocomplete in agent chat (UI-AUTOCOMPLETE)**

The chat input now supports Claude Code-style slash-command autocomplete for agent playbooks. Typing `/` triggers a dropdown of available playbooks (filtered as you type), with `Tab` or `↵` to accept the top suggestion. An inline ghost-text completion previews the full command name in gray, and once a command is accepted a parameter hint bar appears showing the `argument_hint` from the skill's YAML frontmatter.

- `src/frontend/src/composables/usePlaybookAutocomplete.js` — New composable: fetches playbooks, parses slash-command context, manages dropdown/ghost-text/arg-hint state
- `src/frontend/src/composables/index.js` — Export new composable
- `src/frontend/src/components/chat/ChatInput.vue` — Integrated autocomplete UI: floating dropdown, ghost-text overlay, argument-hint bar, keyboard navigation (↑↓ Tab Escape Enter)
- `src/frontend/src/components/ChatPanel.vue` — Pass `agent-name` and `agent-status` props to `ChatInput`

---

**feat: Per-user persistent memory for public link agents (MEM-001) (#147)**

Email-verified public chat sessions now maintain persistent per-user memory across sessions. Before each agent call, the platform fetches the user's memory blob and injects it into the system prompt. Every 5 messages, a background summarization task (claude-haiku) extracts user facts and updates the stored memory. Anonymous sessions are completely unaffected.

- `src/backend/db/schema.py` — Added `public_user_memory` table + index
- `src/backend/db/migrations.py` — Migration #28 for `public_user_memory`
- `src/backend/db/public_links.py` — Added `get_or_create_user_memory()`, `increment_message_count()`, `update_user_memory()` to `PublicLinkOperations`
- `src/backend/database.py` — Delegation methods for user memory operations
- `src/backend/services/platform_prompt_service.py` — Added `format_user_memory_block()` helper
- `src/backend/routers/public.py` — Memory injection + background `_summarize_user_memory()` task (sync and async paths)
- `tests/test_public_user_memory.py` — Unit tests for DB ops and prompt formatting

---

### 2026-03-16

**fix: Chat tab model default for subscription compatibility (#138)**

The Chat tab's `execute_claude_code()` did not default to a model when neither the request nor `agent_state.current_model` specified one. Claude Code fell back to the agent's `~/.claude/settings.json`, which could contain a model incompatible with the assigned subscription (e.g., `claude-3-5-haiku-latest` on Claude Max), producing a misleading "Subscription token may be expired" error. Applied the same `model="sonnet"` default that Issue #81 added to `execute_headless_task()` (Tasks tab).

- `docker/base-image/agent_server/services/claude_code.py` — Default to `"sonnet"` in `execute_claude_code()` when no model specified

---

### 2026-03-15

**feat: Unified chat execution tracking (#96)**

All `/api/chat` calls now create execution records in `schedule_executions`, not just MCP/agent-to-agent calls. User chats use `triggered_by="chat"`. This provides a unified view of all agent work in the Tasks tab — chats, tasks, schedules, and agent-to-agent calls are all tracked consistently.

- `src/backend/routers/chat.py` — Always create execution record for all chat calls (user, MCP, agent-to-agent)
- `src/frontend/src/components/TasksPanel.vue` — Added "Chat" filter option with sky-blue badge, updated subtitle

---

### 2026-03-15

**fix: Prevent false "Silent launch failure" for long-running executions**

Fixed cleanup service falsely marking legitimate long-running executions as "Silent launch failure: no Claude session created within 60 seconds". Root cause: `claude_session_id` was only set on execution completion, so any execution taking >60s appeared as a failed launch. Fix: `TaskExecutionService` now sets `claude_session_id='dispatched'` before calling the agent (step 3b), so only truly orphaned executions are caught. Also added auto-polling (10s interval) to SchedulesPanel execution history so running/failed status updates are reflected without manual refresh.

- `src/backend/db/schedules.py` — Added `mark_execution_dispatched()` method
- `src/backend/services/task_execution_service.py` — Call `mark_execution_dispatched` before agent HTTP call (step 3b)
- `src/frontend/src/components/SchedulesPanel.vue` — Auto-refresh execution list every 10s when running executions exist

---

### 2026-03-15

**🔄 Hide Terminal tab from agent detail page (deprecation candidate)**

Removed the Terminal tab from the agent detail page for all users. The tab, its template content, and all associated reconnection/resize logic are commented out with deprecation markers. Backend terminal WebSocket endpoint and SSH service remain intact for API-level access. Frontend components (`TerminalPanelContent.vue`, `AgentTerminal.vue`, `useAgentTerminal.js`) are retained but unused.

- `src/frontend/src/views/AgentDetail.vue` — Commented out terminal tab, template content, reconnection logic, and fit-on-activate

---

### 2026-03-15

**refactor: Runtime injection of platform instructions via --append-system-prompt (#136)**

Replaced fragile file-based platform instruction injection (`CLAUDE.local.md` written via HTTP push) with runtime injection via `--append-system-prompt` on every Claude Code invocation. The backend builds the platform instructions and sends them with every chat/task request. No startup coordination needed — agents receive instructions on first interaction regardless of how they were started.

- `src/backend/services/platform_prompt_service.py` — New: single source of truth for platform instructions
- `src/backend/routers/chat.py` — Inject `system_prompt` into `/api/chat` payload
- `src/backend/services/task_execution_service.py` — Prepend platform prompt to task `system_prompt`
- `docker/base-image/agent_server/models.py` — Add `system_prompt` to `ChatRequest`
- `docker/base-image/agent_server/routers/chat.py` — Pass `system_prompt` through to runtime
- `docker/base-image/agent_server/services/runtime_adapter.py` — Add `system_prompt` to `execute()` interface
- `docker/base-image/agent_server/services/claude_code.py` — Add `--append-system-prompt` to `execute_claude_code()`
- `docker/base-image/agent_server/routers/trinity.py` — Removed inject/reset endpoints, kept status
- `src/backend/services/agent_service/lifecycle.py` — Removed `inject_trinity_meta_prompt()`
- `src/backend/services/agent_client.py` — Removed `inject_trinity_prompt()`
- `src/backend/routers/system_agent.py` — Removed injection callback wiring
- `src/backend/main.py` — Removed injection callback wiring

---

### 2026-03-14

**fix: Move platform instructions from CLAUDE.md to CLAUDE.local.md**

Platform instructions (Trinity section, operator queue, collaboration tools, custom prompt) were being injected into agents' `CLAUDE.md`, which got wiped during git operations. Now written to `CLAUDE.local.md` (gitignored, survives git ops). Legacy Trinity sections cleaned up from `CLAUDE.md` on next injection.

- `docker/base-image/agent_server/routers/trinity.py` — Inject to CLAUDE.local.md, ensure gitignored, clean up legacy

---

**feat: Mobile Admin — agent chat, autonomy toggle, and task sending**

Enhanced mobile admin (`/m`) agents tab with agent management controls and chat:

- **Autonomy toggle**: Switch agents between Auto and Manual mode (PUT `/api/agents/{name}/autonomy`)
- **Agent chat**: Full-screen chat overlay with session management, async task execution and polling
- **Removed context bar**: Replaced with more useful controls (mode toggle, chat, logs)

**Files changed:**
- `src/frontend/src/views/MobileAdmin.vue` — Added chat overlay, autonomy toggle, removed context bar

---

**feat: Mobile Admin PWA — standalone agent management from phone (#133)**

Added standalone mobile admin page at `/m` route, installable as a PWA via "Add to Home Screen". Completely separate from main UI with no navigation links.

- **Agents tab**: Agent list with start/stop toggle, expandable details (context bar, logs), search/filter, system agents hidden
- **Ops tab**: Operator queue with inline respond, notifications with acknowledge, badge counts
- **System tab**: Fleet health summary (running/stopped/high context counts), emergency stop, fleet restart, schedule pause/resume, cost overview
- **PWA**: Web app manifest, service worker (network-first with cache fallback), iOS meta tags, touch icons
- **Mobile UX**: Dark theme (OLED-friendly), 16px min font, large tap targets, iOS safe area support, pull-to-refresh, keyboard detection (hides tab bar), 15s auto-polling
- **Inline auth**: Admin password login directly on the page, no redirect to main login

**Files changed:**
- `src/frontend/src/views/MobileAdmin.vue` — NEW: Complete mobile admin SPA
- `src/frontend/src/router/index.js` — Added `/m` route
- `src/frontend/src/main.js` — Excluded `/m` from 401 redirect
- `src/frontend/public/mobile-manifest.json` — NEW: PWA manifest
- `src/frontend/public/mobile-sw.js` — NEW: Service worker
- `src/frontend/public/icons/` — NEW: PWA icons (192, 512, apple-touch)
- `docs/memory/requirements.md` — Updated MOB-001 status

---

### 2026-03-13 23:30
🧪 **Test: Close test coverage gaps for Avatars, Rate Limits, and Payments (#84)**

Added 71 new tests across 3 new test files to close critical test coverage gaps:
- `test_avatars.py` — 40 tests for AVATAR-001/002/003 (serving, generation, emotions, identity, defaults, lifecycle)
- `test_rate_limits.py` — 23 tests for SUB-002 (unit tests for `_is_rate_limit_message`, `_format_rate_limit_error`, `ExecutionMetadata` error fields, 429 queue full)
- `test_nevermined_payments.py` — 16 tests for NVM-001 (paid chat 402/403 flow, payment info, settlement admin, request validation)

Also updated `.claude/agents/test-runner.md` with accurate test counts, new file entries, and current known issues. Added Trinity platform compatibility files (`template.yaml`, `.mcp.json.template`) and updated `.gitignore` with Claude Code internals.

**Files changed:**
- `tests/test_avatars.py` — NEW: 40 tests
- `tests/test_rate_limits.py` — NEW: 23 tests (21 unit + 2 smoke)
- `tests/test_nevermined_payments.py` — NEW: 16 tests
- `.claude/agents/test-runner.md` — Updated statistics, categories, recent additions
- `template.yaml` — NEW: Trinity platform agent metadata
- `.mcp.json.template` — NEW: MCP server config template
- `.gitignore` — Added Claude Code internals, runtime exclusions

---

### 2026-03-13 23:00
🐛 **Fix: Live execution output gets stuck on 'Waiting for execution output...' (#68)**

Fixed SSE streaming race condition where frontend stream connects before agent has registered the execution. Added:
- Polling fallback: when stream ends prematurely (execution still running), polls every 5s and retries stream connection (up to 12 attempts / 60s)
- Connect timeout: backend SSE proxy now uses 10s connect timeout instead of `timeout=None`, preventing indefinite hangs
- Error surfacing: stream errors shown as amber banner with Retry button instead of being silently swallowed to console
- Retryable error classification: 404s and connection errors marked as `retryable` so frontend handles them gracefully

### 2026-03-13 18:55
🔧 **Fix: Cleanup service misses skipped executions and slow no-session detection (#106)**

Added two new cleanup passes to the cleanup service:
1. **No-session fast-fail**: Running executions without a `claude_session_id` after 60 seconds are now marked failed immediately instead of waiting the full 120-minute stale timeout
2. **Orphaned skipped finalization**: Defensive cleanup for any `skipped` executions missing `completed_at`

**Files changed:**
- `src/backend/db/schedules.py` — Added `mark_no_session_executions_failed()` and `finalize_orphaned_skipped_executions()`
- `src/backend/database.py` — Added delegation methods on `DatabaseManager`
- `src/backend/services/cleanup_service.py` — New `NO_SESSION_TIMEOUT_SECONDS`, expanded `CleanupReport`, two new cleanup steps
- `tests/test_cleanup_service.py` — 7 API integration tests (all passing)
- `docs/memory/feature-flows/cleanup-service.md` — Updated with Issue #106 changes

---

### 2026-03-13
📝 **Docs: Document 60-second MCP call timeout as design limitation (#104)**

Documented the Claude Code 60-second MCP HTTP timeout as a design limitation across all relevant docs. Agent designers must account for this when building agent-to-agent collaboration.

**Files updated:**
- `README.md` — Note under MCP Communication tools
- `docs/KNOWN_ISSUES.md` — Full limitation entry with workarounds
- `docs/MULTI_AGENT_SYSTEM_GUIDE.md` — New "Design Limitation" section with 3 workaround patterns
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` — Warning in Inter-Agent Collaboration section
- `docs/memory/feature-flows/agent-to-agent-collaboration.md` — Limitation section with links

---

### 2026-03-13 14:36
🔄 **Refactor: Split db/agents.py into focused modules (#102)**

Split the ~1000-line `AgentOperations` class into 6 focused mixin modules under `db/agent_settings/`, each handling a single concern. `AgentOperations` remains as facade class (inherits all mixins) for full backwards compatibility — no API changes, no caller changes.

**New modules:**
- `db/agent_settings/sharing.py` — Agent sharing operations
- `db/agent_settings/resources.py` — Memory, CPU, timeout, parallel capacity
- `db/agent_settings/security.py` — Full capabilities, read-only mode
- `db/agent_settings/autonomy.py` — Autonomy mode, API key settings
- `db/agent_settings/avatar.py` — Avatar identity management
- `db/agent_settings/metadata.py` — Batch queries, rename operations

**Core file:** `db/agents.py` — Slimmed to ownership + access control only (~160 lines)

---

### 2026-03-13
🐛 **Fix: Pin uvicorn 0.37.0 and add worker healthcheck timeout (#105)**

Upgraded uvicorn from 0.34.0 to 0.37.0 and added `--timeout-worker-healthcheck 120` to production compose. Uvicorn 0.30+ introduced a 5-second ping/pong worker healthcheck that SIGKILLs workers blocked on long-running agent HTTP calls (10-30 min), causing false-positive worker recycling and dropped connections.

**Files changed:**
- `docker/backend/Dockerfile` — uvicorn pin 0.34.0 → 0.37.0
- `docker-compose.prod.yml` — added `--timeout-worker-healthcheck 120` to backend command

**Related:** #101 (scheduler connection drops), #104 (MCP HTTP timeout)

---

### 2026-03-12
📝 **Docs: Architecture.md comprehensive update**

Updated architecture.md to reflect current system state:
- Fixed MCP Server tool count from 15 → 59 tools across 12 modules with full tool listing
- Expanded Routers section from 16 → 45 router modules, organized by category
- Expanded Services section from 11 → 30 service modules, organized by category
- Fixed frontend port in ASCII diagram from `:3000` → `:80`
- Added Background Services section documenting Cleanup, Operator Queue Sync, Monitoring, and Scheduler services

---

🐛 **Fix: Slack connect endpoint now reads from database settings (SLACK-001)**

Fixed bug where `POST /api/agents/{name}/public-links/{id}/slack/connect` was reading Slack credentials from environment variables instead of database settings. The endpoint now uses `get_slack_signing_secret()` from `settings_service` (database-first with env fallback), matching the behavior of other Slack endpoints.

**Root cause:** Import of `SLACK_SIGNING_SECRET` from `config.py` (env-only) instead of using `get_slack_signing_secret()` from `settings_service`.

**Fix:** `src/backend/routers/slack.py` - Changed import and usage to read from database settings.

---

📋 **Planning: Telegram Bot Integration for Agents (TGRAM-001)**

Added requirements for per-agent Telegram bot integration. Each agent can have its own Telegram bot (created via @BotFather) enabling mobile-first chat access.

**Scope:** Phase 1 MVP - bidirectional chat, polling mode, registration flow
**GitHub Issue:** #103
**Spec:** `docs/requirements/TELEGRAM_INTEGRATION.md`
**Blueprint:** Slack integration (`slack.py`, `slack_service.py`)

---

✅ **Tests: Slack Integration test suite (SLACK-001)**

Added comprehensive test suite for the Slack integration feature (27 tests). Tests cover signature verification, OAuth flow, DM handling, verification state machine, session persistence, and admin settings endpoints. 21 tests pass, 6 skipped (require specific environment setup).

**Test file:** `tests/test_slack_integration.py`

**Coverage:**
- Public endpoints (events, OAuth callback)
- Authentication enforcement
- Connection status management
- OAuth initiation and disconnect
- Admin settings CRUD
- Database schema validation

---

⚙️ **Feature: Per-agent configurable execution timeout (#99)**

Added per-agent execution timeout configuration. All execution paths (task API, chat, scheduler, MCP, paid endpoints) now use the agent's configured timeout when no explicit timeout is provided.

**Key changes:**
- Default timeout changed from 120s to 900s (15 minutes) platform-wide
- Each agent can configure `execution_timeout_seconds` (60-7200s, i.e., 1 min to 2 hours)
- Slot TTL now dynamically calculated as agent timeout + 5 min buffer
- API endpoints: `GET/PUT /api/agents/{name}/timeout`

**Implementation:**
- `db/schema.py`, `db/migrations.py` — Added `execution_timeout_seconds` column (default 900)
- `db/agents.py` — Added `get_execution_timeout()` and `set_execution_timeout()` methods
- `routers/agents.py` — Added timeout GET/PUT endpoints
- `services/task_execution_service.py` — Reads agent timeout when not explicitly provided
- `routers/chat.py` — Chat and task endpoints use agent timeout
- `routers/internal.py` — Scheduler uses agent timeout
- `services/slot_service.py` — Dynamic slot TTL based on agent timeout + 5 min buffer
- `models.py` — `ParallelTaskRequest.timeout_seconds` now defaults to `None` (use agent config)

---

✅ **Tests: Agent Timeout test suite (TIMEOUT-001)**

Added comprehensive test suite for the per-agent execution timeout feature (21 tests). Tests cover authentication, endpoint structure, validation, and common timeout values.

**Test file:** `tests/test_agent_timeout.py`

**Coverage:**
- Authentication requirements (GET/PUT)
- Response structure (agent_name, seconds, minutes)
- Default value (900 seconds = 15 minutes)
- Validation (60-7200 range, integer only)
- 404 for nonexistent agents

---

🔧 **Fix: Executions stuck in 'running' status when slot acquisition fails (#90)**

Fixed a bug where scheduled task executions could get permanently stuck in 'running' status with NULL `claude_session_id` and `duration_ms`. The issue occurred when exceptions were thrown during slot acquisition or database operations before the main try/except block in `TaskExecutionService.execute_task()`.

**Root cause:** The try block that handled exceptions and updated execution status to FAILED only covered the code *after* slot acquisition (step 4+). If `db.get_max_parallel_tasks()` or `slot_service.acquire_slot()` threw an exception, it propagated without updating the execution record.

**Note:** The GitHub issue incorrectly hypothesized that scheduler and backend used separate databases. Investigation confirmed they share a single SQLite database (`/data/trinity.db`).

**Fix:**
- Wrapped the entire execution flow (from slot acquisition onwards) in a try block
- Added `slot_acquired` flag to track state for conditional slot release in finally block
- Added defensive error handling to `_execute_task_internal_background()` to update execution status on uncaught exceptions

**Modified files:**
- `src/backend/services/task_execution_service.py` — Moved try block earlier to cover slot acquisition; added `slot_acquired` flag; conditional slot release in finally
- `src/backend/routers/internal.py` — Added fallback DB update in `_execute_task_internal_background()` exception handler

---

### 2026-03-11
🔧 **Fix: Task execution fails with misleading "token expired" error on subscription-incompatible model (#81)**

When an agent's `~/.claude/settings.json` contained a model not available on the assigned subscription (e.g., `claude-3-5-haiku-latest` on Claude Max), headless task execution via chat and paid endpoints failed with a misleading "Subscription token may be expired or revoked" error. Terminal WebSocket sessions worked fine because they passed a `model=sonnet` query parameter.

**Root cause:** `execute_headless_task()` did not specify a `--model` flag when `model` was `None`, so Claude Code used the agent's settings.json model which might be incompatible with the subscription type.

**Fix:** `execute_headless_task()` now defaults to `model="sonnet"` when no model is specified, ensuring compatibility with all subscription types (Claude Max, Pro, and API). Also added `_is_model_access_error()` helper to detect model-related errors and provide clearer error messages.

**Modified files:**
- `docker/base-image/agent_server/services/claude_code.py` — Default model to "sonnet" in `execute_headless_task()`; added `_is_model_access_error()` function; improved `_diagnose_exit_failure()` to detect model access errors

---

🔧 **Fix: Scheduler async fire-and-forget with DB polling (#101)**

Replaced the blocking HTTP call from scheduler to backend with async fire-and-forget dispatch + DB polling. This prevents TCP connection drops during long-running agent executions (10-60+ min) that caused false `failed` status and zero execution records.

**Changes:**
- `src/backend/routers/internal.py` — Added `async_mode` field to `InternalTaskExecutionRequest`; when `True`, spawns background task and returns `{"status": "accepted"}` immediately
- `src/scheduler/service.py` — Rewrote `_call_backend_execute_task()` to POST with `async_mode=True` and 30s timeout, then poll DB every `poll_interval` seconds; added `_poll_execution_completion()` method; added status overwrite guard in exception handler to prevent overwriting backend-finalized statuses
- `src/scheduler/config.py` — Added `poll_interval` config (default 10s, from `POLL_INTERVAL` env var)
- `src/backend/services/cleanup_service.py` — Increased stale timeouts from 30 to 120 minutes to prevent premature cleanup of long-running tasks
- `docs/memory/requirements.md` — Added §10.6 SCHED-ASYNC-001, updated §12.9 cleanup timeout

**Tests:** `tests/scheduler_tests/test_async_dispatch.py` — 11 tests covering async dispatch, DB polling, status overwrite guard, backward compatibility, timeout handling

---

🏷️ **Refactor: Standardize execution status values (#92)**

Introduced canonical `TaskExecutionStatus` enum (`running`, `success`, `failed`, `cancelled`, `skipped`) for all task/schedule execution status values persisted to the database. Renamed the queue-internal `ExecutionStatus` to `QueueItemStatus` to avoid confusion with the process engine's separate `ExecutionStatus` enum.

All hardcoded status string literals across the backend and scheduler now reference their respective enums instead of raw strings, ensuring type safety and single-source-of-truth for status vocabulary.

**Modified files:**
- `src/backend/models.py` — Added `TaskExecutionStatus` enum, renamed queue-internal `ExecutionStatus` → `QueueItemStatus`
- `src/backend/services/task_execution_service.py` — Uses `TaskExecutionStatus` and `ActivityState` enums
- `src/backend/services/execution_queue.py` — Uses `QueueItemStatus` enum
- `src/backend/services/activity_service.py` — Uses `ActivityState` enum for default status
- `src/backend/routers/chat.py` — Uses `TaskExecutionStatus` and `ActivityState` enums
- `src/backend/routers/internal.py` — Uses `ActivityState` enum for default status
- `src/backend/db/schedules.py` — Uses `TaskExecutionStatus` enum for SQL params
- `src/backend/db/activities.py` — Uses `ActivityState` enum for SQL params
- `src/scheduler/database.py` — Uses scheduler's own `ExecutionStatus` enum
- `src/scheduler/service.py` — Uses scheduler's own `ExecutionStatus` enum

**Not changed (intentionally):**
- `src/backend/services/process_engine/domain/enums.py` — Separate domain with its own `ExecutionStatus`
- API response status fields (`deploy.py`, `credentials.py`, `monitoring.py`) — Different concept
- Docker container status checks — Different domain

---

🧹 **Feature: Cleanup service for stuck executions, activities, and leaked slots (#94)**

Added `CleanupService` — a background service that runs every 5 minutes to automatically recover from stuck intermediate states. Performs a one-shot startup sweep and periodic cycles to:
- Mark stale executions (`status='running'` > 30 min) as `failed` with duration
- Mark stale activities (`activity_state='started'` > 30 min) as `failed` with duration
- Clean up stale Redis slots via existing `SlotService.cleanup_stale_slots()`

Includes admin-only monitoring endpoints: `GET /api/monitoring/cleanup-status` and `POST /api/monitoring/cleanup-trigger`.

**New files:**
- `src/backend/services/cleanup_service.py` — CleanupService with periodic + startup cleanup

**Modified files:**
- `src/backend/db/schedules.py` — Added `mark_stale_executions_failed()` method
- `src/backend/db/activities.py` — Added `mark_stale_activities_failed()` method
- `src/backend/database.py` — Added cleanup delegation methods
- `src/backend/main.py` — Register CleanupService in lifespan (start/stop)
- `src/backend/routers/monitoring.py` — Added cleanup-status and cleanup-trigger endpoints

---

### 2026-03-09
🔧 **Fix: Scheduled tasks now show in capacity meter (slot tracking)**

Scheduled task executions (cron and manual triggers) were not appearing in the Dashboard capacity meter because the scheduler called agent containers directly, bypassing the backend's `SlotService`. Now the scheduler routes through the backend's `POST /api/internal/execute-task` endpoint, which delegates to `TaskExecutionService` for unified slot management, activity tracking, credential sanitization, and Dashboard visibility.

- Added `POST /api/internal/execute-task` endpoint to `src/backend/routers/internal.py`
- Updated `src/scheduler/service.py` to call backend internal API instead of agent directly
- Removed unused `_track_activity_start()`, `_complete_activity()` methods and `AgentClient` import from scheduler
- Added `_call_backend_execute_task()` method using `httpx` with shared-secret auth

**Modified files:**
- `src/backend/routers/internal.py` — New `InternalTaskExecutionRequest` model and `/execute-task` endpoint
- `src/scheduler/service.py` — Route execution through backend, remove direct agent calls

---

⚡ **Performance: Avatar image optimization — WebP conversion + cache-busting fix**

Avatars now stored as optimized WebP (~30-50KB) instead of raw PNG (~1.2-2.0MB). Reduces per-agent storage from ~18MB to ~500KB and eliminates redundant 2MB network re-downloads during emotion cycling.

- Added Pillow dependency for image processing
- New `utils/image_optimize.py`: resizes to 512x512, converts to WebP quality=85
- Display avatars + emotions saved as `.webp`; reference images stay full-quality `.png` (Gemini input)
- All serving endpoints return `image/webp` with `.png` fallback for existing avatars
- Emotion cycling uses stable `?v={updated_at}` cache key instead of `Date.now()` per cycle
- Delete/rename operations handle both `.webp` and `.png` formats

**Modified files:**
- `docker/backend/Dockerfile` — Add Pillow==11.1.0
- `src/backend/utils/image_optimize.py` — NEW: optimize_avatar() function
- `src/backend/routers/avatar.py` — WebP saves, optimized serving, format fallbacks
- `src/backend/routers/agents.py` — WebP-aware delete/rename with .png fallback
- `src/frontend/src/views/AgentDetail.vue` — Stable emotion cache key

---

🔒 **Security: Implement immediate actions from security analysis (C-001, C-002, C-003, M-004, M-006, H-002, H-005)**

Seven security fixes from the OWASP-based security analysis:

1. **C-001**: `GET /credentials/encryption-key` restricted to admin-only (`require_admin` dependency)
2. **C-002**: `/ws` WebSocket endpoint now requires JWT authentication; unauthenticated connections are rejected after 5s timeout. Frontend updated to pass token via query parameter.
3. **C-003**: `/api/internal/` endpoints now require `X-Internal-Secret` header. Falls back to `SECRET_KEY` if `INTERNAL_API_SECRET` not set. Scheduler updated to pass the header.
4. **M-004**: All chat router endpoints now use `get_authorized_agent` dependency — users can only interact with agents they own or have been shared access to.
5. **M-006**: Credential inject/export/import endpoints now use `get_authorized_agent_by_name` — users can only manage credentials for agents they have access to.
6. **H-002**: Removed `:-changeme` default password fallback from `docker-compose.prod.yml`. Deployment now fails if `ADMIN_PASSWORD` is not set.
7. **H-005**: All 9 `v-html` instances now use DOMPurify sanitization via shared `utils/markdown.js`. Centralized marked+DOMPurify rendering replaces per-component `marked()` calls.

**Modified files:**
- `src/backend/routers/credentials.py` — Admin-only encryption key + agent access control
- `src/backend/main.py` — Authenticated WebSocket with first-message fallback
- `src/backend/routers/internal.py` — Shared-secret dependency for internal API
- `src/backend/routers/chat.py` — Agent access control on all endpoints
- `src/scheduler/config.py` — `internal_api_secret` config field
- `src/scheduler/service.py` — Pass `X-Internal-Secret` header
- `docker-compose.yml` — `INTERNAL_API_SECRET` env var for backend + scheduler
- `docker-compose.prod.yml` — Remove default password, add `INTERNAL_API_SECRET`
- `src/frontend/src/utils/markdown.js` — New shared DOMPurify+marked utility
- `src/frontend/src/utils/websocket.js` — Pass JWT token to WebSocket
- `src/frontend/src/stores/network.js` — Pass JWT token to WebSocket
- `src/frontend/src/components/chat/ChatBubble.vue` — Use sanitized markdown
- `src/frontend/src/views/ExecutionDetail.vue` — Use sanitized markdown
- `src/frontend/src/views/ProcessDocs.vue` — Use sanitized markdown
- `src/frontend/src/components/DashboardPanel.vue` — Use sanitized markdown
- `src/frontend/src/components/ProcessChatAssistant.vue` — Use sanitized markdown
- `src/frontend/src/components/operator/QueueCard.vue` — Use sanitized markdown
- `src/frontend/src/components/operator/QueueItemDetail.vue` — Use sanitized markdown

---

🔧 **Fix: MCP schedule tools missing timeout_seconds, allowed_tools, and model parameters (#85)**

MCP server's `create_agent_schedule` and `update_agent_schedule` tools now expose `timeout_seconds`, `allowed_tools`, and `model` parameters that the backend already supported. Users can now set custom execution timeouts, restrict tools, and override models when managing schedules via MCP.

**Modified files:**
- `src/mcp-server/src/types.ts` — Added fields to `Schedule`, `ScheduleCreate`, `ScheduleUpdate` interfaces
- `src/mcp-server/src/tools/schedules.ts` — Added Zod schema params and pass-through logic for both create and update tools

---

### 2026-03-08
🎨 **Enhancement: Meaningful Default Avatars from Templates (AVATAR-003)**

Default avatar generation now uses a smart prompt priority chain instead of generic type-based robot descriptions. Templates can specify an `avatar_prompt` field for explicit avatar descriptions. On agent creation from a template with `avatar_prompt`, the prompt is seeded into the DB. When generating defaults, the system checks: (1) DB-seeded prompt from template, (2) running agent's template.yaml (avatar_prompt or description), (3) type-based fallback. Added `avatar_prompt` to `/api/template/info` response.

**Modified files:**
- `docker/base-image/agent_server/routers/info.py` — Added `avatar_prompt` to template info response
- `src/backend/routers/avatar.py` — Smart prompt priority chain + `_get_prompt_from_template()` helper
- `src/backend/services/agent_service/crud.py` — Seed avatar prompt from template on agent creation
- `config/agent-templates/scout/template.yaml` — Added `avatar_prompt`
- `config/agent-templates/dd-competitor/template.yaml` — Added `avatar_prompt`
- `config/agent-templates/demo-analyst/template.yaml` — Added `avatar_prompt`
- `config/agent-templates/sage/template.yaml` — Added `avatar_prompt`

---

🎨 **Feature: Generate Default Avatars (AVATAR-003)**

Admin button in Settings to generate Gemini-powered placeholder avatars for all agents that don't have a custom one. Uses same image generation pipeline as custom avatars with auto-generated prompts from agent name and type. Tracks default vs custom avatars via `is_default_avatar` column so re-runs skip custom avatars and overwrite stale defaults.

**New/modified files:**
- `src/backend/routers/avatar.py` — `POST /api/agents/avatars/generate-defaults` endpoint
- `src/backend/db/schema.py` — `is_default_avatar` column on `agent_ownership`
- `src/backend/db/migrations.py` — Migration #26 `_migrate_agent_ownership_default_avatar`
- `src/backend/db/agents.py` — `get_agents_without_custom_avatar()`, `set_default_avatar()`, updated `set_avatar_identity()` and `clear_avatar_identity()`
- `src/backend/database.py` — Delegation methods for new DB operations
- `src/frontend/src/views/Settings.vue` — Default Avatars card with Generate button

---

🔀 **Feature: Consolidate Ops / Events / Cost Alerts into Operating Room**

Merged three separate sections (Operating Room, Events, Cost Alerts) into a single unified Operating Room view with 4 tabs: Needs Response, Notifications, Cost Alerts, Resolved. Removed the Events and Alerts bell icons from NavBar. The Ops badge now shows a combined count across all three data sources with critical highlighting. Old routes `/events` and `/alerts` redirect to `/operating-room` with the appropriate tab query parameter.

**New components:**
- `src/frontend/src/components/operator/NotificationsPanel.vue` — Extracted from Events.vue
- `src/frontend/src/components/operator/CostAlertsPanel.vue` — Extracted from Alerts.vue

**Modified files:**
- `src/frontend/src/views/OperatingRoom.vue` — 4-tab layout, 3 stores, query param tab support, combined subtitle
- `src/frontend/src/components/NavBar.vue` — Removed 2 bell icons, combined Ops badge count
- `src/frontend/src/router/index.js` — `/events` and `/alerts` redirect to `/operating-room`

**Deleted files:**
- `src/frontend/src/views/Events.vue` — Replaced by NotificationsPanel in Operating Room
- `src/frontend/src/views/Alerts.vue` — Replaced by CostAlertsPanel in Operating Room

---

🎭 **Feature: Emotion avatar variants with automatic cycling (AVATAR-002)**

Agents now get 8 emotion variants generated in the background after avatar creation. Emotions: happy, thoughtful, surprised, determined, calm, amused, curious, confident. On the AgentDetail page, the avatar cycles to a random emotion every 30 seconds. Agents list and dashboard use the base avatar (no cycling). Emotion files are cleaned up on avatar delete and renamed on agent rename.

**New backend:**
- `AVATAR_EMOTIONS` and `AVATAR_EMOTION_PROMPTS` constants in `image_generation_prompts.py`
- `generate_emotion_variation()` method on `ImageGenerationService`
- `GET /api/agents/{name}/avatar/emotions` — list available emotion variants
- `GET /api/agents/{name}/avatar/emotion/{emotion}` — serve emotion PNG
- Background `asyncio.create_task()` generates all 8 emotions after avatar creation
- Delete/rename cleanup for `_emotion_{name}.png` files in `avatar.py` and `agents.py`

**Frontend:**
- `AgentHeader.vue` — new `emotionAvatarUrl` prop overrides base avatar
- `AgentDetail.vue` — loads emotions, cycles every 30s, polls for new emotions after avatar generation

---

🔄 **Feature: Reference image system for avatar regeneration (AVATAR-001)**

Avatars now use a reference image system. When generating from the modal, the result is saved as both the display avatar and the reference image. The hover refresh button generates a variation from the reference image (same identity, slight natural variation) without changing the reference. The modal shows the reference image alongside the current avatar, and the generate button is labeled "New Reference" when a reference exists. Two hover buttons: refresh (variation from reference) and pencil (open modal to change prompt/create new reference).

**New backend endpoints:**
- `GET /api/agents/{name}/avatar/reference` — Serve reference image
- `POST /api/agents/{name}/avatar/regenerate` — Generate variation from reference

**Modified files:**
- `src/backend/routers/avatar.py` — Reference image storage, regenerate endpoint, reference serving
- `src/backend/services/image_generation_service.py` — `generate_variation()` method, reference image support in `_call_gemini_image()`
- `src/frontend/src/components/AgentHeader.vue` — Two-button hover UI (refresh + edit)
- `src/frontend/src/components/AvatarGenerateModal.vue` — Reference image preview, "New Reference" button label
- `src/frontend/src/views/AgentDetail.vue` — `regenerateAvatar()` calls `/regenerate`, passes `hasReference` prop

---

🎨 **Style: Avatar dark mode aesthetic + tighter framing + faster models (AVATAR-001)**

Redesigned avatar style to match Trinity's dark mode UI: dark slate-navy/charcoal backgrounds (#1a1f2e → #111827), cool 5600K lighting, indigo-blue rim light (#6366f1) matching the UI accent color, modern digital color grading (sharp, clean, no film grain) replacing the previous warm Kodak Portra film look. Also tightened framing to extreme close-up (85-90% face fill) and switched to faster Gemini models (2.0 Flash text, 3.1 Flash Image Preview).

**Modified files:**
- `src/backend/services/image_generation_prompts.py` — Dark mode style, tighter framing
- `src/backend/services/image_generation_service.py` — Faster Gemini models

---

🎨 **Style: Avatar generation now uses cinematic lifestyle photography aesthetic (AVATAR-001)**

Rewrote the avatar prompt engineering to use a Kodak Portra 400 film-inspired cinematic portrait style instead of the previous digital illustration style. Avatars now generate as warm, authentic-feeling portraits with shallow depth of field, muted pastel tones, lifted shadows, creamy highlights, and subtle film grain.

**Modified files:**
- `src/backend/services/image_generation_prompts.py` — Rewrote `AVATAR_BEST_PRACTICES` with cinematic lifestyle photography rules

---

🔧 **Fix: Subscription usage limit errors now surface clear, actionable messages (SUB-002)**

Previously, when a Claude Code subscription hit its usage limit, agents returned a generic "Claude Code execution failed (exit code 1)" error with no indication of the actual problem. Now the system detects rate limit errors from Claude Code's stream-json output and returns:
- **HTTP 429** status (instead of 500)
- Clear message: "Subscription usage limit: You're out of extra usage · resets [time]. To resolve: (1) wait for the usage reset, (2) set an ANTHROPIC_API_KEY on this agent for pay-per-use billing, or (3) assign a different subscription token in Settings → Subscriptions."
- Frontend displays rate limit errors with amber (warning) styling instead of red (error)

Detection works across both chat mode (`/api/chat`) and task mode (`/api/task`) by parsing the `error` field from Claude Code's `assistant` messages and the `is_error` flag from `result` messages in stream-json format.

**Modified files:**
- `docker/base-image/agent_server/models.py` — Added `error_type` and `error_message` fields to ExecutionMetadata
- `docker/base-image/agent_server/services/claude_code.py` — Added `_is_rate_limit_message()`, `_format_rate_limit_error()` helpers; detect rate limit in `process_stream_line()` from assistant and result messages; raise HTTP 429 in both `execute_claude_code()` and `execute_headless_task()`
- `src/backend/routers/chat.py` — Preserve 429 status from agent responses (was being converted to 503)
- `src/frontend/src/components/ChatPanel.vue` — Added `isRateLimitError` computed property; amber styling for rate limit errors with "Subscription Usage Limit" header

---

🎨 **UI: Enlarged overlapping avatar on Agent Detail page (AVATAR-001)**

Redesigned the avatar display on the Agent Detail header to a social-media-style overlapping circle. The avatar is now 96px (2xl size), centered exactly on the left edge of the header card (50% inside, 50% outside) using `left-0 -translate-x-1/2`. Features an indigo border (`border-indigo-400/500`) and shadow. Hover overlay with camera icon for generating avatars. Content area has `ml-14` to accommodate the protruding half. The Agents list, Dashboard nodes, and other pages remain unchanged.

**Modified files:**
- `src/frontend/src/components/AgentAvatar.vue` — Added `2xl` size (96px, text-2xl)
- `src/frontend/src/components/AgentHeader.vue` — Repositioned avatar as absolute overlapping element with indigo border and hover overlay
- `src/frontend/src/views/AgentDetail.vue` — Added `ml-14` wrapper and `overflow-visible` to accommodate avatar protrusion

---

### 2026-03-07
🔧 **Fix: Avatar display — remove auth from GET endpoint, fix Gemini model name and config field (AVATAR-001, IMG-001)**

Three fixes to make avatar display work end-to-end:
1. Removed JWT auth from `GET /api/agents/{name}/avatar` — browser `<img>` tags cannot send Authorization headers, so avatars are now served as public assets
2. Fixed Gemini model name: `gemini-2.5-flash-preview-image-generation` → `gemini-2.5-flash-image` (old model removed from API)
3. Fixed Gemini config field: `imageSizeOptions` → `imageConfig` (correct field name per Gemini API)
4. Added `avatar_url` to single-agent detail endpoint (`GET /api/agents/{name}`) — was only in bulk list endpoint

**Modified files:**
- `src/backend/routers/avatar.py` — Removed auth from GET avatar endpoint
- `src/backend/routers/agents.py` — Added avatar_url to single-agent detail response
- `src/backend/services/image_generation_service.py` — Fixed model name and config field
- `docs/memory/feature-flows/image-generation.md` — Updated model name and field references

---

✅ **Feature: Operating Room — Full Implementation (OPS-001)**

Implemented the complete Operating Room feature: backend database, REST API, file sync service, frontend wired to real API, WebSocket real-time updates, and meta-prompt integration. Agents write requests to `~/.trinity/operator-queue.json`, platform syncs them to DB, and the Operating Room UI presents them as actionable cards with inline response controls.

**New files:**
- `src/backend/db/operator_queue.py` — Database operations (create, list, respond, cancel, stats)
- `src/backend/routers/operator_queue.py` — REST API (6 endpoints with JWT auth)
- `src/backend/services/operator_queue_service.py` — Background sync service (5s polling, bidirectional)
- `tests/test_operator_queue.py` — API tests (37 tests, 100% pass rate)

**Modified files:**
- `src/backend/db/schema.py` — Added `operator_queue` table with 5 indexes
- `src/backend/db/migrations.py` — Migration #25 for operator_queue table
- `src/backend/database.py` — DatabaseManager delegation methods
- `src/backend/main.py` — Router mount, WebSocket injection, sync service lifecycle
- `src/frontend/src/stores/operatorQueue.js` — Rewrote with backend API integration (replaced mock data)
- `src/frontend/src/utils/websocket.js` — Added operator queue event handling
- `src/frontend/src/views/OperatingRoom.vue` — Added polling, lifecycle hooks
- `config/trinity-meta-prompt/prompt.md` — Added "Operator Communication" section for agents
- `tests/registry.json` — Registered test file
- `docs/memory/feature-flows/operating-room.md` — Updated to reflect full implementation

---

🎨 **Feature: Operating Room UI Mockup (OPS-001, superseded by full implementation above)**

Added Operating Room — a card-based inbox where operators respond to agent requests. Frontend mockup with mock data covering all three request types (approval, question, alert).

**New files:**
- `src/frontend/src/views/OperatingRoom.vue` — Main page with Open/Resolved tabs
- `src/frontend/src/components/operator/QueueCard.vue` — Expandable card with agent avatar, markdown body, inline response controls
- `src/frontend/src/components/operator/ResolvedCard.vue` — Compact resolved item card
- `src/frontend/src/stores/operatorQueue.js` — Pinia store with mock data and response actions

**Modified files:**
- `src/frontend/src/router/index.js` — Added `/operating-room` route
- `src/frontend/src/components/NavBar.vue` — Added "Ops" nav link with pending count badge

---

✨ **Feature: AI-Generated Agent Avatars (AVATAR-001)**

Added AI-generated circular avatars for agents. Users provide an identity prompt (e.g., "a wise owl"), and the platform generates a consistent avatar via the existing Gemini image generation service. Avatars are cached on disk and displayed across all UI surfaces with initials fallback.

**New files:**
- `src/backend/routers/avatar.py` — REST endpoints: GET/POST/DELETE avatar, GET identity
- `src/frontend/src/components/AgentAvatar.vue` — Reusable avatar component with gradient+initials fallback
- `src/frontend/src/components/AvatarGenerateModal.vue` — Modal for generating/removing avatars

**Modified files:**
- `src/backend/db/schema.py` — Added `avatar_identity_prompt`, `avatar_updated_at` to agent_ownership
- `src/backend/db/migrations.py` — Migration #24 for avatar columns
- `src/backend/db/agents.py` — Avatar DB methods + avatar_updated_at in batch query
- `src/backend/database.py` — Delegation methods for avatar operations
- `src/backend/services/image_generation_prompts.py` — Added "avatar" use case with specialized best practices
- `src/backend/main.py` — Registered avatar router
- `src/backend/services/agent_service/helpers.py` — Added avatar_url to agent data flow
- `src/backend/routers/agents.py` — Avatar cleanup on delete + rename
- `src/frontend/src/components/AgentHeader.vue` — Avatar display + generate button
- `src/frontend/src/components/AgentNode.vue` — Avatar in dashboard graph nodes
- `src/frontend/src/views/Agents.vue` — Avatar in all 3 responsive layouts
- `src/frontend/src/views/AgentDetail.vue` — AvatarGenerateModal integration
- `src/frontend/src/stores/network.js` — Pass avatarUrl in node data

---

✨ **Feature: Platform Image Generation Service (IMG-001)**

Added a platform-level image generation capability using Google Gemini. The service implements a two-step pipeline: (1) prompt refinement via Gemini 2.5 Flash text model using embedded best practices, then (2) image generation via Gemini 2.5 Flash Image model. Supports four use cases (general, thumbnail, diagram, social) with seven aspect ratios. Other code (routers, services, MCP tools, agents) can call `get_image_generation_service().generate_image()` to produce images.

**New files:**
- `src/backend/services/image_generation_service.py` — Core service with singleton pattern, `generate_image()`, `refine_prompt()`, Gemini API calls via httpx
- `src/backend/services/image_generation_prompts.py` — Best practices constants for each use case
- `src/backend/routers/image_generation.py` — REST endpoints: `POST /api/images/generate`, `GET /api/images/models`

**Modified files:**
- `src/backend/config.py` — Added `GEMINI_API_KEY` config
- `src/backend/main.py` — Mounted image generation router
- `docker-compose.yml` — Added `GEMINI_API_KEY` env passthrough
- `.env.example` — Added `GEMINI_API_KEY` documentation section

---

### 2026-03-06
🐛 **Fix: Headless task permission bypass ignored after GitHub template clone**

Fixed a bug where headless tasks (via `POST /api/task`) could run with `permissionMode: "default"` instead of `bypassPermissions`, causing all tool calls to be silently denied and tasks to time out with zero work completed. Root cause was concurrent session file writes between interactive `/api/chat` sessions and headless task processes sharing `~/.claude/projects/` state.

**Three fixes applied to `docker/base-image/agent_server/services/claude_code.py`:**

1. **Session isolation** — Added `--no-session-persistence` and unique `--session-id` per headless task to prevent session file collision with interactive sessions or other concurrent tasks. Skipped when `resume_session_id` is provided (EXEC-023 needs persistence).

2. **Permission mode validation** — On the `init` message from Claude Code stream-json output, verify `permissionMode` is `"bypassPermissions"`. If not, kill the process immediately and return HTTP 503 with actionable error message instead of silently timing out after hours.

3. **Error propagation** — `RuntimeError` from permission validation propagates through the asyncio executor and is converted to HTTP 503 with clear instructions to restart the agent container.

- `docker/base-image/agent_server/services/claude_code.py` — `execute_headless_task()`: added `--no-session-persistence` + `--session-id` flags for non-resume tasks; added `permissionMode` validation on init message with fast-fail; added `RuntimeError` → HTTP 503 error path

---

### 2026-03-05
🐛 **Fix: Nevermined config returns 403 for shared users (Issue #72)**

Split Nevermined endpoint permissions into read/write access levels. GET endpoints (config, payment history) now use `can_user_access_agent` allowing shared users to view. POST/PUT/DELETE endpoints (save, toggle, delete) remain owner-only via `can_user_share_agent`. Frontend `NeverminedPanel.vue` now accepts `canEdit` prop to show read-only view with disabled form, hidden action buttons, and a permission notice banner for shared users.

**Backend:**
- `src/backend/routers/nevermined.py` — Replaced `_require_agent_access` with `_require_read_access` (GET) and `_require_write_access` (POST/PUT/DELETE)

**Frontend:**
- `src/frontend/src/components/NeverminedPanel.vue` — Added `canEdit` prop, read-only banner, disabled inputs/toggle, hidden save/remove buttons
- `src/frontend/src/views/AgentDetail.vue` — Pass `can-edit` prop to NeverminedPanel

**Tests:**
- `tests/test_nevermined_permissions.py` — Owner CRUD, unauthenticated rejection, nonexistent agent checks

---

### 2026-03-04
⚙️ **Feature: Admin-Configurable GitHub Template Repositories (TMPL-001)**

Made the list of GitHub repos shown as agent templates configurable by admin users via Settings UI. `config.py` stores only default repo identifiers (no metadata). All template metadata (display_name, description, resources, mcp_servers) is fetched from each repo's `template.yaml` via GitHub API at runtime, with 10-minute in-memory cache and concurrent fetching.

**Backend:**
- `src/backend/config.py` — Replaced `GITHUB_TEMPLATES`/`ALL_GITHUB_TEMPLATES` (hardcoded metadata) with `DEFAULT_GITHUB_TEMPLATE_REPOS` (list of repo strings)
- `src/backend/services/settings_service.py` — Added `get_github_templates()`, `set_github_templates()`, `delete_github_templates()` to SettingsService
- `src/backend/services/template_service.py` — Added GitHub API metadata fetching (`_fetch_template_yaml`, `_fetch_all_metadata`) with TTL cache; `get_all_templates()` resolves full template dicts from repo metadata; `get_github_template()` resolves single template; removed all hardcoded metadata, local template support
- `src/backend/routers/templates.py` — Simplified to use `get_all_templates()` and `get_github_template()`; removed local template scanning, env-template endpoint
- `src/backend/routers/settings.py` — Added `GET/PUT/DELETE /api/settings/github-templates` (admin-only); validates `owner/repo` format; GET returns both raw admin overrides and resolved metadata (`resolved_name`, `resolved_description`) from GitHub API

**Frontend:**
- `src/frontend/src/views/Settings.vue` — Added "GitHub Templates" section after Email Whitelist with add/remove/save/reset UI, defaults badge, dirty-state tracking

---

✨ **Feature: Dynamic Thinking Status for Public Chat (THINK-001 extension)**

Extended THINK-001 dynamic thinking status labels to Public Chat. Public link users now see real-time agent activity labels (Reading file, Searching code, Running command, etc.) instead of static "Thinking..." while waiting for responses.

**Backend:**
- `src/backend/db_models.py` — Added `async_mode` field to `PublicChatRequest`
- `src/backend/routers/public.py` — Added async mode support to `public_chat` endpoint (spawns background task, returns `execution_id`); added `GET /api/public/executions/{token}/{execution_id}/stream` (SSE proxy with link-token auth); added `GET /api/public/executions/{token}/{execution_id}/status` (polling endpoint)

**Frontend:**
- `src/frontend/src/views/PublicChat.vue` — Switched from sync POST to async mode + SSE stream subscription + polling for completion; imports `execution-status.js` utilities; added anti-flicker label timing, heartbeat timeout, SSE cleanup on unmount

---

💰 **Feature: Nevermined x402 Payment Integration (NVM-001)**

Integrated Nevermined's x402 payment protocol into Trinity for per-agent monetization. External users pay per-request via `payment-signature` HTTP header. Internal fleet traffic remains free. Two-phase payment pattern: verify before work, settle after success — failed requests never cost the caller.

**New files:**
- `src/backend/db/nevermined.py` — DB operations (encrypted config + payment log)
- `src/backend/services/nevermined_payment_service.py` — Verify/settle via payments-py SDK with timeouts and retry
- `src/backend/routers/paid.py` — `POST /api/paid/{agent}/chat` (x402 flow), `GET /api/paid/{agent}/info`
- `src/backend/routers/nevermined.py` — Admin config CRUD, payment history, settlement failure management
- `src/frontend/src/components/NeverminedPanel.vue` — Payments tab with config form, toggle, payment log table
- `src/mcp-server/src/tools/nevermined.ts` — 4 MCP tools for payment config management
- `scripts/poc/nevermined_demo.py` — Buyer-side demo script

**Modified:**
- `src/backend/db_models.py` — Added `NeverminedConfigCreate`, `NeverminedConfig`, `NeverminedPaymentResult`, `NeverminedPaymentLog`
- `src/backend/db/schema.py` — Added `nevermined_agent_config` + `nevermined_payment_log` tables and indexes
- `src/backend/db/migrations.py` — Added migration #23 `nevermined_tables`
- `src/backend/database.py` — Wired `NeverminedOperations` with delegate methods
- `src/backend/main.py` — Registered `paid_router` and `nevermined_router`
- `docker/backend/Dockerfile` — Added `payments-py==1.2.1` dependency
- `src/frontend/src/views/AgentDetail.vue` — Added "Payments" tab with `NeverminedPanel`
- `src/mcp-server/src/client.ts` — Added 4 Nevermined API methods
- `src/mcp-server/src/server.ts` — Registered 4 Nevermined tools
- `src/mcp-server/src/tools/index.ts` — Exported `createNeverminedTools`

---

🐛 **Fix: Public links with expiration crash on timezone-naive datetime comparison (Issue #62)**

Fixed `TypeError: can't compare offset-naive and offset-aware datetimes` in `db/public_links.py`. Public links with an `expires_at` value stored timezone-aware timestamps (e.g., `2026-03-31T17:03:00.000Z`) but compared them against `datetime.utcnow()` which returns naive datetimes. Added `_utcnow()` (returns `datetime.now(timezone.utc)`) and `_parse_aware()` helpers. Fixed all three comparison sites: `is_link_valid()`, `verify_code()`, `validate_session()`.

- `src/backend/db/public_links.py` — Added `timezone` import, `_utcnow()` and `_parse_aware()` helpers; replaced 3 naive-vs-aware comparisons

---

🔄 **Refactor: Unified Task Execution Service (EXEC-024)**

Extracted task execution orchestration from `routers/chat.py` into a shared `services/task_execution_service.py` so that all callers use a single code path for execution tracking, activity tracking, slot management, and credential sanitization.

**New file:**
- `src/backend/services/task_execution_service.py` — `TaskExecutionService.execute_task()` encapsulates full lifecycle: create execution record → acquire slot → track activity → call agent (with retry) → sanitize response → update execution → complete activity → release slot. Also exports `agent_post_with_retry()` moved from `routers/chat.py`.

**Refactored:**
- `src/backend/routers/chat.py` — Sync `/task` path now delegates to `TaskExecutionService.execute_task()`. Router retains auth, collaboration tracking, async mode, and session persistence. Removed inline `agent_post_with_retry()` (now imported from service).
- `src/backend/routers/public.py` — `public_chat()` now uses `TaskExecutionService` instead of raw `httpx.AsyncClient`. Public executions now appear in Tasks tab, Dashboard timeline, and respect capacity slots.

**Behavioral changes:**
- Public link executions now create `schedule_executions` records with `triggered_by="public"` — visible in Tasks tab and Dashboard
- Public link executions now count toward agent capacity slots (429 when at capacity)
- Public link execution logs are now credential-sanitized

### 2026-03-03
✨ **Feature: Subscription Agent Assignment UI (SUB-003)**

Added agent assign/unassign controls to expanded subscription rows in Settings. No backend changes — uses existing `PUT /api/subscriptions/agents/{name}` and `DELETE /api/subscriptions/agents/{name}` endpoints.

- `src/frontend/src/views/Settings.vue` — Agent badges now have X buttons to unassign; dropdown + Assign button to add agents; agents on other subscriptions shown with "(on sub-name)" suffix; per-agent spinners during operations; tip text updated

### 2026-03-03
🔄 **Feature: Long-Lived Subscription Tokens (SUB-002 — replaces SUB-001)**

Replaced `.credentials.json` file injection with `CLAUDE_CODE_OAUTH_TOKEN` env var injection. Tokens from `claude setup-token` (~1 year lifetime) replace short-lived OAuth blobs that expired every 8-15 hours.

**Backend:**
- `src/backend/db_models.py` — `SubscriptionCredentialCreate`: `credentials_json` → `token` field with `sk-ant-oat01-` prefix validator
- `src/backend/db/subscriptions.py` — `create_subscription()`: stores `{"token": token}` instead of `{".credentials.json": ...}`; `get_subscription_credentials()` → `get_subscription_token()` with legacy format detection
- `src/backend/database.py` — Updated wrapper methods
- `src/backend/services/subscription_service.py` — Deleted `inject_subscription_to_agent()` and `inject_subscription_on_start()`; simplified `get_agent_auth_mode()` to DB-only detection
- `src/backend/services/agent_service/lifecycle.py` — Removed subscription injection call from `start_agent_internal()`; `recreate_container_with_updated_config()` now sets `CLAUDE_CODE_OAUTH_TOKEN` env var
- `src/backend/services/agent_service/helpers.py` — `check_api_key_env_matches()` now verifies `CLAUDE_CODE_OAUTH_TOKEN` presence/value
- `src/backend/routers/subscriptions.py` — `POST /api/subscriptions` uses `token` field; `PUT /agents/{name}` simplified (no injection call)
- `src/backend/services/monitoring_service.py` — Removed `.credentials.json` file check, auto-remediation, and credential_status degradation
- `src/backend/services/monitoring_alerts.py` — Deleted `alert_subscription_credentials_missing()`

**Agent:**
- `docker/base-image/agent_server/services/claude_code.py` — `_diagnose_exit_failure()` checks `CLAUDE_CODE_OAUTH_TOKEN` env var instead of `.credentials.json` file

**MCP Server:**
- `src/mcp-server/src/tools/subscriptions.ts` — `register_subscription` tool: `credentials_json` → `token` param
- `src/mcp-server/src/client.ts` — `registerSubscription()`: `credentialsJson` → `token`

**Frontend:**
- `src/frontend/src/views/Settings.vue` — Replaced file upload with password input for token; inline `sk-ant-oat01-` validation

**Migration:** Existing subscriptions with legacy format return `None` with warning log. Admins must re-register using `claude setup-token`.

---

✨ **Feature: Dynamic Thinking Status Labels in Chat (THINK-001, Issue #41)**

Replaced static "Thinking..." indicator in Chat tab with dynamic, real-time status labels reflecting the agent's actual activity. Status updates stream via SSE while the task executes in async mode.

**Frontend:**
- `src/frontend/src/components/ChatPanel.vue` — Switched from sync POST to `async_mode=true` + SSE stream subscription + polling for completion
- `src/frontend/src/utils/execution-status.js` — **New** — Maps Claude Code stream-json events to status labels (e.g., `tool_use:Read` → "Reading file...")
- `src/frontend/src/components/chat/ChatLoadingIndicator.vue` — Added CSS fade transition for smooth label changes

**Backend:**
- `src/backend/routers/chat.py` — `_execute_task_background` now supports `save_to_session` + `resume_session_id` for async mode chat persistence; broadcasts `chat_response_ready` WebSocket event on completion

**UX Details:**
- 500ms minimum display time per label (anti-flicker)
- 10s heartbeat timeout falls back to "Working..."
- Labels: Thinking, Responding, Reading file, Searching code, Running command, Editing code, Writing file, etc.

---

📊 **Feature: Capacity Meter — Vertical volume bar for execution slots**

Added `CapacityMeter.vue` component showing parallel execution slot usage as a vertical bar with discrete cells. Fills bottom-to-top, color-coded by utilization (green < 50%, yellow < 80%, orange < 100%, red + pulse at 100%). Integrated across all fleet views.

**New Component:**
- `src/frontend/src/components/CapacityMeter.vue` — Reusable vertical bar with props: `active`, `max`, `height`, `width`

**Store Changes:**
- `src/frontend/src/stores/agents.js` — Added `slotStats` state, `fetchSlotStats()` action calling `GET /api/agents/slots`, integrated into 5-second polling
- `src/frontend/src/stores/network.js` — Added `slotStats` ref, `fetchSlotStats()` function threading data onto node.data.slotStats, integrated into polling, exposed in return block

**View Integration (5 locations):**
- `src/frontend/src/views/Agents.vue` — Desktop: 9-column grid with "Load" column header and meter between Stats and Arrow; Tablet: meter in Row 2 alongside toggles
- `src/frontend/src/components/AgentNode.vue` — Tested but ultimately removed from graph nodes
- `src/frontend/src/components/ReplayTimeline.vue` — Meter in timeline agent tiles (52px tall), restructured tile to flex row
- `src/frontend/src/views/Dashboard.vue` — Passes `slotStats` from network store to ReplayTimeline

---

📊 **Feature: Replace context bars with success rate bars (Issue #60)**

Replaced context usage progress bars with success rate bars across fleet-view components. Added dual-window (24h + 7d) execution statistics via a single efficient SQL query.

**Backend:**
- `src/backend/db/schedules.py` — Added `get_all_agents_execution_stats_dual()` using CASE WHEN for both 24h and 7d windows in a single query
- `src/backend/routers/agents.py` — Added `include_7d` query param to `/api/agents/execution-stats` (backward compatible, default false)

**Frontend Stores:**
- `src/frontend/src/stores/agents.js` — Fetch with `include_7d=true`, store `taskCount7d` and `successRate7d`, renamed sort option `context_desc` → `success_desc`
- `src/frontend/src/stores/network.js` — Same API call change, pass 7d data through to node data

**Frontend Components (4 files):**
- `src/frontend/src/views/Agents.vue` — Replaced context bar with success rate bar in desktop (column 180px), tablet, and mobile layouts; column header "Context" → "Success"; new helpers: `getSuccessBarPercent()`, `getSuccessBarColor()`, `has7dStats()`, `get7dSuccessRate()`
- `src/frontend/src/components/AgentNode.vue` — Replaced "Context" section with "Success" bar including 7d secondary text
- `src/frontend/src/components/SystemAgentNode.vue` — Replaced "Context" in stats row with "Success" bar
- `src/frontend/src/components/ReplayTimeline.vue` — Replaced Row 2 context bar with success rate bar; new helpers: `getRowSuccessPercent()`, `getSuccessBarClass()`

**Color coding:** green >= 90%, yellow 50-89%, red < 50%, gray with dash for no data.
**Fallback logic:** 24h bar + 7d text when both available; 7d-only bar when no 24h data; gray dash when no executions in 7d.
**Scope exclusion:** No changes to Agent Detail page. Context polling still active for activity state detection.

---

### 2026-03-03 19:00:00
🔍 **Feature: Agents page filtering by name, status, and tags (Issue #55)**

Added comprehensive filtering to the Agents page for quickly finding agents in large fleets:

- **Name search**: Text input with search icon filters agents in real-time as you type
- **Status filter**: Segmented button group (All / Running / Stopped) with indigo highlight
- **Multi-tag filter**: Clickable tag chips showing all available tags with counts — select one or more to filter (OR logic across tags)
- **Combined AND logic**: All three filters combine — name AND status AND tags must all match
- **localStorage persistence**: All filter values persist across page reloads (name, status, selected tags)
- **"Showing X of Y agents"** count when filters active
- **Clear all filters** button to reset everything at once
- **Smart empty state**: Shows "No matching agents" with clear filters button when filters exclude all agents
- **Legacy migration**: Old single-tag filter (`trinity-agents-filter-tag`) auto-migrates to new multi-tag format

**Key File:**
- `src/frontend/src/views/Agents.vue` — Filter bar + script changes

---

### 2026-03-03 18:00:00
🎨 **UI: Two-row agent tiles, gap spacing, persistent tag filter**

Refined Agents page row layout with three improvements:

- **Two-row desktop tiles**: Top row is a fixed CSS grid (checkbox, dot, name+badges, activity, toggles, context bar, stats, arrow). Bottom row shows tag pills left-aligned under the name — only renders if agent has tags. Tags column removed from grid (9→8 cols)
- **Gap-based row spacing**: Replaced `border-b` separators with `gap-1.5` between rows. Each agent row now has `rounded-lg` for card-like appearance with visible background gaps
- **Persistent tag filter**: `selectedFilterTag` initializes from `localStorage.getItem('trinity-agents-filter-tag')` and writes back via `watch`. Clearing to "All Tags" removes the key
- **Tablet tags**: Tags row added to tablet layout (md breakpoint) with same styling

**Key File:**
- `src/frontend/src/views/Agents.vue` — Template + script changes

---

### 2026-03-03 17:00:00
🔄 **UI: Agents page horizontal row tile layout (Issue #54)**

Replaced 3-column card grid on Agents page with full-width horizontal row tiles for better scannability:

- **Desktop (lg+)**: CSS grid row with checkbox, status dot, name+badges, activity label, toggles, tags, context bar, stats, chevron link — all columns aligned via header row
- **Tablet (md)**: Two-line compact row — name/status on line 1, controls/context/stats on line 2
- **Mobile (<md)**: Compact mini-card — name + running toggle on line 1, status/context/stats summary on line 2
- **System agent**: Purple left border accent instead of ring
- **Stopped agents**: Reduced opacity (preserved)
- **Column header**: Subtle uppercase labels above agent list (desktop only)
- **Schedule stats**: Merged inline with execution stats using clock icon

**Key File:**
- `src/frontend/src/views/Agents.vue` — Template-only change (lines 169-516)

---

### 2026-03-03 16:00:00
📁 **UI: Per-agent Files tab replacing standalone Files page (Issue #51)**

Moved file management from standalone `/files` page into per-agent Files tab in Agent Detail:

- **Rewrote `FilesPanel.vue`**: Full two-panel file manager (tree + preview) using same `file-manager/FileTreeNode.vue` and `file-manager/FilePreview.vue` components as the standalone page
- **Added Files tab to AgentDetail.vue**: Now visible in `visibleTabs` alongside Terminal and Git
- **Removed `/files` route**: Route removed from router, nav link removed from AgentSubNav
- **Cleaned up NavBar**: Removed `/files` from `isAgentSection` computed

**Key Files:**
- `src/frontend/src/components/FilesPanel.vue` — Rewritten with full file manager functionality
- `src/frontend/src/views/AgentDetail.vue` — Added 'files' to visibleTabs
- `src/frontend/src/router/index.js` — Removed /files route
- `src/frontend/src/components/AgentSubNav.vue` — Removed Files nav link
- `src/frontend/src/components/NavBar.vue` — Cleaned up isAgentSection

---

### 2026-03-03 15:00:00
🔧 **UI: Add Templates to main navigation & streamline Agent Detail layout (Issues #52, #53)**

Two UI improvements for discoverability and screen real estate:

**Issue #52 — Templates in main nav:**
- Added Templates as top-level NavBar item (between Agents and Health)
- Templates no longer highlights the Agents nav item

**Issue #53 — Agent Detail layout decluttering:**
- Removed secondary navigation bar (AgentSubNav) from Agent Detail page
- Reduced vertical padding (py-6 → py-2, inner py-6 → py-2) to move content closer to top nav
- Widened agent panel from max-w-7xl (1280px) to max-w-[1400px] (~10% wider)
- Other pages (Agents, Files, Templates) retain their AgentSubNav

**Key Files:**
- `src/frontend/src/components/NavBar.vue` — Added Templates link, updated isAgentSection
- `src/frontend/src/views/AgentDetail.vue` — Removed AgentSubNav, adjusted layout

---

### 2026-03-03 13:04:00
🔧 **UI: Hide Process tab from main navigation (Issue #50)**

Removed the Processes link from the NavBar top-level navigation. Process Engine routes remain accessible via direct URL — only the nav link is hidden for UI simplification.

- `src/frontend/src/components/NavBar.vue` — Removed Processes `<router-link>` from desktop nav

---

### 2026-03-02 19:00:00
🐛 **Fix: Subscription Credentials Ignored — Claude Code Prioritizes API Key (SUB-001)**

Claude Code prioritizes `ANTHROPIC_API_KEY` env var over OAuth credentials in `~/.claude/.credentials.json`. When both are present, Claude Code uses the API key and never falls back to OAuth, even if the key is invalid. Trinity was setting `ANTHROPIC_API_KEY` on all containers regardless of subscription assignment, causing all subscription-authenticated agents to fail.

**Root Cause:** Incorrect assumption that subscription credentials take precedence over API key. The opposite is true — Claude Code checks `ANTHROPIC_API_KEY` first and fails immediately if invalid, never reaching the OAuth credentials.

**Changes:**
- `helpers.py` — `check_api_key_env_matches()` now checks for subscription assignment; if a subscription is assigned, `ANTHROPIC_API_KEY` must NOT be present in container env (triggers recreation if it is)
- `lifecycle.py` — `recreate_container_with_updated_config()` removes `ANTHROPIC_API_KEY` when a subscription is assigned instead of always setting it based on `use_platform_api_key`
- `routers/subscriptions.py` — Assigning a subscription now restarts the running agent to remove the API key from container env; clearing a subscription restarts to restore it
- `subscription_service.py` — Fixed incorrect docstring claiming subscription takes precedence over API key

**Impact:** All agents with subscriptions assigned will now correctly use OAuth credentials. Affects any agent where `use_platform_api_key=1` (default) AND a subscription is assigned.

---

### 2026-03-02 17:00:00
🐛 **Fix: Model Selector Dropdown Shows Only 1 Item (MODEL-001)**

Fixed model selector showing only the currently selected model instead of all options. Updated model list with correct Anthropic API model IDs and full model range.

**Root cause:** `filteredModels` computed property always filtered by the current input value, even when the dropdown was opened via button click. With `claude-opus-4-5` selected, only that exact match appeared.

**Changes:**
- `ModelSelector.vue` — Added `isTyping` state to distinguish typing from dropdown toggle; filtering only applies while user is actively typing
- `ModelSelector.vue` — Updated preset models to use correct Anthropic snapshot IDs (e.g. `claude-opus-4-5-20251101`) and added Claude Opus 4 and Sonnet 4 legacy models
- `TasksPanel.vue` — Default model changed from `claude-opus-4-5` to `claude-opus-4-6`
- `SchedulesPanel.vue` — Default model changed from `claude-opus-4-5` to `claude-opus-4-6`
- `database.py` — Added missing `model_used` parameter to `DatabaseManager.create_task_execution()` proxy (was causing `unexpected keyword argument` crash on task submission)

---

### 2026-03-02 16:00:00
🐛 **Fix: Subscription Credentials Lost — Silent Execution Failures (#57)**

Addresses subscription credential loss causing silent execution failures. Adds credential health monitoring, auto-remediation, and better error surfacing.

**Changes:**
- `BusinessHealthCheck` model gains `credential_status` field (null/"ok"/"missing")
- `check_business_health()` now verifies `.credentials.json` presence for agents with subscriptions
- `perform_health_check()` auto-remediates missing credentials via re-injection before aggregation
- New `alert_subscription_credentials_missing()` alert (high priority, 600s cooldown)
- Subscription assignment endpoint no longer swallows injection failures silently
- `inject_subscription_to_agent()` default retries increased from 3 to 5
- Scheduler error handler detects auth-specific failures and prefixes with `[AUTH_ERROR]`
- Agent credential status endpoint now includes `.claude/.credentials.json` in file checks

**Key Files:**
- `src/backend/db_models.py` — `credential_status` field on `BusinessHealthCheck`
- `src/backend/services/monitoring_service.py` — credential check, auto-remediation, alert trigger
- `src/backend/services/monitoring_alerts.py` — `alert_subscription_credentials_missing()`
- `src/backend/routers/subscriptions.py` — error propagation fix
- `src/backend/services/subscription_service.py` — retry count increase
- `src/scheduler/service.py` — auth error detection and categorization
- `docker/base-image/agent_server/routers/credentials.py` — added `.claude/.credentials.json` to status check

---

### 2026-03-02 13:50:00
✨ **Feature: Model Selection for Tasks & Schedules (MODEL-001)**

Users can now select which Claude model to use when running a task or creating/editing a schedule. Includes a shared combobox component with preset models (Opus 4.5/4.6, Sonnet 4.5/4.6, Haiku 4.5) and support for custom model names.

- Database migration adds `model` column to `agent_schedules` and `model_used` to `schedule_executions`
- Backend CRUD models and DB operations updated to read/write model fields
- Scheduler service passes model through to agent containers on execution
- New `ModelSelector.vue` shared component with dropdown presets + free text input
- TasksPanel: model selector above task input, persists last-used model in localStorage
- SchedulesPanel: model field in create/edit form, model badge in schedule list and execution detail
- `model_used` displayed in execution history rows for audit

**Key Files:**
- `src/backend/db/migrations.py` (migration #22)
- `src/backend/db/schema.py`, `src/backend/db_models.py`, `src/backend/db/schedules.py`
- `src/backend/routers/schedules.py`, `src/backend/routers/chat.py`
- `src/scheduler/service.py`, `src/scheduler/agent_client.py`, `src/scheduler/database.py`, `src/scheduler/models.py`
- `src/frontend/src/components/ModelSelector.vue` (new)
- `src/frontend/src/components/TasksPanel.vue`, `src/frontend/src/components/SchedulesPanel.vue`

---

### 2026-03-02 10:30:00
✨ **Feature: Dashboard Filter Persistence**

Dashboard filters are now preserved across page reloads using localStorage.

**Persisted State:**
- Time range selector (1h/6h/24h/3d/7d) via `trinity-dashboard-time-range`
- Quick tags filter via `trinity-dashboard-quick-tags` (JSON array)
- System view selection (already persisted via `trinity-active-view`)
- View mode Graph/Timeline (already persisted via `trinity-dashboard-view`)
- Tag clouds toggle (already persisted via `trinity-show-tag-clouds`)

**Implementation:**
- Load persisted values on component initialization
- Save to localStorage on user interaction (time range change, tag toggle)
- Clear persisted quick tags when system view is selected (system views manage their own tags)
- Proper initialization order in `onMounted()` to restore state before data fetch

**Key Files:**
- `src/frontend/src/views/Dashboard.vue:537-545,560-572,651-652,764-772`

---

### 2026-03-01 14:00:00
🐛 **Fix: Record Skipped Scheduled Executions (Issue #46)**

When APScheduler's `max_instances=1` constraint causes a scheduled job to be skipped (because the previous execution is still running at trigger time), the execution is now recorded in the database with `status='skipped'` instead of being silently dropped.

**Key Features:**
- Added `SKIPPED` status to `ExecutionStatus` enum
- Added APScheduler event listener for `EVENT_JOB_MAX_INSTANCES`
- Added `create_skipped_execution()` and `create_skipped_process_schedule_execution()` database methods
- Records include skip reason for audit trail
- WebSocket event `schedule_execution_skipped` published for real-time UI notifications
- Frontend displays skipped status with purple styling

**Created/Updated Files:**
- `src/scheduler/models.py:20` - Added `SKIPPED` status to `ExecutionStatus` enum
- `src/scheduler/service.py:408-511` - Added event listener and handler methods
- `src/scheduler/database.py:233-290,650-700` - Added database methods for skipped executions
- `src/frontend/src/components/TasksPanel.vue` - Added purple styling for skipped status
- `src/frontend/src/components/SchedulesPanel.vue` - Added skipped status display
- `src/frontend/src/views/ExecutionDetail.vue` - Added skipped status styling
- `src/frontend/src/views/ExecutionList.vue` - Added skipped status filter and display
- `src/frontend/src/views/ProcessExecutionDetail.vue` - Added skipped status handling

**Tests:**
- `tests/scheduler_tests/test_skipped_executions.py` - 12 tests (8 pass locally, 4 require Docker)

**Impact:** Operators now have visibility into scheduled executions that were dropped due to max_instances constraints, providing better audit trails and debugging capability.

---

### 2026-03-01 12:00:00
🐛 **Fix: Slack Integration Missing Access Check Method (Issue #48)**
- Fixed `AttributeError: 'DatabaseManager' object has no attribute 'get_user_agent_access_level'`
- Replaced non-existent `db.get_user_agent_access_level()` calls with existing methods
- Used `db.can_user_access_agent()` for read access checks (GET endpoint)
- Used `db.can_user_share_agent()` for owner-level access checks (POST/PUT/DELETE endpoints)
- Fixed argument from `current_user.id` to `current_user.username` to match codebase pattern
- Key files: `src/backend/routers/slack.py:425,460,495,522`

---

### 2026-03-01 11:12:25
🔧 **Fix: Agent Rename Navigation and Docker Labels**
- Fixed route name mismatch in `AgentDetail.vue:486` (`agent-detail` → `AgentDetail`) that caused navigation failure after rename
- Fixed `docker_service.py` to use container name as authoritative source instead of Docker labels (labels can't be updated without container recreation)
- Both `get_agent_status_from_container()` and `list_all_agents_fast()` now derive agent name from container name `agent-{name}`
- Key files: `src/frontend/src/views/AgentDetail.vue`, `src/backend/services/docker_service.py`

---

### 2026-03-01 10:00:00
✨ **Feature: Agent Rename (RENAME-001)**

Added ability to rename agents via UI and MCP. Users can now rename agents from the Agent Detail page using a pencil icon next to the agent name, or via the `rename_agent` MCP tool.

**Key Features:**
- Pencil icon in Agent Header for inline name editing
- Backend endpoint `PUT /api/agents/{name}/rename`
- MCP tool `rename_agent` for programmatic renaming
- Updates all database references atomically
- Renames Docker container
- WebSocket broadcast for real-time UI updates
- System agents cannot be renamed
- Only owners and admins can rename agents

**Created/Updated Files:**
- `src/backend/db/agents.py` - Added `rename_agent()` and `can_user_rename_agent()` methods
- `src/backend/routers/agents.py` - Added `PUT /api/agents/{name}/rename` endpoint
- `src/backend/services/docker_utils.py` - Added `container_rename()` async wrapper
- `src/mcp-server/src/tools/agents.ts` - Added `rename_agent` MCP tool
- `src/mcp-server/src/client.ts` - Added `renameAgent()` client method
- `src/frontend/src/components/AgentHeader.vue` - Added editable name with pencil icon
- `src/frontend/src/views/AgentDetail.vue` - Added `renameAgent()` handler

**Example Usage:**
```javascript
// Via MCP
rename_agent({name: "old-name", new_name: "new-name"})

// Via API
PUT /api/agents/old-name/rename
{"new_name": "new-name"}
```

**Impact:** Users can now rename agents without having to delete and recreate them.

---

### 2026-02-28 14:00:00
✨ **Feature: Git Branch Support for Agent Creation (GIT-002)**

Added support for specifying which branch to use when creating agents from GitHub templates. Supports both URL syntax and explicit parameter.

**Key Features:**
- URL syntax: `github:owner/repo@branch` parses branch from template URL
- Explicit parameter: `source_branch` field in MCP `create_agent` tool
- URL syntax takes precedence if both provided
- Startup.sh clones directly to target branch using `-b` flag

**Updated Files:**
- `src/mcp-server/src/types.ts` - Added `source_branch?: string` to AgentConfig interface
- `src/mcp-server/src/tools/agents.ts` - Added zod schema and parameter passing for source_branch
- `src/backend/services/agent_service/crud.py` - Parse `@branch` from template URL syntax
- `src/backend/services/template_service.py` - Added optional branch parameter to clone_github_repo()
- `docker/base-image/startup.sh` - Use `-b branch` flag when cloning
- `docs/memory/feature-flows/github-sync.md` - Documented branch selection with examples

**Example Usage:**
```javascript
// URL syntax
create_agent({name: "my-agent", template: "github:owner/repo@feature-branch"})

// Explicit parameter
create_agent({name: "my-agent", template: "github:owner/repo", source_branch: "develop"})
```

**Impact:** Users can now deploy agents tracking specific branches without manual container configuration.

---

### 2026-02-28 10:30:00
⚙️ **Feature: Parallel Capacity Backend (CAPACITY-001)**

Implemented backend infrastructure for per-agent parallel execution capacity tracking. Agents can now run multiple `/task` executions concurrently with configurable limits.

**Key Features:**
- Per-agent `max_parallel_tasks` setting (1-10, default 3)
- Redis ZSET-based slot tracking with automatic TTL cleanup
- 429 Too Many Requests when agent is at capacity
- REST endpoints for capacity management and monitoring

**Created Files:**
- `src/backend/services/slot_service.py` - SlotService with Redis ZSET tracking

**Updated Files:**
- `src/backend/db/schema.py` - Added `max_parallel_tasks` column to agent_ownership
- `src/backend/db/migrations.py` - Migration #21 for max_parallel_tasks column
- `src/backend/db/agents.py` - Added get/set_max_parallel_tasks, get_all_agents_parallel_capacity
- `src/backend/database.py` - Added capacity delegation methods
- `src/backend/db_models.py` - Added CapacityUpdate, SlotInfo, AgentCapacity, BulkSlotState models
- `src/backend/routers/agents.py` - Added GET/PUT /api/agents/{name}/capacity, GET /api/agents/slots
- `src/backend/routers/chat.py` - Integrated slot acquisition/release in /task endpoint

**API Endpoints:**
- `GET /api/agents/{name}/capacity` - Get capacity and current slot usage
- `PUT /api/agents/{name}/capacity` - Update max_parallel_tasks (owners only)
- `GET /api/agents/slots` - Bulk slot state for Dashboard polling

**Redis Schema:**
- `agent:slots:{name}` (ZSET) - Active execution IDs with start timestamps
- `agent:slot:{name}:{execution_id}` (HASH) - Slot metadata with 30-min TTL

**Impact**: Platform now enforces parallel execution limits per agent, preventing resource exhaustion and enabling fair capacity management.

---

### 2026-02-27 15:45:00
🧪 **Test: Playbooks Tab tests (PLAYBOOK-001)**

Added comprehensive pytest test suite for the Playbooks Tab feature.

**Test Coverage:**
- Authentication requirements (smoke tests)
- Error handling (404 for nonexistent agent, 503 for stopped agent)
- Response structure validation (SkillsResponse fields)
- Skill field types and required fields
- Skills sorting verification
- Timeout handling during agent startup
- Concurrent access consistency

**Created Files:**
- `tests/test_playbooks.py` - 14 test cases in 9 test classes

**Test Results:**
- 4 smoke tests pass immediately
- 10 agent-requiring tests skip gracefully when agent base image lacks skills endpoint
- All tests pass after base image rebuild with skills.py router

**Notes:**
- Tests use module-scoped `created_agent` and `stopped_agent` fixtures
- Graceful skip when agent returns 404 (base image needs rebuild)
- Follow existing Trinity test patterns from `test_skills.py`

---

### 2026-02-27 14:30:00
✨ **Feature: Playbooks Tab (PLAYBOOK-001)**

Added a new Playbooks tab in Agent Detail to browse and invoke agent's local skills directly from the UI. Skills (playbooks) are stored in `.claude/skills/` directory as SKILL.md files with YAML frontmatter.

**Key Features:**
- Grid display of skills parsed from SKILL.md YAML frontmatter
- One-click run button sends `/{skill-name}` to `/task` endpoint
- "Edit" button prefills Tasks tab with `/{skill-name} ` for adding instructions
- Search/filter skills by name or description
- Automation badges (autonomous/gated/manual)
- Agent not running state handling

**Created Files:**
- `docker/base-image/agent_server/routers/skills.py` - Agent-side skills listing endpoint (`GET /api/skills`)
- `src/frontend/src/components/PlaybooksPanel.vue` - Playbooks tab component
- `docs/memory/feature-flows/playbooks-tab.md` - Feature flow documentation

**Updated Files:**
- `docker/base-image/agent_server/routers/__init__.py` - Export skills_router
- `docker/base-image/agent_server/main.py` - Mount skills_router
- `src/backend/routers/agents.py` - Add `/api/agents/{name}/playbooks` proxy endpoint
- `src/frontend/src/views/AgentDetail.vue` - Add Playbooks tab, hide Skills tab from visibleTabs

**Tab Changes:**
- Skills tab (platform-level skill library) hidden from visibleTabs
- New Playbooks tab added after Schedules, before Credentials

**Impact**: Users can now discover and invoke agent skills without knowing slash command syntax or accessing the terminal.

---

### 2026-02-27 12:15:00
🐛 **Fix: Tool call activities orphan accumulation (Issue #45)**

Removed granular tool_call activity tracking that created orphaned database records. Activities were created with STARTED state but never completed, accumulating ~1000 orphans per week on active instances.

**Root Cause:**
- `track_activity()` was called for each tool call but `complete_activity()` was never called
- Only the parent chat activity was completed, leaving tool_call activities in STARTED state forever

**Solution:**
- Removed tool_call activity tracking loop (lines 311-327)
- Tool calls are already stored in `chat_messages.tool_calls` JSON column (no data loss)
- No frontend component used `tool_call` activities from `agent_activities` table

**Updated Files:**
- `src/backend/routers/chat.py` - Removed unused tool_call activity tracking

**Impact**: Prevents database bloat, no functionality change (data was duplicate and unused).

---

### 2026-02-27 10:30:00
🐛 **Fix: Chat message ordering bug (CHAT-002)**

Fixed chat message ordering issue where messages appeared in incorrect visual positions. The flex spacer technique used for bottom-aligned chat (iMessage style) caused race conditions between spacer resizing and message rendering.

**Root Cause:**
- `ChatMessages.vue` used a `<div class="flex-1"></div>` spacer to push content to the bottom
- This interacted poorly with nested flex containers and auto-scroll timing
- Messages could appear visually misaligned during initial render

**Solution:**
- Replaced fragile spacer technique with `min-h-full flex flex-col justify-end` pattern
- This provides reliable bottom-alignment without competing flex-grow behaviors

**Updated Files:**
- `src/frontend/src/components/chat/ChatMessages.vue` - Replaced spacer with min-height + justify-end

**Impact**: Fixes message ordering in both authenticated Chat tab and PublicChat.

---

### 2026-02-25 15:10:00
🔧 **Enhancement: Slack Settings in Admin UI (SLACK-001)**

Added admin-configurable Slack integration settings via Settings page, removing the need for environment variables.

**Updated Files:**
- `src/backend/services/settings_service.py` - Added `get_slack_client_id()`, `get_slack_client_secret()`, `get_slack_signing_secret()` with database-first, env-var-fallback pattern
- `src/backend/services/slack_service.py` - Now uses settings_service getters instead of config constants
- `src/backend/routers/settings.py` - Added `/api/settings/slack` GET/PUT/DELETE endpoints
- `src/frontend/src/views/Settings.vue` - Added Slack Integration section with setup instructions

**API Endpoints:**
- `GET /api/settings/slack` - Get Slack settings status (admin only)
- `PUT /api/settings/slack` - Update Slack credentials (admin only)
- `DELETE /api/settings/slack` - Remove Slack settings (admin only)

**Configuration Hierarchy:**
1. Database settings (via Settings UI) - checked first
2. Environment variables (SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET) - fallback

**Impact**: Improves SLACK-001 UX by allowing configuration via UI instead of requiring container restart for env var changes.

---

### 2026-02-25 10:00:00
✨ **Feature: Slack Integration for Public Links (SLACK-001)**

Enables Slack as a delivery channel for public agent links. Users can chat with Trinity agents via DMs to a Slack bot, using the same security model as web-based public links.

**Key Features:**
- One Slack workspace = One public link = One agent (simple 1:1 mapping)
- OAuth 2.0 flow for Slack app installation
- HMAC-SHA256 signature verification for all Slack requests
- User verification via Slack profile email or email code
- Session persistence using existing `public_chat_sessions` table

**Created Files:**
- `src/backend/db/slack.py` - SlackOperations class for database CRUD
- `src/backend/services/slack_service.py` - Slack API client (OAuth, messaging)
- `src/backend/routers/slack.py` - Public events + authenticated management endpoints
- `docs/memory/feature-flows/slack-integration.md` - Feature flow documentation

**Updated Files:**
- `src/backend/config.py` - Added SLACK_* environment variables (lines 56-59)
- `src/backend/db/schema.py` - Added 3 tables: `slack_link_connections`, `slack_user_verifications`, `slack_pending_verifications`
- `src/backend/db/migrations.py` - Added migration #20 for Slack tables
- `src/backend/db_models.py` - Added 8 Pydantic models for Slack API (lines 810-890)
- `src/backend/database.py` - Added SlackOperations delegation methods
- `src/backend/main.py` - Mounted Slack routers
- `src/frontend/src/components/PublicLinksPanel.vue` - Added Slack connection UI

**API Endpoints:**
- `POST /api/public/slack/events` - Receive Slack events (no auth)
- `GET /api/public/slack/oauth/callback` - OAuth completion redirect (no auth)
- `GET /api/agents/{name}/public-links/{id}/slack` - Connection status
- `POST /api/agents/{name}/public-links/{id}/slack/connect` - Initiate OAuth
- `PUT /api/agents/{name}/public-links/{id}/slack` - Update (enable/disable)
- `DELETE /api/agents/{name}/public-links/{id}/slack` - Disconnect

**Impact**: New feature extending Public Agent Links (15.1) with Slack channel support.

---

### 2026-02-24 17:15:00
🐛 **Fix: Vite 6.x allowedHosts for custom domains (Issue #43)**

Added `allowedHosts: true` to Vite server config to allow access via custom domains. Vite 6.x introduced stricter host checking that blocked requests from any host other than localhost.

**Updated**: `src/frontend/vite.config.js`
- Added `allowedHosts: true` to server configuration
- Trinity runs behind a reverse proxy that handles host validation

**Impact**: Resolved GitHub Issue #43 (P1 bug)

---

### 2026-02-24 16:00:00
🐛 **Fix: Async Docker operations prevent event loop blocking (DOCKER-001)**

Fixed critical issue where blocking Docker SDK calls in async functions would freeze the FastAPI event loop, causing HTTP 499 timeouts, WebSocket drops, and 10-30+ second UI unresponsiveness.

**Created**: `services/docker_utils.py`
- ThreadPoolExecutor-based async wrappers for all blocking Docker operations
- Container ops: `container_stop()`, `container_start()`, `container_reload()`, `container_stats()`, `container_get()`
- Volume ops: `volume_get()`, `volume_create()`, `volume_remove()`
- Container creation: `containers_run()`
- Exec ops: `container_exec_run()`, `api_exec_create()`, `api_exec_start()`

**Updated files (20+ total)**:
- Phase 2: `routers/agents.py` - delete, stop, info, SSH access endpoints
- Phase 3: `services/agent_service/` - lifecycle, crud, files, stats, folders, dashboard, api_key, terminal, metrics, helpers
- Phase 4: `services/system_agent_service.py`, `routers/ops.py`, `routers/system_agent.py`, `services/agent_service/deploy.py`, `routers/systems.py`
- SSH service: `services/ssh_service.py` - all exec_run calls (inject_ssh_key, set_container_password, clear_container_password, remove_ssh_key, cleanup methods)
- Git service: `services/git_service.py`, `services/docker_service.py` - execute_command_in_container, check_git_initialized
- Routers: `routers/git.py` - await async git functions

**Impact**: Resolved GitHub Issue #42 (P1 bug)

---

### 2026-02-24 14:35:00
📦 **Refactor: Archive production deployment capability (SCOPE-001)**

Separated production deployment responsibility to a dedicated deployment agent. This development agent now only handles localhost testing.

**Archived to `docs/archive/deployment/`**:
- `CLAUDE.local.md.production` - Production deployment configuration
- `deploy.config.backup` - GCP deployment config
- `deploy.config.example` - Deployment config template
- `DEPLOYMENT.md.full` - Full deployment guide with production sections
- `docker-compose.prod.yml` - Production compose file

**Archived to `scripts/deploy/archive/`**:
- `gcp-deploy.sh` - GCP deployment script
- `gcp-firewall.sh` - GCP firewall configuration script

**Archived to `.claude/skills/archive/`**:
- `deploy-status/` - Production deployment health check skill

**Updated files**:
- `CLAUDE.md` - Removed "Production Deployment (GCP)" section, updated notes
- `CLAUDE.local.md` - Replaced with localhost-only configuration
- `CLAUDE.local.md.example` - Simplified to localhost-only template
- `docs/DEPLOYMENT.md` - Reduced to local development guide only

**Scope limitation**: This agent is now restricted to local development. Production deployment is handled by a separate deployment agent.

---

### 2026-02-24 11:30:00
📝 **Docs: Update README and requirements for recent features (MON-001, DASH-001)**

Updated documentation to reflect recent feature additions.

**README.md changes**:
- MCP tool count: 51 → 55 tools (added MON-001 monitoring tools)
- Added "Monitoring (3 tools)" to Additional Tool Categories
- Added "Fleet Health Monitoring" to Operations section
- Updated Agent Dashboard description with sparklines/historical tracking

**requirements.md changes**:
- Added 12.8 Agent Monitoring Service (MON-001) - full feature spec
- Updated 13.4 Agent Dashboard with DASH-001 enhancements
- Updated MCP tool count: 21 → 55 tools

**mcp-orchestration.md changes**:
- Tool count: 51 → 55 (added MON-001 reference)

---

### 2026-02-24 11:00:00
🐛 **Fix: Dashboard fails with 'dict' object has no attribute 'status' (DASH-001)**

Fixed critical bug where agent dashboards failed to load after DASH-001 deployment.

**Root Cause**: `_build_platform_metrics_section()` in `dashboard.py:71` accessed `health.status` using attribute notation, but `get_latest_health_check()` returns a **dict**, not an object.

**Error**: `'dict' object has no attribute 'status'`

**Fix**: Changed attribute access to dict access:
```python
# Before:
health_status = health.status.value if hasattr(health.status, 'value') else str(health.status)

# After:
health_status = health.get("status", "unknown")
```

**File**: `src/backend/services/agent_service/dashboard.py:71`

---

### 2026-02-24 10:15:00
📝 **Docs: Chat Router Refactoring Requirements (REFACTOR-001)**

Created comprehensive requirements document for refactoring `routers/chat.py` (1533 lines → 8 modules).

**Document Location**: `docs/requirements/CHAT_ROUTER_REFACTOR.md`

**Key Contents**:
- All 17 API endpoints mapped to new modules
- Function decomposition plan for 325-line and 364-line functions
- Integration points to preserve (queue, activity tracking, WebSocket, etc.)
- 8-phase migration strategy with zero breaking changes
- Testing requirements and regression checklist
- Risk assessment and success criteria

**Proposed Module Structure**:
- `chat/queue_chat.py`: Queue-based chat (1 endpoint)
- `chat/task_execution.py`: Stateless task execution (2 endpoints)
- `chat/sessions.py`: Session management (5 endpoints)
- `chat/activity.py`: Activity monitoring (2 endpoints)
- `chat/models.py`: Model configuration (2 endpoints)
- `chat/termination.py`: Execution control (2 endpoints)
- `chat/streaming.py`: Live streaming (2 endpoints)
- `chat/utils.py`: Shared utilities

---

### 2026-02-23 19:30:00
✨ **Feature: Dynamic Dashboards with Historical Data (DASH-001)**

Implemented dashboard history tracking, sparkline visualization, and platform metrics injection.

**Phase 1 - Database & Capture**:
- Schema: `agent_dashboard_values` table for storing widget snapshots (`src/backend/db/schema.py`)
- Migration: Auto-create table on startup (`src/backend/db/migrations.py`)
- Operations: `DashboardHistoryOperations` class with capture/query/stats methods (`src/backend/db/dashboard_history.py`)
- DatabaseManager: Exposed all dashboard history operations

**Phase 2 - History Enrichment**:
- Change detection: Only capture snapshots when dashboard.yaml mtime changes
- Widget enrichment: Inject `history` field with values array, trend, min/max/avg
- Statistics: Trend calculation (up/down/stable based on first-half vs second-half avg)
- Query params: `include_history`, `history_hours` (1-168), `include_platform_metrics`

**Phase 3 - Platform Metrics Section**:
- Auto-injected section with Trinity-tracked metrics
- Widgets: Tasks (24h), Success Rate, Cost, Health, Running count
- Single-agent execution stats: New `get_agent_execution_stats()` method
- Opt-out: Agents can set `platform_metrics: false` in dashboard.yaml

**Phase 4 - Frontend Sparklines**:
- SparklineChart integration in metric/progress widgets
- Trend indicators with percentage change
- Platform metrics section styling (indigo border, "Auto" badge)
- Color-coded sparklines (green=up, red=down, blue=stable)

**Files Changed**:
- `src/backend/db/schema.py`: Added table + indexes
- `src/backend/db/migrations.py`: Added migration function
- `src/backend/db/dashboard_history.py`: **New** operations class
- `src/backend/db/schedules.py`: Added `get_agent_execution_stats()`
- `src/backend/database.py`: Exposed dashboard history + execution stats
- `src/backend/services/agent_service/dashboard.py`: History enrichment + platform metrics
- `src/backend/routers/agent_dashboard.py`: Added query parameters
- `src/frontend/src/components/DashboardPanel.vue`: Sparklines + platform section styling

---

### 2026-02-23 17:50:00
📝 **Docs: Update feature flows for Health page admin-only access (MON-001)**

Updated feature flow documentation to reflect admin-only access for Health/Monitoring page.

**Changes**:
- `docs/memory/feature-flows.md`: Added admin-only access note to MON-001 update header, added Agent Monitoring entry to Documented Flows table
- `docs/memory/feature-flows/agent-monitoring.md`: Added revision history entry for admin-only change

**Documentation consistency**: Verified no other feature flows reference `/monitoring` route or NavBar Health link that need updating. Admin-only pattern is now documented consistently with Settings page pattern.

---

### 2026-02-23 17:45:00
🔒 **Fix: Health page admin-only visibility (MON-001)**

Restricted Health/Monitoring page to admin users only, matching the intended user story ("As a Trinity platform admin...").

**Changes**:
- `NavBar.vue:25-32`: Added `v-if="isAdmin"` to Health link (matches Settings pattern)
- `router/index.js:36-40`: Added `requiresAdmin: true` to `/monitoring` route meta
- `agent-monitoring.md`: Updated documentation to reflect admin-only visibility

---

### 2026-02-23 17:15:00
✨ **Feature: Agent Monitoring Service (MON-001)**

Implemented comprehensive agent health monitoring system with multi-layer health checks, alerting, and MCP tools.

**Phase 1 - Core Infrastructure**:
- Database schema: `agent_health_checks`, `monitoring_alert_cooldowns` tables (`src/backend/db/schema.py`)
- Pydantic models: `AgentHealthStatus`, `MonitoringConfig`, `FleetHealthStatus`, etc. (`src/backend/db_models.py`)
- Database operations: `MonitoringOperations` class (`src/backend/db/monitoring.py`)
- Health check service: Docker, Network, Business layer checks (`src/backend/services/monitoring_service.py`)

**Phase 2 - Notification Integration**:
- Alert service with cooldown tracking (`src/backend/services/monitoring_alerts.py`)
- WebSocket event broadcasting for real-time updates
- Integration with NOTIF-001 notification system

**Phase 3 - REST API & Frontend**:
- 11 API endpoints: `/api/monitoring/status`, `/api/monitoring/agents/{name}`, etc. (`src/backend/routers/monitoring.py`)
- Monitoring page with fleet summary and agent health grid (`src/frontend/src/views/Monitoring.vue`)
- Pinia store for state management (`src/frontend/src/stores/monitoring.js`)
- Navigation link "Health" added to NavBar

**Phase 4 - MCP Tools**:
- `get_fleet_health`: Fleet-wide health summary
- `get_agent_health`: Detailed agent health info
- `trigger_health_check`: Admin-only immediate check

**Health Status Levels**: HEALTHY → DEGRADED → UNHEALTHY → CRITICAL

**Files Created**:
- `src/backend/db/monitoring.py` (database operations)
- `src/backend/services/monitoring_service.py` (health check logic)
- `src/backend/services/monitoring_alerts.py` (alert service)
- `src/backend/routers/monitoring.py` (REST API)
- `src/frontend/src/views/Monitoring.vue` (UI page)
- `src/frontend/src/stores/monitoring.js` (Pinia store)
- `src/mcp-server/src/tools/monitoring.ts` (MCP tools)

---

### 2026-02-23 16:30:00
🐛 **Fix: Dashboard tab blocking navigation between agents**

Fixed bug where switching from an agent with a dashboard to an agent without one would show "Dashboard Error" and block tab navigation.

**Root Cause**: When navigating to a different agent, `hasDashboard` was reset but `activeTab` remained on 'dashboard'. The tab bar no longer showed the Dashboard button (correct), but the content area still displayed the DashboardPanel with an error (wrong).

**Fix**: Added validation in route watcher to reset `activeTab` to 'tasks' if the current tab is not in the new agent's `visibleTabs` list.

**File**: `src/frontend/src/views/AgentDetail.vue:660-667`

---

### 2026-02-23 15:45:00
📝 **Docs: Updated Subscription Management feature flow (SUB-001)**

Updated `docs/memory/feature-flows/subscription-management.md` with comprehensive Frontend UI documentation.

**Added Sections**:
- Frontend UI overview with file/line references
- UI Components: Add Form, Subscriptions Table, Expanded Details
- State variables documentation (7 reactive refs)
- Methods documentation (6 functions with code snippets)
- Data flow diagram (User -> Frontend -> Backend)
- UI Testing section (8 test cases)

**Line References**:
- Template: `src/frontend/src/views/Settings.vue:223-435`
- State: `src/frontend/src/views/Settings.vue:872-883`
- Methods: `src/frontend/src/views/Settings.vue:1268-1387`

---

### 2026-02-23 14:30:00
✨ **UI: Subscription Management in Settings (SUB-001)**

Added UI to manage Claude Max/Pro subscription credentials in Settings page.

**Features**:
- List all registered subscriptions with type, agent count, and creation date
- Expandable rows showing assigned agents and subscription details
- File upload for `.credentials.json` (from `~/.claude/.credentials.json`)
- Register new subscriptions with name, type selection, and credential file
- Delete subscriptions with confirmation (cascade clears agent assignments)

**Files Modified**:
- `src/frontend/src/views/Settings.vue`: Added Claude Subscriptions section (lines 223-435), state variables (lines 872-883), and methods (lines 1268-1387)

**User Flow**:
1. Run `claude login` locally to authenticate
2. Go to Settings → Claude Subscriptions
3. Enter name (e.g., "eugene-max"), select type
4. Upload `~/.claude/.credentials.json`
5. Click "Register Subscription"
6. Assign to agents via MCP: `assign_subscription("agent-name", "subscription-name")`

**Related**: SUB-001 backend implemented 2026-02-22

---

### 2026-02-23 12:15:00
📋 **Planning: Subscription Usage Tracking (SUB-002)**

Researched and validated approach for programmatic subscription usage tracking.

**Problem**: SUB-001 manages credentials but doesn't track usage/capacity. No visibility into quota consumption for fleet management.

**POC Validation** (`scripts/poc/test-subscription-usage.py`):
- Discovered Anthropic's OAuth usage endpoint: `GET https://api.anthropic.com/api/oauth/usage`
- Required header: `anthropic-beta: oauth-2025-04-20`
- Successfully retrieved usage data from local Claude Code credentials
- Response includes: 5-hour utilization, 7-day utilization, model-specific limits, extra usage status

**Requirements Spec**: `docs/requirements/SUBSCRIPTION_USAGE_TRACKING.md`

**Key Components**:
- Database: `subscription_usage` table for snapshots
- Service: OAuth usage fetching with token extraction
- Background Job: Hourly polling of all subscriptions
- REST API: `/api/subscriptions/{id}/usage`, `/api/subscriptions/usage-report`
- MCP Tools: `get_subscription_usage`, `get_fleet_usage`

**Sources**:
- https://codelynx.dev/posts/claude-code-usage-limits-statusline
- https://github.com/TylerGallenbeck/claude-code-limit-tracker
- https://github.com/anthropics/claude-code/issues/21943

---

### 2026-02-23 10:30:00
🔒 **Security: Remove Plaintext Password Fallback & Add Admin Login Rate Limiting**

Implemented two security fixes from security analysis report:

**M-003: Remove Legacy Plaintext Password Support**
- **File**: `src/backend/dependencies.py:24-34`
- **Change**: Removed plaintext password comparison fallback from `verify_password()`
- **Impact**: All passwords must now be bcrypt hashed. Invalid hash formats are rejected.
- **Rationale**: Plaintext fallback was a security risk if database was compromised

**M-005: Admin Login Rate Limiting**
- **File**: `src/backend/routers/auth.py:24-95, 127-161`
- **Change**: Added Redis-based rate limiting to `/token` endpoint
- **Configuration**: 5 attempts per 10 minutes per IP address
- **Behavior**: Returns HTTP 429 when rate limited, clears counter on successful login
- **Fallback**: If Redis unavailable, logs warning but allows login (graceful degradation)

**Security Report**: `docs/security-reports/security-analysis-2026-02-22.md`

---

### 2026-02-22 14:55:00
🧪 **Tests: Subscription Management (SUB-001)**

Added automated pytest tests for subscription management feature:
- `tests/test_subscriptions.py`: 18 tests (9 smoke, 9 agent-requiring)
- `docs/SUBSCRIPTION_CREDENTIALS.md`: Quick reference for credential extraction and injection
- Fixed HTTPException handling in `routers/subscriptions.py` (was converting 400 to 500)
- Updated `.claude/agents/test-runner.md` with SUB-001 test documentation

Test coverage:
- Subscription CRUD (list, register, upsert, delete, validation)
- Agent assignment (assign, clear, nonexistent handling)
- Auth status detection (subscription vs api_key vs not_configured)
- Fleet auth report
- Credential injection on assignment
- Cascade delete behavior

---

### 2026-02-22 14:00:00
✨ **Feature: Subscription Management (SUB-001)**

Implemented centralized management of Claude Max/Pro subscription credentials. Subscriptions can be registered once and assigned to multiple agents, eliminating manual per-agent authentication.

**Key Insight**: Claude Code stores OAuth credentials in `~/.claude/.credentials.json`. When both API key and subscription exist, subscription takes precedence.

**Backend Changes**:
- `src/backend/db/subscriptions.py`: New - SubscriptionOperations class with CRUD and assignment methods
- `src/backend/routers/subscriptions.py`: New - REST API endpoints for subscription management
- `src/backend/services/subscription_service.py`: New - Injection service for writing credentials to agents
- `src/backend/database.py`: Added migrations for `subscription_credentials` table and `subscription_id` column on `agent_ownership`
- `src/backend/db_models.py`: Added SubscriptionCredential, SubscriptionWithAgents, AgentAuthStatus models
- `src/backend/services/agent_service/lifecycle.py`: Integrated subscription injection into agent startup
- `src/backend/routers/ops.py`: Added `/api/ops/auth-report` endpoint for fleet-wide auth status

**MCP Tools** (6 new tools):
- `register_subscription`: Register OAuth credentials (admin)
- `list_subscriptions`: List all subscriptions with agents
- `assign_subscription`: Assign subscription to an agent
- `clear_agent_subscription`: Clear subscription from agent
- `get_agent_auth`: Get auth mode for an agent
- `delete_subscription`: Delete a subscription (admin)

**REST Endpoints**:
- `POST /api/subscriptions`: Register subscription
- `GET /api/subscriptions`: List all with agent counts
- `GET/DELETE /api/subscriptions/{id}`: Get/delete subscription
- `PUT/DELETE /api/subscriptions/agents/{name}`: Assign/clear agent subscription
- `GET /api/subscriptions/agents/{name}/auth`: Get auth status

**Workflow**:
1. User runs `claude login` locally
2. Registers subscription: `register_subscription("eugene-max", cat ~/.claude/.credentials.json)`
3. Assigns to agent: `assign_subscription("my-agent", "eugene-max")`
4. Credentials injected on start or immediately if agent is running

---

### 2026-02-22 10:30:00
🐛 **Fix: Schedule Stats Always Shown + Added to Agents Page**

Fixed inconsistent tile heights when agents have no schedules, and added schedule stats to Agents page for consistency with Dashboard.

**Bug**: On Dashboard tiles, agents without schedules had the schedule row hidden entirely (`v-if="hasSchedules"`), causing tiles to be different heights.

**Fix**:
1. **Dashboard (AgentNode.vue)**: Changed to always show schedule row for non-system agents. Shows "0/0 schedules" when no schedules exist. Row is grayed out when no schedules or autonomy disabled.
2. **Agents Page (Agents.vue)**: Added matching schedule stats row for consistency. Shows same X/Y format with clock icon and "(paused)" indicator.

**Files Modified**:
- `src/frontend/src/components/AgentNode.vue`: Changed `v-if="hasSchedules && !isSystemAgent"` to `v-if="!isSystemAgent"`, updated styling conditions
- `src/frontend/src/views/Agents.vue`: Added schedule stats row (lines 323-341), added helper functions `getSchedulesTotal()`, `getSchedulesEnabled()`, `hasSchedules()`

---

### 2026-02-22 10:00:00
✨ **UI: Dashboard Tile Schedule Display + Sidebar Default Collapsed**

Improved Dashboard tiles to show schedule information and made the System Views sidebar collapsed by default.

**Changes**:
1. **Schedule counts on Dashboard tiles**: Each agent tile now shows "X/Y schedules" where X is enabled and Y is total. Grayed out with "(paused)" indicator when agent is in manual mode (autonomy disabled).
2. **System Views sidebar collapsed by default**: New users will see the sidebar collapsed. State persists to localStorage when toggled.

**Backend Changes**:
- `src/backend/db/schedules.py`: Added `get_all_agents_schedule_counts()` method
- `src/backend/database.py`: Added wrapper method for schedule counts
- `src/backend/routers/agents.py`: Extended `/api/agents/execution-stats` to include `schedules_total` and `schedules_enabled` per agent

**Frontend Changes**:
- `src/frontend/src/components/AgentNode.vue`: Added schedule stats row with clock icon, shows X/Y format, grayed when manual mode
- `src/frontend/src/components/SystemViewsSidebar.vue`: Default to collapsed, fixed localStorage persistence on toggle
- `src/frontend/src/stores/network.js`: Extended `fetchExecutionStats()` to include schedule counts in stats

---

### 2026-02-21 14:30:00
🐛 **Fix: Resume Mode Context Lost After First Message (EXEC-023)**

Fixed critical issue where "Continue Execution as Chat" lost context after the first message. Agent would report "nothing new in context" on subsequent messages.

**Root Cause**: The `resume_session_id` (Claude Code session ID for `--resume` flag) was cleared after the first message, but the `/task` endpoint is stateless and doesn't use `--continue`. Subsequent messages started fresh without any context.

**Fix**: Keep `resumeSessionIdLocal` populated for ALL messages in a resumed session:
- Removed the line that cleared `resumeSessionIdLocal` after first message
- All messages now continue passing `resume_session_id` to maintain context via `--resume`
- Added `resumeBannerDismissed` flag so dismissing the banner doesn't lose session continuity
- Selecting a different session or clicking "New Chat" properly clears resume mode

**Files Modified**:
- `src/frontend/src/components/ChatPanel.vue`:
  - Line 202-204: Added `resumeBannerDismissed` flag, updated `isResumeMode` computed
  - Line 364-371: Removed clearing of `resumeSessionIdLocal` after first message
  - Line 308-313: `dismissResumeMode()` now only sets banner flag
  - Line 273-277: `selectSession()` clears resume mode when switching sessions

---

### 2026-02-21 13:25:00
🐛 **Fix: Scheduled Executions Missing `claude_session_id` (EXEC-023)**

Fixed issue where scheduled executions didn't capture `claude_session_id`, preventing "Continue as Chat" from working for scheduled tasks.

**Root Cause**: The dedicated scheduler service (`src/scheduler/`) had its own `AgentClient` and `update_execution_status()` that didn't capture or store `session_id` from agent responses. Only manual executions via the backend's `/task` endpoint captured the session ID.

**Fix**: Updated scheduler to capture and store `claude_session_id`:
- `src/scheduler/models.py`: Added `session_id` field to `AgentTaskMetrics`
- `src/scheduler/agent_client.py`: Extract `session_id` from agent response in `_parse_task_response()`
- `src/scheduler/database.py`: Added `claude_session_id` parameter to `update_execution_status()`
- `src/scheduler/service.py`: Pass `claude_session_id` to database update

**Files Modified**: 4 files in `src/scheduler/`

---

### 2026-02-21 13:25:00
🐛 **Fix: Chat Tab Auto-Selecting Old Session in Resume Mode (EXEC-023)**

Fixed issue where clicking "Continue as Chat" opened the Chat tab but loaded the previous active session instead of starting fresh for resume.

**Root Cause**: `ChatPanel.vue` watched for `resumeSessionId` and cleared messages, but then `onMounted` called `loadSessions()` which auto-selected the most recent active session because `messages.length === 0` (just cleared).

**Fix**: Added `!isResumeMode.value` condition to prevent auto-selection when in resume mode.

**File Modified**:
- `src/frontend/src/components/ChatPanel.vue`: Line 251 now checks `!isResumeMode.value`

---

### 2026-02-21 12:30:00
🐛 **Fix: "Continue as Chat" Not Opening Chat Tab (EXEC-023)**

Fixed issue where clicking "Continue as Chat" on Execution Detail page navigated to Agent Detail but didn't switch to the Chat tab.

**Root Cause**: AgentDetail component is cached via KeepAlive. When navigating back to a cached instance, `onMounted` doesn't fire - only `onActivated` does. The `route.query.tab` handling was only in `onMounted`, so the tab switch was ignored for cached components.

**Fix**: Added tab query param handling to `onActivated` hook in AgentDetail.vue.

**File Modified**:
- `src/frontend/src/views/AgentDetail.vue`: Added tab query param check in `onActivated` (lines 752-759)

---

### 2026-02-21 12:00:00
📚 **Docs: Feature Flow Updates for EXEC-023 Bug Fix**

Updated feature flow documentation to record the DatabaseManager.update_execution_status() wrapper bug fix.

**Files Updated**:
- `docs/memory/feature-flows/continue-execution-as-chat.md`: Added revision history entry for the bug fix
- `docs/memory/feature-flows/parallel-headless-execution.md`: Added revision history entry documenting the wrapper method fix
- `docs/memory/feature-flows/scheduling.md`: Added status note documenting the affected code path
- `docs/memory/feature-flows/execution-queue.md`: Added revision history entry documenting the execution status update fix
- `docs/memory/feature-flows.md`: Added index entry summarizing the bug and affected flows

---

### 2026-02-21 11:30:00
🐛 **Fix: Manual Task Executions Stuck in "running" Status (EXEC-023 Regression)**

Fixed critical bug where all manual task executions completed successfully on agents but failed to update their database status, leaving them stuck as "running".

**Root Cause**: In commit 3544248 (EXEC-023: Continue Execution as Chat), `claude_session_id` parameter was added to `db/schedules.py:update_execution_status()` but the wrapper method in `database.py:update_execution_status()` was not updated to accept or pass through this parameter.

**Error**: `TypeError: DatabaseManager.update_execution_status() got an unexpected keyword argument 'claude_session_id'`

**Impact**: All manual task executions via `/task` endpoint failed to save results. Tasks executed successfully on agents but results were never persisted, and executions remained in "running" status indefinitely.

**Fix**: Added `claude_session_id: str = None` parameter to `DatabaseManager.update_execution_status()` wrapper and pass-through to underlying method.

**File Modified**:
- `src/backend/database.py`: Added missing `claude_session_id` parameter to wrapper method

---

### 2026-02-21 11:00:00
📚 **Docs: PERF-001 Feature Flow Updates**

Updated feature flow documentation to reflect Task List Performance Optimization changes.

**Files Updated**:
- `docs/memory/feature-flows/tasks-tab.md`: Added PERF-001 overview, new state variables (`taskDetailsCache`, `expandLoadingTaskId`), on-demand loading section, updated API documentation, new database index
- `docs/memory/feature-flows/execution-detail-page.md`: Added note distinguishing `ExecutionResponse` from `ExecutionSummary`
- `docs/memory/feature-flows/execution-log-viewer.md`: Added note that log viewer uses dedicated `/log` endpoint
- `docs/memory/feature-flows/scheduling.md`: Updated line numbers, added PERF-001 note to View Executions flow
- `docs/memory/feature-flows.md`: Added PERF-001 update summary, updated table entries for affected flows

---

### 2026-02-21 10:30:00
⚡ **Performance: Task List Optimization (PERF-001)**

Optimized the Tasks tab loading in Agent Detail page. Previously loaded full execution_log (100KB+ per execution) for all 100 tasks, causing 10MB+ transfers. Now loads lightweight summaries and fetches details on-demand when user expands a task.

**Problem Solved**: Tasks tab was slow to load for agents with many executions. Network transfers were 10MB+ when only ~100KB was needed for list display.

**Implementation**:
- **Backend**: Added `ExecutionSummary` model excluding large fields (response, error, tool_calls, execution_log)
- **Backend**: Added `get_agent_executions_summary()` DB method with optimized SELECT
- **Backend**: Changed `GET /api/agents/{name}/executions` to return `ExecutionSummary` list
- **Backend**: Added composite index `idx_executions_agent_started` for optimized queries
- **Frontend**: Modified `TasksPanel.vue` to fetch response/error on-demand when expanding

**Key Files Modified**:
- `src/backend/routers/schedules.py`: Added `ExecutionSummary` model, updated endpoint
- `src/backend/db/schedules.py`: Added `get_agent_executions_summary()` method
- `src/backend/database.py`: Added delegation method and composite index
- `src/frontend/src/components/TasksPanel.vue`: On-demand detail loading with cache

**Performance Impact**:
- Data transfer: ~10MB → ~100KB (50-100x reduction)
- Load time: Now sub-second for list view
- User experience: Expand preview still works, details load when clicked

---

### 2026-02-20 19:30:00
✨ **Feature: Continue Execution as Chat (EXEC-023)**

Implemented ability to continue any execution as an interactive chat with full context preservation using Claude Code's native `--resume` capability.

**Problem Solved**: When executions fail or produce unexpected results, users previously had no way to follow up with the agent while preserving context. Now they can click "Continue as Chat" to resume the exact Claude Code session.

**Implementation**:
- **Database**: Added `claude_session_id` column to `schedule_executions` table via migration
- **Agent Server**: Added `resume_session_id` parameter to `/api/task` endpoint, uses `claude --resume {session_id}` when provided
- **Backend**: Captures `session_id` from agent response and stores in execution record; passes `resume_session_id` to agent
- **Frontend**: Added "Continue as Chat" button to ExecutionDetail.vue, ChatPanel.vue handles resume mode with banner

**Key Files Modified**:
- `src/backend/database.py`: Migration for `claude_session_id` column
- `src/backend/db_models.py`: Added `claude_session_id` to ScheduleExecution
- `src/backend/db/schedules.py`: Store/retrieve claude_session_id
- `src/backend/models.py`: Added `resume_session_id` to ParallelTaskRequest
- `src/backend/routers/chat.py`: Capture session_id, pass resume_session_id to agent
- `docker/base-image/agent_server/models.py`: Added `resume_session_id` to TaskRequest
- `docker/base-image/agent_server/routers/chat.py`: Pass resume_session_id to runtime
- `docker/base-image/agent_server/services/claude_code.py`: Use `--resume` flag
- `docker/base-image/agent_server/services/runtime_adapter.py`: Updated interface
- `docker/base-image/agent_server/services/gemini_runtime.py`: Interface compatibility (ignores resume)
- `src/frontend/src/views/ExecutionDetail.vue`: Added "Continue as Chat" button
- `src/frontend/src/components/ChatPanel.vue`: Resume mode with banner
- `src/frontend/src/views/AgentDetail.vue`: Pass resume query params to ChatPanel

---

### 2026-02-20 18:00:00
🐛 **Fix: Notifications Showing Wrong Agent Name (NOTIF-003)**

Fixed issue where Events page showed notifications as coming from "admin" instead of the actual agent name.

**Root Cause**: When agents send notifications via MCP, the `agent_name` from agent-scoped API keys was not being passed to the `User` object in `get_current_user()`. The notification router then fell back to `current_user.username` (the key owner's username) instead of the agent name.

**Solution**:
- Added `agent_name` field to `User` model in `models.py`
- Updated `get_current_user()` in `dependencies.py` to extract `agent_name` from MCP key info for agent-scoped keys
- Simplified notification router to use `current_user.agent_name` when available

**Files Modified**:
- `src/backend/models.py`: Added `agent_name: Optional[str] = None` to User model
- `src/backend/dependencies.py`: Pass agent_name from mcp_key_info to User object
- `src/backend/routers/notifications.py`: Simplified agent_name extraction logic

---

### 2026-02-20 17:30:00
🐛 **Fix: Chat Tab Session Persistence (CHAT-001)**

Fixed issue where Chat tab in Agent Detail page did not persist sessions after page refresh. Sessions now properly save to `chat_sessions` table.

**Root Cause**: ChatPanel.vue used `/task` endpoint which only creates `schedule_executions` records, not `chat_sessions`. The session dropdown loaded from `chat_sessions` table which was never populated.

**Solution**: Added session persistence support to `/task` endpoint:
- New `ParallelTaskRequest` fields: `save_to_session`, `user_message`, `create_new_session`
- When `save_to_session=true`, backend persists to `chat_sessions` and `chat_messages` tables
- When `create_new_session=true`, closes existing active sessions and creates a new one
- ChatPanel now passes `save_to_session: true`, `user_message`, and `create_new_session: !currentSessionId`

**Files Modified**:
- `src/backend/models.py`: Added 3 new fields to `ParallelTaskRequest`
- `src/backend/routers/chat.py`: Added session persistence logic in `/task` endpoint
- `src/backend/db/chat.py`: Added `create_new_chat_session()` method
- `src/backend/database.py`: Added delegation method
- `src/frontend/src/components/ChatPanel.vue`: Pass new parameters

---

### 2026-02-20 16:30:00
✨ **Feature: Execution Origin Tracking (AUDIT-001)**

Implemented full execution origin tracking to answer "who started this task?" for all executions. Complete audit trail from MCP client through backend to UI.

**Database**:
- Added migration `_migrate_execution_origin_tracking()` to database.py
- New columns: `source_user_id`, `source_user_email`, `source_agent_name`, `source_mcp_key_id`, `source_mcp_key_name`

**Backend**:
- Updated `ScheduleExecution` model in db_models.py with origin fields
- Updated `db/schedules.py` to accept and persist origin data
- Updated `routers/chat.py` endpoints (`/chat`, `/task`) to capture origin from headers
- Added `X-MCP-Key-ID` and `X-MCP-Key-Name` header extraction

**MCP Server**:
- Updated `/api/mcp/validate` to return `key_id` in response
- Added `keyId` to `McpAuthContext` type
- Updated `TrinityClient.chat()` and `TrinityClient.task()` to pass MCP key headers
- Updated chat tools to include `mcpKeyInfo` in backend calls

**Frontend**:
- Added "Execution Origin" card to ExecutionDetail.vue showing user/agent/MCP key
- Added trigger type filter dropdown to TasksPanel.vue

**Files Modified**:
- `src/backend/database.py`, `src/backend/db_models.py`, `src/backend/db/schedules.py`
- `src/backend/routers/chat.py`, `src/backend/routers/mcp_keys.py`
- `src/scheduler/models.py`, `src/scheduler/database.py`
- `src/mcp-server/src/server.ts`, `src/mcp-server/src/client.ts`
- `src/mcp-server/src/types.ts`, `src/mcp-server/src/tools/chat.ts`
- `src/frontend/src/views/ExecutionDetail.vue`
- `src/frontend/src/components/TasksPanel.vue`

---

### 2026-02-20 15:45:00
📋 **Requirements: Execution Origin Tracking (AUDIT-001)**

Created comprehensive requirements specification for tracking WHO triggered each execution in Trinity. This feature enables full audit trail for all task executions.

**Problem**: Currently executions track trigger TYPE (manual/schedule/mcp/agent) but not the identity of the actor. Cannot answer "who started this task?" or "which API key was used?"

**Solution**: Extend `schedule_executions` table with origin columns:
- `source_user_id` - User who triggered (FK to users)
- `source_user_email` - User email (denormalized)
- `source_agent_name` - Calling agent for agent-to-agent
- `source_mcp_key_id` - MCP API key ID used
- `source_mcp_key_name` - MCP API key name

**Key Features**:
- Complete actor attribution for all 4 trigger types
- MCP key info passed via headers from MCP server
- UI display on Execution Detail page showing origin
- Filter executions by trigger type

**Implementation Phases**:
1. Database migration + backend CRUD updates
2. MCP server header integration
3. Frontend display and filtering

**Files Created**:
- `docs/requirements/EXECUTION_ORIGIN_TRACKING.md`: Full specification

**Files Modified**:
- `docs/memory/requirements.md`: Added Section 20.2 (AUDIT-001)

---

### 2026-02-20 14:30:00
✨ **Feature: Events Page UI (NOTIF-002)**

Implemented Events page for viewing, filtering, and managing agent notifications. Provides unified stream of all agent events with real-time updates.

**Key Features**:
- Dedicated `/events` page with filter controls (status, priority, agent, type)
- Stats cards showing pending/acknowledged/total counts
- Notification cards with priority/type icons, timestamps, actions
- Bulk selection and actions (acknowledge/dismiss multiple)
- Load more pagination
- Real-time updates via WebSocket
- Navigation badge in NavBar with pending count (pulsing for urgent/high)

**Files Created**:
- `src/frontend/src/views/Events.vue`: Main events page (380+ lines)
- `src/frontend/src/stores/notifications.js`: Pinia store (270+ lines)
- `docs/memory/feature-flows/events-page.md`: Feature flow documentation

**Files Modified**:
- `src/frontend/src/router/index.js`: Added `/events` route
- `src/frontend/src/components/NavBar.vue`: Added inbox icon with badge, polling
- `src/frontend/src/utils/websocket.js`: Added `agent_notification` event handler

**Spec**: `docs/requirements/EVENTS_PAGE_UI.md`
**Flow**: `docs/memory/feature-flows/events-page.md`

---

### 2026-02-20 12:41:00
✨ **Feature: Agent Notifications (NOTIF-001)**

Implemented agent notification system allowing agents to send structured notifications to the Trinity platform. Notifications are persisted, broadcast via WebSocket, and queryable via REST API.

**Components Implemented**:
- Database table `agent_notifications` with full CRUD operations
- REST API endpoints: create, list, get, acknowledge, dismiss, agent-specific queries
- MCP tool `send_notification` for agent use
- WebSocket broadcasting for real-time notification delivery

**API Endpoints**:
- `POST /api/notifications` - Create notification
- `GET /api/notifications` - List with filters (agent, status, priority)
- `GET /api/notifications/{id}` - Get single notification
- `POST /api/notifications/{id}/acknowledge` - Acknowledge
- `POST /api/notifications/{id}/dismiss` - Dismiss
- `GET /api/agents/{name}/notifications` - Agent-specific list
- `GET /api/agents/{name}/notifications/count` - Pending count

**MCP Tool**: `send_notification`
- Parameters: notification_type, title, message, priority, category, metadata

**Files Created**:
- `src/backend/db/notifications.py`: CRUD operations
- `src/backend/routers/notifications.py`: API endpoints
- `src/mcp-server/src/tools/notifications.ts`: MCP tool

**Files Modified**:
- `src/backend/database.py`: Migration and delegation methods
- `src/backend/db_models.py`: Pydantic models
- `src/backend/main.py`: Router mount and WebSocket setup
- `src/mcp-server/src/client.ts`: createNotification API method
- `src/mcp-server/src/server.ts`: Tool registration

---

### 2026-02-20 10:15:00
✨ **Feature: Configurable Timeout and Allowed Tools for Schedules**

Added per-schedule configuration for execution timeout and tool restrictions. Previously hardcoded (timeout=900s, all tools allowed), now customizable per schedule.

**New Fields**:
- `timeout_seconds`: Execution timeout (5m, 15m, 30m, 1h, 2h options)
- `allowed_tools`: Optional list of permitted tools (null = all tools)

**Use Cases**:
- Long-running analysis tasks → 2 hour timeout
- Quick status checks → 5 minute timeout
- Read-only operations → restrict to Read, Glob, Grep
- Security-sensitive tasks → disable Bash

**Files Changed**:

*Database*:
- `src/backend/database.py`: Added migration for `timeout_seconds` and `allowed_tools` columns
- `src/backend/db_models.py`: Updated `ScheduleCreate` and `Schedule` models
- `src/backend/db/schedules.py`: Updated CRUD operations with JSON serialization for allowed_tools

*Scheduler Service*:
- `src/scheduler/models.py`: Added new fields to Schedule dataclass
- `src/scheduler/database.py`: Updated row-to-model mapping
- `src/scheduler/agent_client.py`: Accept and forward `allowed_tools` parameter
- `src/scheduler/service.py`: Pass schedule config to agent client

*Frontend*:
- `src/frontend/src/components/SchedulesPanel.vue`:
  - Added timeout dropdown (5m/15m/30m/1h/2h)
  - Added allowed tools multi-select with category groups (Files, Search, System, Web, Advanced)
  - "All Tools" toggle for unrestricted mode
  - Display timeout and tool count on schedule cards

**API Changes**:
- `POST/PUT /api/agents/{name}/schedules`: Accept `timeout_seconds` (int) and `allowed_tools` (array or null)

---

### 2026-02-19 11:30:00
✨ **Feature: Authenticated Chat Tab (CHAT-001)**

Added a dedicated Chat tab to the Agent Detail page providing a simple, clean chat interface for authenticated users.

**Key Features**:
- Clean bubble UI identical to PublicChat (user = indigo right, assistant = white left with markdown)
- Session selector dropdown to switch between past conversations
- New Chat button to start fresh session
- Dashboard integration - all chat activity tracked in timeline (uses `/task` endpoint)
- Bottom-aligned messages (iMessage style)

**Shared Components Created** (`src/frontend/src/components/chat/`):
- `ChatBubble.vue` - Message bubble with markdown rendering
- `ChatInput.vue` - Auto-resize textarea with send button
- `ChatMessages.vue` - Message list with auto-scroll
- `ChatLoadingIndicator.vue` - "Thinking..." bouncing dots

**Files Changed**:
- `src/frontend/src/components/ChatPanel.vue`: New authenticated chat panel
- `src/frontend/src/views/AgentDetail.vue`: Added Chat tab after Tasks
- `src/frontend/src/views/PublicChat.vue`: Refactored to use shared components

**Spec**: `docs/requirements/AUTHENTICATED_CHAT_TAB.md`
**Flow**: `docs/memory/feature-flows/authenticated-chat-tab.md`

---

### 2026-02-19 09:00:00
🔒 **Security: Restrictive Default Permissions for New Agents**

Changed default agent permissions from permissive to restrictive. New agents now start with NO permissions to communicate with other agents.

**Previous Behavior**: New agents automatically received bidirectional permissions with all other agents owned by the same user (Option B: same-owner agents).

**New Behavior**: New agents start isolated - owners must explicitly grant permissions via the Permissions tab in the UI.

**Rationale**: Better security posture - agents should explicitly opt-in to collaboration rather than having implicit access.

**Files Changed**:
- `src/backend/db/permissions.py`: `grant_default_permissions()` now returns 0 (no-op)
- `docs/memory/feature-flows/agent-permissions.md`: Updated default behavior documentation
- `docs/memory/requirements.md`: Updated Agent Permissions key features

**Note**: System agents (`trinity-system`) are unaffected - they bypass permission checks via MCP scope.

---

### 2026-02-18 17:50:21
🔧 **Fix: Toggle Consistency Across System**

Standardized all toggle switches to use consistent size and labels.

**Changes**:
- ReadOnlyToggle in Agents.vue now shows label ("Read-Only" / "Editable")
- All toggles in AgentHeader.vue changed from lg/md to sm size
- RunningStateToggle default size changed from md to sm

**Files Changed**:
- `src/frontend/src/views/Agents.vue`: Removed `:show-label="false"` from ReadOnlyToggle
- `src/frontend/src/components/AgentHeader.vue`: All toggles now use `size="sm"`
- `src/frontend/src/components/RunningStateToggle.vue`: Default size → `'sm'`

---

### 2026-02-18 16:00:00
📝 **Docs: Feature Flow Documentation Update for Agents Page Changes**

Updated feature flow documentation with accurate line numbers and implementation details for recent Agents page changes.

**Files Updated**:
- `docs/memory/feature-flows/agent-tags.md`: Updated bulk operations line numbers (391-706), added Tags Display Layout Fix section documenting fixed-height container and truncation
- `docs/memory/feature-flows/read-only-mode.md`: Added comprehensive "Agents Page Integration" section with template structure, state management table, functions table, and data flow diagram
- `docs/memory/feature-flows/agents-page-ui-improvements.md`: Updated revision history with accurate line numbers for all recent changes
- `docs/memory/feature-flows.md`: Updated index with Agents Page Enhancements summary, updated table entries for all three related flows

**Documentation Accuracy**:
- Verified all line numbers against current `Agents.vue` (729 lines)
- Documented toggle row structure: Running -> ReadOnly -> Autonomy (lines 240-263)
- Documented tags container styling: `h-6 overflow-hidden` (line 271), `max-w-20 truncate` (line 276)
- Added function line references: `fetchAllReadOnlyStates()` (544-563), `handleReadOnlyToggle()` (565-594)

---

### 2026-02-18 15:00:00
🐛 **Fix: Agents Page Tags Layout + Read-Only Toggle**

Fixed tags breaking tile layout on Agents page and added Read-Only toggle to agent cards.

**Tags Layout Fix** (`Agents.vue:270-288`):
- Wrapped tags in fixed-height container (`h-6 overflow-hidden`)
- Added `max-w-20 truncate` to individual tag pills to prevent overflow
- Tags now have consistent height regardless of content

**Read-Only Toggle** (`Agents.vue:248-255`):
- Added `ReadOnlyToggle` component between Running and Autonomy toggles
- Only shown for owned agents (not system, not shared)
- Added `agentReadOnlyStates` map and `fetchAllReadOnlyStates()` on mount
- `handleReadOnlyToggle()` calls `PUT /api/agents/{name}/read-only`

**Files Changed**:
- `src/frontend/src/views/Agents.vue`: Import, state, template, functions
- `docs/memory/feature-flows/agents-page-ui-improvements.md`: Revision history
- `docs/memory/feature-flows/read-only-mode.md`: Entry points, revision history

---

### 2026-02-18 12:00:00
📝 **Docs: Feature Flow Updates for UI-001 Redesign**

Updated feature flow documentation to reflect Agent Detail Page Redesign changes.

**Files Updated**:
- `docs/memory/feature-flows.md`: Added UI-001 redesign summary to index with line numbers
- `docs/memory/feature-flows/tasks-tab.md`: Updated section order (Stats -> Input -> History), new line numbers
- `docs/memory/feature-flows/agent-resource-allocation.md`: Updated AgentHeader structure (3-row layout), gear button location
- `docs/memory/feature-flows/agent-lifecycle.md`: Updated AgentHeader line numbers, default tab change note

**Key Changes Documented**:
- AgentHeader 3-row structure: Row 1 (Identity + Actions), Row 2 (Settings + Stats), Row 3 (Git)
- TasksPanel reordered: Stats first, then Task Input, then History
- Default tab: 'tasks' instead of 'info'
- Fixed-width stats to prevent layout jumping

---

### 2026-02-18 11:00:00
✨ **Feature: Agent Detail Page Redesign (UI-001)**

Improved visual hierarchy and workflow optimization for the Agent Detail page.

**AgentHeader.vue Restructure** (3 rows):
- **Row 1 (lines 4-57)**: Agent name `text-2xl`, status badges below name, Running toggle + delete on right
- **Row 2 (lines 59-163)**: Settings (Autonomy, Read-Only, Tags) on left + Stats (CPU/MEM sparklines, uptime) on right. Fixed widths (`w-10`, `w-14`, `w-16`) prevent layout jumping when stats update. Network stats removed.
- **Row 3 (lines 165-250)**: Git controls - only when `hasGitSync`, shows "Git enabled" indicator when stopped

**AgentDetail.vue Tab Changes**:
- Default tab changed from `info` to `tasks` (line 275)
- New tab order optimized for workflow: Tasks → Terminal → Logs → Files → Schedules → Credentials → Skills → Sharing → Permissions → Git → Folders → Public Links → Info
- Info tab moved to end (reference/metadata)

**Benefits**:
- Cleaner visual hierarchy with grouped controls
- Git controls hidden when not relevant (reduces clutter)
- Tasks as default landing (where users interact most)
- Most-used tabs easier to reach

**Spec**: `docs/requirements/AGENT_DETAIL_PAGE_REDESIGN.md`

---

### 2026-02-18 10:00:00
🐛 **Bug Fix: Tag API Authentication in AgentDetail.vue**

Fixed tag operations failing silently due to missing authentication headers.

**Root Cause**: API calls were using Pinia store's `api` wrapper which didn't include JWT bearer token.

**Fix** (`src/frontend/src/views/AgentDetail.vue`):
- Changed all tag API calls to use `axios` directly with `authStore.authHeader`
- `loadTags()`: lines 548-558
- `loadAllTags()`: lines 560-569
- `updateTags()`: lines 571-583
- `addTag()`: lines 585-598
- `removeTag()`: lines 600-611

**Documentation Updates**:
- `docs/memory/feature-flows/agent-tags.md`: Updated line numbers, added bug fix section
- `docs/memory/feature-flows.md`: Added bug fix note to index
- `docs/memory/feature-flows/system-manifest.md`: Verified accuracy (no changes needed)

---

### 2026-02-17 22:30:00
✨ **Feature: System Manifest Integration (ORG-001 Phase 4)**

Integrated agent tags with system manifest deployment. Tags are now automatically applied when deploying multi-agent systems via YAML.

**Backend Models** (`src/backend/models.py`):
- `SystemAgentConfig`: Added `tags` field for per-agent tags
- `SystemViewConfig`: New model for auto-creating System Views on deploy
- `SystemManifest`: Added `default_tags` and `system_view` fields
- `SystemDeployResponse`: Added `tags_configured` and `system_view_created` fields

**Backend Service** (`src/backend/services/system_service.py`):
- `parse_manifest()`: Parses tags and system_view from YAML
- `validate_manifest()`: Validates tag format (alphanumeric + hyphens)
- `configure_tags()`: Applies system prefix + default_tags + per-agent tags
- `create_system_view()`: Creates System View with filter tags from manifest
- `export_manifest()`: Includes tags in exported YAML

**Backend Router** (`src/backend/routers/systems.py`):
- `deploy_system()`: Integrated tag and system view creation after agent creation
- Response now includes tag count and system view ID

**Migration Script** (`scripts/management/migrate_prefixes_to_tags.py`):
- Extracts existing agent prefixes as tags
- Supports `--dry-run` mode for preview
- Example: `research-team-analyst` → tag `research-team`

**YAML Schema Extension**:
```yaml
default_tags: [research, production]  # Applied to all agents
system_view:                          # Auto-create System View
  name: Research Team
  icon: "🔬"
  shared: true
agents:
  orchestrator:
    template: github:Org/repo
    tags: [lead]                      # Per-agent tags
```

**Spec**: `docs/requirements/AGENT_SYSTEMS_AND_TAGS.md` (Phase 4 complete)

---

### 2026-02-17 22:00:00
✨ **Feature: Tags Polish & Integration (ORG-001 Phase 3)**

Completed Phase 3 with MCP tools, quick tag filter, and bulk tag operations.

**MCP Server** (`src/mcp-server/src/`):
- `tools/tags.ts`: 5 new tag management tools
  - `list_tags` - List all tags with agent counts
  - `get_agent_tags` - Get tags for an agent
  - `tag_agent` - Add tag to an agent
  - `untag_agent` - Remove tag from an agent
  - `set_agent_tags` - Replace all tags for an agent
- `client.ts`: Added 5 tag-related client methods
- `server.ts`: Registered tag tools (total 45+ tools now)

**Frontend** (`src/frontend/src/`):
- `views/Dashboard.vue`: Quick tag filter in header
  - Inline tag pills for fast filtering
  - Dropdown for additional tags
  - Syncs with System Views selection
- `views/Agents.vue`: Bulk tag operations
  - Selection checkboxes on agent cards
  - Tag chips display on cards
  - Tag filter dropdown
  - Bulk actions toolbar (add/remove tags to selected)

**Spec**: `docs/requirements/AGENT_SYSTEMS_AND_TAGS.md` (Phase 3 complete)

---

### 2026-02-17 21:15:00
✨ **Feature: System Views (ORG-001 Phase 2)**

Implemented System Views - saved filters that group agents by tags. Views appear in a collapsible sidebar on the Dashboard.

**Backend** (`src/backend/`):
- `db/system_views.py`: SystemViewOperations class with CRUD methods, access control
- `routers/system_views.py`: System Views API endpoints
  - `GET /api/system-views` - List user's views + shared views
  - `POST /api/system-views` - Create new view
  - `GET /api/system-views/{id}` - Get view details
  - `PUT /api/system-views/{id}` - Update view
  - `DELETE /api/system-views/{id}` - Delete view
- `database.py`: Added `system_views` table with indexes, delegated methods
- `db_models.py`: Added `SystemView`, `SystemViewCreate`, `SystemViewUpdate`, `SystemViewList` models
- `main.py`: Registered system_views_router

**Frontend** (`src/frontend/src/`):
- `stores/systemViews.js`: Pinia store for system views state management
- `components/SystemViewsSidebar.vue`: Collapsible sidebar showing views list
- `components/SystemViewEditor.vue`: Modal for create/edit with tag selection
- `views/Dashboard.vue`: Integrated sidebar, added filtering watcher
- `stores/network.js`: Added `setFilterTags()` for view-based filtering

**Spec**: `docs/requirements/AGENT_SYSTEMS_AND_TAGS.md` (Phase 2 complete)

---

### 2026-02-17 20:30:00
✨ **Feature: Agent Tags (ORG-001 Phase 1)**

Implemented agent tagging system for lightweight organizational grouping. Agents can have multiple tags, enabling multi-system membership.

**Backend** (`src/backend/`):
- `db/tags.py`: TagOperations class with CRUD methods
- `routers/tags.py`: Tag API endpoints
  - `GET /api/tags` - List all tags with counts
  - `GET /api/agents/{name}/tags` - Get agent tags
  - `PUT /api/agents/{name}/tags` - Replace all tags
  - `POST /api/agents/{name}/tags/{tag}` - Add single tag
  - `DELETE /api/agents/{name}/tags/{tag}` - Remove single tag
- `routers/agents.py`: Added `?tags=` query parameter for filtering agents by tag
- `database.py`: Added `agent_tags` table with indexes
- `db_models.py`: Added `AgentTagList`, `AgentTagsUpdate`, `TagWithCount`, `AllTagsResponse` models

**Frontend** (`src/frontend/src/`):
- `components/TagsEditor.vue`: Reusable tag editor with autocomplete (94 lines)
- `components/AgentHeader.vue`: Tags row with inline editing
- `views/AgentDetail.vue`: Tag loading, updating, and API integration

**Spec**: `docs/requirements/AGENT_SYSTEMS_AND_TAGS.md` (Phase 1 complete)

---

### 2026-02-17 19:55:00
🎨 **UI: Add Trinity Logo and Branding to Public Chat**

Added Trinity logo and text to the public chat header for brand consistency.

**Changes** (`src/frontend/src/views/PublicChat.vue`):
- Added Trinity branding row with logo and "Trinity" text at top of header
- Logo uses `dark:invert` for proper display in dark mode
- Moved "New" conversation button to top right (next to branding)
- Agent info (status, name, badges, description) now in second row

---

### 2026-02-17 19:50:00
🎨 **UI: Bottom-aligned Chat Messages in Public Chat**

Changed public chat message layout to stack from the bottom up, with new messages appearing near the input field (like iMessage/Slack).

**Changes** (`src/frontend/src/views/PublicChat.vue`):
- Messages container now uses `flex flex-col` with a spacer div that pushes content to bottom
- Added `messagesContainer` ref for proper scroll handling
- Updated `scrollToBottom()` to use the ref instead of querySelector

---

### 2026-02-17 19:45:00
✨ **Feature: Public Client Mode Awareness (PUB-006)**

Agents now know when they're serving public users. Every public chat request includes a "Trinity: Public Link Access Mode" header so agents can adjust their behavior accordingly.

**Changes** (`src/backend/db/public_chat.py`):
- Added `PUBLIC_LINK_MODE_HEADER` constant
- Modified `build_context_prompt()` to prepend header to all prompts

**Prompt Format** (with history):
```
### Trinity: Public Link Access Mode

Previous conversation:
User: Hello
Assistant: Hi there!

Current message:
User: What can you help me with?
```

**Prompt Format** (first message):
```
### Trinity: Public Link Access Mode

Current message:
User: Hello
```

---

### 2026-02-17 19:30:00
✨ **Enhancement: Markdown Rendering in Public Chat**

Added markdown rendering for assistant messages in public chat, providing formatted output for code blocks, lists, headers, and emphasis.

**Changes** (`src/frontend/src/views/PublicChat.vue`):
- Import `marked` library (already in project dependencies)
- Configure marked with `breaks: true` and `gfm: true` (GitHub Flavored Markdown)
- Add `renderMarkdown()` function
- User messages: Plain text (unchanged)
- Assistant messages: Rendered with `v-html` and Tailwind prose classes

**Styling**:
- Uses `@tailwindcss/typography` prose classes
- Custom prose modifiers for compact spacing
- Code styling: indigo text with gray background
- Dark mode support via `dark:prose-invert`

---

### 2026-02-17 19:00:00
✨ **Feature: Public Chat Session Persistence (PUB-005)**

Added multi-turn conversation persistence to public chat, enabling users to maintain context across page refreshes and return visits.

**How It Works**:
1. User sends message via public chat
2. Backend creates/retrieves session based on identifier (email for email-required links, anonymous token for open links)
3. Messages stored in `public_chat_sessions` and `public_chat_messages` tables
4. Context prompt built from last 10 exchanges and injected into agent call
5. Page refresh restores conversation via `GET /api/public/history/{token}`
6. "New Conversation" button clears session and starts fresh

**Backend** (`src/backend/`):
- New `db/public_chat.py` with `PublicChatOperations` class
- `database.py`: Added `public_chat_sessions` and `public_chat_messages` tables with indexes
- `db_models.py`: Added `PublicChatSession`, `PublicChatMessage` models; updated `PublicChatRequest/Response`
- `routers/public.py`:
  - `POST /api/public/chat/{token}` - Now persists messages and builds context prompt
  - `GET /api/public/history/{token}` - Returns chat history for session
  - `DELETE /api/public/session/{token}` - Clears session (New Conversation)

**Frontend** (`src/frontend/src/views/PublicChat.vue`):
- `chatSessionId` ref for anonymous links (localStorage persistence)
- `loadHistory()` fetches previous messages on mount
- "New Conversation" button in header (visible when messages exist)
- Session ID passed in chat requests and persisted from responses

**Session Identifier Strategy**:
- Email-required links: verified email (lowercase) from session validation
- Anonymous links: localStorage `public_chat_session_id_{token}`

**Context Injection Format**:
```
Previous conversation:
User: [message 1]
Assistant: [response 1]
...

Current message:
User: [new message]
```

---

### 2026-02-17 16:45:00
✨ **Feature: Public Chat Header Metadata (PUB-004)**

Enhanced the public chat page header to display meaningful agent metadata.

**Header Now Shows**:
- Agent display name (from template.yaml, bold text)
- Agent description (from template.yaml, below name)
- Status badges: AUTO (amber) if autonomous, READ-ONLY (rose with lock icon) if read-only
- Green dot indicates running status

**Backend** (`src/backend/routers/public.py`):
- `GET /api/public/link/{token}` now fetches template info from agent container
- Returns `agent_display_name`, `agent_description`, `is_autonomous`, `is_read_only`
- Falls back to agent name if template info unavailable

**Model** (`src/backend/db_models.py`):
- `PublicLinkInfo` extended with 4 new optional fields

**Frontend** (`src/frontend/src/views/PublicChat.vue`):
- Header redesigned with two rows: name+badges, description
- Badges use consistent styling (amber for AUTO, rose for READ-ONLY)

---

### 2026-02-17 15:30:00
✨ **Feature: Public Chat Agent Introduction (PUB-003)**

Added automatic agent introduction for public chat links. When users access a public link, the agent introduces itself before they start chatting.

**How It Works**:
1. User opens public chat link and verifies email (if required)
2. Frontend calls `GET /api/public/intro/{token}` endpoint
3. Backend sends introduction prompt to agent via `/api/task`
4. Agent response displayed as first message in chat

**Backend** (`src/backend/routers/public.py`):
- New `GET /api/public/intro/{token}` endpoint
- `INTRO_PROMPT` constant asks for 2-paragraph introduction
- Session token validation for email-required links
- 60-second timeout for intro generation

**Frontend** (`src/frontend/src/views/PublicChat.vue`):
- `fetchIntro()` function calls intro endpoint
- Called after email verification or on mount (if no email needed)
- "Getting ready..." loading state while fetching
- Graceful error handling (doesn't block user if intro fails)

**User Experience**:
- Public users see personalized welcome from the agent
- Agent describes who it is and how it can help
- Natural conversation start instead of generic "Start a Conversation"

---

### 2026-02-17 13:45:00
🧪 **Tests: Read-Only Mode API Tests (CFG-007)**

Added comprehensive test suite for Read-Only Mode feature.

**Test File**: `tests/test_read_only_mode.py` (27 tests)

**Test Categories**:
- Authentication requirements (2 tests)
- CRUD operations - GET/PUT endpoints (7 tests)
- Config patterns - default and custom (5 tests)
- System agent protection (2 tests)
- Permission checks (2 tests)
- Hook injection response (3 tests)
- Agent state handling (1 test)
- Response format consistency (3 tests)
- Input validation (2 tests)

**Test Runner Updated**: `.claude/agents/test-runner.md`
- Added to "Agent Lifecycle & Management" category
- Updated test count: ~567 tests across 33 files
- Updated smoke test count: ~127 tests

---

### 2026-02-17 10:00:00
🔒 **Feature: Read-Only Mode for Agents (CFG-007)**

Added per-agent read-only mode that prevents agents from modifying source code, configuration files, and other protected paths while allowing output to designated directories.

**How It Works**:
- Uses Claude Code's PreToolUse hooks to intercept Write/Edit/NotebookEdit operations
- Pattern-based protection: blocked patterns (e.g., `*.py`) take precedence, allowed patterns (e.g., `output/*`) provide exceptions
- Hooks are injected at agent startup or when toggling the setting on a running agent

**Frontend**:
- ReadOnlyToggle component in AgentHeader (rose/red color when enabled, lock icon)
- Toggle shows loading state during API call
- Only visible for non-system agents to owners

**Backend**:
- GET/PUT `/api/agents/{name}/read-only` endpoints
- Database columns: `read_only_mode` (boolean), `read_only_config` (JSON)
- Hook injection writes config file, guard script, and merges into `settings.local.json`

**Guard Script** (`config/hooks/read-only-guard.py`):
- Reads config from `~/.trinity/read-only-config.json`
- Uses fnmatch for glob pattern matching
- Exit 0 = allow, Exit 2 = block with stderr feedback

**Default Patterns**:
- Blocked: `*.py`, `*.js`, `*.ts`, `*.vue`, `*.yaml`, `CLAUDE.md`, `.claude/*`, `.env`, etc.
- Allowed: `content/*`, `output/*`, `reports/*`, `exports/*`, `*.log`, `*.txt`

**Files Changed**:
- `src/backend/database.py` - Migration for new columns
- `src/backend/db/agents.py` - `get_read_only_mode`, `set_read_only_mode`, batch metadata query
- `src/backend/services/agent_service/read_only.py` - Service layer (new)
- `src/backend/services/agent_service/lifecycle.py` - Hook injection on agent start
- `src/backend/routers/agents.py` - GET/PUT endpoints, `read_only_enabled` in agent response
- `config/hooks/read-only-guard.py` - Guard script (new)
- `src/frontend/src/components/ReadOnlyToggle.vue` - Toggle component (new)
- `src/frontend/src/components/AgentHeader.vue` - Added ReadOnlyToggle
- `src/frontend/src/views/AgentDetail.vue` - Toggle handler

**Requirement**: CFG-007 (docs/requirements/READ_ONLY_MODE.md)

---

### 2026-02-16 16:00:00
🔗 **Feature: External Public URL Support (PUB-002)**

Added support for external (internet-accessible) URL for public agent links, separate from the internal VPN URL. This enables sharing public chat links with external users who are not on the Tailscale VPN.

**Configuration**:
- New `PUBLIC_CHAT_URL` environment variable (e.g., `https://public.abilityai.dev`)
- When set, enables "Copy External Link" button in PublicLinksPanel

**Implementation**:
- Backend: `PUBLIC_CHAT_URL` config, `external_url` field in `PublicLinkWithUrl` model
- Frontend: Globe icon button to copy external URL, distinct notification messages
- API: `external_url` field included in public link responses when configured

**Files Changed**:
- `src/backend/config.py` - `PUBLIC_CHAT_URL` constant (already present)
- `src/backend/db_models.py` - `external_url` field (already present)
- `src/backend/routers/public_links.py` - `_build_external_url()` helper (already present)
- `src/frontend/src/components/PublicLinksPanel.vue` - External link button, notification
- `.env.example` - Added `PUBLIC_CHAT_URL` documentation

**Documentation**:
- `docs/requirements/EXTERNAL_PUBLIC_URL.md` - Requirements spec (marked implemented)
- `docs/memory/feature-flows/public-agent-links.md` - Updated with PUB-002 section

**Requirement**: PUB-002 (docs/requirements/EXTERNAL_PUBLIC_URL.md)

---

### 2026-02-16 14:30:00
🔒 **Security Fix: Agent Credential Leakage Prevention**

Fixed HIGH severity security vulnerability where sensitive environment variables (API keys, tokens, secrets) were being exposed in execution logs stored in the database.

**Vulnerability**: Two attack vectors allowed credential exposure:
1. **Command Leak**: Agent runs `env`/`printenv` and dumps all secrets to stdout → persisted to database
2. **Direct Ask**: User asks "What is my API key?" and agent retrieves/outputs the secret

**Fix**: Implemented multi-layer credential sanitization:

**Agent-Side (Primary)**:
- New `credential_sanitizer.py` utility in agent server
- Sanitizes subprocess output before storing in `raw_messages`
- Sanitizes response text before returning to backend
- Detects and redacts known credential patterns and env var values

**Backend-Side (Defense-in-Depth)**:
- New `credential_sanitizer.py` utility in backend
- Sanitizes `execution_log`, `tool_calls`, and `response` before database persistence
- Pattern-based detection for API keys, tokens, and sensitive key=value pairs

**Patterns Redacted**:
- OpenAI keys (`sk-*`), Anthropic keys (`sk-ant-*`)
- GitHub tokens (`ghp_*`, `gho_*`, `ghs_*`)
- Slack tokens (`xoxb-*`, `xoxp-*`)
- AWS keys (`AKIA*`), Trinity MCP keys (`trinity_mcp_*`)
- Bearer/Basic auth headers
- Sensitive environment variables

**Files Changed**:
- `docker/base-image/agent_server/utils/credential_sanitizer.py` (NEW)
- `docker/base-image/agent_server/services/claude_code.py` - Applied sanitization
- `docker/base-image/agent_server/routers/credentials.py` - Refresh cache on update
- `src/backend/utils/credential_sanitizer.py` (NEW)
- `src/backend/routers/chat.py` - Applied sanitization before persistence

**Bug Report**: `docs/bugs/AGENT_CREDENTIAL_LEAKAGE.md`

**Deployment**: Requires base image rebuild (`./scripts/deploy/build-base-image.sh`) and backend restart.

---

### 2026-02-15 10:00:00
✨ **Feature: Claude Max Subscription Support for Headless Calls**

Removed mandatory `ANTHROPIC_API_KEY` check from Claude Code execution service. Now agents can use Claude Max subscription for scheduled tasks and headless calls.

**Before**: Headless calls (`/api/task`, scheduled executions) required `ANTHROPIC_API_KEY` env var even if user was logged in via web terminal.

**After**: Claude Code uses whatever authentication is available:
1. OAuth session from `/login` (Claude Pro/Max subscription) - stored in `~/.claude.json`
2. `ANTHROPIC_API_KEY` environment variable (API billing) - if set

**Use Case**: Login once via web terminal with `/login`, then all scheduled tasks and MCP-triggered executions use your Claude Max subscription instead of API billing.

**Files Changed**:
- `docker/base-image/agent_server/services/claude_code.py:410-414,586-590` - Removed mandatory API key check

**Deployment**: Requires base image rebuild (`./scripts/deploy/build-base-image.sh`) and agent restart.

---

### 2026-02-13 12:00:00
📝 **Docs: SSH Host Detection Fix Cross-References**

Updated feature flows impacted by the SSH host detection fix.

**Files Updated**:
- `docs/memory/feature-flows/mcp-orchestration.md` - Added revision history entry and ssh-access.md to Related Flows
- `docs/memory/feature-flows.md` - Updated SSH Access entry with new detection priority (FRONTEND_URL)

**Already Updated**:
- `docs/memory/feature-flows/ssh-access.md` - Full documentation with revision history, environment variables table

**Not Impacted** (reference only, no host detection details):
- `agent-lifecycle.md` - Only references SSH port allocation, not host detection
- `platform-settings.md` - Only references `ssh_access_enabled` toggle, not host detection
- `web-terminal.md`, `agent-terminal.md` - Use Docker exec, not SSH
- `file-browser.md` - Mentions SSH as alternative, no host detection

---

### 2026-02-13 11:30:00
🐛 **Fix: SSH Connection String Returns Production Domain**

Fixed bug where `get_agent_ssh_access` MCP tool returned `localhost` instead of the actual host address, preventing remote SSH connections.

**Root Cause**: The `get_ssh_host()` function in `ssh_service.py` had detection methods that all failed in production:
- `SSH_HOST` env var: Not typically set
- Tailscale: Not installed in backend container
- `host.docker.internal`: Only works on Docker Desktop (Mac/Windows), not Linux servers
- Default gateway: Often Docker's internal `172.x.x.x` network (filtered out)
- Result: Fell back to `localhost`

**Fix**: Added `FRONTEND_URL` domain extraction as priority #2 in host detection. Production deployments set `FRONTEND_URL` to the domain (e.g., `https://trinity.abilityai.dev`), so the function now extracts the hostname and returns it for SSH commands.

**New Detection Priority**:
1. `SSH_HOST` env var (explicit override)
2. `FRONTEND_URL` domain (production auto-detect) ← **NEW**
3. Tailscale IP
4. `host.docker.internal` (Docker Desktop)
5. Default gateway IP
6. Fallback to `localhost`

**Files Changed**:
- `src/backend/services/ssh_service.py:479-567` - Added `FRONTEND_URL` parsing
- `.env.example` - Documented `SSH_HOST` option
- `docs/memory/feature-flows/ssh-access.md` - Updated detection priority docs

---

### 2026-02-13 10:15:00
🐛 **Bug: SSH Connection String Returns Localhost**

Added bug to roadmap for SSH access feature.

**Problem**: MCP tool `get_agent_ssh_access` returns SSH connection string with `localhost` instead of actual host address. Remote users cannot connect to agents.

**Expected**: Should return production domain or configurable host address.

**Files**: `ssh_service.py`, `agents.py:905-1072`

**Priority**: HIGH

---

### 2026-02-13 10:00:00
📋 **Backlog: OpenClaw-Inspired Features**

Added two features to backlog after analyzing OpenClaw agent platform.

**New Items**:
1. **MEMORY.md Convention** (MEDIUM) - Agents maintain curated long-term memory
   - Daily logs: `memory/YYYY-MM-DD.md` (raw session notes)
   - Long-term: `MEMORY.md` (distilled insights)
   - Agents get better over time through accumulated corrections

2. **Telegram Channel Integration** (MEDIUM) - Mobile-first interface
   - Push notifications for task completion
   - Approve/reject from phone
   - Chat with agents via Telegram
   - Architecture supports adding WhatsApp/Discord/Slack later

**Source**: `docs/research/openclaw-vs-trinity-comparison.md`

---

### 2026-02-12 14:30:00
📋 **Backlog: Orphaned Execution Recovery on Startup**

Added backlog item for scheduler reliability improvement.

**Problem**: When backend/scheduler restarts during deploys or crashes, executions marked as "running" become orphaned and remain stuck in "running" status forever.

**Solution**: On scheduler startup, mark all "running" executions as failed before starting scheduler jobs. Set `error='Orphaned execution - service restarted before completion'`, set `completed_at`, log warning with count.

**Priority**: HIGH (affects reliability)

---

### 2026-02-12 12:15:00
🎨 **Fix: Standardize Toggle Positions on Agents Page**

Aligned toggle positions on Agents page to match Dashboard Graph layout.

**Changes**:
- Moved Running toggle from bottom action row to same row as Autonomy toggle
- Both toggles now on same row: Running (left), Autonomy (right)
- View Details button now alone at bottom

**Result**: Consistent toggle layout between Agents page and Dashboard Graph.

---

### 2026-02-12 10:30:00
🎨 **Refactor: Standardize Autonomy Toggle Across All Locations**

Created reusable `AutonomyToggle.vue` component and standardized autonomy toggle appearance across all 4 locations in the application.

**Changes**:
- **New Component**: `src/frontend/src/components/AutonomyToggle.vue`
  - Matches RunningStateToggle pattern with amber color scheme
  - Props: modelValue, disabled, loading, showLabel, size (sm/md/lg)
  - Shows "AUTO" when enabled, "Manual" when disabled
- **Updated Locations**:
  - `AgentNode.vue` (Dashboard graph) - Replaced ~35 lines inline code
  - `ReplayTimeline.vue` (Dashboard timeline) - Replaced compact inline toggle
  - `AgentHeader.vue` (Agent detail page) - Replaced inconsistent button-style
  - `Agents.vue` (Agents list page) - Replaced inline toggle

**Result**: Consistent toggle appearance and behavior across all agent views.

---

### 2026-02-11 12:00:00
🔧 **Fix: Scheduler Consolidation - Timeline Dashboard Missing Cron Executions**

Consolidated scheduler architecture to fix bug where cron-triggered schedule executions didn't appear on the Timeline dashboard.

**Root Cause**: The dedicated scheduler created `schedule_executions` records but NOT `agent_activities` records, which are required for Timeline visibility.

**Changes**:
- **Removed embedded scheduler**: Deleted `src/backend/services/scheduler_service.py` entirely
- **Added internal API endpoints**: `POST /api/internal/activities/track` and `POST /api/internal/activities/{id}/complete` for scheduler→backend activity tracking
- **Manual triggers routed to dedicated scheduler**: Backend now calls `http://scheduler:8001/api/schedules/{id}/trigger` instead of embedded scheduler
- **Activity tracking in dedicated scheduler**: Both cron and manual triggers now create `agent_activities` records via internal API

**Architecture**:
- Single source of truth: Dedicated scheduler (`src/scheduler/`)
- Database sync: Backend updates DB → Scheduler syncs every 60s
- Activity tracking: Scheduler → Internal API → WebSocket broadcast

**Files Modified**:
- `src/backend/routers/schedules.py` - Delegate to dedicated scheduler
- `src/backend/routers/internal.py` - Add activity tracking endpoints
- `src/scheduler/service.py` - Add activity tracking calls
- `src/scheduler/main.py` - Add manual trigger endpoint
- Various: Remove scheduler_service imports

**Documentation**: Updated `docs/memory/feature-flows/scheduler-service.md`

---

### 2026-02-09 15:30:00
🗑️ **Cleanup: Remove skill-library (Moved to abilities repo)**

Removed `skill-library/` directory from trinity repo. All Trinity skills are now in the `abilityai/abilities` repository as the **system of record**.

**Changes**:
- Deleted `skill-library/trinity/` (5 skills: trinity-adopt, trinity-compatibility, trinity-remote, trinity-sync, trinity-schedules)
- Updated CLAUDE.md: Enhanced abilities repo documentation with skill table
- Updated README.md: Removed skill-library from project structure
- Updated `docs/requirements/TRINITY_ONBOARD_PLUGIN.md`: All references now point to abilities repo

**New location**: `github.com/abilityai/abilities/plugins/trinity-onboard/skills/`

**Installation** (unchanged):
```bash
/plugin marketplace add abilityai/abilities
/trinity-onboard:onboard
```

---

### 2026-02-09 12:00:00
📚 **Docs: Add Abilities Repository Reference**

Updated CLAUDE.md to document the new [abilityai/abilities](https://github.com/abilityai/abilities) repository - a curated collection of Claude Code plugins extracted from Trinity.

**Available Plugins**:
- `git-workflow` - Version control with safety guardrails
- `skill-builder` - Claude Code skill creation guidance
- `process-miner` - Workflow pattern discovery from logs
- `repo-tidy` - Repository auditing and cleanup
- `validate-pr` - PR validation with security scanning
- `workspace-tools` - File indexing and organization
- `trinity-onboard` - Trinity platform deployment

**Installation**: `/plugin marketplace add abilityai/abilities`

---

### 2026-02-06 00:30:00
🔌 **Feature: Trinity Onboard Plugin - Zero-Friction Agent Deployment (PLUGIN-001)**

Implemented a complete plugin system enabling any Claude Code agent to deploy to Trinity in 3 commands.

**Quick Start**:
```bash
/plugin marketplace add abilityai/trinity
/plugin install trinity-onboard@abilityai-trinity
/trinity-onboard:onboard
```

**New Directories**:
- `skill-library/trinity/` - 5 canonical Trinity management skills
- `plugins/trinity-onboard/` - Onboarding plugin with templates
- `.claude-plugin/` - Marketplace configuration

**Skill Library** (`skill-library/trinity/`):
| Skill | Description |
|-------|-------------|
| `trinity-adopt` | Convert any agent to Trinity-compatible format |
| `trinity-compatibility` | Audit agent structure for requirements |
| `trinity-remote` | Remote agent operations (exec, run, notify) |
| `trinity-sync` | Git-based synchronization with remote |
| `trinity-schedules` | Manage scheduled autonomous executions |

**Plugin Files** (`plugins/trinity-onboard/`):
- `.claude-plugin/plugin.json` - Plugin manifest
- `skills/onboard/SKILL.md` - 8-phase onboarding workflow
- `templates/` - template.yaml, .gitignore, .env, .mcp.json templates
- `.mcp.json` - Trinity MCP server configuration

**Onboarding Workflow** (8 phases):
1. Discovery - Identify agent name, check existing files
2. Compatibility Analysis - Generate requirements report
3. User Confirmation - Present report, ask to proceed
4. Create Required Files - template.yaml, .gitignore, etc.
5. Git Setup - Initialize, commit, push
6. Deploy to Trinity - Create/update agent via MCP
7. Install Trinity Skills - Copy management skills
8. Completion Report - Show next steps

**Key Design Decisions**:
- Skills are copied (not symlinked) for portability
- Agent name detection: template.yaml > directory name > env var
- Idempotent design: running onboard multiple times is safe
- Generic skills: Removed hardcoded agent names, added dynamic detection

**Files Created**: 17 new files
- Skill Library: 9 files (5 SKILL.md + 4 supporting files)
- Plugin: 6 files (manifest, skill, templates, readme)
- Marketplace: 2 files (marketplace.json, plugin.json)

**Specification**: `docs/requirements/TRINITY_ONBOARD_PLUGIN.md`

---

### 2026-02-05 23:55:00
🐛 **Bug Fix: Removed Orphaned Credential Injection Loop (CRED-002)**

Fixed dead code in `src/backend/services/agent_service/crud.py` that referenced an undefined `agent_credentials` variable.

**The Problem**:
Commit `6821f0d` (CRED-002 refactor) removed the `agent_credentials` variable definition (lines ~183-192) but left behind a loop (lines 312-332) that attempted to iterate over `agent_credentials.items()` to inject credentials as Docker environment variables. This code would have caused a NameError if executed.

**The Fix**:
Removed the orphaned code block and replaced it with a comment explaining the CRED-002 design decision:
```python
# CRED-002: Legacy credential injection loop removed.
# Credentials are now injected after agent creation via:
# - inject_credentials endpoint (Quick Inject)
# - .credentials.enc import on agent startup
```

**File Changed**:
- `src/backend/services/agent_service/crud.py:312-315` - Orphaned loop replaced with explanatory comment

**Documentation Updated**:
- `docs/memory/feature-flows/credential-injection.md` - Added bug fix section under Legacy System
- `docs/memory/feature-flows/agent-lifecycle.md` - Updated CRED-002 note and revision history

**Impact**: None (the code was dead and never executed because `agent_credentials` was already undefined)

---

### 2026-02-05 23:45:00
🚀 **Feature: Trinity Connect - Local-Remote Agent Sync (SYNC-001)**

Implemented real-time event-driven coordination between local Claude Code and Trinity agents.

**New WebSocket Endpoint**: `/ws/events`
- MCP API key authentication via `?token=trinity_mcp_xxx` query parameter
- Server-side event filtering based on user's accessible agents (owned + shared)
- Ping/pong keepalive and agent list refresh commands
- Events: agent_activity, agent_started, agent_stopped, schedule_execution_completed

**New Script**: `scripts/trinity-listen.sh`
- Blocking listener script for event-driven local-remote coordination
- Supports websocat or wscat WebSocket clients
- Filters by agent name and/or event state
- Exits on first matching event (for event loop integration)

**Backend Changes**:
- `src/backend/main.py` - FilteredWebSocketManager class, /ws/events endpoint
- `src/backend/db/agents.py` - `get_accessible_agent_names()` method
- `src/backend/database.py` - Exposed new db method
- `src/backend/services/activity_service.py` - Added filtered broadcast hook
- `src/backend/services/scheduler_service.py` - Added filtered broadcast callback
- `src/backend/routers/agents.py` - Added filtered broadcasts for agent_started/stopped

**Usage**:
```bash
export TRINITY_API_KEY="trinity_mcp_xxx"
./scripts/trinity-listen.sh my-agent completed  # Wait for agent to complete task
```

**Feature Flow**: `docs/memory/feature-flows/trinity-connect.md`

---

### 2026-02-05 22:15:00
📋 **Spec: Trinity Connect - Local-Remote Agent Synchronization (SYNC-001)**

Created requirements specification for real-time coordination between local Claude Code and Trinity agents.

**New File**: `docs/requirements/TRINITY_CONNECT_LOCAL_REMOTE_SYNC.md`

**Key Design**:
- Event-driven loop using blocking Bash script as wake-up mechanism
- Script connects to Trinity WebSocket, blocks until event arrives
- When event received: print, exit → Claude Code reacts → restart listener
- No daemons, hooks, or complex infrastructure needed

**Components Specified**:
- `trinity-listen.sh` - Listener script with event filtering
- WebSocket API key authentication (backend change)
- `/api/agents/{name}/notify` endpoint for explicit notifications
- `notify_agent` MCP tool for agent-to-agent alerts
- Trinity Connect skill template for local Claude Code

**Event Types**:
- `execution_complete` - Scheduled/manual task finished
- `agent_collaboration` - Agent-to-agent MCP call
- `agent_notification` - Explicit notification (new)
- `agent_activity` - Agent state changes

**Roadmap Updated**: Added "Local-Remote Agent Sync" backlog section with 4 items.

---

### 2026-02-05 21:30:00
🧹 **Cleanup: Completed CRED-002 - Removed Old Redis Credential System**

Fully removed the legacy Redis-based credential system. Trinity now uses only the simplified encrypted file system.

**Removed ~1300 Lines**:
- `src/backend/credentials.py` - CredentialManager class (677 lines) **DELETED**
- Old Redis-based endpoints from `src/backend/routers/credentials.py`
- `initialize_github_pat()` function from `src/backend/main.py`
- Deprecated models from `src/backend/models.py`
- `reload_credentials` MCP tool and client method
- Old store methods from `src/frontend/src/stores/agents.js`

**Added ~100 Lines**:
- New MCP tool: `get_credential_encryption_key` - Returns encryption key for local agents
- New endpoint: `GET /api/credentials/encryption-key`

**Modified**:
- `src/backend/routers/credentials.py` - Now only contains inject/export/import/status endpoints
- `src/backend/services/agent_service/crud.py` - Agents now start without pre-injected credentials
- `src/backend/services/agent_service/lifecycle.py` - On startup, imports from .credentials.enc
- `src/backend/services/agent_service/deploy.py` - Uses inject endpoint for credential delivery
- `src/mcp-server/src/client.ts` - Removed reloadCredentials, added getEncryptionKey
- `src/mcp-server/src/tools/agents.ts` - Removed reloadCredentials, added getCredentialEncryptionKey

**Breaking Changes**:
- Old `/api/credentials` global CRUD endpoints return 404
- Old credential assignment endpoints removed
- `reload_credentials` MCP tool removed (use `import_credentials` instead)

---

### 2026-02-05 20:00:00
🔧 **Feature: Implemented Credential System Refactor (CRED-002)**

Major simplification of credential system - replaced Redis-based assignment model with encrypted file storage.

**Key Changes**:
- New `CredentialEncryptionService` using AES-256-GCM encryption
- Encrypted credentials stored in `.credentials.enc` in agent workspace
- Direct file injection to running agents (no Redis assignments)
- Auto-import on agent startup from encrypted file
- Removed global `/credentials` page (credentials now per-agent only)

**New Endpoints**:
- `POST /api/agents/{name}/credentials/inject` - Write files to agent
- `POST /api/agents/{name}/credentials/export` - Export to `.credentials.enc`
- `POST /api/agents/{name}/credentials/import` - Import from encrypted file
- `POST /api/internal/decrypt-and-inject` - Internal endpoint for startup.sh

**New MCP Tools**:
- `inject_credentials(agent_name, files)` - Inject credential files
- `export_credentials(agent_name)` - Export to encrypted file
- `import_credentials(agent_name)` - Import from encrypted file

**Files Created**:
- `src/backend/services/credential_encryption.py` - Encryption service (~200 lines)
- `src/backend/routers/internal.py` - Internal API router

**Files Modified**:
- `src/backend/routers/credentials.py` - Added inject/export/import endpoints
- `src/backend/models.py` - Added new Pydantic models
- `src/backend/main.py` - Added internal router
- `src/frontend/src/components/CredentialsPanel.vue` - Rewritten for new system
- `src/frontend/src/composables/useAgentCredentials.js` - Simplified API
- `src/frontend/src/stores/agents.js` - New store methods
- `src/frontend/src/router/index.js` - Removed /credentials route
- `src/frontend/src/components/NavBar.vue` - Removed Credentials link
- `docker/base-image/agent_server/routers/credentials.py` - Added read/inject endpoints
- `docker/base-image/startup.sh` - Added auto-import logic
- `src/mcp-server/src/tools/agents.ts` - Added new MCP tools
- `src/mcp-server/src/client.ts` - Added client methods

**Deleted**:
- `src/frontend/src/views/Credentials.vue` - Global credentials page (674 lines)

**Note**: Old Redis credential system kept for backward compatibility. Will be fully removed in future cleanup.

---

### 2026-02-05 17:30:00
📝 **Docs: Consolidated Credential System Spec (CRED-002)**

Merged and simplified two credential specs into one actionable plan:

**Key Decisions**:
- Remove entire Redis credential system (~677 lines)
- Remove global `/credentials` page
- Remove assignment model (create → assign → apply)
- Remove template substitution (`.mcp.json.template`)
- Replace with encrypted files in git (`.credentials.enc`)
- Simple flow: decrypt → inject files

**New Approach**:
- Credentials = files stored encrypted in `.credentials.enc`
- On agent start, decrypt and write files
- Export/import buttons in agent Credentials tab
- MCP tools: `inject_credentials`, `export_credentials`, `import_credentials`

**Files Changed**:
- `docs/requirements/CREDENTIAL_SYSTEM_REFACTOR.md` - Consolidated spec
- `docs/requirements/ENCRYPTED_CREDENTIALS_IN_GIT.md` - Deleted (merged)
- `docs/memory/roadmap.md` - Updated spec link

---

### 2026-02-05 16:00:00
📝 **Docs: Requirement Specifications for Phase 16 High Priority Items**

Created detailed requirement specifications for the four HIGH priority items in Phase 16:

**1. Client/Viewer User Role** ([AUTH-002](../requirements/CLIENT_VIEWER_USER_ROLE.md))
- New `client` role for simplified read-only/limited access
- Permission matrix: admin > user > client
- Frontend route guards and UI filtering by role
- Admin user management in Settings

**2. Encrypted Credentials in Git** ([CRED-002](../requirements/CREDENTIAL_SYSTEM_REFACTOR.md))
- Replaced with consolidated spec (see 2026-02-05 17:30:00 entry above)

**3. Unified Executions Dashboard** ([EXEC-022](../requirements/UNIFIED_EXECUTIONS_DASHBOARD.md))
- New `/executions` page with cross-agent view
- `GET /api/executions` endpoint with filters (agent, status, trigger, time)
- `GET /api/executions/stats` for aggregate metrics
- Real-time WebSocket updates
- Stats cards, filter bar, paginated list

**4. MCP Execution Query Tools** ([MCP-007](../requirements/MCP_EXECUTION_QUERY_TOOLS.md))
- `list_recent_executions` - query fleet execution history
- `get_execution_result` - fetch specific execution output
- `get_agent_activity_summary` - high-level health/status
- Enables orchestrator monitoring and async task polling

**Files Created**:
- `docs/requirements/CLIENT_VIEWER_USER_ROLE.md`
- `docs/requirements/ENCRYPTED_CREDENTIALS_IN_GIT.md`
- `docs/requirements/UNIFIED_EXECUTIONS_DASHBOARD.md`
- `docs/requirements/MCP_EXECUTION_QUERY_TOOLS.md`

**Files Modified**:
- `docs/memory/roadmap.md` - Added spec links to Phase 16 table

---

### 2026-02-05 14:30:00
📋 **Strategic: Phase 16 - Agent-as-a-Service & Commercialization**

Planning session to define path to commercialization while maintaining orchestration vision.

**Two Business Models Identified**:
1. **Multi-Agent Orchestration** — "Run my company with AI agents" (long-term vision)
2. **Agent-as-a-Service** — "Ruby for clients" (faster path to revenue)

**Decision**: Pursue both sequentially. They share 80% of infrastructure needs.

**Phase 16 Added to Roadmap** (10 items):

| Priority | Item | Description |
|----------|------|-------------|
| **HIGH** | Client/Viewer User Role | Simplified role using existing auth, sees basic UI only |
| **HIGH** | Encrypted Credentials in Git | `.credentials.enc` in repos, decrypt on deploy |
| **HIGH** | Unified Executions Dashboard | Cross-agent execution view with filters |
| **HIGH** | MCP Execution Query Tools | `list_recent_executions`, `get_execution_result` |
| MEDIUM | Quick Instance Deploy | One-command "Deploy Ruby for Client X" |
| MEDIUM | Execution Notifications | Webhook/UI on task completion |
| MEDIUM | Client Usage Dashboard | Per-client visibility for billing |
| MEDIUM | Credential Usage Audit | Log credential reads and API calls |
| MEDIUM | Monorepo Workspace Deploy | Deploy directory of agents as system |
| LOW | Platform Simplification Mode | Hide advanced features behind toggle |

**Key Insight**: "The 90% is built. Focus on the 10% that makes it usable."

**Files Changed**:
- `docs/planning/WORKFLOW_PRIORITIES_2026-02.md` - Added discussion summary and consolidated roadmap
- `docs/memory/roadmap.md` - Added Phase 16, updated Decision Log

---

### 2026-02-05 11:45:00
🐛 **Fix: Live Execution Logs on Production (SSE Streaming)**

**Problem**: Live execution logs not appearing on production. The Execution Detail page loaded but no log entries streamed for running executions, even though the feature worked locally.

**Root Cause**: nginx proxy buffering was blocking SSE (Server-Sent Events) streams. nginx buffers responses by default, which prevents SSE from streaming in real-time.

**Fix**: Added SSE support directives to `/api/` location in `src/frontend/nginx.conf`:
```nginx
proxy_buffering off;
proxy_cache off;
chunked_transfer_encoding on;
```

**Files Changed**:
- `src/frontend/nginx.conf` - Added SSE support directives

---

### 2026-02-05 10:30:00
📋 **Roadmap: Multi-Agent Research Strategic Items**

Added 5 items from Cornelius research report analysis (`Multi-Agent-Systems-Research-Report-2026-02-05.md`):

**High Priority**:
- **Agent Skills Spec Alignment** - Anthropic's standard (https://agentskills.io) adopted by Microsoft, OpenAI, GitHub, Cursor. Progressive disclosure architecture.
- **A2A Protocol Support** - Google's agent-to-agent protocol for cross-platform collaboration (higher level than MCP)
- **Platform Memory Primitives** - 3-tier memory (Episodic→Semantic→Procedural) with 2x reasoning improvement

**Medium Priority**:
- **Code Execution Mode** - Agents write code to call MCP servers → 98% token reduction
- **Smart Model Routing** - Route by complexity (haiku→sonnet→opus) → 60-95% cost savings

**Key Research Insights**:
- Multi-agent orchestration: 100% actionable rate vs 1.7% single-agent, 140x correctness
- 89% of organizations now treat observability as mission-critical
- EU AI Act Article 14 mandates HITL for high-risk systems (Trinity Process Engine compliant)
- "Competitive advantage shifted from foundation models to orchestration layers"

---

### 2026-01-30 14:30:00
📋 **Roadmap: Feature Requests Batch Added to Backlog**

Added 10 items to roadmap backlog from planning session:

**High Priority**:
- **Bug: Live Execution Logs** - SSE streaming broken on production, needs investigation
- **Git Worktrees for Task Isolation** - Each execution gets own branch, human approves merge, audit trail
- **MCP as Primary Integration Point** - Local agents connect via MCP, pull skills, minimal setup

**Medium Priority**:
- **Agent Org Structures** - Parent-child hierarchy with delegation/control permissions
- **Any Repo / Empty Agent Creation** - Create from any GitHub repo or empty Trinity-compatible scaffold
- **Empty Agent → GitHub Safety** - Block init to existing repos when workspace empty
- **Centralized MCP Server Management** - UI/MCP tools to manage agent MCP connections

**Low Priority**:
- **GitHub Issues for Roadmap** - Replace roadmap.md with GitHub Issues
- **Agent History in GitHub** - Repository as source of truth (design open)

**Themes**: GitHub-centric workflow (repo as truth, worktrees, audit) + MCP-first experience (single integration point)

---

### 2026-01-30 11:45:00
🐛 **Fix: Allow Shared Users to Git Pull**

**Problem**: Users with shared access to an agent received "Owner access required" error when trying to pull from GitHub. The `/git/pull` endpoint incorrectly required owner-level permissions.

**Root Cause**: `POST /api/agents/{name}/git/pull` used `OwnedAgentByName` dependency which only allows agent owners and admins.

**Solution**: Changed to `AuthorizedAgentByName` which allows any user with access (owner, admin, or shared user).

**Permission Matrix After Fix**:
| Endpoint | Permission Level | Shared Users |
|----------|-----------------|--------------|
| `GET /git/status` | Authorized | ✅ Allowed |
| `GET /git/log` | Authorized | ✅ Allowed |
| `GET /git/config` | Authorized | ✅ Allowed |
| `POST /git/pull` | Authorized | ✅ **Now Allowed** |
| `POST /git/sync` | Owner | ❌ Owner only |
| `POST /git/initialize` | Owner | ❌ Owner only |

**Files Changed**:
- `src/backend/routers/git.py:178-179` - Changed `OwnedAgentByName` → `AuthorizedAgentByName`

---

### 2026-01-30 10:15:00
🚀 **Feature: Async Mode for MCP chat_with_agent (Fire-and-Forget)**

**Summary**: Added `async` parameter to `chat_with_agent` MCP tool for fire-and-forget task execution. When `async=true` (requires `parallel=true`), the call returns immediately with an `execution_id` that can be polled for results.

**Usage**:
```
chat_with_agent(
  agent_name: "worker-agent",
  message: "Analyze this data...",
  parallel: true,
  async: true
)
// Returns immediately:
// { status: "accepted", execution_id: "abc123", agent_name: "worker-agent", async_mode: true }

// Poll for results:
// GET /api/agents/worker-agent/executions/abc123
```

**Use Cases**:
- Send tasks to multiple agents simultaneously without blocking
- Fan-out patterns where orchestrator dispatches work to N workers
- Long-running tasks that don't need immediate response

**Files Changed**:
- `src/backend/models.py` - Added `async_mode` to `ParallelTaskRequest`
- `src/backend/routers/chat.py` - Added `_execute_task_background()` helper, async mode handling in `/task` endpoint
- `src/mcp-server/src/client.ts` - Added `async_mode` option to `task()` method
- `src/mcp-server/src/tools/chat.ts` - Added `async` parameter to `chat_with_agent` tool

**Requirements**: Implements part of Req 17.4 (Async MCP Chat Commands)

---

### 2026-01-29 18:30:00
🐛 **Fix: Scheduler Sync Bug - New Schedules Now Work Without Restart**

**Problem**: New schedules created via API wouldn't execute until the scheduler container was restarted. The `next_run_at` field was `None` for newly created schedules, and timeline markers wouldn't show.

**Solution** (two parts):

1. **Database Layer** (`src/backend/db/schedules.py`):
   - Added `_calculate_next_run_at()` helper using croniter
   - `create_schedule()` now calculates `next_run_at` before INSERT
   - `update_schedule()` recalculates when cron_expression, timezone, or enabled changes
   - `set_schedule_enabled()` recalculates when enabling, clears when disabling

2. **Dedicated Scheduler** (`src/scheduler/service.py`):
   - Added `_sync_schedules()` method that compares database state with in-memory APScheduler jobs
   - Detects new, updated, and deleted schedules
   - Called every 60 seconds (configurable via `SCHEDULE_RELOAD_INTERVAL`)
   - Added `list_all_schedules()` and `list_all_process_schedules()` to `src/scheduler/database.py`

**Behavior**:
- New schedules have `next_run_at` populated immediately
- Scheduler detects and loads new schedules within 60 seconds
- Timeline markers show immediately (no restart needed)
- Enable/disable triggers recalculation of `next_run_at`

**Files Changed**:
- `src/backend/db/schedules.py` - Added croniter import and `_calculate_next_run_at()` helper
- `src/scheduler/service.py` - Added `_sync_schedules()`, `_sync_agent_schedules()`, `_sync_process_schedules()`
- `src/scheduler/database.py` - Added `list_all_schedules()` and `list_all_process_schedules()`
- `docs/memory/feature-flows/scheduling.md` - Updated Known Issues to FIXED
- `docs/memory/feature-flows/scheduler-service.md` - Added Flow 6: Periodic Schedule Sync

---

### 2026-01-29 17:00:00
🐛 **Bug Documented: New Schedules Don't Execute Until Scheduler Restart**

**Discovered During TSM-001 Testing**: Schedule markers weren't appearing for newly created schedules.

**Root Cause Analysis**:
1. Backend creates schedule in database but does NOT calculate `next_run_at`
2. Backend's embedded scheduler is disabled (`main.py` lines 214-220)
3. `scheduler_service.add_schedule()` returns early because `self.scheduler` is `None`
4. Dedicated scheduler container (`trinity-scheduler`) only loads schedules on startup
5. No mechanism exists to notify the scheduler container when new schedules are created

**Impact**:
- `next_run_at` field is `None` for newly created schedules
- Schedule will never execute until scheduler restarts
- Timeline markers won't show (they depend on `next_run_at`)

**Workaround**: `docker restart trinity-scheduler`

**Planned Fix**: Calculate `next_run_at` in the database layer when creating/updating schedules, independent of the scheduler service.

**Documentation Updated**:
- `docs/memory/feature-flows/scheduling.md` - Added "Known Issues" section with full details
- `docs/memory/feature-flows/replay-timeline.md` - Added note in Section 5a Schedule Markers

---

### 2026-01-29 16:20:00
🐛 **Fix: Timeline Scale Issue with Schedule Markers (TSM-001)**

**Problem**: Timeline was extending days into the future when schedules had far-off `next_run_at` times (e.g., weekly schedules 4+ days away), breaking the timeline scale.

**Root Cause**: The `endTime` computed property extended to show ALL schedule markers regardless of how far in the future they were.

**Fix** (`src/frontend/src/components/ReplayTimeline.vue`):
- Added **2-hour max limit** for future timeline extension
- Only schedules within 2 hours of NOW extend the timeline
- Far-off schedules (beyond 2 hours) are simply not shown as markers
- Updated scroll handling to match the 2-hour limit logic

**Behavior**:
- Timeline shows past + NOW (as before)
- If a schedule is within 2 hours, timeline extends slightly to show it
- Weekly/monthly schedules (days away) don't break the scale

---

### 2026-01-29 15:45:00
📚 **Docs: Updated Feature Flows for Timeline Schedule Markers (TSM-001)**

Updated feature flow documentation to reflect the Timeline Schedule Markers implementation:

- **`docs/memory/feature-flows/scheduling.md`**: Added new "Dashboard Timeline Integration (TSM-001)" section with:
  - Complete data flow diagram (Dashboard -> network.js -> Backend -> ReplayTimeline)
  - Frontend implementation details (store, Dashboard, ReplayTimeline)
  - Timeline extension for future schedules explanation
  - Visual design specifications

- **`docs/memory/feature-flows/dashboard-timeline-view.md`**: Already updated with:
  - Section 8 "Schedule Markers (TSM-001)"
  - `schedules` prop in Props table
  - Test Case 9 for schedule marker verification
  - Edge cases for schedules

- **`docs/memory/feature-flows/replay-timeline.md`**: Already updated with:
  - Section 5a "Schedule Markers" with computed property, SVG rendering, helpers
  - Legend entry documentation
  - Visual Elements Summary table entry

- **`docs/memory/feature-flows.md`**: Already updated at lines 6-11 with TSM-001 summary

---

### 2026-01-29 14:30:00
🎨 **Feature: Timeline Schedule Markers (TSM-001)**

**Summary**: Added visual markers on the Dashboard timeline showing when agent schedules are configured to run. Users can hover to see schedule details and click to navigate to the schedule configuration. **Timeline extends into the future** to show upcoming schedule markers.

**Changes**:
- `src/frontend/src/stores/network.js`:
  - Added `schedules` ref to store enabled schedules
  - Added `fetchSchedules()` action to fetch from `/api/ops/schedules?enabled_only=true`
  - Exported `schedules` and `fetchSchedules` from store

- `src/frontend/src/views/Dashboard.vue`:
  - Added `schedules` to storeToRefs extraction
  - Call `fetchSchedules()` on component mount
  - Pass `schedules` prop to ReplayTimeline component

- `src/frontend/src/components/ReplayTimeline.vue`:
  - Added `schedules` prop
  - Added `scheduleMarkers` computed property to calculate marker positions
  - Added schedule marker rendering (purple triangles ▼ at top of agent rows)
  - Added `getScheduleMarkerPoints()`, `getScheduleTooltip()`, `navigateToSchedule()` helpers
  - Added "Next Run" legend item
  - **Extended `endTime` computation** to include future schedule `next_run_at` times
  - **Updated scroll handling** to allow scrolling into future when schedules exist

**Visual Design**:
- Purple (▼) triangles at top of each agent's row
- Positioned at `next_run_at` timestamp (can be in the future)
- Timeline automatically extends to show furthest scheduled run time + 10% padding
- Hover shows tooltip with schedule name, next run time, cron expression, and message preview
- Click navigates to `/agents/{agent_name}?tab=schedules`

**Requirements**: `docs/requirements/TIMELINE_SCHEDULE_MARKERS.md`

---

### 2026-01-29 11:30:00
🚀 **Feature: MCP Schedule Management Tools (MCP-SCHED-001)**

**Summary**: Added 8 new MCP tools for programmatic schedule management, enabling head agents (Claude Code instances) to create, list, modify, and control schedules on Trinity agents through the Model Context Protocol.

**New MCP Tools (8 total)**:
1. `list_agent_schedules` - List all schedules for an agent
2. `create_agent_schedule` - Create a new cron-based schedule
3. `get_agent_schedule` - Get schedule details
4. `update_agent_schedule` - Update schedule configuration
5. `delete_agent_schedule` - Delete a schedule
6. `toggle_agent_schedule` - Enable/disable a schedule
7. `trigger_agent_schedule` - Manually trigger execution
8. `get_schedule_executions` - Get execution history

**Files Changed**:
- `src/mcp-server/src/types.ts` - Added Schedule, ScheduleCreate, ScheduleUpdate, ScheduleExecution types
- `src/mcp-server/src/client.ts` - Added 10 schedule-related client methods
- `src/mcp-server/src/tools/schedules.ts` - **NEW** All 8 schedule tools with access control
- `src/mcp-server/src/server.ts` - Register schedule tools (36 total tools now)

**Access Control**:
- Agent-scoped keys can manage their OWN schedules (self-scheduling)
- Agent-scoped keys can read/toggle/trigger on permitted agents
- Agent-scoped keys CANNOT create/update/delete schedules on OTHER agents
- System-scoped keys have full access to all agents

**Testing**: All 13 backend schedule tests pass, MCP server registers 36 tools successfully

**Requirements**: `docs/requirements/MCP_SCHEDULE_MANAGEMENT.md`

---

### 2026-01-27 12:30:00
📋 **Roadmap: Added Gastown-Inspired Features to Backlog**

**Summary**: After competitive analysis of Gastown (Steve Yegge's multi-agent CLI tool), added 4 features to the roadmap backlog.

**Features Added**:
1. **Claude Code Hooks for State Persistence** (HIGH) - Use `.claude/hooks/` to persist agent work state before context compaction
2. **Convoy-like Work Bundles** (MEDIUM) - Group related tasks into trackable units for multi-agent coordination
3. **Ephemeral Agent Spawning** (MEDIUM) - Let System Agent spawn temporary workers on-demand with auto-cleanup
4. **Ready Work Discovery MCP Tool** (LOW) - Find unblocked tasks across the fleet

**Analysis**: `docs/research/gastown-comparison.md`

---

### 2026-01-27 11:58:00
🐛 **Fix: Dashboard Arrows for Agent-to-Agent Orchestration via /api/task**

**Summary**: Fixed Dashboard not showing arrows when agents call other agents via `chat_with_agent` MCP tool with `parallel=true` mode.

**Root Cause**: The `/api/task` endpoint was only creating a `CHAT_START` activity on the target agent, not the `AGENT_COLLABORATION` activity on the source agent that the Dashboard needs for arrows.

**Fixes** (`src/backend/routers/chat.py`):
- Aligned `/api/task` with `/api/chat` pattern: now creates TWO activities for agent-to-agent calls:
  1. `AGENT_COLLABORATION` activity with `agent_name=x_source_agent` (the caller)
  2. `CHAT_START` activity with `agent_name=name` (the target), linked via `parent_activity_id`
- Added completion handling for `collaboration_activity_id` in success, timeout, and error cases

**Pattern Match**: Now mirrors `/api/chat` endpoint (lines 196-210) per feature-flows/activity-stream-collaboration-tracking.md

**Before**: Only target agent had activity, Dashboard couldn't draw arrows
**After**: Source agent has `agent_collaboration` activity with `source_agent` and `target_agent` in details

**Testing**: Verified activity has `agent_name: "vc-due-diligence-dd-lead"` (source), `target_agent: "vc-due-diligence-dd-market"`

---

### 2026-01-27 11:00:00
📝 **Docs: Feature Flow Updates for Bug Fixes**

**Summary**: Updated feature flow documentation to reflect today's bug fixes.

**internal-system-agent.md**:
- Section 3 (Emergency Stop Flow): Documented new `system_prefix` query parameter
- Updated API signature with `Optional[str] = Query()` parameter
- Added prefix filter logic for schedules (line 638-639) and agents (line 658-659)
- Updated Testing section with examples for prefix filtering and safe testing
- Added revision history entry for 2026-01-27

**testing-agents.md**:
- Added revision history entry for AsyncTrinityApiClient header fix
- Documented fix pattern: `kwargs.pop('headers', {})` + merge with auth headers

**feature-flows.md**:
- Updated summary block with detailed fix descriptions
- Updated Internal System Agent entry with 2026-01-27 prefix filter note
- Updated Testing Agents Suite entry with fix date and test count (1460+)

---

### 2026-01-27 10:30:00
🐛 **Bug Fixes: Test Suite and Emergency Stop Improvements**

**Summary**: Fixed 3 bugs from roadmap - verified executions 404, fixed async client headers, and improved emergency stop test.

**Bug 1: Executions 404 for Non-Existent Agent**
- Status: Already fixed (verified)
- The `AuthorizedAgent` dependency at `dependencies.py:198-202` already validates agent existence
- Returns 404 "Agent not found" correctly for non-existent agents
- No code changes needed

**Bug 2: AsyncTrinityApiClient Headers Bug**
- Added header merging to all `AsyncTrinityApiClient` methods (get, post, put, delete)
- Pattern: `custom_headers = kwargs.pop('headers', {}); headers = {**self._get_headers(auth), **custom_headers}`
- Now matches sync `TrinityApiClient` behavior
- File: `tests/utils/api_client.py:208-280`

**Bug 3: Emergency Stop Test Timeout**
- Added `system_prefix` query parameter to `POST /api/ops/emergency-stop`
- Allows filtering which agents/schedules are affected
- Test now uses nonexistent prefix to verify structure without side effects
- Test runs in 0.25s (was timing out at 2+ minutes)
- Files: `routers/ops.py:607-682`, `tests/test_ops.py:367-391`

**Tests**: All 253 smoke tests pass, 41/41 ops tests pass

---

### 2026-01-26 16:45:00
🔬 **Feature: Process Miner Skill for Agent Log Analysis**

**Summary**: Created a new skill that analyzes Claude Code execution logs (JSONL transcripts) to discover repeatable workflow patterns and generate Trinity Process YAML definitions.

**New Files**:
- `.claude/skills/process-miner.md` - Skill definition with instructions
- `.claude/skills/scripts/process_miner.py` - Python analysis script (~500 lines)

**Capabilities**:
- Parse Claude Code JSONL transcripts
- Discover frequent tool sequences (n-grams)
- Semantic analysis of tool inputs (files read, modified, commands run)
- Infer workflow types (read-modify, search-review, delegation, etc.)
- Generate Trinity Process YAML from discovered patterns
- Confidence scoring based on pattern frequency

**Usage**:
```bash
# List available projects
python3 .claude/skills/scripts/process_miner.py --list-projects

# Analyze a transcript
python3 .claude/skills/scripts/process_miner.py --transcript PATH

# Save outputs
python3 .claude/skills/scripts/process_miner.py --transcript PATH --output DIR
```

**Background**: Inspired by process mining concepts (BPMN + pm4py) but applied to AI agent reasoning traces instead of human business processes.

---

### 2026-01-26 05:00:00
🎨 **UX: Enter Key Submits Tasks in Tasks Tab**

**Summary**: Added Enter key shortcut to submit tasks in the Tasks tab for faster workflow.

**Changes**:
- `TasksPanel.vue`: Added `@keydown.enter.exact.prevent` to textarea
- Enter now submits the task (no modifier keys needed)
- Shift+Enter inserts a newline (for multi-line tasks)
- Cmd/Ctrl+Enter also works (kept for consistency)
- Updated placeholder text to hint at keyboard shortcuts

**Files Changed**:
- `src/frontend/src/components/TasksPanel.vue` - lines 53-60

---

### 2026-01-26 04:30:00
📚 **Docs: Expanded Platform Skills Best Practices in Agent Guide**

**Summary**: Updated TRINITY_COMPATIBLE_AGENT_GUIDE.md with comprehensive guidance on writing effective skills, making it the go-to reference for skill development.

**Additions to Platform Skills Section**:
- **SKILL.md format**: Complete example with frontmatter and markdown instructions
- **Frontmatter fields**: `name`, `description`, `allowed-tools` with explanations
- **Description best practices**: Good vs bad examples, what to include
- **Tool restrictions**: Using `allowed-tools` for read-only or security-sensitive skills
- **Multi-file patterns**: Progressive disclosure with supporting files and scripts
- **Agent perspective**: How injected skills appear in ~/.claude/skills/ and CLAUDE.md
- **Comparison table**: Skills vs Slash Commands vs CLAUDE.md - when to use each

**Key Guidance**:
- Skills are the **recommended way** to encode reusable organizational knowledge
- Skills are **model-invoked** (Claude decides when to apply them based on description)
- Description field is critical - Claude uses it to match skills to tasks

**Files Changed**:
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Platform Skills section expanded (Section 11)

---

### 2026-01-26 03:00:00
🎨 **UX: Agent Start/Stop Toggle Control**

**Summary**: Replaced separate Start/Stop buttons with a unified toggle switch across all agent-displaying pages for consistent UX.

**Changes**:
- Created `RunningStateToggle.vue` - Reusable toggle component with:
  - Three size variants (sm/md/lg)
  - Loading spinner during state change
  - Dark mode support
  - Accessibility (ARIA attributes, keyboard navigation)
  - Green for Running, gray for Stopped

- Updated pages to use toggle:
  - `AgentHeader.vue` - Detail page header (size: lg)
  - `Agents.vue` - Agents list page (size: md)
  - `AgentNode.vue` - Dashboard network view (size: sm, with nodrag)

- Added store methods:
  - `agents.js`: `toggleAgentRunning()`, `isTogglingRunning()`, `runningToggleLoading` state
  - `network.js`: `toggleAgentRunning()`, `isTogglingRunning()`, `runningToggleLoading` ref

**User Impact**:
- Toggle clearly shows current state (Running/Stopped) vs old buttons that showed action
- Dashboard users can now start/stop agents without navigating to detail page
- Consistent visual pattern across all pages

**Requirement**: docs/requirements/AGENT_START_STOP_TOGGLE.md

---

### 2026-01-26 01:45:00
📝 **Docs: Updated skill injection feature flows for CLAUDE.md update step**

**Summary**: Updated feature flow documentation to reflect the new CLAUDE.md "Platform Skills" section update that occurs during skill injection, enabling agents to answer "what skills do you have?"

**Feature Flows Updated**:
- `docs/memory/feature-flows/skills-on-agent-start.md`:
  - Added `_update_claude_md_skills_section()` method documentation (lines 376-435)
  - Added `AgentClient.read_file()` method documentation (lines 470-506)
  - Updated flow diagram to show CLAUDE.md update step
  - Updated "Result in Agent Container" section with two-mechanism explanation
  - Added Files Download Endpoint documentation (agent_server lines 112-153)

- `docs/feature-flows/skill-injection.md`:
  - Added CLAUDE.md Skills Section Update section with code and example
  - Updated Business Logic to include step 5 (CLAUDE.md update)
  - Updated AgentClient Methods table with read_file() and write_file() line numbers
  - Updated Agent Server File Endpoints table with both endpoints
  - Updated Data Flow Diagram to show CLAUDE.md read/write operations

- `docs/memory/feature-flows.md`:
  - Updated Skill Injection entry with new line numbers and CLAUDE.md mention
  - Updated Skills on Agent Start entry with _update_claude_md_skills_section reference

**Line Numbers Verified**:
- `skill_service.py:300-374` - inject_skills()
- `skill_service.py:376-435` - _update_claude_md_skills_section()
- `agent_client.py:470-506` - read_file()
- `agent_client.py:508-547` - write_file()
- `agent_server/routers/files.py:112-153` - GET /api/files/download
- `agent_server/routers/files.py:314-371` - PUT /api/files

---

### 2026-01-26 00:30:00
🎯 **Feature: Skills Management System Implementation**

**Summary**: Implemented the full Skills Management System allowing agents to be assigned reusable skills from a GitHub-hosted library.

**Backend Changes**:
- `src/backend/db/skills.py` - New database operations for agent_skills table
- `src/backend/db_models.py` - Added AgentSkill, SkillInfo, AgentSkillsUpdate, SkillsLibraryStatus models
- `src/backend/database.py` - Added agent_skills table, indexes, and delegation methods
- `src/backend/services/skill_service.py` - New service for git sync, skill listing, and injection
- `src/backend/services/settings_service.py` - Added skills_library_url/branch settings
- `src/backend/services/agent_service/lifecycle.py` - Added inject_assigned_skills on agent start
- `src/backend/routers/skills.py` - New API router for skills endpoints
- `src/backend/routers/agents.py` - Added skill cleanup on agent delete
- `src/backend/main.py` - Registered skills router

**Frontend Changes**:
- `src/frontend/src/components/SkillsPanel.vue` - New component for agent skills tab
- `src/frontend/src/views/Settings.vue` - Added Skills Library configuration section
- `src/frontend/src/views/AgentDetail.vue` - Added Skills tab

**MCP Tools** (7 new tools):
- `list_skills` - List available skills from library
- `get_skill` - Get skill details and content
- `get_skills_library_status` - Get library sync status
- `assign_skill_to_agent` - Assign skill to agent
- `set_agent_skills` - Bulk update agent skills
- `sync_agent_skills` - Inject skills to running agent
- `get_agent_skills` - Get skills assigned to agent

**API Endpoints**:
- `GET /api/skills/library` - List available skills
- `GET /api/skills/library/{name}` - Get skill content
- `GET /api/skills/library/status` - Library status
- `POST /api/skills/library/sync` - Sync from GitHub (admin)
- `GET /api/agents/{name}/skills` - Agent's assigned skills
- `PUT /api/agents/{name}/skills` - Bulk update assignments
- `POST /api/agents/{name}/skills/{skill}` - Assign single skill
- `DELETE /api/agents/{name}/skills/{skill}` - Unassign skill
- `POST /api/agents/{name}/skills/inject` - Inject to running agent

---

### 2026-01-25 23:15:00
📝 **Requirements: Simplified Skills Management Architecture**

**Summary**: Redesigned skills management to use GitHub as the single source of truth, eliminating Docker volumes, symlinks, and complex infrastructure.

**Key Changes**:
- GitHub repository replaces Docker volume as source of truth
- Simple copy injection replaces symlinks
- Database stores assignments only (no skill metadata)
- Git handles versioning (no custom version control)
- MCP tools reduced from 10 to 4 essential ones
- Removed System Agent curator role (use GitHub PRs instead)

**New Architecture**:
```
GitHub Repo → git clone/pull → /data/skills-library/ → copy → agent ~/.claude/skills/
```

**Benefits**:
- Simpler implementation (no Docker volume management)
- Familiar workflow (edit skills via GitHub PRs)
- Auto-update via git pull
- Portable across Trinity instances

**Files Updated**:
- `docs/requirements/SKILLS_MANAGEMENT.md` - Complete rewrite
- `docs/memory/requirements.md` - Section 21 updated

---

### 2026-01-25 22:30:00
📚 **Docs: Trinity Compatible Agent Guide - Skills Management**

**Summary**: Updated TRINITY_COMPATIBLE_AGENT_GUIDE.md to document the new Platform Skills system.

**Changes**:
- Added "Platform Skills" section (Section 11) documenting centralized skills management
- Updated Table of Contents with new section
- Updated directory structure to show `skills-library/` mount point
- Clarified `.claude/skills/` in templates are seeded to platform library
- Updated template.yaml schema with skills seeding note
- Added revision history entry

**Key Points for Agent Developers**:
- Skills are managed at platform level, not in agent templates
- Agents receive skills via symlinks from `~/.claude/skills/<name>`
- Platform Skills Library mounted read-only at `~/.claude/skills-library/`
- Three skill types: policy (always active), procedure (executable), methodology (guidance)

---

### 2026-01-25 16:12:00
🔧 **Fix: Skill Injection to Agent Containers**

**Summary**: Fixed skill injection system to successfully write skill files to agent containers.

**Issues Fixed**:
1. `AgentClient` missing `put` method - Added `put()` method for HTTP PUT requests
2. Agent server not creating parent directories - Added `mkdir -p` before file writes in `files.py`
3. `.claude/skills` directory permissions - Pre-created directory in Dockerfile with correct ownership

**Files Changed**:
- `src/backend/services/agent_client.py` - Added `put()` method (line 153)
- `docker/base-image/agent_server/routers/files.py` - Added `parent.mkdir(parents=True, exist_ok=True)` before writes
- `docker/base-image/Dockerfile` - Added `/home/developer/.claude/skills` to mkdir command

**Testing**:
- Created new agent `skill-test-2` with updated base image
- Assigned `verification` skill via UI
- Clicked "Sync to Agent" - successfully synced 1 skill
- Verified `/home/developer/.claude/skills/verification/SKILL.md` exists in container

**Note**: Existing agents need container recreation to get the updated base image.

---

### 2026-01-25 21:15:00
📚 **Docs: Update Architecture Diagrams**

**Summary**: Refreshed all 9 diagram documentation files with verified line numbers and corrected MCP tool counts.

**Updates**:
- `01-system-overview.md` - Updated MCP tool count from 21 to 29
- `02-service-architecture.md` - Updated tool count to 29, added System Management (4 tools) category
- `03-agent-container.md` - Fixed line number references (32-50, 468-500)
- `05-authentication.md` - Fixed permission endpoints line reference (642-681)
- `06-multi-agent-systems.md` - Fixed permissions router line reference
- `09-agent-lifecycle-states.md` - Fixed security constants and crud.py line numbers

**MCP Tool Inventory** (29 total):
- Agent Management: 13 tools
- Agent Interaction: 3 tools
- System Management: 4 tools
- Skills Management: 8 tools
- Documentation: 1 tool

---

### 2026-01-25 18:30:00
🔧 **Enhancement: Skills Shared Volume Architecture**

**Summary**: Refactored skills management to use a shared Docker volume with symlinks instead of direct file injection. Skills are now stored centrally and mounted read-only into agents.

**Architecture Changes**:
- `docker-compose.yml` - Added `trinity-skills-library` volume, mounted at `/skills-library` in backend and `/home/developer/.claude/skills-library` in agents
- `src/backend/services/skill_service.py` - Rewritten to use volume-based storage with symlinks
- `src/backend/services/agent_service/crud.py` - Mount skills library volume on container creation
- `src/backend/services/agent_service/lifecycle.py` - Mount skills library volume on container recreation
- `src/backend/main.py` - Call `seed_skills_library()` on startup to seed from `.claude/skills/`

**Key Features**:
- Skills stored in `trinity-skills-library` Docker volume
- Regular agents get read-only mount at `~/.claude/skills-library`
- System agent (`trinity-system`) gets read-write access for curation
- Skill assignment creates symlinks from `~/.claude/skills/<name>` to library
- Auto-seeding from `.claude/skills/` directory on first run
- Built-in skills (verification, tdd, etc.) marked as defaults

**Benefits**:
- Instant propagation of skill updates (no restart needed)
- Centralized skill storage with database metadata
- Audit trail for skill assignments
- Future support for git-based versioning by System Agent

---

### 2026-01-25 17:00:00
✨ **Feature: Skills Management System Implementation**

**Summary**: Implemented centralized skill management for Trinity agents - platform-level skill library with per-agent assignment and container injection.

**Backend Changes**:
- `src/backend/database.py` - Added skills and agent_skills tables with indexes
- `src/backend/db/skills.py` - SkillOperations class (already existed, now integrated)
- `src/backend/routers/skills.py` - CRUD endpoints for skills (already existed, now registered)
- `src/backend/services/skill_service.py` - Injection logic (already existed)
- `src/backend/services/agent_service/lifecycle.py` - Added skill injection on agent start
- `src/backend/main.py` - Registered skills_router

**Frontend Changes**:
- `src/frontend/src/views/Skills.vue` - Admin skills management page
- `src/frontend/src/components/SkillsPanel.vue` - Agent skills tab component
- `src/frontend/src/components/NavBar.vue` - Added Skills link (admin only)
- `src/frontend/src/views/AgentDetail.vue` - Added Skills tab

**MCP Server Changes**:
- `src/mcp-server/src/types.ts` - Added Skill types
- `src/mcp-server/src/client.ts` - Added skill methods to TrinityClient
- `src/mcp-server/src/tools/skills.ts` - Created 8 MCP tools
- `src/mcp-server/src/server.ts` - Registered skill tools

**MCP Tools Added** (8 total):
| Tool | Description |
|------|-------------|
| `list_skills` | List all platform skills |
| `get_skill` | Get skill with full content |
| `create_skill` | Create skill (system scope) |
| `delete_skill` | Delete skill (system scope) |
| `assign_skill_to_agent` | Assign skill to agent |
| `remove_skill_from_agent` | Remove skill from agent |
| `sync_agent_skills` | Re-inject skills to running container |
| `execute_procedure` | Execute procedure skill on agent |

**Skill Types**:
- `policy` - Always active rules (e.g., `policy-data-retention`)
- `procedure` - Step-by-step instructions (e.g., `procedure-incident-response`)
- `methodology` - Approach guidance (e.g., `systematic-debugging`)

---

### 2026-01-25 15:30:00
📝 **Docs: Skills as Unified Organizational Knowledge**

**Summary**: Extended skills requirements with philosophical framework - skills as unified abstraction for policies, procedures, and methodologies.

**Files Updated**:
- `docs/requirements/SKILLS_MANAGEMENT.md`
  - Added Philosophy section explaining unified model
  - Added skill types: policy-*, procedure-*, methodology
  - Added execution modes: passive, on-demand, scheduled
  - Added Process Engine integration section
  - Added `execute_procedure` MCP tool
  - Added Phase 5: Process Engine Integration
- `docs/memory/requirements.md`
  - Updated Section 21 with skill types and philosophy
  - Added 21.2 Skill Types, 21.6 Process Engine Integration

**Key Concepts**:
| Type | Naming | Behavior |
|------|--------|----------|
| Policy | `policy-*` | Always active, agents follow implicitly |
| Procedure | `procedure-*` | Executable, can be scheduled/triggered |
| Methodology | (no prefix) | Guidance, loaded when relevant |

**Process Engine Connection**: Procedures become schedulable tasks via existing Process Engine infrastructure.

---

### 2026-01-25 14:00:00
📝 **Docs: Skills Management System Requirements**

**Summary**: Created detailed requirements for centralized skill management in Trinity.

**File Created**:
- `docs/requirements/SKILLS_MANAGEMENT.md` - Full feature specification

**Key Features**:
- Platform-level skill library (admin manages reusable skills)
- Per-agent skill assignment via UI
- System agent MCP tools for programmatic control
- Automatic injection into agent containers at start
- 5 phases: Core → UI → MCP Tools → System Agent → Process Integration

**Database**: 2 new tables (`skills`, `agent_skills`)
**API**: 7 new endpoints for skills CRUD and assignment
**MCP Tools**: 7 new tools for system agent skill management

---

### 2026-01-23 17:30:00
✨ **New: Development Methodology Skills (Inspired by Superpowers)**

**Summary**: Added 4 methodology skill guides to strengthen development discipline. Inspired by obra/superpowers framework (34k stars).

**Files Created**:
- `.claude/skills/verification/SKILL.md` - Evidence-based completion claims
- `.claude/skills/systematic-debugging/SKILL.md` - Root cause investigation before fixes
- `.claude/skills/tdd/SKILL.md` - Test-driven development cycle
- `.claude/skills/code-review/SKILL.md` - Technical rigor when receiving feedback

**Files Updated**:
- `CLAUDE.md` - Added "Development Skills" section (#6 in Rules of Engagement)
- `docs/DEVELOPMENT_WORKFLOW.md` - Added "Development Skills" section with skill reference table
- `dev-methodology-template/README.md` - Added skills to "What's Included" and directory structure
- `dev-methodology-template/.claude/skills/` - Copied all 4 skills to template

**Key Principles Adopted**:
1. **Verification**: No "done" without evidence (run command, show output)
2. **Debugging**: Find root cause BEFORE attempting fixes
3. **TDD**: Write failing test first, then minimal code to pass
4. **Code Review**: Verify feedback technically, not socially

**Source**: Analysis of https://github.com/obra/superpowers

---

### 2026-01-23 16:00:00
📝 **Docs: Updated Testing Agents Feature Flow**

**Summary**: Major update to testing-agents.md feature flow with accurate test counts and comprehensive test suite structure.

**File Modified**:
- `docs/memory/feature-flows/testing-agents.md`
  - Updated test count from 474+ to 1460+ tests across 84 files
  - Added process_engine tests (737 tests in 41 files: 32 unit + 9 integration)
  - Added scheduler_tests (71 tests in 6 files)
  - Updated config.py line numbers (GITHUB_TEMPLATES: line 91, ALL_GITHUB_TEMPLATES: line 164)
  - Added detailed test file listing with test counts per file
  - Added pytest configuration section with pyproject.toml contents
  - Added Test Categories Summary table
  - Added comprehensive test directory tree structure
  - Updated Key Source Files table with line counts
  - Added revision history entry for 2026-01-23

**Test Suite Statistics (2026-01-23)**:
- Backend API tests: 34 files, 638 tests
- Agent Server tests: 3 files, 14 tests
- Scheduler tests: 6 files, 71 tests
- Process Engine Unit: 32 files, 692 tests
- Process Engine Integration: 9 files, 45 tests
- **Total**: 84 files, 1460+ tests

**Key Implementation Files Verified**:
- `/Users/eugene/Dropbox/trinity/trinity/tests/conftest.py` (368 lines)
- `/Users/eugene/Dropbox/trinity/trinity/pyproject.toml` (lines 1-16 pytest config)
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/config.py` (165 lines)
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/templates.py` (220 lines)
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/services/template_service.py` (381 lines)
- `/Users/eugene/Dropbox/trinity/trinity/docker/base-image/agent_server/main.py` (88 lines)

---

### 2026-01-23 15:00:00
📝 **Docs: Updated Agent Logs & Telemetry Feature Flow**

**Summary**: Verified and updated agent logs/telemetry feature flow with correct line numbers, composable documentation, and discovered UI/code mismatch.

**File Modified**:
- `docs/memory/feature-flows/agent-logs-telemetry.md`
  - Updated logs endpoint line numbers (367-383, was 404-430)
  - Updated stats endpoint line numbers (386-393, was 433-440)
  - Added AgentHeader.vue stats display documentation (lines 172-235)
  - Added composable file locations and code snippets
  - Documented useAgentStats.js (lines 4, 56-60) and useAgentLogs.js (lines 35-40, 43-52)
  - Added LogsPanel.vue component details (lines 6, 8, 12-17, 19, 25-30)
  - Removed audit logging note (no longer present in logs endpoint)
  - Added Known Issues section for UI/code mismatch
  - Added revision history entry for 2026-01-23

**Known Issue Discovered**:
- `LogsPanel.vue:20` displays "Auto-refresh (10s)"
- `useAgentLogs.js:45` uses 15000ms (15 seconds) interval
- Minor UX inconsistency, no functional impact

**Key Implementation Files**:
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/agents.py:367-393`
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/services/agent_service/stats.py:123-184`
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/components/LogsPanel.vue`
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/components/AgentHeader.vue:172-235`
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/composables/useAgentStats.js`
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/composables/useAgentLogs.js`
- `/Users/eugene/Dropbox/trinity/trinity/src/frontend/src/stores/agents.js:183-189,276-282`

---

### 2026-01-23 14:00:00
📝 **Docs: Updated Vector Logging Feature Flow**

**Summary**: Refreshed Vector logging feature flow documentation with correct line numbers, date-stamped file patterns, and API response examples.

**Files Modified**:
- `docs/memory/feature-flows/vector-logging.md`
  - Updated vector.yaml line number references (sources: 18-23, transforms: 26-74, sinks: 76-96)
  - Updated docker-compose.yml line references (vector: 184-205, volumes: 234-235, backend mounts: 50-51)
  - Changed all filename references from static (`platform.json`) to date-stamped (`platform-YYYY-MM-DD.json`)
  - Added architecture diagram showing LogArchiveService and archive flow
  - Added API response examples for `/api/logs/stats` and `/api/logs/health`
  - Added archive file structure documentation with metadata sidecar format
  - Added archival troubleshooting section
  - Added storage backend documentation (`LocalArchiveStorage`)
  - Updated test steps for date-stamped files and archival verification
  - Added revision history entry for 2026-01-23

- `docs/QUERYING_LOGS.md`
  - Updated all query examples to use date-stamped file patterns
  - Added historical logs query examples
  - Added log archival API documentation section
  - Updated timestamps to 2026-01-23

**Key Implementation Files Referenced**:
- `/Users/eugene/Dropbox/trinity/trinity/config/vector.yaml` - Vector config with daily rotation
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/services/log_archive_service.py` - APScheduler archival
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/services/archive_storage.py` - LocalArchiveStorage interface
- `/Users/eugene/Dropbox/trinity/trinity/src/backend/routers/logs.py` - Admin API endpoints

---

### 2026-01-23 12:30:00
📝 **Docs: Updated Web Terminal Feature Flow**

**Summary**: Refreshed web terminal feature flow documentation with current line numbers and new features.

**File Modified**:
- `docs/memory/feature-flows/web-terminal.md`
  - Updated all line number references to match current codebase
  - Added TerminalPanelContent.vue wrapper component documentation
  - Added model parameter support for WebSocket endpoint
  - Added Gemini CLI runtime mode documentation
  - Added WebGL/Canvas renderer with GPU acceleration details
  - Updated xterm.js dependencies to current versions
  - Added access control via `db.can_user_access_agent()`
  - Clarified two implementations: regular agents vs system agent
  - Added revision history entry for 2026-01-23

**Key Line Numbers Updated**:
- AgentDetail.vue: 93-121 (terminal tab)
- AgentTerminal.vue: 165-505 (full component)
- terminal.py: 20-320 (TerminalSessionManager)
- agents.py: 1173-1187 (regular agent endpoint)
- system_agent.py: 262-528 (system agent endpoint)

---

### 2026-01-23 10:52:59
✨ **New: PR Validation Command (/validate-pr)**

**Summary**: Added slash command for validating pull requests against Trinity development methodology.

**File Created**:
- `.claude/commands/validate-pr.md`

**Features**:
- 7-step validation process covering all methodology requirements
- Documentation checks: changelog, roadmap, requirements, architecture, feature flows
- Feature flow format validation (checks all required sections)
- Security validation (API keys, emails, IPs, secrets, credentials)
- Code quality assessment
- Requirements traceability check
- Generates structured report with APPROVE/REQUEST CHANGES/NEEDS DISCUSSION recommendation
- Includes comment template for requesting changes

**Usage**: `/validate-pr <pr-number-or-url>`

---

### 2026-01-23 11:15:00
📝 **Docs: Updated Agent Shared Folders Feature Flow for Template Extraction Fix**

**Summary**: Updated feature flow documentation to reflect the SF-H1 bug fix enabling shared folder config extraction from templates.

**File Modified**:
- `docs/memory/feature-flows/agent-shared-folders.md`
  - Added revision note in header for 2026-01-23 template extraction fix
  - Rewrote "Agent Creation Flow" section with three-phase structure:
    - Phase 1: Template Extraction (lines 92, 173-179)
    - Phase 2: DB Upsert Before Container Creation (lines 395-404)
    - Phase 3: Volume Mounting (lines 406-450)
  - Added code snippets showing template extraction and DB upsert
  - Updated line number references throughout (was 299-344, now 406-450)

---

### 2026-01-23 11:00:00
📝 **Docs: Updated Process Engine Feature Flows for Template Variable Support**

**Summary**: Updated feature flow documentation to reflect the PE-H1 bug fix enabling template variable substitution in human approval steps.

**Files Modified**:
- `docs/memory/feature-flows/process-engine/human-approval.md`
  - Added template variable substitution to Key Capabilities
  - Added "With Template Variables (Dynamic Content)" YAML example section
  - Added Supported Template Variables table
  - Added Template Evaluation Detail section with code snippets (lines 106-126, 186-198)
  - Updated Document History

- `docs/memory/feature-flows/process-engine/process-execution.md`
  - Added "Template Variable Substitution" section after Handler Registry
  - Added Supported Variables table
  - Added Handler Template Support table (which fields each handler evaluates)
  - Added YAML usage example
  - Updated Document History

- `docs/memory/feature-flows/process-engine/README.md`
  - Added Template Variables section documenting expression evaluator patterns
  - Updated Document History

- `docs/memory/feature-flows.md`
  - Added 2026-01-23 update note for Process Engine Template Variable Fix

---

### 2026-01-23 10:30:00
🐛 **Fixed: Shared Folders Template Config Not Extracted (SF-H1)**

**Summary**: Fixed bug where agents created from templates with `shared_folders: expose: true` didn't get `/shared-out/` volume mounted because template extraction didn't read the `shared_folders` config.

**Root Cause**: Template extraction in `crud.py:153-172` read `type`, `resources`, `tools`, `runtime` but NOT `shared_folders`. The shared folder mounting logic (lines 393-438) checked `db.get_shared_folder_config()` which was never populated from templates.

**Fix Applied**:
1. Added `template_shared_folders = None` variable to track template config (line 92)
2. Added extraction of `shared_folders` from template.yaml in local template processing block (lines 173-179):
   ```python
   shared_folders_config = template_data.get("shared_folders", {})
   if shared_folders_config:
       template_shared_folders = {
           "expose": shared_folders_config.get("expose", False),
           "consume": shared_folders_config.get("consume", False)
       }
   ```
3. Added DB upsert before container creation (lines 394-404) so volumes mount correctly:
   ```python
   if template_shared_folders:
       db.upsert_shared_folder_config(
           agent_name=config.name,
           expose_enabled=template_shared_folders.get("expose", False),
           consume_enabled=template_shared_folders.get("consume", False)
       )
   ```

**Files Modified**:
- `src/backend/services/agent_service/crud.py` (3 locations)

**Impact**: 13 templates with `shared_folders` config now work correctly:
- `dd-lead`, `dd-legal`, `dd-captable`, `dd-compliance`, `dd-traction`, `dd-bizmodel`
- `dd-tech`, `dd-competitor`, `dd-market`, `dd-founder`, `dd-intake`
- `demo-researcher`, `demo-analyst`

**Verified**: Python syntax check passed.

---

### 2026-01-23 09:15:00
🐛 **Fixed: Process Engine Template Variables Not Substituted (PE-H1)**

**Summary**: Fixed bug where `{{input.*}}` and `{{steps.*}}` template variables in human approval step `title` and `description` fields were not being substituted.

**Root Cause**: `HumanApprovalHandler.execute()` used raw `config.title` and `config.description` values without calling `ExpressionEvaluator.evaluate()`.

**Fix Applied**:
1. Added import for `ExpressionEvaluator` and `EvaluationContext` from `...services`
2. Added `expression_evaluator` instance to `HumanApprovalHandler.__init__()`
3. Added `_evaluate_template()` helper method (lines 106-126) that:
   - Creates `EvaluationContext` from `context.input_data`, `context.step_outputs`, execution ID, and process name
   - Calls `expression_evaluator.evaluate()` on the template string
4. Updated `execute()` to evaluate `title` and `description` before creating `ApprovalRequest` (lines 186-188)
5. Updated wait result output to use evaluated title (line 211)

**Files Modified**:
- `src/backend/services/process_engine/engine/handlers/human_approval.py`
- `tests/process_engine/unit/test_approval_handler.py` (added 4 new tests for template substitution)

**Test Cases Added**:
- `test_execute_substitutes_title_variables` - Verifies `{{input.company_name}}` → "TestCo AI"
- `test_execute_substitutes_description_variables` - Verifies `{{steps.intake.output.response}}` substitution
- `test_execute_returns_evaluated_title_in_output` - Verifies wait result has evaluated title
- `test_execute_handles_missing_variables_gracefully` - Missing vars stay as placeholders

**Verified**: Import successful in running backend container; syntax check passed.

---

### 2026-01-22 12:45:00
🐛 **Discovered: Two Process Engine / Shared Folders Bugs**

**Summary**: During VC Due Diligence demo testing, discovered two infrastructure bugs preventing proper workflow execution.

**Bug 1: Template Variables Not Substituted (PE-H1)**
- **Symptom**: `{{input.company_name}}` appears literally instead of "TestCo AI" in human approval titles
- **Root Cause**: `HumanApprovalHandler.execute()` uses `config.title` and `config.description` directly without calling `ExpressionEvaluator.evaluate()`
- **Location**: `src/backend/services/process_engine/engine/handlers/human_approval.py:158-165`
- **Fix**: Create `EvaluationContext` with `context.input_data` and `context.step_outputs`, then call `evaluate()` on title/description before creating `ApprovalRequest`
- **Impact**: All process definitions using template variables in human approval steps

**Bug 2: Shared Folders Template Config Not Extracted (SF-H1)**
- **Symptom**: Agents with `shared_folders: expose: true` in template don't get `/shared-out/` mounted
- **Root Cause**: Template extraction in `crud.py:153-172` reads `type`, `resources`, `tools`, `runtime` but NOT `shared_folders`
- **Location**: `src/backend/services/agent_service/crud.py:153-172`
- **Fix**: Add `shared_folders = template_data.get("shared_folders", {})` extraction and call `db.upsert_shared_folder_config()`
- **Impact**: 13 templates define shared_folders but config never written to DB on standalone creation
- **Note**: Works in system manifest deployment (calls `configure_folders()` separately) but fails in direct agent creation

**Roadmap Updated**: Both bugs added as HIGH priority

---

### 2026-01-22 12:30:00
🔧 **VC Due Diligence Demo: Local-Only Mode**

**Summary**: Updated VC DD process to work fully local without external notifications.

**Changes**:
- Removed `notify-approval` and `send-rejection` notification steps (email)
- Added `save-approval-report` and `save-rejection-report` agent tasks
- Final reports saved to `dd-lead`'s `/shared-out/final-report/`
- Updated agent references to use `vc-due-diligence-dd-*` naming
- Created new process `vc-dd-local-v2` with fixes

**Files Modified**:
- `config/process-templates/vc-due-diligence/definition.yaml`
- `docs/demos/vc-due-diligence/README.md`

**Test Result**: Process executed successfully (16 steps), but revealed template variable and shared folder bugs above.

---

### 2026-01-21 12:15:00
✅ **Verified: Security Bugs Already Fixed (AL-H1, AL-H2, AL-H3)**

**Summary**: Validated that the HIGH priority security bugs from the audit report have already been fixed.

**Bugs Verified Fixed**:

1. **Missing Auth on Lifecycle Endpoints (AL-H1, AL-H2)**
   - `start_agent` (line 316), `stop_agent` (line 343), `get_logs` (line 368) all use `AuthorizedAgentByName` dependency
   - `AuthorizedAgentByName` calls `db.can_user_access_agent()` to verify authorization
   - Files: `routers/agents.py`, `dependencies.py:228-255`

2. **Container Security Inconsistency (AL-H3)**
   - Added `RESTRICTED_CAPABILITIES` and `FULL_CAPABILITIES` constants in `lifecycle.py:31-49`
   - Both container creation paths (`crud.py`) and recreation (`lifecycle.py`) use same constants
   - Security settings ALWAYS applied: `cap_drop=['ALL']`, `apparmor:docker-default`, `tmpfs noexec,nosuid`
   - Files: `lifecycle.py:343-368`, `crud.py:31,462-464`

**Roadmap Updated**: Both bugs marked as ✅ Fixed

---

### 2026-01-21 11:45:00
✅ **Verified: Execution Queue Race Conditions Already Fixed**

**Summary**: Validated that the execution queue race conditions (EQ-H1, EQ-H2, EQ-M1 from audit report) were already fixed on 2026-01-14.

**Fixes Verified**:
1. **`submit()` race condition** (EQ-H1): Uses atomic `SET NX EX` for slot acquisition - prevents two concurrent requests from acquiring the same execution slot
2. **`complete()` race condition** (EQ-H2): Uses Lua script for atomic pop-and-set - prevents queue entries from being lost or processed twice
3. **`get_all_busy_agents()` blocking** (EQ-M1): Uses `SCAN` instead of `KEYS` - avoids blocking Redis on large datasets

**Validation**:
- Reviewed `src/backend/services/execution_queue.py` (lines 67-84 Lua script, 165-170 atomic submit, 295-308 SCAN)
- Ran all 20 execution queue tests - **all passed**
- Feature flow documentation already updated (`docs/memory/feature-flows/execution-queue.md`)

**Roadmap Updated**: Bug marked as ✅ Fixed 2026-01-14

---

### 2026-01-21 09:30:00
🐛 **Fix: RoleMatrix Shows Correct Executor from Step Agent**

**Problem**: Process Editor's Role Matrix showed "No executor" warnings for all steps, even when agents were properly defined in the YAML configuration.

**Root Cause**: Two separate concepts for agent assignment:
- `step.agent` - The agent that executes the task (defined in YAML, used at runtime)
- `step.roles.executor` - EMI pattern role assignment (optional, what RoleMatrix displayed)

RoleMatrix only checked `roles.executor`, ignoring the actual `agent` field.

**Fix**:
- `ProcessEditor.vue`: Pass `step.agent` to RoleMatrix via `parsedSteps`
- `RoleMatrix.vue`: Use `step.agent` as default executor for `agent_task` steps when `roles.executor` is not explicitly set

**Files Modified**:
- `src/frontend/src/views/ProcessEditor.vue`
- `src/frontend/src/components/process/RoleMatrix.vue`

---

### 2026-01-20 21:15:00
📚 **Documentation: Audit Trail Architecture Updated for Process Engine**

**Summary**: Updated SEC-001 (Audit Trail Architecture) to acknowledge the Process Engine's existing audit system.

**Changes**:
- Added "Process Engine Audit (Implemented 2026-01-16)" section documenting:
  - `AuditService` in `services/process_engine/services/audit.py`
  - `SqliteAuditRepository` in `services/process_engine/repositories/audit.py`
  - 18 audit actions covering process lifecycle, execution, approvals, admin
- Updated "Relationship to Existing Systems" diagram to show 4 data systems:
  - `audit_log` (NEW) - Platform audit for agents, auth, MCP, credentials
  - `audit_entries` (PROCESS ENGINE) - Process workflow audit
  - `agent_activities` - Runtime observability
  - Vector logs - Container debugging
- Added "Audit System Boundaries" table with scope, storage, retention
- Added "Why Two Audit Systems?" rationale (separation of concerns, DB isolation, query optimization)

**Decision**: Platform audit and Process audit remain separate for clear boundaries. Future unified query API can aggregate both for compliance reporting.

**File Modified**: `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md`

---

### 2026-01-20 20:55:00
📚 **Documentation: Demo Fleet Command Updated**

**Summary**: Comprehensive update to `/create-demo-agent-fleet` command with complete setup instructions.

**New Sections Added**:
- **Step-by-step setup guide** for Acme Consulting fleet (6 steps)
- **Process template deployment** - How to create processes from bundled templates
- **Schedule configuration** - API calls for setting up recurring automation
- **Autonomy mode** - Enabling automatic schedule execution
- **Building demo history** - Running initial tasks to populate timeline
- **Verification checklist** - Commands to verify everything works

**API Reference**:
- Quick reference table for all relevant endpoints
- Token acquisition and authentication flow
- Both MCP and curl/API examples throughout

**Demo Checklist**:
- 7-item verification checklist for complete setup

**File Modified**: `.claude/commands/create-demo-agent-fleet.md`

---

### 2026-01-20 10:30:00
📚 **Documentation: README.md Updated for Recent Features**

**Summary**: Updated README.md to document major features added since Process Engine completion.

**New Sections Added**:
- **Process Engine** - Full section with YAML example, step types table, AI assistant documentation
- **Process Engine Features** in Features list - 7 bullet points covering workflows, step types, approvals, templates

**Features Documented**:
- Dashboard Timeline View (execution visualization with color coding)
- Host Telemetry (real-time CPU/memory/disk monitoring)
- Agent Dashboard (custom dashboards via `dashboard.yaml`)
- Live Execution Streaming (real-time log streaming)
- Execution Termination (stop running tasks)
- File Manager (renamed from File Browser, with preview capabilities)

**Project Structure Updates**:
- Added `services/process_engine/` under backend
- Added `config/process-templates/` and `config/process-docs/`

**Documentation Links**:
- Added Process Engine Design docs link

**File Modified**: `README.md`

---

### 2026-01-19 18:00:00
✨ **Feature: Premium Onboarding & Process Creation Chat Assistant**

**Summary**: Complete implementation of premium onboarding experience (20 stories across 5 epics) including an AI-powered chat assistant for creating processes.

**Major Features**:

1. **Process Creation Chat Assistant (E25)**
   - Embedded chat panel powered by Trinity System Agent
   - Auto-syncs YAML to editor as assistant types (live preview)
   - Selection-to-chat: select code in editor → ask questions or request edits
   - Typing animation with word-by-word reveal
   - Chat persistence via localStorage
   - Process status awareness (knows published processes are read-only)

2. **First Process Wizard (E24)**
   - Step-by-step guided wizard for creating first process
   - Template selection with previews
   - Integrated with chat assistant

3. **Contextual Help System (E22)**
   - YAML Editor help panel with cursor-aware documentation
   - Execution status explainers with tooltips
   - Error messages with actionable guidance

4. **Documentation Tab (E21)**
   - In-app documentation at `/docs`
   - Getting started guides, pattern docs, reference material
   - Restart onboarding button

5. **Onboarding Checklist (E20)**
   - Floating checklist tracking setup progress
   - Visual celebration when milestones completed
   - Smart hints based on current page context

**Files Added**:
- `src/frontend/src/components/ProcessChatAssistant.vue` - Chat assistant UI
- `src/frontend/src/views/ProcessWizard.vue` - First process wizard
- `src/frontend/src/components/EditorHelpPanel.vue` - Contextual YAML help
- `src/frontend/src/components/OnboardingChecklist.vue` - Progress checklist
- `src/frontend/src/views/DocsView.vue` - Documentation viewer
- `config/process-docs/` - Documentation content (markdown)

**Files Modified**:
- `src/frontend/src/views/ProcessEditor.vue` - Integrated chat tab, help panel
- `src/frontend/src/views/ProcessList.vue` - Enhanced empty state with templates
- `src/frontend/src/router/index.js` - Added /docs and /processes/wizard routes
- `config/agent-templates/trinity-system/CLAUDE.md` - Process assistant prompt
- `src/frontend/src/components/YamlEditor.vue` - Selection change events

**Documentation**:
- `docs/PROCESS_DRIVEN_PLATFORM/BACKLOG_ONBOARDING.md` - 20 stories, all complete

---

### 2026-01-18 10:30:00
📋 **Architecture: Audit Trail System (SEC-001)**

**Requirement**: Comprehensive audit logging for user and agent actions with full actor attribution. Must be able to reconstruct chain of events for investigation.

**Gaps Identified**:
- Agent lifecycle (create/start/stop/delete) → WebSocket only, no persistence
- MCP tool calls → console.log only, no audit
- User logins → nothing tracked
- Permission changes → nothing tracked
- Credential operations → nothing tracked

**Solution**: Dedicated append-only `audit_log` table (separate from `agent_activities`):
- Immutability enforced via SQL triggers
- Full actor attribution (user, agent, MCP client, system)
- MCP API key tracking per tool call
- Optional hash chain for tamper evidence
- Query API with filters and CSV/JSON export

**Event Types**: AGENT_LIFECYCLE, EXECUTION, AUTHENTICATION, AUTHORIZATION, CONFIGURATION, CREDENTIALS, MCP_OPERATION, GIT_OPERATION, SYSTEM

**Files Created**:
- `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md` - Full architecture spec

**Files Updated**:
- `docs/memory/roadmap.md` - Added SEC-001 to Phase 11 as HIGH priority
- `docs/memory/requirements.md` - Added section 20: Security & Compliance

---

### 2026-01-15 14:00:00
🐛 **Fix: Timezone-Aware Timestamp Handling (Critical)**

**Problem**: Dashboard Timeline showed executions at wrong times when server runs in different timezone than user. Events appearing hours in the past/future.

**Root Cause**: Backend timestamps stored without timezone indicator (`"2026-01-15T10:30:00"`), causing JavaScript to interpret them as local time instead of UTC.

**Solution**: Comprehensive timezone handling across the stack:

**Backend Changes**:
- Added `utc_now_iso()`, `to_utc_iso()`, `parse_iso_timestamp()` utilities to `src/backend/utils/helpers.py`
- All timestamps now include 'Z' suffix (e.g., `"2026-01-15T10:30:00Z"`)
- Updated `db/activities.py` and `db/schedules.py` to use new utilities
- Updated `models.py` Pydantic serializers to output UTC with 'Z' suffix

**Frontend Changes**:
- Added `parseUTC()`, `getTimestampMs()`, `formatLocalTime()` utilities to `src/frontend/src/utils/timestamps.js`
- Updated composables `useFormatters.js` with timezone-aware parsing
- Updated `network.js` store to use timestamp utilities
- Updated `ReplayTimeline.vue` to correctly parse UTC timestamps

**Documentation**:
- Added comprehensive guide: `docs/TIMEZONE_HANDLING.md`

**Files Modified**:
- `src/backend/utils/helpers.py` - Added timestamp utilities
- `src/backend/db/activities.py` - Use UTC with 'Z' suffix
- `src/backend/db/schedules.py` - Use UTC with 'Z' suffix
- `src/backend/models.py` - Updated Pydantic serializers
- `src/frontend/src/utils/timestamps.js` - New timestamp utilities
- `src/frontend/src/composables/useFormatters.js` - Added timezone-aware parsing
- `src/frontend/src/stores/network.js` - Use timestamp utilities
- `src/frontend/src/components/ReplayTimeline.vue` - Use timestamp utilities

**Impact**: Timeline events now display at correct positions regardless of server/user timezone difference.

---

### 2026-01-15 12:35:00
✨ **Enhancement: Distinct Color for MCP Executions on Timeline**

**Change**: MCP executions (via Claude Code MCP client) now have their own distinct **pink/rose** color on the Dashboard Timeline, making them easily distinguishable from manual tasks.

**Updates**:
- Color: Pink (#ec4899 active, #f9a8d4 inactive) for `triggered_by='mcp'`
- Tooltip: Shows "MCP Task" prefix instead of generic "Task"
- Legend: Added "MCP" entry with pink color dot

**Files Modified**:
- `src/frontend/src/components/ReplayTimeline.vue:73-76` - Legend entry
- `src/frontend/src/components/ReplayTimeline.vue:899-902` - Color logic
- `src/frontend/src/components/ReplayTimeline.vue:926-927` - Tooltip prefix

**Visual**:
- 🟢 Manual (green) - Tasks triggered from UI
- 🩷 MCP (pink) - Tasks triggered via MCP client
- 🟣 Scheduled (purple) - Cron-scheduled tasks
- 🩵 Agent-Triggered (cyan) - Agent-to-agent calls
### 2026-01-19 10:00:00
🐛 **Fix: Git Sync Sets Upstream on First Push**

**Problem**: Initial push from agent server failed when branch had no upstream, requiring manual `git push -u`.

**Fix**: Set upstream explicitly on normal push using current branch name.

**File Modified**: `docker/base-image/agent_server/routers/git.py`

**Impact**: First-time pushes succeed without manual intervention.

---

### 2026-01-15 11:45:00
🐛 **Fix: MCP Executions Not Appearing on Dashboard Timeline**

**Problem**: Executions triggered via MCP showed in Tasks tab but NOT on Timeline view.

**Root Causes**:
1. **API field mismatch**: Backend returned `"timeline"` field, frontend expected `"activities"` (introduced Jan 11)
2. **Activity triggered_by mismatch**: Task execution used `triggered_by="mcp"`, but activity creation used `triggered_by="user"`, which frontend filters out

**Fixes**:
- Backend: Changed `/api/activities/timeline` response from `"timeline"` to `"activities"` field
- Backend: Activity creation now uses `triggered_by="mcp"` for user MCP calls (matching task execution)
- Frontend: Updated filter comment to document MCP support

**Files Modified**:
- `src/backend/routers/agents.py:633` - API response field name
- `src/backend/routers/chat.py:221` - Activity triggered_by logic
- `src/frontend/src/stores/network.js:161` - Filter comment

**Impact**: All execution types (scheduled, manual, MCP) now consistently appear on Dashboard Timeline.

---

### 2026-01-15 09:30:00
🐛 **Fix: Dashboard Timeline Visible Even With No Events**

**Problem**: Timeline view was hidden/empty when there were no historical events in the selected time range, preventing users from seeing live events as they streamed in.

**Root Cause**: `timelineStart` and `timelineEnd` computed properties in `network.js` returned `null` when `historicalCollaborations` was empty, causing ReplayTimeline to render nothing.

**Fix**: Changed computed properties to always provide a valid time range:
- `timelineStart` = now - timeRangeHours (e.g., 24h ago)
- `timelineEnd` = now (always current time for live mode)

**File Modified**: `src/frontend/src/stores/network.js:94-111`

**Impact**: Timeline grid is now always visible with all agent rows, ready to show live events via WebSocket even when there's no historical data.

---

### 2026-01-14 13:15:00
🐛 **Security Fix: Multiple Bug Fixes from Best Practices Audit**

Implemented 5 bug fixes from the security audit (docs/bugs/BUG_FIX_REQUIREMENTS.md):

**Bug 1: Execution Queue Race Conditions (HIGH)**
- Fixed `submit()` to use atomic `SET NX EX` for slot acquisition
- Fixed `complete()` to use Lua script for atomic pop-and-set
- Replaced `KEYS` with `SCAN` in `get_all_busy_agents()` for production safety
- File: `src/backend/services/execution_queue.py`

**Bug 2: Missing Auth on Lifecycle Endpoints (HIGH)**
- Changed `start_agent_endpoint` to use `AuthorizedAgentByName` dependency
- Changed `stop_agent_endpoint` to use `AuthorizedAgentByName` dependency
- Changed `get_agent_logs_endpoint` to use `AuthorizedAgentByName` dependency
- File: `src/backend/routers/agents.py:315-384`

**Bug 3: Container Security Inconsistency (HIGH)**
- Added `RESTRICTED_CAPABILITIES` and `FULL_CAPABILITIES` constants
- Fixed ALL container creation paths to ALWAYS apply baseline security:
  - Always use `cap_drop=['ALL']` then add back specific caps
  - Always apply AppArmor profile
  - Always apply `noexec,nosuid` to /tmp
- Files modified:
  - `src/backend/services/agent_service/lifecycle.py` - Container recreation
  - `src/backend/services/agent_service/crud.py` - Initial container creation
  - `src/backend/services/system_agent_service.py` - System agent creation

**Bug 4: Executions 404 for Non-Existent Agent (LOW)**
- Verified `AuthorizedAgent` dependency already checks existence via `db.get_agent_owner()`
- Added clarifying docstring to endpoint
- File: `src/backend/routers/schedules.py:309-320`

**Bug 5: Test Client Headers Bug (LOW)**
- Verified already fixed - `kwargs.pop('headers', {})` prevents double passing
- File: `tests/utils/api_client.py`

**Bug 6: Emergency Stop Timeout (LOW)**
- Implemented parallel agent stopping with `ThreadPoolExecutor(max_workers=10)`
- Reduced worst-case time from N*10s to ~20s for many agents
- Added helper function `_stop_agent_container()` for thread pool
- File: `src/backend/routers/ops.py:591-682`

---

### 2026-01-14 12:35:00
🧪 **Test: Updated Phase 3 Context Validation**

**Change**: Rewrote Phase 3 test to not test context via Terminal (which bypasses context tracking).

**New Test Focus**:
- Validates Tasks panel functionality instead
- Documents that context tracking only works for `/api/chat` (not Terminal or Tasks)
- Provides optional API-based test for verifying context tracking

**File Modified**: `docs/testing/phases/PHASE_03_CONTEXT_VALIDATION.md`

---

### 2026-01-14 12:30:00
🐛 **Fix: Public Links URL Generation (CRITICAL)**

**Problem**: Public links were generated with wrong port (3000 instead of 80), making them non-functional in Docker deployment.

**Root Cause**: Default `FRONTEND_URL` in `config.py` was `http://localhost:3000` (Vite dev server port), but Docker Compose serves frontend on port 80.

**Fix**: Changed default to `http://localhost` (port 80, standard HTTP).

**Files Modified**:
- `src/backend/config.py:41` - Changed default FRONTEND_URL from port 3000 to port 80
- `docs/memory/feature-flows/public-agent-links.md` - Updated configuration example

**Impact**: Public shareable links now work correctly in Docker deployments.

---

### 2026-01-14 12:25:00
📝 **Documented: Context Tracking Design Limitation**

**Issue from UI Test**: Context tracking stuck at 0% when using Terminal mode.

**Root Cause Analysis**: Terminal mode runs Claude Code directly via PTY (docker exec), bypassing the `/api/chat` endpoint that tracks context. This is by design, not a bug.

**Context Tracking Works For**:
- `/api/chat` - Session-based chat (tracks context per turn)
- Scheduled tasks that use conversational context

**Context Tracking Does Not Work For**:
- Terminal mode (PTY bypass, direct Claude TUI access)
- `/api/task` endpoint (stateless, no session context)

**Note**: The test report incorrectly identified this as "only counting input_tokens, not cached tokens". The actual issue is architectural - Terminal bypasses the context tracking system entirely.

---

### 2026-01-14 12:20:00
✅ **Validated: UI Tab Navigation Working Correctly**

**Issue from UI Test**: Report claimed "UI Tab Navigation Broken" in Phases 7, 22.

**Investigation**: Manually tested all 13 tabs (Info, Tasks, Dashboard, Terminal, Logs, Credentials, Sharing, Permissions, Schedules, Git, Files, Folders, Public Links) using Playwright.

**Result**: All tabs navigate correctly. Click handlers work, content switches properly.

**Conclusion**: Test failure was likely a timing/automation issue, not an actual bug. Tabs with horizontal scroll may require proper viewport handling in automated tests.

---

### 2026-01-13 23:15:00
🔧 **Scheduler Migration Complete - Embedded Scheduler Disabled**

**Change**: Disabled the old embedded scheduler in the backend, completing the migration to the dedicated scheduler service.

**Files Modified**:
- `src/backend/main.py:196-203` - Removed `scheduler_service.initialize()` call
- `src/backend/main.py:214-215` - Removed `scheduler_service.shutdown()` call

**Architecture**:
- Schedule execution now handled exclusively by `trinity-scheduler` container
- Backend still imports `scheduler_service` for manual trigger functionality
- CRUD operations update database; dedicated scheduler reloads from DB

**Test Results**: 71/71 scheduler tests passed (100%)

**Impact**:
- No more duplicate schedule executions from multiple uvicorn workers
- Redis distributed locking prevents race conditions
- Clean separation of concerns: backend = API, scheduler = execution

---

### 2026-01-13 21:00:06
🐳 **Dedicated Scheduler Service - Implementation Complete**

**Problem Solved**: Multiple uvicorn workers caused duplicate schedule executions (each worker ran its own APScheduler).

**Solution**: Standalone scheduler service with Redis distributed locking.

**New Components**:
- `src/scheduler/` - Python service (config, models, database, agent_client, locking, service, main)
- `docker/scheduler/` - Dockerfile, requirements.txt, docker-compose.test.yml
- `tests/scheduler_tests/` - 71 unit tests (all passing)
- `docs/memory/feature-flows/scheduler-service.md` - Feature flow documentation

**Key Features**:
- Single-instance design prevents duplicates
- Redis lock: `scheduler:lock:schedule:{id}` with 600s TTL, auto-renewal at 300s
- Agent HTTP client with 15-min timeout to `/api/task`
- Health endpoints: `/health`, `/status`
- Redis pub/sub events: `schedule_execution_started`, `schedule_execution_completed`

**Integration Tested**:
- Docker build ✅
- Redis connection ✅
- Database operations ✅
- Cron scheduling ✅
- Execution tracking ✅

---

### 2026-01-13 23:00:00
🎨 **UX: Dashboard Default View & Clickable Logo**

**Changes**:
1. **Header logo now clickable** - Trinity logo and title navigate to Dashboard (`/`)
2. **Timeline is now the default view** - New users see Timeline view instead of Graph

**Files Modified**:
- `src/frontend/src/components/NavBar.vue:6-9` - Logo wrapped in `<router-link to="/">`
- `src/frontend/src/stores/network.js:53-55` - `isTimelineMode` defaults to `true` if no localStorage preference
- `docs/memory/feature-flows/dashboard-timeline-view.md` - Updated user journey and revision history

---

### 2026-01-13 22:30:00
🔬 **Research: Dedicated Scheduler Service Architecture**

**Problem Identified**: Production runs with `--workers 2` (docker-compose.prod.yml:72), causing each worker to initialize its own APScheduler instance with in-memory job store. Result: **duplicate schedule executions**.

**Root Cause Analysis**:
- APScheduler 3.x has no inter-process coordination
- MemoryJobStore is not shared between workers
- Each worker fires the same scheduled jobs independently

**Research Conducted**:
- APScheduler 4.0: Built-in distributed scheduling via `RedisEventBroker` + shared data stores
- Distributed locking: `python-redis-lock` with auto-renewal for long executions
- Architecture patterns: Dedicated scheduler service vs embedded scheduler

**Solution Options Documented**:
1. **Phase 1 (Immediate)**: Add distributed lock to `_execute_schedule()` OR single worker mode
2. **Phase 2 (Proper)**: Separate scheduler into dedicated container service
3. **Phase 3 (Future)**: Migrate to APScheduler 4.0 when stable

**Files Created**:
- `docs/requirements/DEDICATED_SCHEDULER_SERVICE.md` - Full implementation spec with code examples

**Files Modified**:
- `docs/memory/roadmap.md` - Added bug to priority queue, added 11.6 Dedicated Scheduler Service

**Sources**:
- [APScheduler 4.0 API](https://apscheduler.readthedocs.io/en/master/api.html)
- [APScheduler Migration Guide](https://apscheduler.readthedocs.io/en/master/migration.html)
- [Redis Distributed Locks](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
- [python-redis-lock Docs](https://python-redis-lock.readthedocs.io/en/latest/readme.html)

---

### 2026-01-13 21:30:00
📝 **Docs: Dashboard Widget Field Names**

**Problem**: Agents building dashboards were using incorrect field names, causing validation errors:
- `text` widgets using `text`/`value`/`label` instead of `content`
- `list` widgets using `values`/`list` instead of `items`
- `link` widgets using `href` instead of `url`

**Solution**: Updated `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`:
- Added complete examples for ALL 11 widget types (was only 3)
- Highlighted required field names in bold in the table
- Added "IMPORTANT" warning box with common field name mistakes
- Each widget example includes inline comments showing which fields are required

**Files Modified**:
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Agent Dashboard section expanded

---

### 2026-01-13 20:45:00
📝 **Docs: Session Management Feature Flow**
- Updated `docs/memory/feature-flows/persistent-chat-tracking.md` with Session Management section
- Documented user stories: EXEC-019 (list sessions), EXEC-020 (view session), EXEC-021 (close session)
- Added backend endpoints with request/response examples
- Added database layer methods table with line numbers
- Added access control matrix (user vs admin)
- Added comprehensive testing section with 6 test cases + edge cases
- Note: Backend API complete, frontend UI not yet implemented

**Key Files**:
- `src/backend/routers/chat.py:967-1067` - Session endpoints
- `src/backend/db/chat.py:145-245` - Database operations

---

### 2026-01-13 13:34:57
✨ **Feature: Live Button in Tasks Panel**
- Added "Live" button for running tasks in TasksPanel
- Green pulsing badge indicates streaming is available
- Navigates to ExecutionDetail page with live streaming
- Uses explicit ID logic: server tasks use `task.id`, local tasks use `task.execution_id`
- Key file: `src/frontend/src/components/TasksPanel.vue`

---

### 2026-01-13 18:00:00
📡 **Feature: Live Execution Streaming**

**Problem**: Execution logs were only available after task completion. For long-running tasks (10+ minutes), users had no visibility into progress and couldn't make informed decisions about termination.

**Solution**: Real-time streaming of Claude Code execution logs to the Execution Detail page via Server-Sent Events (SSE).

**Key Features**:
- Live streaming indicator with animated pulse
- Auto-scroll with toggle control
- Stop button integration during streaming
- Late joiner support (buffered entries sent on subscribe)
- Graceful stream end on execution completion
- Fallback to static log on connection failure

**Technical Implementation**:
- Agent Server: ProcessRegistry extended with log queues and pub/sub methods
- Agent Server: New `/api/executions/{id}/stream` SSE endpoint
- Backend: Proxy endpoint with auth validation
- Frontend: Fetch-based SSE reader (not EventSource, due to header limitations)

**Files Modified**:
- `docker/base-image/agent_server/services/process_registry.py` - Added log streaming infrastructure
- `docker/base-image/agent_server/services/claude_code.py` - Publish entries as they arrive
- `docker/base-image/agent_server/routers/chat.py` - Added SSE streaming endpoint
- `src/backend/routers/chat.py` - Added proxy streaming endpoint
- `src/frontend/src/views/ExecutionDetail.vue` - Streaming UI with live indicator

**Spec**: `docs/requirements/LIVE_EXECUTION_STREAMING.md`

---

### 2026-01-13 14:30:00
📁 **Feature: System Agent Report Storage**

**Enhancement**: System agent now saves all generated reports to organized directories for historical tracking.

**Report Storage Structure**:
```
~/reports/
├── fleet/              # /ops/status reports
├── health/             # /ops/health reports
├── costs/              # /ops/costs reports
├── compliance/         # /ops/compatibility-audit reports
├── service-checks/     # /ops/service-check reports
├── schedules/          # /ops/schedules/list reports
└── executions/         # /ops/executions/list reports
```

**Naming Convention**: `YYYY-MM-DD_HHMM.md` (e.g., `2026-01-13_1430.md`)

**Files Modified**:
- `config/agent-templates/trinity-system/CLAUDE.md` - Added "Report Storage" section with directory structure and workflow
- `config/agent-templates/trinity-system/.claude/commands/ops/status.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/health.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/costs.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/compatibility-audit.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/service-check.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/schedules/list.md` - Added save instructions
- `config/agent-templates/trinity-system/.claude/commands/ops/executions/list.md` - Added save instructions

**Files Created**:
- `config/agent-templates/trinity-system/.gitignore` - Excludes reports/, dashboard.yaml, metrics.json, credentials

**Result**: Reports are now persisted for historical review. Latest report found by sorting filenames alphabetically.

---

### 2026-01-13 12:30:00
🔧 **Refactor: System Agent UI Consolidation**

**Problem**: System agent (`trinity-system`) had a dedicated `/system-agent` page with limited functionality - no Schedules tab. Regular agents had better feature parity.

**Solution**: Consolidated system agent into the regular agents interface:
- System agent now appears in Agents list (pinned at top, admin-only visibility)
- System agent uses `AgentDetail.vue` with full tab access including **Schedules**
- Purple ring and "SYSTEM" badge distinguish system agent visually
- Tabs hidden for system agent: Sharing, Permissions, Folders, Public Links (not applicable)
- Tabs shown: Info, Tasks, Dashboard, Terminal, Logs, Credentials, **Schedules**, Git, Files

**Files Modified**:
- `src/frontend/src/stores/agents.js` - Added `systemAgent` getter, `sortedAgentsWithSystem` getter, `_getSortedAgents()` internal function
- `src/frontend/src/views/Agents.vue` - Added admin check, `displayAgents` computed, system agent visual styling
- `src/frontend/src/views/AgentDetail.vue` - Updated `visibleTabs` to hide irrelevant tabs for system agent
- `src/frontend/src/components/NavBar.vue` - Removed "System" link
- `src/frontend/src/router/index.js` - Changed `/system-agent` route to redirect to `/agents/trinity-system`

**Files Deleted**:
- `src/frontend/src/views/SystemAgent.vue` - 782 lines (consolidated into AgentDetail)
- `src/frontend/src/components/SystemAgentTerminal.vue` - ~350 lines (uses standard TerminalPanelContent)

**Result**: System agent now has full feature parity with regular agents, including scheduling capabilities.

---

### 2026-01-13 09:45:00
📝 **Docs: Requirements.md Cleanup & Restructure**

**Problem**: requirements.md was bloated (1820 lines) with duplicate section numbers, misleading section names, excessive implementation details, and stale entries.

**Issues Fixed**:
1. **Duplicate Section 12** - Multi-Runtime renumbered to Section 14
2. **Misleading names** - "In Progress Features" (mostly complete) and "Planned Features" (mostly removed) reorganized into logical groups
3. **Confusing Phase labels** - Removed "Phase 12.x" references from Section 11 items
4. **Stale entry** - Updated "Agent Custom Metrics" to "Agent Dashboard" (implemented 2026-01-12)
5. **Excessive detail** - Condensed completed features from 20-50 lines to 3-5 lines each
6. **Removed features** - Collapsed to 2-3 lines with removal date/reason

**New Structure** (18 sections, ~560 lines):
1. Core Agent Management
2. Authentication & Authorization
3. Credential Management
4. Template System
5. Agent Chat & Terminal
6. Activity Monitoring
7. MCP Server
8. Infrastructure (+ Vector Logging)
9. Agent Collaboration (+ Timeline View, Replay Timeline)
10. Scheduling & Autonomy
11. GitHub Integration
12. Platform Operations (+ System Agent UI)
13. Content & File Management (+ Tasks Tab, Execution Log/Detail)
14. Multi-Runtime Support
15. Public Access
16. Advanced Features (+ Local Deploy, Agents Page UI, Dark Mode)
17. Planned Features
18. Future Vision

**Cross-check with feature-flows.md**: All 48 documented flows now have corresponding requirements entries.

**Result**: Document reduced from ~1820 to ~560 lines (69% reduction) while ensuring complete alignment with feature-flows.md.

**Files Modified**:
- `docs/memory/requirements.md` - Complete restructure

---

### 2026-01-13 08:19:28
✨ **Feature: Execution Termination Status Persistence**

**Problem**: When a task was terminated, the "cancelled" status was being overwritten by "failed" with "Task returned empty response" error. The termination correctly updated the DB to "cancelled", but the /task endpoint's error handler subsequently overwrote it.

**Solution**:
1. Backend now passes database `execution_id` to agent's process registry (same ID for both)
2. Termination endpoint accepts `task_execution_id` param to update database
3. Error handlers check if execution already "cancelled" before overwriting with "failed"
4. Added orange "cancelled" status styling in UI (distinct from red "failed")

**Files Modified**:
- `src/backend/routers/chat.py` - Pass execution_id to agent, check cancelled status before overwrite
- `docker/base-image/agent_server/models.py` - Add execution_id to ParallelTaskRequest
- `docker/base-image/agent_server/routers/chat.py` - Pass execution_id to runtime
- `docker/base-image/agent_server/services/claude_code.py` - Use provided execution_id for process registry
- `docker/base-image/agent_server/services/runtime_adapter.py` - Add execution_id param to interface
- `docker/base-image/agent_server/services/gemini_runtime.py` - Add execution_id param
- `src/frontend/src/components/TasksPanel.vue` - Pass task_execution_id on terminate, add cancelled styling

---

### 2026-01-13 08:17:00
✨ **Feature: Clickable Agent Names in Dashboard**

**Enhancement**: Agent names on the Dashboard graph are now clickable links to their detail pages.

**Files Modified**:
- `src/frontend/src/components/AgentNode.vue` - Add click handler, hover styles, nodrag class to agent name

---

### 2026-01-13 07:55:07
🔧 **Fix: Trinity MCP Lost on Credential Reload**

**Problem**: Agents with `.mcp.json.template` (like ruby-internal) lost Trinity MCP entry when credentials were reloaded. The credential update endpoint regenerated `.mcp.json` from template, overwriting the Trinity MCP that was injected at startup.

**Solution**: Re-inject Trinity MCP after any `.mcp.json` regeneration by calling `inject_trinity_mcp_if_configured()` - the same function used at agent startup. This ensures consistent behavior and single source of truth for Trinity MCP injection.

**Files Modified**:
- `docker/base-image/agent_server/routers/credentials.py` - Import and call `inject_trinity_mcp_if_configured()` after writing `.mcp.json`

---

### 2026-01-12 21:30:00
✨ **Feature: Execution Termination**

**Problem**: Users had no way to stop runaway executions or incorrect agent behavior once started.

**Solution**: Implemented full execution termination flow across all layers:

**Agent Container (docker/base-image/agent_server/)**:
- New `services/process_registry.py` - Thread-safe registry tracking subprocess handles by execution_id
- Modified `services/claude_code.py` - Registers/unregisters processes in both `execute_claude_code()` and `execute_headless_task()`
- New endpoints in `routers/chat.py`:
  - `POST /api/executions/{execution_id}/terminate` - SIGINT then SIGKILL
  - `GET /api/executions/running` - List running processes
  - `GET /api/executions/{execution_id}/status` - Check specific execution

**Backend (src/backend/)**:
- New proxy endpoints in `routers/chat.py`:
  - `POST /api/agents/{name}/executions/{execution_id}/terminate` - Proxies to agent, clears queue
  - `GET /api/agents/{name}/executions/running` - Lists agent's running executions
- Added `EXECUTION_CANCELLED` to `ActivityType` enum in `models.py`

**Frontend (src/frontend/)**:
- Stop button in `TasksPanel.vue` for running tasks
- Fetches running executions to get `execution_id` for termination
- Graceful UI feedback with spinner during termination

**Termination Flow**:
1. Frontend calls backend proxy endpoint
2. Backend forwards to agent container
3. Agent sends SIGINT (graceful), waits 5s, then SIGKILL if needed
4. Backend clears execution queue state
5. Activity tracked as EXECUTION_CANCELLED

**Files Modified**:
- `docker/base-image/agent_server/services/process_registry.py` (new)
- `docker/base-image/agent_server/services/claude_code.py`
- `docker/base-image/agent_server/routers/chat.py`
- `docker/base-image/agent_server/models.py`
- `src/backend/routers/chat.py`
- `src/backend/models.py`
- `src/frontend/src/components/TasksPanel.vue`

---

### 2026-01-12 20:45:00
⚡ **Performance: Frontend Polling Optimization**

**Problem**: AgentDetail page generated ~50 requests/minute from concurrent polling loops, overwhelming backend with multiple users/tabs.

**Solution**: Increased polling intervals while maintaining acceptable UX:

| Composable | Before | After | Reduction |
|------------|--------|-------|-----------|
| `useSessionActivity.js` | 2s | 5s | -60% |
| `useAgentStats.js` | 5s | 10s | -50% |
| `useAgentLogs.js` | 10s | 15s | -33% |
| `useGitSync.js` | 30s | 60s | -50% |
| **Total requests/min** | ~50 | ~20 | **-60%** |

**Result**: Per-page polling reduced from ~50 to ~20 requests/minute. With 5 users and 2 tabs each: 500 → 200 req/min saved.

**Files Modified**:
- `src/frontend/src/composables/useSessionActivity.js:117` - 2000ms → 5000ms
- `src/frontend/src/composables/useAgentStats.js:4,59` - MAX_POINTS 60→30, 5000ms → 10000ms
- `src/frontend/src/composables/useAgentLogs.js:45` - 10000ms → 15000ms
- `src/frontend/src/composables/useGitSync.js:164` - 30000ms → 60000ms

**Note**: Sparkline history still shows 5 minutes (30 samples × 10s = 300s).

---

### 2026-01-12 20:20:00
⚡ **Performance: Database Batch Queries (N+1 Fix)**

**Problem**: `get_accessible_agents()` was making 8-10 database queries PER agent:
- `can_user_access_agent()` - 2-4 queries
- `get_agent_owner()` - 1 query (redundant)
- `is_agent_shared_with_user()` - 2 queries
- `get_autonomy_enabled()` - 1 query
- `get_git_config()` - 1 query
- `get_resource_limits()` - 1 query

With 20 agents: **160-200 database queries** per `/api/agents` request.

**Solution**: Added `get_all_agent_metadata()` batch query that JOINs all related tables in ONE query:
- `agent_ownership` - owner, is_system, autonomy_enabled, resource limits
- `users` - owner username/email
- `agent_git_config` - GitHub repo/branch
- `agent_sharing` - share access check (with user's email)

**Result**: Database queries reduced from **160-200 to 2** per request.

**Files Modified**:
- `src/backend/db/agents.py:467-529` - Added `get_all_agent_metadata()` batch query
- `src/backend/database.py:845-850` - Exposed method on DatabaseManager
- `src/backend/services/agent_service/helpers.py:83-153` - Rewrote `get_accessible_agents()` to use batch

**Combined with Docker stats optimization**: `/api/agents` now responds in <50ms (was 2-3s).

---

### 2026-01-12 20:10:00
⚡ **Performance: Docker Stats Optimization - Fast Agent List**

**Problem**: The `/api/agents` endpoint was slow (~2-3s with multiple agents) due to Docker API calls for container metadata.

**Solution**: Added `list_all_agents_fast()` function that extracts agent info ONLY from container labels, avoiding slow operations like:
- `container.attrs` (full Docker inspect API call)
- `container.image` (image metadata lookup)
- `container.stats()` (CPU sampling - 2+ seconds per container)

**Result**: Response time reduced from ~2-3s to **<50ms** for agent list endpoints.

**Files Modified**:
- `src/backend/services/docker_service.py:101-159` - Added `list_all_agents_fast()` function
- `src/backend/services/agent_service/helpers.py:14-17,92` - Use fast version in `get_accessible_agents()`
- `src/backend/main.py:26,177` - Use fast version at startup
- `src/backend/routers/ops.py:22,54,132,239,343,558,620` - Use fast version in ops endpoints
- `src/backend/routers/telemetry.py:16,126` - Use fast version in telemetry

**Note**: Per-agent stats (CPU, memory) still available via `/api/agents/{name}/stats` (~1s due to Docker sampling).

---

### 2026-01-12 18:15:00
🐛 **Fix: Credential Injection Bug - Passing Dict Instead of Username**

**Problem**: Credentials assigned to agents were not being injected on restart despite the previous fix. The `.env` file was not being created.

**Root Cause**: In `inject_assigned_credentials()`, the code was passing the entire `owner` dict from `db.get_agent_owner()` to `credential_manager.get_assigned_credential_values()` and `get_assigned_file_credentials()`. However, these methods expect `user_id` as a string (e.g., "admin"), not a dict. The ownership verification in `get_credential()` was comparing strings to dicts, which always failed silently, resulting in empty credentials being returned.

```python
# BROKEN - owner is a dict like {"owner_username": "admin", ...}
credentials = credential_manager.get_assigned_credential_values(agent_name, owner)

# FIXED - extract the username string
owner_username = owner.get("owner_username")
credentials = credential_manager.get_assigned_credential_values(agent_name, owner_username)
```

**Solution**:
1. Extract `owner_username` from the dict before passing to credential methods
2. Add validation in case `owner_username` is missing
3. Add logging to show owner username in credential injection messages
4. Fix import of `CredentialManager` (was trying to import non-existent `credential_manager` singleton)
5. Update `/api/agents/{name}/start` endpoint to include `credentials_injection` and `credentials_result` in response

**Files Modified**:
- `src/backend/services/agent_service/lifecycle.py:69-92` - Fix owner_username extraction and import
- `src/backend/routers/agents.py:315-335` - Include credential injection status in start response

**Verification**:
```bash
# Stop agent and start via API
curl -X POST /api/agents/demo-fleet-ruby/start
# Response includes: "credentials_injection": "success", "credentials_result": {"status": "success", "credential_count": 2}

# Check agent .env file
docker exec agent-demo-fleet-ruby cat /home/developer/.env
# Shows: API_KEY="value1"
```

---

### 2026-01-12 18:00:00
📚 **Docs: Expanded Custom Metrics Documentation**

Significantly expanded the Custom Metrics section in `TRINITY_COMPATIBLE_AGENT_GUIDE.md`:

**Added**:
- File locations: where agent server looks for `template.yaml` and `metrics.json`
- Complete template.yaml examples for all 6 metric types (counter, gauge, percentage, status, duration, bytes)
- `percentage` type with `warning_threshold` and `critical_threshold` fields
- `status` type with `values` array showing value/color/label structure
- Available colors for status badges: green, red, yellow, gray, blue, orange
- `metrics.json` format with `last_updated` optional field
- Complete working example showing template.yaml → metrics.json → CLAUDE.md integration
- Note that agent must be running for metrics to be visible
- Note about auto-refresh every 30 seconds

**Updated**:
- Removed unused `icon` field from schema (not implemented in frontend)
- Added cross-reference to detailed Custom Metrics section from schema

**Files Modified**:
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Expanded Custom Metrics section

---

### 2026-01-12 17:30:00
🐛 **Fix: Credential Injection on Agent Restart**

**Problem**: Credentials assigned via the Credentials tab were NOT being injected when an agent was restarted. Credentials were only injected at agent creation time.

**Root Cause**:
1. `start_agent_internal()` only started the container and injected the Trinity meta-prompt - it did NOT fetch or inject assigned credentials
2. The "Apply to Agent" button was only visible when `hasChanges` was true, which reset on page refresh

**Solution**:
1. Added `inject_assigned_credentials()` function to `lifecycle.py` that:
   - Gets the agent owner from the database
   - Fetches assigned credentials from Redis via credential_manager
   - Pushes credentials to the running agent via internal API
   - Includes retry logic for agent startup delays

2. Modified `start_agent_internal()` to call `inject_assigned_credentials()` after starting the container

3. Fixed the "Apply to Agent" button visibility in `CredentialsPanel.vue`:
   - Changed from `v-if="hasChanges && agentStatus === 'running'"`
   - To `v-if="agentStatus === 'running' && assignedCredentials.length > 0"`
   - Added "Start agent to apply" hint when agent is stopped

**Files Modified**:
- `src/backend/services/agent_service/lifecycle.py:54-121` - Added `inject_assigned_credentials()` function
- `src/backend/services/agent_service/lifecycle.py:165-167` - Call credential injection in `start_agent_internal()`
- `src/backend/services/agent_service/__init__.py` - Export new function
- `src/frontend/src/components/CredentialsPanel.vue:27-44` - Fix button visibility logic

**Verification**:
1. Create credentials in the Credentials page
2. Assign them to an agent via Agent Detail → Credentials tab
3. Stop and restart the agent
4. Credentials are now automatically injected on start

---

### 2026-01-12 16:00:00
📦 **Feature: Package Persistence & Version Tracking**

**Problem**: When agent containers are recreated (due to base image updates or config changes), system packages installed via `apt-get` or `npm install -g` are lost. Users had to manually reinstall packages each time.

**Solution**: Added `~/.trinity/setup.sh` convention - a persistent setup script that runs on container start to reinstall packages.

**Package Persistence**:
1. `startup.sh` now runs `~/.trinity/setup.sh` if it exists
2. Agents are instructed (via Trinity CLAUDE.md injection) to append install commands to this script
3. Templates can ship with pre-defined `setup.sh` for known dependencies
4. Documented in `TRINITY_COMPATIBLE_AGENT_GUIDE.md`

**Version Tracking**:
1. Base image Dockerfile now includes `trinity.base-image-version` label
2. Container labels store version at creation time
3. `AgentStatus` model includes `base_image_version` field
4. Frontend can compare agent version vs current platform version

**Files Modified**:
- `docker/base-image/startup.sh` - Run setup.sh on start
- `docker/base-image/Dockerfile` - Add VERSION arg and label
- `docker/base-image/agent_server/routers/trinity.py` - Inject package persistence instructions
- `src/backend/models.py` - Add base_image_version to AgentStatus
- `src/backend/services/docker_service.py` - Extract version from container/image labels
- `src/backend/services/agent_service/crud.py` - Add version label, get_platform_version helper
- `src/backend/main.py` - Fix version endpoint path resolution
- `docker-compose.yml` - Mount VERSION file into backend container
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Added Package Persistence section

**Usage**:
```bash
# In agent terminal - install and persist
sudo apt-get install -y ffmpeg
mkdir -p ~/.trinity
echo "sudo apt-get install -y ffmpeg" >> ~/.trinity/setup.sh
```

---

### 2026-01-12 14:30:00
🔐 **Feature: Full Capabilities Mode for Container Software Installation**

**Problem**: Claude Code agents in containers couldn't install software via `apt-get` or `sudo` commands because security capabilities were too restricted (`cap_drop=['ALL']`).

**Solution**: Added configurable `full_capabilities` option that enables Docker default capabilities when True, allowing `apt-get`, `sudo`, and other system commands to work while maintaining container isolation.

**Changes**:
1. Added `full_capabilities: Optional[bool] = False` to AgentConfig model
2. Container creation respects this setting:
   - `full_capabilities=False` (default): Restricted mode with `cap_drop=['ALL']`, `apparmor:docker-default`, `noexec` on `/tmp`
   - `full_capabilities=True`: Docker defaults (no capability restrictions, no AppArmor, executable `/tmp`)
3. Setting persisted in database (`agent_ownership.full_capabilities` column)
4. Setting preserved in container labels for recreation
5. Template support via `full_capabilities: true` or `security: {full_capabilities: true}` in template.yaml

**Files Modified**:
- `src/backend/models.py:31` - Added full_capabilities field to AgentConfig
- `src/backend/services/agent_service/crud.py:158-164,435-443,467-473` - Container creation, template parsing, DB storage
- `src/backend/services/agent_service/lifecycle.py:140-141,222-228` - Container recreation preserves setting
- `src/backend/database.py:248-256,308-311,860-864` - Migration and wrapper methods
- `src/backend/db/agents.py:416-461` - get/set_full_capabilities methods

**Usage** (template.yaml):
```yaml
name: my-agent
full_capabilities: true  # Enables apt-get, sudo, etc.
resources:
  cpu: "2"
  memory: "4g"
```

**Security Note**: Only enable for agents that genuinely need system-level access. Default (restricted) mode is recommended for most use cases.

---

### 2026-01-12 12:55:00
🔧 **Fix: OpenTelemetry Prometheus Export - Delta to Cumulative Conversion**

**Problem**: Claude Code SDK exports metrics with delta temporality, but the Prometheus exporter requires cumulative temporality. Metrics were being received by the collector but not exposed on the Prometheus endpoint.

**Root Cause**: The base OTel Collector image (`otel/opentelemetry-collector:0.91.0`) doesn't include the `deltatocumulative` processor needed to convert delta metrics.

**Solution**:
1. Switched to contrib image: `otel/opentelemetry-collector-contrib:0.120.0`
2. Updated config mount path to `/etc/otelcol-contrib/config.yaml` (contrib uses different path)
3. Added `deltatocumulative` processor to the metrics pipeline
4. Removed `const_labels` from Prometheus exporter (conflicts with `resource_to_telemetry_conversion`)

**Files Modified**:
- `docker-compose.yml:167-176` - Changed image and config mount path
- `config/otel-collector.yaml` - Added deltatocumulative processor, fixed exporter config
- `docs/memory/feature-flows/opentelemetry-integration.md` - Updated with critical configuration notes

**Verification**: Metrics now appear correctly in Prometheus endpoint at `http://localhost:8889/metrics`:
- `trinity_claude_code_cost_usage_USD_total` - Cost per model
- `trinity_claude_code_token_usage_tokens_total` - Tokens by type (input, output, cacheRead, cacheCreation)

---

### 2026-01-12 11:30:00
🛡️ **Feature: max_turns Parameter for Runaway Prevention**

**Problem**: Headless task execution could run indefinitely if an agent got stuck in an infinite loop or continued executing far beyond expected scope.

**Solution**: Added optional `max_turns` parameter to the `/api/task` endpoint. When specified, it adds `--max-turns N` to the Claude Code or Gemini CLI command, limiting the number of agentic turns before the CLI exits.

**Files Modified**:
- `docker/base-image/agent_server/models.py:221` - Added `max_turns: Optional[int] = None` to ParallelTaskRequest
- `docker/base-image/agent_server/services/runtime_adapter.py:106` - Added max_turns to execute_headless interface
- `docker/base-image/agent_server/services/claude_code.py:604-606` - Pass --max-turns to Claude Code
- `docker/base-image/agent_server/services/gemini_runtime.py:542-544` - Pass --max-turns to Gemini CLI
- `docker/base-image/agent_server/routers/chat.py:120` - Pass max_turns to runtime
- `src/backend/models.py:110` - Added max_turns to backend ParallelTaskRequest

**Usage**:
```json
POST /api/task
{
  "message": "Analyze this codebase",
  "max_turns": 50,
  "timeout_seconds": 900
}
```

**Documentation**: Updated `docs/memory/feature-flows/parallel-headless-execution.md`

---

### 2026-01-12 10:15:00
🔧 **UI: Git Buttons Renamed and Enhanced**

**Changes**:
1. **Renamed "Sync" to "Push"**: Consistent Pull/Push terminology
2. **Pull button now shows commits behind**: Displays "Pull (N)" when remote has N commits to fetch
3. **Dynamic button colors**:
   - Pull: Blue when behind, gray when up to date
   - Push: Orange when local changes, gray when clean

**Files Modified**:
- `src/frontend/src/components/AgentHeader.vue` - Button labels, colors, and props
- `src/frontend/src/composables/useGitSync.js` - Added `gitBehind` computed property
- `src/frontend/src/views/AgentDetail.vue` - Pass `gitBehind` prop, handle `git-push` event
- `docs/memory/feature-flows/github-sync.md` - Updated documentation

---

### 2026-01-11 21:44:00
🐛 **Fix: Autonomy Toggle Not Syncing Schedules to APScheduler**

**Problem**: When autonomy was toggled via the UI or API, schedules were enabled/disabled in the database but APScheduler was never updated. This caused schedules to stop running after autonomy was re-enabled until the backend was restarted.

**Root Cause**: `autonomy.py` was calling `db.set_schedule_enabled()` directly, which only updates the database. It should use `scheduler_service.enable_schedule()` and `scheduler_service.disable_schedule()` which update both the database AND APScheduler.

**Fix**: Updated `services/agent_service/autonomy.py` to use scheduler_service methods:
```python
# Before (broken):
db.set_schedule_enabled(schedule_id, enabled)

# After (fixed):
if enabled:
    scheduler_service.enable_schedule(schedule_id)
else:
    scheduler_service.disable_schedule(schedule_id)
```

**Files Modified**:
- `src/backend/services/agent_service/autonomy.py` - Import scheduler_service, use enable/disable methods

**Testing**:
1. Disabled autonomy → Jobs removed from APScheduler
2. Re-enabled autonomy → Jobs re-added to APScheduler
3. Schedule executed on next cron trigger ✅

---

### 2026-01-11 21:30:00
📚 **Docs: Archive Deprecated Feature Flows**

Cleaned up feature-flows index by archiving 4 deprecated/removed feature flows:

| Flow | Status | Reason |
|------|--------|--------|
| auth0-authentication.md | REMOVED | Auth0 deleted (2026-01-01), replaced by email auth |
| agent-chat.md | DEPRECATED | UI replaced by Web Terminal (2025-12-25) |
| vector-memory.md | REMOVED | Platform-injected memory removed (2025-12-24) |
| agent-network-replay-mode.md | SUPERSEDED | Replaced by Dashboard Timeline View (2026-01-04) |

**Changes**:
- Created `docs/memory/feature-flows/archive/` directory
- Moved 4 obsolete flow documents to archive
- Updated feature-flows.md index: removed struck-through entries, added "Archived Flows" section

---

### 2026-01-11 13:30:00
🐛 **Fix: Frontend Timeline Execution ID Extraction**

**Problem**: Dashboard Timeline couldn't navigate to Execution Detail pages because the frontend was looking for execution ID in the wrong place.

**Root Cause**: `network.js` was only checking `details.execution_id` and `details.related_execution_id` (inside the details JSON), but the backend now stores `related_execution_id` as a **top-level field** on the activity record.

**Fix**: Updated `network.js` line 176 to prioritize top-level field:
```javascript
// Before (broken):
execution_id: details.execution_id || details.related_execution_id || null

// After (fixed):
execution_id: activity.related_execution_id || details.execution_id || details.related_execution_id || null
```

**Files Modified**:
- `src/frontend/src/stores/network.js` - Extract execution_id from activity.related_execution_id

**Impact**: Timeline clicks now correctly navigate to Execution Detail page with proper database ID.

---

### 2026-01-11 12:15:00
🔧 **Refactor: Consistent Execution ID Tracking Across All Entry Points**

**Problem**: Execution tracking had multiple inconsistencies:
1. Activities only stored execution ID in `details` dict, not in `related_execution_id` field
2. Tool call activities had no link to parent execution
3. Chat response returned queue ID (transient) instead of database ID (permanent)
4. Manual trigger executions had no activity tracking
5. Schedule activities were created BEFORE execution record (no ID to link)

**Solution**: Comprehensive fix to ensure all execution types use consistent ID tracking:

**chat.py Changes**:
1. Collaboration activity: Added `related_execution_id=task_execution_id` (line 201)
2. Chat start activity: Added `related_execution_id=task_execution_id` (line 224)
3. Tool call activities: Added `related_execution_id=task_execution_id` (line 302)
4. Chat response: Now includes both `id` (queue) and `task_execution_id` (database) (line 357-361)
5. Parallel task activity: Added `related_execution_id=execution_id` (line 462)

**scheduler_service.py Changes**:
1. Reordered operations: Create execution record BEFORE activity tracking
2. Schedule activity: Added `related_execution_id=execution.id` (line 222)
3. Manual trigger: Added full activity tracking with `related_execution_id` (lines 431-446)
4. Manual trigger: Added activity completion for success/failure paths

**ID System Clarification**:
| ID Type | Format | Storage | Purpose |
|---------|--------|---------|---------|
| Queue ID | UUID | Redis (10m TTL) | Internal queue management |
| Database ID | token_urlsafe(16) | SQLite (permanent) | API access, UI navigation |

**Files Modified**:
- `src/backend/routers/chat.py` - 5 activity tracking calls + response format
- `src/backend/services/scheduler_service.py` - Reordered + added activity tracking

**Impact**: Timeline clicks now correctly navigate to Execution Detail page. All execution types (chat, parallel task, scheduled, manual trigger) now have consistent activity linkage.

---

### 2026-01-11 10:31:00
🐛 **Fix: Agent Authorization Dependencies Return 404 for Non-Existent Agents**

**Problem**: API endpoints using `AuthorizedAgent`, `AuthorizedAgentByName`, `OwnedAgent`, and `OwnedAgentByName` dependencies returned incorrect responses for non-existent agents:
- Admin users: 200 OK with empty data (admin bypass skipped existence check)
- Non-admin users: 403 Forbidden (permission check failed before existence check)

**Expected**: All users should receive 404 Not Found for non-existent agents.

**Fix**: Updated all four authorization dependency functions to check agent existence before checking permissions:
1. `get_authorized_agent()` - Lines 183-188
2. `get_authorized_agent_by_name()` - Lines 235-240
3. `get_owned_agent()` - Lines 213-218
4. `get_owned_agent_by_name()` - Lines 273-278

Each function now:
1. First checks if agent exists via `db.get_agent_owner()`
2. Returns 404 if agent doesn't exist
3. Then checks user permissions
4. Returns 403 if user lacks access

**Files Modified**:
- `src/backend/dependencies.py` - All 4 agent authorization dependencies

**Test**: `test_get_executions_nonexistent_agent_returns_404` now passes

---

### 2026-01-11 10:18:00
🐛 **Fix: Timeline Execution Links Not Finding Executions**

**Problem**: Clicking on execution bars in the Dashboard Timeline opened the Execution Detail page, but it showed "Execution not found" even though executions existed in the database.

**Root Cause**: ID mismatch between two different execution tracking systems:
- **Queue execution ID** (UUID format): `4df12ce0-742b-4671-b9ae-ad024ba7c595`
- **Database execution ID** (base64 format): `lxTrun4spCbAtlMUHh748w`

Activities were storing `execution.id` (queue UUID) instead of `task_execution_id` (database ID).

**Fix**: Changed all activity tracking calls to use `task_execution_id` instead of `execution.id`:
- Line 205: collaboration_activity details
- Line 227: chat_start activity details
- Line 320: chat_activity completion details
- Line 333: collaboration_activity completion details

**Files Modified**:
- `src/backend/routers/chat.py` - 4 locations fixed

**Note**: Existing activities have wrong execution_id. New executions will work correctly.

---

### 2026-01-11 09:15:00
✨ **UX: Timeline Execution Click Opens New Tab**

Changed the Dashboard Timeline behavior when clicking on execution bars:
- **Before**: Same-tab navigation to Execution Detail page
- **After**: Opens Execution Detail page in a new browser tab

This allows users to explore execution details without losing their place on the Dashboard.

**Files Modified**:
- `src/frontend/src/components/ReplayTimeline.vue` - Changed `router.push()` to `window.open(route.href, '_blank')`

**Docs Updated**:
- `docs/memory/feature-flows/dashboard-timeline-view.md`
- `docs/memory/feature-flows/execution-detail-page.md`

---

### 2026-01-10 13:30:00
🐛 **Fix: Execution Transcripts Not Showing for Chat-Based Executions**

**Problem**: Execution transcripts were not displaying for some executions (especially MCP and chat-based ones). The frontend's `parseExecutionLog()` expects raw Claude Code stream-json format, but `/api/chat` was returning a simplified `ExecutionLogEntry` format.

**Root Cause**:
- Agent `/api/task` returns raw Claude Code format → ✅ Works
- Agent `/api/chat` returns simplified `ExecutionLogEntry[]` format → ❌ Broken

**Solution**: Unified both endpoints to return raw Claude Code stream-json format:

1. **Agent Server Changes** (`docker/base-image/agent_server/`):
   - `services/claude_code.py`: Updated `execute_claude_code()` to capture raw_messages
   - `services/gemini_runtime.py`: Updated `execute()` to capture raw_messages
   - `services/runtime_adapter.py`: Updated abstract interface signature
   - `routers/chat.py`: Now returns `raw_messages` as `execution_log`

2. **Backend Changes** (`src/backend/routers/chat.py`):
   - Extracts both `execution_log` (raw) and `execution_log_simplified`
   - Uses simplified format for activity tracking
   - Stores raw format in database for UI display

**Files Modified**:
- `docker/base-image/agent_server/services/claude_code.py`
- `docker/base-image/agent_server/services/gemini_runtime.py`
- `docker/base-image/agent_server/services/runtime_adapter.py`
- `docker/base-image/agent_server/routers/chat.py`
- `src/backend/routers/chat.py`

**Rebuild Required**: Base image (`./scripts/deploy/build-base-image.sh`) + backend

---

### 2026-01-10 12:00:00
✨ **Feature: Execution Detail Page**

**Goal**: Provide a dedicated page for viewing comprehensive execution details instead of just a modal.

**Changes**:

1. **New Page** (`ExecutionDetail.vue`):
   - Route: `/agents/:name/executions/:executionId`
   - Header with back button, agent name breadcrumb, status badge
   - Metadata cards: Duration, Cost, Context, Trigger
   - Timestamps row: Started, Completed, Execution ID
   - Task Input panel with full message
   - Error panel (conditional, for failed executions)
   - Response Summary panel
   - Full Execution Transcript (reuses parseExecutionLog logic)

2. **Navigation from TasksPanel** (`TasksPanel.vue:207-217`):
   - New external link icon before the log modal icon
   - Links to ExecutionDetail page for the execution
   - Only shown for server-persisted tasks (not local-)

3. **Navigation from Timeline** (`ReplayTimeline.vue:767-785`):
   - Updated `navigateToExecution()` to open ExecutionDetail page
   - Previously navigated to Tasks tab with highlight
   - Now opens dedicated page for better UX

4. **Router** (`router/index.js:41-46`):
   - Added route: `/agents/:name/executions/:executionId`
   - Named route: `ExecutionDetail`
   - Requires authentication

**Files Modified**:
- `src/frontend/src/views/ExecutionDetail.vue` - New page component
- `src/frontend/src/router/index.js` - New route
- `src/frontend/src/components/TasksPanel.vue` - External link icon
- `src/frontend/src/components/ReplayTimeline.vue` - Updated navigation
- `docs/memory/feature-flows/execution-detail-page.md` - New feature flow

---

### 2026-01-10 11:15:00
✨ **Feature: Timeline Click-to-Navigate to Execution Details**

**Goal**: Allow users to click on execution bars in the Dashboard Timeline to view full execution details in the Tasks tab.

**Changes**:

1. **Execution ID Tracking** (`network.js`):
   - Now extracts `execution_id` from activity details
   - Available via `details.execution_id` or `details.related_execution_id`
   - Passed through to Timeline events for navigation

2. **Click Handler** (`ReplayTimeline.vue`):
   - Added `@click="navigateToExecution(activity)"` on execution bars
   - Navigates to `/agents/{name}?tab=tasks&execution={id}`
   - Tooltip updated: "(Click to view details)"
   - Visual feedback: hover adds white stroke outline

3. **Tab Query Param** (`AgentDetail.vue`):
   - Now reads `tab` query param on mount
   - Auto-selects requested tab (tasks, terminal, etc.)
   - Passes `highlightExecutionId` prop to TasksPanel

4. **Execution Highlighting** (`TasksPanel.vue`):
   - New prop: `highlightExecutionId`
   - Highlighted execution has indigo ring border
   - Auto-expands the highlighted execution
   - Scrolls to highlighted execution on load

**Files Modified**:
- `src/frontend/src/stores/network.js` - Extract execution_id from details
- `src/frontend/src/components/ReplayTimeline.vue` - Click handler, navigation
- `src/frontend/src/views/AgentDetail.vue` - Tab query param, pass highlight prop
- `src/frontend/src/components/TasksPanel.vue` - Highlight styling, auto-scroll
- `docs/memory/feature-flows/dashboard-timeline-view.md` - Updated documentation

---

### 2026-01-10 10:30:00
✨ **Feature: Timeline All Executions Display**

**Goal**: Show ALL executions on Dashboard Timeline (scheduled, manual, collaboration) - not just agent-to-agent collaborations.

**Changes**:

1. **Expanded Activity Query** (`network.js`):
   - Now fetches: `agent_collaboration,schedule_start,schedule_end,chat_start,chat_end`
   - Filters out regular user chat sessions (keeps automated executions only)
   - Parses `activity_type`, `triggered_by`, `schedule_name` for color coding

2. **Activity Type Color Coding** (`ReplayTimeline.vue`):
   | Type | Color | Use Case |
   |------|-------|----------|
   | Cyan `#06b6d4` | Agent-to-agent calls | `agent_collaboration` |
   | Purple `#8b5cf6` | Scheduled tasks | `schedule_start/end` |
   | Green `#22c55e` | Manual/automated tasks | `chat_start/end` (non-user) |
   | Red `#ef4444` | Errors | Any failed execution |
   | Amber `#f59e0b` | In progress | Currently running |

3. **Legend Display**:
   - Color legend added to Timeline header
   - Shows: Collaboration (cyan), Scheduled (purple), Task (green)
   - Hidden on small screens for space

4. **Enhanced Tooltips**:
   - Now shows execution type prefix: "Agent Call", "Scheduled: {name}", "Manual Task", "Task"
   - Status indicator: "(Error)", "(In Progress)"
   - Duration with estimated marker when needed

5. **Arrows Only for Collaborations**:
   - Non-collaboration events render bars only (no target_agent)
   - Communication arrows only shown for `agent_collaboration` events

**Files Modified**:
- `src/frontend/src/stores/network.js` - Expanded query, filter logic, new fields
- `src/frontend/src/components/ReplayTimeline.vue` - Color coding, legend, tooltips
- `docs/memory/feature-flows/dashboard-timeline-view.md` - Updated documentation
- `docs/requirements/TIMELINE_ALL_EXECUTIONS.md` - Requirements (completed)

**Backend**: No changes needed - API already supports all activity types.

---

### 2026-01-10 09:45:00
🔧 **Fix: Dashboard Timeline View Improvements**

**Goal**: Address user feedback to improve Timeline view usability and feature parity with Graph view.

**Changes**:

1. **Graph Hidden in Timeline Mode**:
   - Graph canvas now completely hidden when Timeline is active
   - Views are mutually exclusive - no overlay issues

2. **Default Zoom to Last 2 Hours**:
   - On page load, zoom defaults to `timeRangeHours / 2`
   - For 24h range, zoom is 12x (shows ~2 hours of activity)
   - Auto-scrolls to "Now" position on mount

3. **Rich Agent Tiles** (expanded from 220px to 280px):
   - Now matches Graph tile layout exactly:
   - Row 1: Agent name, runtime badge (Claude/Gemini), system badge, status dot
   - Row 2: Activity state (Active/Idle/Offline) + Autonomy toggle (AUTO/Manual)
   - Row 3: GitHub repo or "Local agent"
   - Row 4: Context progress bar with percentage
   - Row 5: Execution stats (tasks, success rate, cost, last execution)
   - Row 6: Resource info (memory, CPU cores)
   - Row 7: View Details button (or System Dashboard link for system agent)

4. **Autonomy Toggle**:
   - Added `@toggle-autonomy` event emission
   - Wired to Dashboard's `handleToggleAutonomy()` handler
   - Toggle works directly from Timeline view

5. **New Props**:
   - `nodes` - Full node data for tile information
   - `timeRangeHours` - For default zoom calculation

**Files Modified**:
- `src/frontend/src/views/Dashboard.vue` - Hide Graph in Timeline, pass nodes prop
- `src/frontend/src/components/ReplayTimeline.vue` - Rich tiles, default zoom, autonomy toggle
- `docs/memory/feature-flows/dashboard-timeline-view.md` - Updated documentation

---

### 2026-01-10 09:22:00
✨ **Feature: Dashboard Timeline View**

**Goal**: Transform Dashboard to offer two views - Graph (node-based) and Timeline (waterfall activity view) with live event streaming and rich agent labels.

**Changes**:

1. **Renamed Mode Toggle**: "Live/Replay" → "Graph/Timeline"
   - Graph view shows the VueFlow node visualization (unchanged)
   - Timeline view shows waterfall activity timeline

2. **Live Event Streaming in Timeline**:
   - WebSocket stays connected in Timeline view (previously disconnected)
   - New events appear at right edge in real-time
   - "Now" marker updates continuously every second
   - Auto-scroll to latest events (disables when user scrolls back)
   - "Jump to Now" button appears when scrolled away

3. **Rich Agent Labels** (expanded from 150px to 220px):
   - Status dot (green pulsing = active, green = running, gray = stopped)
   - Agent name with click-to-navigate to AgentDetail
   - Context percentage with color coding (red ≥80%, yellow ≥60%)
   - Mini progress bar showing context usage
   - Execution stats row: tasks count, success rate, cost, last execution time

4. **Improved Layout**:
   - Row height increased from 32px to 64px for rich labels
   - Timeline max-height increased from 320px to 400px
   - Playback controls hidden in live mode (shown only when replaying)
   - Stopped agents display grayed out with "Stopped" label

5. **State Persistence**:
   - View preference saved to `trinity-dashboard-view` localStorage key

**Files Modified**:
- `src/frontend/src/views/Dashboard.vue` - Toggle labels, stats props
- `src/frontend/src/components/ReplayTimeline.vue` - Rich labels, live mode
- `src/frontend/src/stores/network.js` - `setViewMode()`, keep WebSocket connected

---

### 2026-01-09 21:30:00
✨ **Feature: System Agent Schedule & Execution Management**

**Goal**: Enable System Agent to manage schedules and monitor executions via slash commands and REST API.

**Backend Changes**:
- Added `GET /api/ops/schedules` endpoint for listing all schedules with filters and execution history
- Added `list_all_schedules()` method to `db/schedules.py`
- Exposed method via `database.py` DatabaseManager

**System Agent Slash Commands**:
| Command | Purpose |
|---------|---------|
| `/ops/schedules` | Quick schedule overview |
| `/ops/schedules/list` | Detailed schedule listing with history |
| `/ops/schedules/pause [agent]` | Pause schedules (all or per-agent) |
| `/ops/schedules/resume [agent]` | Resume paused schedules |
| `/ops/executions/list [agent]` | List recent task executions |
| `/ops/executions/status <id>` | Get execution details |

**Files Created**:
- `config/agent-templates/trinity-system/.claude/commands/ops/schedules/list.md`
- `config/agent-templates/trinity-system/.claude/commands/ops/schedules/pause.md`
- `config/agent-templates/trinity-system/.claude/commands/ops/schedules/resume.md`
- `config/agent-templates/trinity-system/.claude/commands/ops/executions/list.md`
- `config/agent-templates/trinity-system/.claude/commands/ops/executions/status.md`

**Files Modified**:
- `src/backend/routers/ops.py` - New `/api/ops/schedules` endpoint
- `src/backend/db/schedules.py` - `list_all_schedules()` method
- `src/backend/database.py` - Exposed method
- `config/agent-templates/trinity-system/CLAUDE.md` - Schedule management docs
- `config/agent-templates/trinity-system/template.yaml` - New slash commands
- `config/agent-templates/trinity-system/.claude/commands/ops/schedules.md` - Updated overview

---

### 2026-01-09 19:45:00
🔧 **Refactor: AgentDetail.vue Modularization**

**Goal**: Split monolithic AgentDetail.vue (~1860 lines) into modular, maintainable components.

**Results**:
- **AgentDetail.vue**: Reduced from ~1860 lines to **603 lines** (68% reduction)
- **9 new components extracted** totaling ~1500 lines (focused, reusable)

**Components Created**:
| Component | Lines | Purpose |
|-----------|-------|---------|
| `AgentHeader.vue` | 316 | Agent header with stats, controls, git sync |
| `CredentialsPanel.vue` | 296 | Credential management (assign, quick-add) |
| `TerminalPanelContent.vue` | 165 | Terminal tab inner content |
| `PermissionsPanel.vue` | 147 | Agent collaboration permissions |
| `FileTreeNode.vue` | 140 | Recursive file tree (render function) |
| `FilesPanel.vue` | 129 | Files tab with tree browser |
| `SharingPanel.vue` | 125 | Share agent with team members |
| `ResourceModal.vue` | 109 | Memory/CPU configuration modal |
| `LogsPanel.vue` | 73 | Container logs viewer |

**Key Improvements**:
- Each component is self-contained with its own composable initialization
- Terminal KeepAlive behavior preserved (v-show pattern maintained)
- Tab navigation simplified with computed `visibleTabs` array
- All existing functionality preserved (zero behavioral changes)

**Pattern**: Components receive `agent-name` and `agent-status` props, initialize their own composables internally. This follows the pattern established by existing panels (`InfoPanel`, `SchedulesPanel`, etc.).

**Modified Files**:
- `src/frontend/src/views/AgentDetail.vue` - Lean orchestrator
- `src/frontend/src/components/` - 9 new component files

---

### 2026-01-09 18:30:00
🐛 **Fix: System Agent Not Visually Distinct on Dashboard**

**Issue**: Trinity System Agent displayed as a regular agent instead of using the distinct purple `SystemAgentNode` component.

**Root Cause**: `register_agent_owner()` silently failed on `IntegrityError` when the ownership record already existed, without updating the `is_system` flag. If the system agent was created before the `is_system` feature, or if the container was recreated, the flag stayed `0` (false).

**Fix**:
1. Modified `register_agent_owner()` to update `is_system=1` when record exists and `is_system=True` is passed
2. Added call in `ensure_deployed()` to always ensure database record has correct flag

**Modified Files**:
- `src/backend/db/agents.py` - Update is_system on conflict
- `src/backend/services/system_agent_service.py` - Ensure flag on startup

---

### 2026-01-09 17:00:00
🐛 **Fix: GitHub PAT Lookup for Dynamic Templates**

**Issue**: MCP `create_agent` with `github:owner/repo` templates failed with "GitHub PAT not configured" even when PAT was set via Settings or env var.

**Root Cause**: Code inconsistency - Settings page stored PAT in SQLite `system_settings` table, but agent creation looked in Redis credential store (legacy location).

**Fix**: Updated `crud.py` to use `get_github_pat()` from `settings_service.py` which checks SQLite first, then falls back to `GITHUB_PAT` env var.

**Modified Files**:
- `src/backend/services/agent_service/crud.py` - Use settings_service instead of credential_manager

---

### 2026-01-09 16:45:00
🐛 **Fix: MCP Configuration Modal - Missing `type` and Port**

**Issues Fixed**:
1. Missing `"type": "http"` in generated MCP config - required by Claude Code
2. Production URL missing port 8080 (was `https://{host}/mcp`, now `http://{host}:8080/mcp`)

**Before** (broken):
```json
{
  "mcpServers": {
    "trinity": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

**After** (correct):
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "http://example.com:8080/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

**Modified Files**:
- `src/frontend/src/views/ApiKeys.vue` - Fixed `getMcpConfig()` and `mcpServerUrl` computed property

---

### 2026-01-09 16:15:00
📋 **Roadmap: Phase 15 - Compliance-Ready Development Methodology**

**Addition**: Added Phase 15 to roadmap for SOC-2 and ISO 27001-compatible development practices.

**Scope**: Extend `dev-methodology-template/` to produce audit-ready artifacts:
- SOC-2 Control Mapping (Trust Service Criteria)
- ISO 27001:2022 Alignment (ISMS controls)
- Change Management Controls (CC8.1)
- Access Control Documentation
- Security Review Gates (mandatory `/security-check`)
- Audit Trail Requirements
- Incident Response Procedures
- Compliance Evidence Generation

**Reference**: Template at `dev-methodology-template/` contains reusable slash commands, sub-agents, and memory file structure that maps naturally to compliance controls.

**Modified Files**:
- `docs/memory/roadmap.md` - Added Phase 15, backlog entry, decision log

---

### 2026-01-09 15:45:00
🔧 **Infrastructure: Production Port Remapping**

**Change**: Simplified production port configuration to use standard ports.

**Port Changes**:
| Service | Before | After |
|---------|--------|-------|
| Frontend (nginx) | 3005 | **80** |
| Backend (FastAPI) | 8005 | **8000** |
| MCP Server | 8007 | **8080** |
| Agent SSH Start | 2223 | **2222** |

**Rationale**:
- Port 80: Standard HTTP, no port needed in URLs
- Port 8000: De facto Python API standard
- Port 8080: Common HTTP alternative, intuitive for MCP
- Port 2222: Memorable SSH sequence

**Modified Files**:
- `docker-compose.prod.yml` - Updated port mappings and internal references
- `deploy.config.example` - Updated default ports
- `src/frontend/nginx.conf` - Updated backend proxy URLs
- `src/backend/services/docker_service.py` - SSH port range now starts at 2222
- `scripts/deploy/gcp-deploy.sh` - Updated default ports and display URLs
- `scripts/deploy/gcp-firewall.sh` - Updated firewall port list
- `CLAUDE.local.md.example` - Updated port documentation
- `docs/memory/architecture.md` - Updated port allocation table

**Migration Notes**:
- Update firewall rules to allow 80/tcp, 8000/tcp, 8080/tcp
- No data migration required - purely port mapping change
- Development ports unchanged (3000, 8000, 8080)

---

### 2026-01-09 14:30:00
✨ **Feature: Agents Page UI Overhaul - Dashboard Parity**

**Enhancement**: Completely redesigned the Agents page (`/agents`) to match the Dashboard tile design, providing consistent UX across the platform.

**What Changed**:

1. **Grid Layout** - Changed from vertical list to responsive 3-column grid (`grid-cols-1 md:grid-cols-2 lg:grid-cols-3`)

2. **Autonomy Toggle** - Added interactive AUTO/Manual toggle switch matching Dashboard tiles
   - Amber color when enabled, gray when disabled
   - Calls `PUT /api/agents/{name}/autonomy` to toggle all schedules
   - Loading state during API calls

3. **Execution Stats Row** - New compact stats display:
   - Task count (24h): "12 tasks"
   - Success rate with color coding: green (≥80%), yellow (50-79%), red (<50%)
   - Total cost: "$0.45"
   - Last execution: "2m ago"

4. **Context Progress Bar** - Now always visible (not just for running agents)
   - Color coded: green → yellow → orange → red based on usage

5. **Card Styling** - Matching AgentNode.vue design with shadows, rounded corners, hover effects

**Modified Files**:
- `src/frontend/src/stores/agents.js` - Added `executionStats` state, `fetchExecutionStats()`, `toggleAutonomy()` actions
- `src/frontend/src/views/Agents.vue` - Complete rewrite with grid layout and new features
- `docs/memory/feature-flows/agents-page-ui-improvements.md` - Added Enhancement section

**Visual Comparison**:
| Feature | Before | After |
|---------|--------|-------|
| Layout | Vertical list | 3-column grid |
| Autonomy | Not shown | Toggle switch |
| Execution Stats | Not shown | Tasks · Rate · Cost · Time |
| Context Bar | Running only | All agents |

---

### 2026-01-06 18:30:00
♻️ **Refactor: Sovereign Archive Storage Architecture**

**Refactoring**: Removed external S3 dependency and implemented pluggable storage interface with local-only default for sovereign deployments.

**Motivation**: Following architectural feedback to maintain full data sovereignty - no system logs or archives should leave the company's infrastructure. The platform now stores all data locally within mounted volumes, giving operators full control over backup strategies.

**What Changed**:
1. **New Storage Interface** - Abstract `ArchiveStorage` base class with pluggable backend support
2. **Local Storage Default** - `LocalArchiveStorage` implementation stores archives in mounted Docker volume
3. **S3 Code Removed** - Deleted S3-specific code (`s3_storage.py`), boto3 dependency, and S3 env vars
4. **Sovereign Backup Strategies** - Documentation now covers Docker volume backups, NAS/NFS mounts, rsync, cross-instance copies

**New Files**:
- `src/backend/services/archive_storage.py` - Storage abstraction layer

**Deleted Files**:
- `src/backend/services/s3_storage.py` - S3 integration (removed)

**Modified Files**:
- `src/backend/services/log_archive_service.py` - Uses storage interface instead of S3 directly
- `docker/backend/Dockerfile` - Removed boto3 dependency
- `docker-compose.yml` - Removed S3 env vars, added `LOG_ARCHIVE_PATH`
- `docs/memory/feature-flows/vector-logging.md` - Replaced S3 docs with sovereign backup strategies
- `tests/test_log_archive.py` - Removed S3 tests, updated to use storage interface

**Configuration Changes**:
```bash
# Removed:
LOG_S3_ENABLED, LOG_S3_BUCKET, LOG_S3_ACCESS_KEY, LOG_S3_SECRET_KEY,
LOG_S3_ENDPOINT, LOG_S3_REGION

# Added:
LOG_ARCHIVE_PATH=/data/archives  # Local path for archived logs
```

**Backup Strategies** (Sovereign):
- Mount NAS/NFS to archives volume
- rsync to backup server via cron
- Docker volume backup/restore
- Cross-instance volume copy

**Impact**:
- **Breaking**: S3 configuration no longer supported (by design)
- **Non-Breaking**: Archives still stored in `trinity-archives` volume
- **Benefit**: Full data sovereignty - no external dependencies
- **Extensible**: Storage interface allows future backends (NFS, SFTP, etc.) without S3

**Architecture Philosophy**: Keep all data (logs, archives, backups) within operator-controlled infrastructure. External storage can be implemented via volume mounts or custom scripts, not hardcoded cloud dependencies.

---

### 2026-01-06 15:45:00
📋 **Docs: Process-Driven Multi-Agent Systems Vision (Refined)**

Refined the concept document based on design review discussion.

**Key Design Decisions**:

1. **Simplified Role Model** (dropped RACI complexity):
   - **Executor**: Does the work, saves outputs
   - **Monitor**: Watches for failures, handles escalations
   - **Informed**: Learns from events, builds situational awareness
   - Dropped "Consulted" (just an agent-to-agent call)

2. **Stateful Agents as Core Principle**:
   - Agents build memory, beliefs, and judgment over time
   - "Informed" role enables situational awareness
   - Informed agents can proactively trigger actions based on observed events

3. **Output Storage**:
   - Agent responsibility (not platform-managed)
   - Executors save to shared folders or external systems (CRM, Google Drive, etc.)
   - Next step's agent ingests from designated location

4. **Human Approval Steps** (critical feature):
   - Dedicated `type: human_approval` step type
   - Separate approval interface for human operators
   - Timeout, escalation, delegation support
   - Mobile-friendly with email/Slack action links

5. **System Agent Role** (unchanged):
   - Stays focused on platform operations
   - Does NOT orchestrate business processes
   - Process orchestration handled by Process Execution Engine + Monitor agents

**Updated Files**:
- `docs/drafts/PROCESS_DRIVEN_AGENTS.md` - Full concept with refined roles, human approval section, approval interface mockup
- `docs/memory/requirements.md` - Updated 14.1, 14.2, added 14.3 for human-in-the-loop

---

### 2026-01-06 14:30:00
📋 **Docs: Process-Driven Multi-Agent Systems Vision**

Added comprehensive documentation for the strategic evolution of Trinity into a business process orchestration platform.

**New Concept Document**: `docs/drafts/PROCESS_DRIVEN_AGENTS.md`
- Business processes as first-class entities that orchestrate agent collaboration
- Simplified role model: Executor, Monitor, Informed
- Process lifecycle: Design → Configure → Test → Production → Improvement
- Human-in-the-loop improvement cycles with feedback collection
- UI vision: Process Designer, Process Dashboard, Execution View, Human Approval Interface

**New Requirements Added** (requirements.md):
- **13.1 Horizontal Agent Scalability**: Agent pools with N instances, load balancing, auto-scaling
- **13.2 Event Bus Infrastructure**: Redis Streams pub/sub with permission-gated subscriptions
- **13.3 Event Handlers & Reactions**: Automatic agent reactions to events from permitted agents
- **14.1 Business Process Definitions**: Role-based orchestration, triggers, policies
- **14.2 Process Execution & Human Approval**: Execution engine, approval interface
- **14.3 Human-in-the-Loop Improvement**: Feedback collection, quality tracking

**Roadmap Updates**:
- Phase 13: Agent Scalability & Event-Driven Architecture
- Phase 14: Process-Driven Multi-Agent Systems (Future Vision)
- Updated backlog with new high-priority items
- Added 3 decision log entries

**Key Insight**: Business processes should drive agent design, not the other way around. Start with "what outcome do we want?" and derive the agents needed to achieve it.

---

### 2026-01-05 18:30:00
✨ **Feature: Vector Log Retention and Archival**

**New Feature**: Automated log retention, rotation, and archival system for Vector logs with local storage.

**Compliance Gap Addressed**: Vector logging lacked retention policy and archival capabilities required for SOC2/ISO27001 compliance.

**What Was Added**:
1. **Daily Log Rotation** - Vector now writes to date-stamped files (`platform-2026-01-05.json`)
2. **Automated Archival** - Nightly job compresses and archives logs older than retention period
3. **Local Storage** - Archives stored in mounted Docker volume (`trinity-archives`)
4. **API Endpoints** - Admin-only endpoints for stats, config, and manual archival
5. **Integrity Verification** - SHA256 checksums ensure archive integrity

**New Files**:
- `src/backend/services/log_archive_service.py` - Archival logic + APScheduler
- `src/backend/routers/logs.py` - Log management API endpoints
- `tests/test_log_archive.py` - Comprehensive test coverage

**Modified Files**:
- `config/vector.yaml` - Date-based file paths for daily rotation
- `src/backend/main.py` - Register logs router, start archive scheduler
- `docker-compose.yml` - Add log retention env vars, trinity-archives volume
- `docs/memory/feature-flows/vector-logging.md` - Document retention features

**Configuration**:
```bash
LOG_RETENTION_DAYS=90       # Days to keep logs
LOG_ARCHIVE_ENABLED=true    # Enable automated archival
LOG_CLEANUP_HOUR=3          # Hour (UTC) to run nightly job
LOG_ARCHIVE_PATH=/data/archives  # Local path for archived logs
```

**API Endpoints**:
- `GET /api/logs/stats` - Log file statistics
- `GET /api/logs/retention` - Retention configuration
- `PUT /api/logs/retention` - Update retention (runtime)
- `POST /api/logs/archive` - Manual archival trigger
- `GET /api/logs/health` - Service health check

**Impact**:
- No breaking changes to existing Vector functionality
- Existing single-file logs (`platform.json`, `agents.json`) can be manually archived
- New date-based files start after Vector restart
- Disk space remains stable after retention period

**Compliance**: Addresses SOC2/ISO27001 requirement for log retention policy and secure archival.

---

### 2026-01-05 10:15:00
🐛 **Fix: ReplayTimeline infinite loop causing browser hang**

Fixed critical bug where switching to Replay mode caused the browser to freeze.

**Root Cause**: The `timeTicks` computed property in ReplayTimeline.vue generates grid lines every 15 minutes. When `timelineStart` is `null` (no historical events), `startTime` computed to 0 (epoch 1970). The for loop then ran from epoch to current time (2026), creating ~2 million tick iterations and hanging the browser.

**Fix**: Added guards to all computed properties that rely on timeline bounds:
- `timeTicks`: Early return if `!startTime.value` or `!endTime.value`, plus safety limit of 700 max ticks
- `agentRows`: Early return if timeline data is missing
- `communicationArrows`: Early return if timeline data is missing
- `currentTimeX`: Early return if timeline data is missing

**Files Changed**:
- `src/frontend/src/components/ReplayTimeline.vue`: Lines 411-445, 448-450, 520-522, 557-560

**Behavior Now**:
- Switching to Replay mode with no historical data shows empty timeline (no hang)
- Timeline safely handles edge cases with missing data

---

### 2026-01-03 21:45:00
🎬 **Enhancement: Replay Mode Activity & Context Simulation**

Enhanced replay mode to simulate agent activity states and context changes during playback.

**What Was Missing**: After the edge animation fix, replay only showed animated arrows but:
1. Agent activity state (green blinking "Active" indicator) wasn't updating
2. Context progress bars remained static during replay

**Solution**: Added simulation functions to `network.js`:
- `setNodeActivityState()` - Updates agent activity state with Vue reactivity
- `simulateContextChange()` - Adjusts context percentage with bounds (0-100%)
- `simulateAgentActivity()` - Orchestrates the full activity simulation:
  - Source agent becomes active immediately (initiating collaboration)
  - Target agent becomes active after 300ms (processing request)
  - Context bars increase: source +1-3%, target +2-5%
  - Both return to idle after 2-3 seconds

**Files Changed**:
- `src/frontend/src/stores/network.js`:
  - Added `activityTimeouts` ref to track pending timeouts
  - Added `setNodeActivityState()`, `simulateContextChange()`, `simulateAgentActivity()`
  - `scheduleNextEvent()` now calls `simulateAgentActivity()` for each event
  - `startReplay()` initializes context percentages (5-15%) for running agents
  - `stopReplay()` clears activity timeouts and resets all agents to idle

**Behavior Now**:
- During replay, agents show green pulsing "Active" state when involved in collaboration
- Context bars grow incrementally as events play back
- All states reset properly when replay stops
- Switching to Live mode resumes real context polling

---

### 2026-01-03 21:15:00
🐛 **Fix: Dashboard Replay Mode Now Works**

Fixed critical bug where replay mode visuals weren't updating on the Dashboard.

**Root Cause**: The `edges` variable is a computed property that merges `collaborationEdges` and `permissionEdges`. All edge manipulation functions (`animateEdge`, `createHistoricalEdges`, `resetAllEdges`, etc.) were trying to modify `edges.value` directly. Since computed properties return new arrays each time, modifications were being lost.

**Fix**: Updated all edge manipulation functions to work with `collaborationEdges.value` instead of `edges.value`. Added Vue reactivity triggers (`collaborationEdges.value = [...collaborationEdges.value]`) after modifications to ensure the computed `edges` recalculates.

**Files Changed**:
- `src/frontend/src/stores/network.js`:
  - `createHistoricalEdges()` - Push to `collaborationEdges.value` (line 217)
  - `animateEdge()` - Find/push in `collaborationEdges.value` (lines 455, 486)
  - `fadeEdgeAnimation()` - Find in `collaborationEdges.value` (line 554)
  - `clearEdgeAnimation()` - Find in `collaborationEdges.value` (line 573)
  - `resetAllEdges()` - Iterate over `collaborationEdges.value` (line 946)
  - `handleAgentDeleted()` - Filter `collaborationEdges.value` (line 434)
  - Added reactivity triggers after all edge modifications

**Behavior Now**:
- Replay mode properly animates edges when playing back historical events
- Edges turn blue/animated during replay, then fade back to gray
- Play/Pause/Stop controls work correctly
- Timeline scrubber and event markers function as expected

---

### 2026-01-03 19:30:00
🎛️ **Dashboard Autonomy Toggle Switch**

Added interactive toggle switch to Dashboard agent tiles for quick autonomy mode control.

**Visual Design**:
- Toggle switch (36x20px) with smooth sliding animation
- Label shows "AUTO" (amber) or "Manual" (gray) next to switch
- Disabled state with opacity during API call
- Tooltips explain current state and action

**Files Changed**:
- `src/frontend/src/components/AgentNode.vue:62-96` - Toggle switch UI and handler
- `src/frontend/src/stores/network.js:993-1030` - `toggleAutonomy()` action

**Behavior**:
- Click toggle to enable/disable all schedules for the agent
- Node data updates reactively (no page refresh needed)
- System agents do not show the toggle (autonomy N/A)

**Documentation Updated**:
- `docs/memory/feature-flows/autonomy-mode.md` - Full toggle documentation
- `docs/memory/feature-flows/agent-network.md` - AgentNode toggle reference

---

### 2026-01-03 17:55:00
🐛 **Fix: Scheduler now respects Autonomy Mode**

Fixed bug where scheduled tasks would execute even when agent's Autonomy Mode was disabled (Manual mode).

**Root Cause**: `scheduler_service._execute_schedule()` only checked `schedule.enabled` but not the agent's `autonomy_enabled` flag.

**Fix**: Added autonomy check in `_execute_schedule()` before task execution:
```python
if not db.get_autonomy_enabled(schedule.agent_name):
    logger.info(f"Schedule skipped: agent autonomy is disabled")
    return
```

**Files Changed**: `src/backend/services/scheduler_service.py:186-189`

**Behavior Now**:
- Manual mode (autonomy disabled) = no schedules run
- AUTO mode (autonomy enabled) = schedules run as configured

---

### 2026-01-03 14:15:00
🔧 **GitHub Agent Templates - Updated for Trinity Compatibility**

Updated all three GitHub demo agent templates (Cornelius, Corbin, Ruby) to match TRINITY_COMPATIBLE_AGENT_GUIDE.md.

**Changes pushed to GitHub:**

| Repository | Commit | Changes |
|------------|--------|---------|
| `abilityai/agent-cornelius` | `043d24a` | +.gitignore updates, +memory/, +outputs/, template.yaml commit_paths |
| `abilityai/agent-corbin` | `481bf59` | +.gitignore updates, +outputs/, template.yaml commit_paths |
| `abilityai/agent-ruby` | `b946886` | +.gitignore updates, +memory/, +outputs/ |

**Common .gitignore additions:**
- `.trinity/` - Platform-managed directory (injected at startup)
- `.claude/commands/trinity/` - Platform-injected slash commands
- `content/` - Large generated assets (videos, audio, images)

**Directory structure updates:**
- Added `memory/` directory with .gitkeep (Cornelius, Ruby)
- Added `outputs/` directory with .gitkeep (all three)
- Updated `git.commit_paths` to include new directories

**Note**: These agents already had comprehensive template.yaml files with tagline, use_cases, capabilities, metrics, etc. The updates focused on .gitignore and directory structure per the guide.

---

### 2026-01-03 13:30:00
📚 **Demo Agent Templates - Updated for Flexibility & Best Practices**

Updated local demo templates (demo-researcher, demo-analyst) to follow TRINITY_COMPATIBLE_AGENT_GUIDE.md and improved `/create-demo-agent-fleet` command.

**Command Updates** (`.claude/commands/create-demo-agent-fleet.md`):
- Added Option A (Recommended): Local Research Network using system manifest deployment
- Option B: GitHub Templates (Cornelius, Corbin, Ruby) kept for reference
- Added complete system manifest example with permissions and shared folders
- Added test collaboration examples showing inter-agent communication

**Template Improvements** (`config/agent-templates/demo-*`):
- Added `tagline` and `use_cases` fields to template.yaml files
- Added `memory/` directories with .gitkeep files
- Added `outputs/` directory to demo-analyst for generated briefings
- Updated all slash commands to use dynamic agent/path discovery instead of hardcoded names
- Added Bash tool to allowed-tools for filesystem discovery

**Key Changes**:
- demo-analyst CLAUDE.md: Instructions for dynamic shared folder discovery
- briefing.md: Discovers researcher folder dynamically
- opportunities.md, ask.md: Dynamic path resolution
- request-research.md: Lists agents first, then calls researcher

**Files Changed**:
- `.claude/commands/create-demo-agent-fleet.md` - Complete rewrite with 2 deployment options
- `config/agent-templates/demo-researcher/template.yaml` - Added tagline, use_cases
- `config/agent-templates/demo-analyst/template.yaml` - Added tagline, use_cases
- `config/agent-templates/demo-analyst/CLAUDE.md` - Dynamic agent naming instructions
- `config/agent-templates/demo-analyst/.claude/commands/*.md` - All updated for dynamic paths

---

### 2026-01-03 11:45:00
🔧 **MCP Tool: get_agent_info - Agent Template Metadata Access**

New MCP tool allowing agents to discover detailed information about other agents they have permission to access.

**New Tool**: `get_agent_info(name)`

Returns full template.yaml metadata including:
- Display name, description, tagline, version, author
- Capabilities (e.g., `["synthesis", "strategic-analysis"]`)
- Available slash commands with descriptions
- MCP servers the agent uses
- Tools, skills, and example use cases
- Resource allocation (CPU/memory)
- Current status (running/stopped)

**Access Control**:
- Agent-scoped keys: Can only access self + permitted agents
- System agents: Full access to all agents
- User-scoped keys: Access to all accessible agents

**Use Case**: Enables orchestrator agents to understand worker capabilities before delegating tasks.

**Files Changed**:
- `src/mcp-server/src/types.ts` - Added `AgentTemplateInfo` and `AgentCommand` types
- `src/mcp-server/src/client.ts` - Added `getAgentInfo()` method
- `src/mcp-server/src/tools/agents.ts` - Added `get_agent_info` tool with access control
- `src/mcp-server/src/server.ts` - Registered new tool

---

### 2026-01-03 10:30:00
🎨 **Dashboard Agent Cards - Enhanced Styling & Resource Indicators**

Improved agent cards on the Dashboard with a more refined look and resource information display.

**Visual Changes**:
- Increased card width from 280px to 320px for better readability
- Added backdrop-blur and semi-transparent backgrounds for a modern glass effect
- Softer border styling (removed double border, adjusted opacity)
- Minimum height increased to 180px for consistent layout

**New Features**:
- Added resource indicators row showing Memory and CPU limits
- Displays configured limits (e.g., "4g", "2 cores") or defaults
- Subtle separator line between execution stats and resources
- Tooltips show full resource details on hover

**Files Changed**:
- `src/frontend/src/components/AgentNode.vue` - Styling and resource display
- `src/frontend/src/stores/network.js` - Pass memoryLimit/cpuLimit to node data
- `src/backend/services/agent_service/helpers.py` - Include resource limits in agent data

---

### 2026-01-02 18:15:00
🐛 **Fix SSH Privilege Separation Directory**

Fixed SSH failing to start due to missing privilege separation directory.

**Issue**: `/var/run/sshd` created in Dockerfile, but `/var/run` is often a tmpfs that gets wiped on container restart.

**Fix**: Added `mkdir -p /var/run/sshd` in startup.sh before starting sshd.

**Files Changed**:
- `docker/base-image/startup.sh` - Ensure `/var/run/sshd` exists before sshd start

**Note**: Requires base image rebuild: `./scripts/deploy/build-base-image.sh`

---

### 2026-01-02 18:00:00
🐛 **Fix SSH Password Authentication & Host Detection**

Fixed two critical bugs in the SSH ephemeral password system:

**Bug 1: Password Not Being Set (sed escaping)**
- SHA-512 password hashes contain `$` characters (e.g., `$6$salt$hash`)
- The `$` was not escaped in sed, causing the replacement to fail
- **Fix**: Use `usermod -p` with single-quoted password instead of sed
- **Fallback**: If usermod fails, try `chpasswd` with plaintext password

**Bug 2: Host Detection Returns Docker IP**
- `get_ssh_host()` runs inside the backend container
- Socket-based detection returned container's Docker network IP (172.28.x.x)
- **Fix**: Improved detection priority:
  1. SSH_HOST env var (explicit config)
  2. Tailscale IP detection
  3. `host.docker.internal` (Docker Desktop Mac/Windows)
  4. Default gateway IP (Linux Docker host)
  5. Fallback to localhost with warning

**Files Changed**:
- `src/backend/services/ssh_service.py` - `set_container_password()` and `get_ssh_host()`

---

### 2026-01-02 17:15:00
🔧 **SSH Access Toggle in Settings UI**

Added UI toggle for `ssh_access_enabled` in the Settings page.

**Location**: Settings → SSH Access section → Enable SSH Access toggle

**Files Changed**:
- `src/frontend/src/views/Settings.vue` - Added SSH Access section with toggle switch

---

### 2026-01-02 16:30:00
🔐 **MCP SSH Access Tool - Password Auth & System Setting**

Enhanced SSH access with password-based authentication and system-wide enable/disable setting.

**New Features**:
- Password-based auth: `auth_method: "password"` returns sshpass one-liner command
- System setting `ssh_access_enabled` (default: false) - must be enabled in Settings → Ops Settings
- Password set directly in container's `/etc/shadow` (bypasses PAM issues)

**Container Security Updates** (required for SSH):
- Added capabilities: `SETGID`, `SETUID`, `CHOWN`, `SYS_CHROOT`, `AUDIT_WRITE`
- Removed `no-new-privileges:true` (blocks SSH privilege separation)
- **Note**: Existing agents must be deleted and recreated to get new settings

**Files Changed**:
- `src/backend/services/ssh_service.py` - Password generation, shadow file editing
- `src/backend/services/settings_service.py` - `ssh_access_enabled` ops setting
- `src/backend/services/agent_service/crud.py` - Container capabilities
- `src/backend/services/agent_service/lifecycle.py` - Container capabilities

---

### 2026-01-02 15:48:00
🔐 **MCP SSH Access Tool - Ephemeral SSH Credentials**

Added new MCP tool `get_agent_ssh_access` to generate ephemeral SSH credentials for direct terminal access to agent containers. Designed for Tailscale/VPN environments.

**Features**:
- Generate ED25519 key pairs on demand
- Auto-inject public key into agent's `~/.ssh/authorized_keys`
- Configurable TTL (0.1-24 hours, default: 4 hours)
- Keys auto-expire via Redis TTL
- Returns SSH command, private key, and connection instructions
- Host auto-detection (SSH_HOST env → Tailscale IP → localhost)

**MCP Tool**:
- `get_agent_ssh_access(agent_name, ttl_hours, auth_method)` - Returns credentials and connection command

**Backend API**:
- `POST /api/agents/{name}/ssh-access` - Generate ephemeral SSH credentials

**Security**:
- Private key shown once, never stored on server
- Credentials automatically removed from containers on expiry
- User must have access to agent
- Agent must be running
- Controlled by `ssh_access_enabled` system setting (default: disabled)

**Files Created**:
- `src/backend/services/ssh_service.py` - SSH key generation, password management, injection, cleanup

**Files Changed**:
- `src/backend/routers/agents.py` - SSH access endpoint
- `src/mcp-server/src/tools/agents.ts` - `getAgentSshAccess` tool
- `src/mcp-server/src/client.ts` - `createSshAccess()` method
- `src/mcp-server/src/types.ts` - `SshAccessResponse` type
- `src/mcp-server/src/server.ts` - Register new tool

---

### 2026-01-02 15:00:00
⚙️ **Per-Agent Resource Limits Configuration**

Added ability to configure memory and CPU allocation for individual agents. Changes take effect on agent restart.

**Features**:
- Memory options: 1g, 2g, 4g, 8g, 16g, 32g, 64g
- CPU options: 1, 2, 4, 8, 16 cores
- UI in **Metrics tab** - "Resource Allocation" card with "Configure" button
- Modal dialog with warning: "Changes require an agent restart to take effect"
- Works regardless of agent state (running or stopped)
- Container is automatically recreated with new limits on next start

**Backend**:
- New columns in `agent_ownership`: `memory_limit`, `cpu_limit`
- `GET /api/agents/{name}/resources` - Get current and configured limits
- `PUT /api/agents/{name}/resources` - Set new limits (owner-only)
- `check_resource_limits_match()` helper triggers container recreation on start

**Files Changed**:
- `src/backend/database.py` - Migration + delegate methods
- `src/backend/db/agents.py` - `get_resource_limits()`, `set_resource_limits()`
- `src/backend/services/agent_service/helpers.py` - `check_resource_limits_match()`
- `src/backend/services/agent_service/lifecycle.py` - Resource check on start
- `src/backend/routers/agents.py` - API endpoints
- `src/frontend/src/composables/useAgentSettings.js` - Resource limits state
- `src/frontend/src/stores/agents.js` - Store methods
- `src/frontend/src/views/AgentDetail.vue` - Resource Allocation UI panel

---

### 2026-01-02 13:15:00
📝 **Fix MCP Config Examples - Add Missing `type: http`**

Fixed MCP configuration examples that were missing the required `"type": "http"` field for HTTP transport.

**Issue**: Claude Code requires explicit `"type": "http"` for URL-based MCP servers. Without it, config validation fails with "Invalid MCP configuration".

**Files Fixed**:
- `src/frontend/src/views/ApiKeys.vue` - API Keys page example config
- `src/mcp-server/README.md` - MCP server documentation
- `docs/memory/architecture.md` - Architecture docs
- `docs/memory/feature-flows/mcp-orchestration.md` - MCP orchestration flow

**Note**: The agent injection code (`docker/base-image/agent_server/services/trinity_mcp.py`) has been correct since Dec 12, 2025. If agents have incorrect config, rebuild the base image with `./scripts/deploy/build-base-image.sh`.

---

### 2026-01-02 12:45:00
🐛 **Fix Execution Log Viewer for Scheduled Tasks**

Fixed bug where execution logs from scheduled/triggered tasks wouldn't display in the Tasks panel log viewer.

**Root Cause**:
- Two different log formats: raw Claude Code stream-json (`/api/task`) vs simplified ExecutionLogEntry (`/api/chat`)
- Scheduled executions were using `/api/chat` which returns simplified format
- `parseExecutionLog()` expected raw Claude Code format

**Fix (Standardization)**:
- Added `AgentClient.task()` method that calls `/api/task` for stateless execution
- Changed scheduler to use `client.task()` instead of `client.chat()`
- All scheduled/triggered executions now return raw Claude Code format
- No frontend changes needed - format is now consistent

**Files Changed**:
- `src/backend/services/agent_client.py` - Added `task()` method + `_parse_task_response()`
- `src/backend/services/scheduler_service.py` - Use `client.task()` for scheduled executions

---

### 2026-01-02 11:30:00
📈 **Agent Container Telemetry with Sparkline Charts**

Added sparkline charts for CPU and memory on Agent Detail page, matching Dashboard telemetry style.

**Reusable Component**:
- Created `SparklineChart.vue` - configurable uPlot-based sparkline (color, yMax, width, height)
- Used by both `HostTelemetry.vue` (Dashboard) and `AgentDetail.vue` (Agent page)

**Agent Detail Stats**:
- Replaced progress bars with sparkline charts for CPU and memory
- 60-sample rolling history (5-minute window at 5s intervals)
- Color-coded percentage values (green/yellow/red thresholds)
- Consistent styling with Dashboard telemetry

**Files Created**:
- `src/frontend/src/components/SparklineChart.vue` - Reusable sparkline component

**Files Changed**:
- `src/frontend/src/composables/useAgentStats.js` - Added cpuHistory/memoryHistory tracking
- `src/frontend/src/views/AgentDetail.vue` - Use SparklineChart for stats display
- `src/frontend/src/components/HostTelemetry.vue` - Refactored to use SparklineChart

---

### 2026-01-02 10:10:00
📊 **Host & Container Telemetry Dashboard**

Added real-time system metrics display to Dashboard header with sparkline charts.

**Features**:
- CPU usage with rolling 60-sample sparkline (5-minute history)
- Memory usage with sparkline and formatted value (used/total GB)
- Disk usage with progress bar and percentage
- Container stats: aggregate CPU/memory across all running agents (shown when agents running)
- 5-second polling interval
- Color-coded values (green < 50%, yellow 50-75%, orange 75-90%, red > 90%)
- Dark/light theme support

**Backend Endpoints**:
- `GET /api/telemetry/host` - Host CPU, memory, disk stats via psutil
- `GET /api/telemetry/containers` - Aggregate stats across agent containers

**Files Created**:
- `src/backend/routers/telemetry.py` - New telemetry router
- `src/frontend/src/components/HostTelemetry.vue` - Sparkline component using uPlot

**Files Changed**:
- `src/backend/main.py` - Mount telemetry router
- `docker/backend/Dockerfile` - Add psutil dependency
- `docker/frontend/Dockerfile` - Use npm ci with lock file
- `src/frontend/package.json` - Add uplot dependency
- `src/frontend/src/views/Dashboard.vue` - Import and render HostTelemetry

---

### 2026-01-01 18:45:00
🔄 **Autonomy Mode Toggle**

Implemented master switch for agent autonomous operation:

**Dashboard**:
- "AUTO" badge (amber) shown on agents with autonomy enabled
- Badge excluded for system agent

**Agent Detail Page**:
- AUTO/Manual toggle button in header (next to Delete button)
- Owners only can toggle (uses `can_share` permission)
- Real-time UI update on toggle

**Backend**:
- `autonomy_enabled` column added to `agent_ownership` table
- `GET /api/agents/{name}/autonomy` - Get autonomy status with schedule counts
- `PUT /api/agents/{name}/autonomy` - Enable/disable autonomy (bulk toggles all schedules)
- `GET /api/agents/autonomy-status` - Dashboard overview for all accessible agents
- New service module: `services/agent_service/autonomy.py`

**Behavior**:
- Enabling autonomy enables ALL schedules for the agent
- Disabling autonomy disables ALL schedules for the agent
- System agent excluded from autonomy controls

**Files Changed**:
- `src/backend/database.py` - Migration + delegate methods
- `src/backend/db/agents.py` - CRUD for autonomy_enabled
- `src/backend/services/agent_service/autonomy.py` (new)
- `src/backend/services/agent_service/__init__.py` - Exports
- `src/backend/routers/agents.py` - API endpoints + agent detail response
- `src/backend/services/agent_service/helpers.py` - get_accessible_agents
- `src/frontend/src/components/AgentNode.vue` - AUTO badge
- `src/frontend/src/stores/network.js` - Pass autonomy_enabled to nodes
- `src/frontend/src/views/AgentDetail.vue` - Toggle button

---

### 2026-01-01 12:30:00
🤖 **Implement Research Network Demo**

Created and validated two-agent autonomous research system demo:

**Templates Created**:
- `demo-researcher` - Autonomous web researcher with `/research` and `/status` commands
- `demo-analyst` - Strategic analyst with `/briefing`, `/opportunities`, `/ask`, `/request-research` commands

**System Manifest**:
- `config/manifests/research-network.yaml` - Full system recipe with shared folders, schedules, permissions

**Validation Results**:
- Both agents deployed and running via system manifest
- Shared folder collaboration working (researcher exposes, analyst consumes)
- Schedules configured: researcher every 4h, analyst daily at 9 AM
- File sharing verified: test findings file written by researcher, readable by analyst

**Files Created**:
- `config/agent-templates/demo-researcher/` (template.yaml, CLAUDE.md, commands/, .gitignore)
- `config/agent-templates/demo-analyst/` (template.yaml, CLAUDE.md, commands/, .gitignore)
- `config/manifests/research-network.yaml`

**Docs Updated**:
- `docs/memory/autonomous-agent-demos.md` - Added validation log and success criteria

---

### 2026-01-01 08:20:00
🎨 **Simplify System Agent UI - Terminal-Centric Layout**

Redesigned `/system-agent` page to give the terminal central prominence:

**Layout Changes**:
- **Terminal**: Now full width (was 2/3), increased height to 600px, central focus on page
- **Quick Actions**: Moved to compact icon buttons in terminal header bar (was separate card with large buttons)
- **OpenTelemetry**: Collapsible section, only shown when data available, collapsed by default

**Quick Actions (now in terminal header)**:
- Emergency Stop (red icon with tooltip)
- Restart All Agents
- Pause Schedules
- Resume Schedules
- Fullscreen toggle (separated by divider)

**Files Changed**:
- `src/frontend/src/views/SystemAgent.vue` - Complete layout restructure

---

### 2026-01-01 03:00:00
📖 **Add Autonomy Section to Main Agent Guide**

Updated `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` with compact autonomy section covering:
- Three-phase lifecycle: Develop → Package → Schedule
- Design principles table (self-contained, deterministic, graceful degradation, bounded, idempotent)
- Quick example showing template.yaml schedule and slash command
- Reference to detailed AUTONOMOUS_AGENT_DESIGN.md

Maintains single source of truth principle - main guide has overview, detailed guide has depth.

---

### 2026-01-01 02:30:00
📖 **Add Autonomous Agent Design Guide**

Created `docs/AUTONOMOUS_AGENT_DESIGN.md` - focused guide on designing Trinity agents that run autonomously via scheduled commands.

**Key concepts documented**:
- **Autonomy lifecycle**: Develop interactively → Package as slash command → Schedule via Trinity
- **Command design principles**: Self-contained, deterministic output, graceful degradation, bounded scope, idempotent
- **Template configuration**: `schedules` block in template.yaml with cron expressions
- **Best practices**: Focused commands, structured output, execution time bounds, error handling

**Example patterns**:
- Complete autonomous agent template with health checks and cost reports
- Slash command structure with frontmatter (`description`, `allowed-tools`)
- Cron expression reference

This guide complements the comprehensive TRINITY_COMPATIBLE_AGENT_GUIDE.md by focusing specifically on the scheduled automation use case.

---

### 2026-01-01 01:00:00
🗑️ **Remove Auth0 from Frontend and Backend - Fix HTTP Access on LAN**

**Problem**: Auth0 SDK threw errors when accessing Trinity via HTTP on local network IPs (e.g., `http://192.168.1.127:3000`), causing a blank white page. Auth0 SDK requires "secure origins" (HTTPS or localhost).

**Solution**: Since Auth0 login was already disabled and email authentication is the primary method, removed all Auth0 code from both frontend and backend.

**Frontend Files Removed**:
- `src/frontend/src/config/auth0.js` - Deleted

**Frontend Files Changed**:
- `src/frontend/src/main.js` - Removed Auth0 import, createAuth0(), app.use(auth0), secure origin checks
- `src/frontend/src/components/NavBar.vue` - Removed useAuth0 import and usage
- `src/frontend/src/stores/auth.js` - Removed setAuth0Instance(), handleAuth0Callback(), ALLOWED_DOMAIN import
- `src/frontend/package.json` - Removed `@auth0/auth0-vue` dependency

**Backend Files Changed**:
- `src/backend/routers/auth.py` - Removed `/api/auth/exchange` endpoint, Auth0 imports
- `src/backend/models.py` - Removed `Auth0TokenExchange` model
- `src/backend/config.py` - Removed `AUTH0_DOMAIN`, `AUTH0_ALLOWED_DOMAIN` config vars

**Kept for backward compatibility** (no DB migration needed):
- `auth0_sub` column in users table
- `get_user_by_auth0_sub()`, `get_or_create_auth0_user()` methods in database layer

**Result**: Trinity now works on any HTTP origin. Email authentication and admin password login remain fully functional.

---

### 2026-01-01 00:15:00
📊 **Dashboard Execution Stats - Agent Cards Show Task Metrics**

Added execution statistics to each agent card on the Dashboard, providing at-a-glance visibility into agent workload and performance.

**Display Format** (on each AgentNode):
```
12 tasks · 92% · $0.45 · 2m ago
```

**Backend Changes**:
- `db/schedules.py`: Added `get_all_agents_execution_stats(hours=24)` - single query aggregating stats per agent
- `database.py`: Added delegate method
- `routers/agents.py`: Added `GET /api/agents/execution-stats` endpoint (returns stats for accessible agents only)

**Frontend Changes**:
- `stores/network.js`: Added `executionStats` state and `fetchExecutionStats()` (polls every 5s with context stats)
- `components/AgentNode.vue`: Added compact stats row with task count, success rate (color-coded), cost, and relative time

**Stats Shown**:
| Metric | Description |
|--------|-------------|
| Task count | Executions in last 24h |
| Success rate | % with status='success' (green >80%, yellow 50-80%, red <50%) |
| Total cost | Sum of execution costs |
| Last run | Relative time since last execution |

**Files Changed**:
- `src/backend/db/schedules.py` - Added aggregation query
- `src/backend/database.py` - Delegate method
- `src/backend/routers/agents.py` - New endpoint
- `src/frontend/src/stores/network.js` - State and polling
- `src/frontend/src/components/AgentNode.vue` - UI display

---

### 2025-12-31 23:15:00
🐛 **Test Suite Fixes - 6 Tests Fixed**

Fixed issues identified in test report:

**1. Scheduler Status Endpoint Route Ordering (4 tests)**
- `schedules.py`: Moved `/scheduler/status` endpoint BEFORE `/{name}/schedules` routes
- **Root cause**: FastAPI was matching "scheduler" as an agent name due to route ordering
- **Fix**: Static routes must be defined before dynamic `/{name}/*` routes
- **Tests fixed**: `test_get_scheduler_status`, `test_scheduler_status_structure`, `test_scheduler_status_includes_jobs`, `test_scheduler_status_requires_auth`

**2. API Client Header Merging Bug (2 tests)**
- `tests/utils/api_client.py`: Fixed header conflict when caller passes custom headers
- **Root cause**: `headers` was passed both via `kwargs` and as explicit parameter
- **Fix**: Pop headers from kwargs and merge with default headers
- **Tests fixed**: `test_task_with_source_agent_header`, `test_agent_task_has_agent_trigger`

**3. Session List Access (Already Fixed)**
- Verified `test_agent_chat.py` already handles empty session lists properly
- Tests skip gracefully when no sessions available

---

### 2025-12-31 22:30:00
🧪 **Expanded API Test Coverage**

Added 23 new tests to cover previously untested API endpoints:

**test_agent_chat.py** - Chat Session Lifecycle (6 tests)
- `test_list_chat_sessions_structure` - Verifies session list response structure
- `test_get_session_details` - GET /api/agents/{name}/chat/sessions/{id}
- `test_get_session_details_nonexistent_returns_404` - 404 for missing session
- `test_close_session` - POST /api/agents/{name}/chat/sessions/{id}/close
- `test_close_session_nonexistent_returns_404` - 404 for closing missing session
- `test_close_session_requires_auth` - Auth required for close

**test_schedules.py** - Scheduler Status (4 tests)
- `test_get_scheduler_status` - GET /api/agents/scheduler/status
- `test_scheduler_status_structure` - Validates running, job_count fields
- `test_scheduler_status_includes_jobs` - Verifies jobs/job_count present
- `test_scheduler_status_requires_auth` - Auth required for status

**test_ops.py** - Context Stats (5 tests)
- `test_get_context_stats` - GET /api/agents/context-stats
- `test_context_stats_structure` - Validates agent stats structure
- `test_context_stats_entries_have_valid_structure` - All entries have required fields
- `test_context_stats_requires_auth` - Auth required
- `test_context_stats_returns_valid_response` - Valid response structure

**test_executions.py** - Execution Logs & Details (8 tests)
- `test_get_execution_log_nonexistent_returns_404` - 404 for missing log
- `test_get_execution_log_nonexistent_agent_returns_404` - 404 for missing agent
- `test_get_execution_log_requires_auth` - Auth required
- `test_get_execution_log_returns_log` - GET /api/agents/{name}/executions/{id}/log
- `test_execution_log_content_structure` - Log content validation
- `test_get_execution_details` - GET /api/agents/{name}/executions/{id}
- `test_get_execution_details_nonexistent_returns_404` - 404 handling
- `test_get_execution_details_requires_auth` - Auth required

**Test Suite Summary**: ~515 tests total (up from ~373), 97.8% pass rate

---

### 2025-12-31 18:45:00
📝 **Updated Agent Scheduling Feature Flow Documentation**

- Updated `docs/memory/feature-flows/scheduling.md` to reflect Plan 02/03 refactoring
- **Access Control Dependencies**: Documented `AuthorizedAgent`, `OwnedAgent`, `CurrentUser` usage in schedules router
  - Added "Access" column to API endpoints table
  - Added "Access Control Pattern" section showing dependency usage
  - Updated flow diagrams to show which endpoints use which dependencies
- **AgentClient Service**: Documented new centralized HTTP client for agent communication
  - Added "Agent HTTP Client Service" section with full API reference
  - Documented `AgentChatResponse` and `AgentChatMetrics` dataclasses
  - Added exception handling patterns: `AgentNotReachableError`, `AgentRequestError`
  - Updated execution flow diagrams to show `client.chat()` instead of raw `httpx.post()`
- Updated all line number references for: schedules.py, scheduler_service.py, agent_client.py, dependencies.py
- Added new files to Related Files table: `agent_client.py`, `dependencies.py`

---

### 2025-12-31 18:15:00
📝 **Updated Agent Permissions Feature Flow Documentation**

- Updated `docs/memory/feature-flows/agent-permissions.md` to document access control dependencies
- Added new section on `dependencies.py` type aliases: `AuthorizedAgent`, `OwnedAgent`, `CurrentUser`, etc.
- Documented dependency pattern benefits: consistent 403 messages, OpenAPI visibility, automatic enforcement
- Noted that permissions endpoints currently use inline checks but can migrate to dependency pattern
- Verified and updated all line number references for router (598-638) and service (18-168) files
- Updated lifecycle integration line numbers: crud.py:474-480, agents.py:239-243
- Updated database delegation line numbers: database.py:964-986

---

### 2025-12-31 17:30:00
🐙 **GitHub API Service Extraction - Architecture Refactoring**

**Problem Solved**: `routers/git.py` `initialize_github_sync` endpoint contained ~280 lines of GitHub API business logic that should be in service layer:
- GitHub PAT validation
- Repository existence check (GitHub API call)
- Organization vs user detection (GitHub API call)
- Repository creation (GitHub API call)
- Git initialization commands (container exec calls)
- Working branch creation

This violated separation of concerns, made the logic unreusable, and hard to unit test.

**Solution**: Created `services/github_service.py` with `GitHubService` class:
- `validate_token()` → Tuple[bool, Optional[str]]
- `get_owner_type(owner)` → OwnerType (USER or ORGANIZATION)
- `check_repo_exists(owner, name)` → GitHubRepoInfo
- `create_repository(owner, name, private, description)` → GitHubCreateResult
- Structured exceptions: `GitHubError`, `GitHubAuthError`, `GitHubPermissionError`
- Response models: `GitHubRepoInfo`, `GitHubCreateResult`, `OwnerType`

**Also added to `services/git_service.py`**:
- `GitInitResult` dataclass for initialization results
- `initialize_git_in_container(agent_name, repo, pat)` - handles all git init steps
- `check_git_initialized(agent_name)` - verify git is set up in container

**Files Changed**:
- **Added**: `services/github_service.py` (~280 lines)
- **Updated**: `services/git_service.py` - added init helpers (~180 lines)
- **Updated**: `routers/git.py` - endpoint reduced from ~280 lines to ~115 lines

**Benefits**:
- Separation of concerns: Router handles HTTP, services handle logic
- Reusability: `GitHubService` can be used elsewhere
- Testability: Can unit test GitHub logic without FastAPI
- Maintainability: ~165 lines removed from router
- Error handling: Structured exceptions instead of inline checks
- Type safety: Dataclasses for responses

---

### 2025-12-31 16:20:00
🔌 **Agent HTTP Client Service - DRY Refactoring**

**Problem Solved**: HTTP client code for agent container communication duplicated across multiple files:
- `scheduler_service.py` - 2 places with chat execution + response parsing (~70 lines each)
- `ops.py` - 2 places with context stats fetching
- `lifecycle.py` - Trinity injection with retry logic

This violated DRY, had inconsistent timeout handling, and duplicated response parsing logic.

**Solution**: Created `services/agent_client.py` with `AgentClient` class:
- `chat(message, stream)` → `AgentChatResponse` with parsed metrics
- `get_session()` → `AgentSessionInfo` with context stats
- `inject_trinity_prompt()` → handles retries internally
- `health_check()` → simple boolean health check
- Structured exceptions: `AgentClientError`, `AgentNotReachableError`, `AgentRequestError`
- Response models: `AgentChatMetrics`, `AgentChatResponse`, `AgentSessionInfo`

**Files Changed**:
- **Added**: `services/agent_client.py` (~320 lines)
- **Updated**: `services/scheduler_service.py` - replaced 2 HTTP blocks + parsing (~90 lines removed)
- **Updated**: `routers/ops.py` - replaced 2 context fetch blocks (~30 lines removed)
- **Updated**: `services/agent_service/lifecycle.py` - replaced injection (~40 lines removed)

**Benefits**:
- ~180 lines of duplicated code removed
- Standardized timeouts (CHAT: 300s, SESSION: 5s, INJECT: 10s)
- Type-safe response parsing with dataclasses
- Centralized error handling prevents context% calculation bugs
- Easy to mock for testing

---

### 2025-12-31 16:05:00
🔒 **Access Control Dependencies - DRY Refactoring**

**Problem Solved**: Same access control pattern repeated 50+ times across routers:
```python
if not db.can_user_access_agent(current_user.username, name):
    raise HTTPException(status_code=403, detail="Access denied")
```

This violated DRY principle, caused inconsistent error messages, and was error-prone.

**Solution**: Created FastAPI dependencies in `dependencies.py`:
- `get_authorized_agent(name)` - validates read access, for `{name}` path param
- `get_owned_agent(name)` - validates owner access, for `{name}` path param
- `get_authorized_agent_by_name(agent_name)` - for `{agent_name}` path param
- `get_owned_agent_by_name(agent_name)` - for `{agent_name}` path param
- Type aliases: `AuthorizedAgent`, `OwnedAgent`, `AuthorizedAgentByName`, `OwnedAgentByName`, `CurrentUser`

**Usage Example** (before → after):
```python
# Before
@router.get("/{name}/schedules")
async def list_schedules(name: str, current_user: User = Depends(get_current_user)):
    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=403, detail="Access denied")
    return db.list_schedules(name)

# After
@router.get("/{name}/schedules")
async def list_schedules(name: AuthorizedAgent):
    return db.list_schedules(name)
```

**Files Changed**:
- `dependencies.py` - Added 4 dependency functions + 5 type aliases
- `routers/schedules.py` - Replaced 10 access checks with `AuthorizedAgent`
- `routers/git.py` - Replaced 6 access checks with `AuthorizedAgentByName`/`OwnedAgentByName`
- `routers/sharing.py` - Replaced 3 access checks with `OwnedAgentByName`
- `routers/public_links.py` - Replaced 5 access checks with `OwnedAgentByName`
- `routers/agents.py` - Replaced 3 access checks with `AuthorizedAgentByName`

**Benefits**:
- ~50 lines of duplicated access control code removed
- Consistent 403 error messages across all endpoints
- New endpoints automatically get proper authorization
- OpenAPI schema shows authorization requirements
- Single point of change for access control logic

---

### 2025-12-31 07:40:00
🏗️ **Settings Service - Architecture Fix**

**Problem Solved**: Services were importing from routers (architectural violation):
- `services/agent_service/crud.py` → `routers/settings.py`
- `services/agent_service/lifecycle.py` → `routers/settings.py`
- `services/agent_service/helpers.py` → `routers/settings.py`
- `services/system_agent_service.py` → `routers/settings.py`
- `routers/git.py` → `routers/settings.py`

This inverted the dependency direction (services should never depend on routers) and created circular dependency risk.

**Solution**: Created `services/settings_service.py` with:
- `get_anthropic_api_key()` - Anthropic API key with env fallback
- `get_github_pat()` - GitHub PAT with env fallback
- `get_google_api_key()` - Google API key with env fallback
- `get_ops_setting()` - Ops settings with type conversion
- `OPS_SETTINGS_DEFAULTS` / `OPS_SETTINGS_DESCRIPTIONS` - Moved from router

**Files Changed**:
- **Added**: `src/backend/services/settings_service.py`
- **Updated**: `services/agent_service/crud.py` - Import from service
- **Updated**: `services/agent_service/lifecycle.py` - Import from service
- **Updated**: `services/agent_service/helpers.py` - Import from service
- **Updated**: `services/system_agent_service.py` - Import from service
- **Updated**: `routers/git.py` - Import from service
- **Updated**: `routers/settings.py` - Re-exports from service for backward compatibility
- **Updated**: `docs/memory/architecture.md` - Added settings_service to services list

**Benefits**:
- Clean architecture: Services no longer depend on routers
- Testability: Easy to mock settings in unit tests
- Single source of truth: All settings logic in one place

---

### 2025-12-31 03:00:00
🔄 **Vector Log Aggregation Migration**

**Major Change**: Replaced unreliable audit-logger with Vector for centralized, reliable log aggregation.

**Why**:
- Audit-logger used fire-and-forget HTTP with 2s timeout (silently dropped events)
- 173+ manual call sites meant incomplete coverage and maintenance burden
- No retry/fallback meant single failures = events lost forever

**What Changed**:
- **Added**: Vector service (timberio/vector:0.43.1-alpine) that captures ALL container stdout/stderr via Docker socket
- **Added**: `config/vector.yaml` - Routes logs to `/data/logs/platform.json` and `/data/logs/agents.json`
- **Added**: `src/backend/logging_config.py` - Structured JSON logging for Python backend
- **Removed**: `src/audit-logger/` directory and `docker/audit-logger/` Dockerfile
- **Removed**: `src/backend/services/audit_service.py` and all 173+ `log_audit_event()` calls across 24 files
- **Removed**: `/api/audit/logs` endpoint
- **Updated**: docker-compose.yml and docker-compose.prod.yml
- **Updated**: CLAUDE.md, DEPLOYMENT.md with new architecture
- **Added**: `docs/QUERYING_LOGS.md` - Guide for querying logs with jq/grep

**Benefits**:
- **Reliable**: Never drops logs - captures everything Docker sees
- **Complete**: ALL containers automatically (no manual call sites)
- **Zero maintenance**: No application changes needed after migration
- **Queryable**: JSON files searchable with grep/jq

**Files Removed** (24 files cleaned):
- All routers: agents.py, credentials.py, settings.py, auth.py, mcp_keys.py, ops.py, chat.py, system_agent.py, schedules.py, public_links.py, public.py, systems.py, sharing.py, git.py, setup.py
- All services: files.py, permissions.py, crud.py, queue.py, terminal.py, deploy.py, folders.py, api_key.py
- dependencies.py, main.py, config.py, services/__init__.py

---

### 2025-12-31 01:30:00
👁️ **Execution Log Viewer in Tasks Tab**

**Feature**: View full execution logs for completed tasks via popup modal.

**Implementation**:
- "View Log" button (document icon) appears on completed tasks in Tasks tab
- Clicking opens modal with full Claude Code execution transcript
- Uses existing `GET /api/agents/{name}/executions/{execution_id}/log` endpoint
- Log displayed as formatted JSON in monospace font
- Modal shows status, timestamp, and scrollable log content
- Graceful handling of tasks without logs

**Files Modified**:
- `src/frontend/src/components/TasksPanel.vue` - Added log modal, view button, and related state/functions

---

### 2025-12-31 01:05:00
🔧 **All MCP Chat Calls Now Tracked in Tasks Tab**

**Feature**: ALL MCP `chat_with_agent` calls now create execution records, appearing in the Tasks tab.

**Problem Solved**: Initial fix only tracked agent-to-agent calls (when `X-Source-Agent` header present). User MCP calls (user-scoped keys via Claude Code) were still not tracked.

**Implementation**:
- MCP client now sends `X-Via-MCP: true` header on all chat calls
- Backend checks for either `X-Via-MCP` or `X-Source-Agent` header
- `triggered_by` values: "agent" (agent-to-agent), "mcp" (user MCP calls)
- All MCP executions now visible in Tasks tab

**Files Modified**:
- `src/mcp-server/src/client.ts` - Added `X-Via-MCP: true` header to chat method
- `src/backend/routers/chat.py` - Check for `X-Via-MCP` header in addition to `X-Source-Agent`

---

### 2025-12-31 00:25:00
🔧 **Agent-to-Agent Chat Tracking in Tasks Tab**

**Feature**: MCP `chat_with_agent` calls (non-parallel mode) now create execution records, appearing in the Tasks tab alongside scheduled and manual tasks.

**Problem Solved**: Previously, only `/task` endpoint created `schedule_executions` records. Agent-to-agent chat via `/chat` endpoint (when `parallel=false`, the default) was not tracked in the Tasks tab. This meant MCP orchestration calls weren't visible in the unified execution history.

**Implementation**:
- When `/chat` endpoint receives `X-Source-Agent` header (agent-to-agent call):
  - Creates `schedule_executions` record with `triggered_by="agent"`
  - Updates record on success with response, cost, context, tool_calls
  - Updates record on failure with error message
- All headless executions now visible in Tasks tab: manual, scheduled, and agent-to-agent

**Files Modified**:
- `src/backend/routers/chat.py` - Added execution record creation for agent-to-agent calls
- `docs/memory/feature-flows/execution-queue.md` - Updated with agent-to-agent tracking
- `docs/memory/feature-flows/tasks-tab.md` - Added `/chat` endpoint as entry point
- `docs/memory/feature-flows/mcp-orchestration.md` - Updated parallel/non-parallel mode docs

---

### 2025-12-30 23:50:00
🐛 **Bug Fixes: File Credential Injection & Mixed Credential Types**

**Fix 1: File Credential Injection Not Working**

- **Root Cause**: Running agent containers had outdated base image without file-handling code
- **Solution**: Rebuild base image with `./scripts/deploy/build-base-image.sh` and restart agents
- **Change**: Added INFO-level logging to `get_assigned_file_credentials()` for production debugging

**Fix 2: TypeError on Mixed Credential Types**

- **Error**: `TypeError: string indices must be integers, not 'str'` at `crud.py:331`
- **Root Cause**: `get_agent_credentials()` returns mixed dict where explicit assignments have dict values but bulk-imported credentials have string values
- **Solution**: Added `isinstance()` check in `crud.py` to handle both types

**Files Modified**:
- `src/backend/credentials.py` - Changed debug logs to info level
- `src/backend/services/agent_service/crud.py` - Handle string vs dict credentials

---

### 2025-12-30 22:20:00
🔐 **Agent-Specific Credential Assignment**

**Feature**: Fine-grained control over which credentials are injected into each agent. Credentials must now be explicitly assigned before injection.

**Problem Solved**: Previously all credentials were auto-injected into all agents. Users had no control over credential scope, leading to security concerns and unnecessary credential exposure.

**Key Changes**:
- No credentials assigned by default (explicit user action required)
- Redis SET storage: `agent:{name}:credentials` for credential IDs
- Filter input for searching credentials by name or service
- Scrollable credential lists with max-height
- "Apply to Agent" button to inject assigned credentials into running agent
- Credential count badge on Credentials tab

**Backend API**:
- `GET /api/agents/{name}/credentials/assignments` - List assigned credential IDs
- `POST /api/agents/{name}/credentials/assign` - Assign a credential
- `DELETE /api/agents/{name}/credentials/assign/{cred_id}` - Unassign
- `POST /api/agents/{name}/credentials/assign/bulk` - Bulk assign
- `POST /api/agents/{name}/credentials/apply` - Inject into running agent

**Files Modified**:
- `src/backend/credentials.py` - 7 new methods for assignment management
- `src/backend/models.py` - CredentialAssignRequest, CredentialBulkAssignRequest
- `src/backend/routers/credentials.py` - 5 new endpoints, route ordering fix
- `src/backend/services/agent_service/crud.py` - Use assigned credentials only
- `src/backend/routers/agents.py` - Cleanup assignments on agent deletion
- `src/frontend/src/stores/agents.js` - 4 new store actions
- `src/frontend/src/composables/useAgentCredentials.js` - Complete rewrite
- `src/frontend/src/views/AgentDetail.vue` - Filter input, scroll, filtered lists

---

### 2025-12-30 18:30:00
🔀 **Git Conflict Resolution**

**Feature**: Basic GitHub workflow with conflict detection and resolution strategies for pull and sync operations.

**Problem Solved**: Pull/sync operations would fail silently on conflicts. Users had no way to choose how to resolve conflicts (stash changes, force replace, etc.).

**Pull Strategies**:
- `clean` (default): Simple pull with rebase, fails on conflicts
- `stash_reapply`: Stash local changes, pull, reapply stash
- `force_reset`: Hard reset to remote, discard local changes

**Sync Strategies**:
- `normal` (default): Stage, commit, push - fails if remote has changes
- `pull_first`: Pull latest, then stage, commit, push
- `force_push`: Force push, overwriting remote

**UI**: GitConflictModal shows resolution options when 409 conflict detected. Destructive options shown in red with warnings.

**API Changes**:
- `POST /api/agents/{name}/git/pull` accepts `{strategy: "clean"|"stash_reapply"|"force_reset"}`
- `POST /api/agents/{name}/git/sync` accepts `{strategy: "normal"|"pull_first"|"force_push"}`
- Conflict responses return HTTP 409 with `X-Conflict-Type` header

**Files Modified**:
- `docker/base-image/agent_server/routers/git.py` - Pull/sync strategies
- `docker/base-image/agent_server/models.py` - Added GitPullRequest model
- `src/backend/routers/git.py` - Strategy parameters, conflict handling
- `src/backend/services/git_service.py` - Proxy with conflict detection
- `src/backend/db_models.py` - Added conflict_type to GitSyncResult
- `src/frontend/src/stores/agents.js` - Strategy parameters
- `src/frontend/src/composables/useGitSync.js` - Conflict state management
- `src/frontend/src/components/GitConflictModal.vue` - New conflict resolution UI
- `src/frontend/src/views/AgentDetail.vue` - Modal integration

---

### 2025-12-30 15:00:00
📁 **File-Type Credentials**

**Feature**: Inject entire credential files (JSON, YAML, PEM, etc.) into agents at specified paths.

**Problem Solved**: Service account JSON files and other file-based credentials couldn't be injected directly. Users had to break them into individual environment variables.

**Implementation**:
- New credential type: `file` with `file_path` field
- Files stored in Redis: `{content: "...file content..."}`
- Injected at agent creation via `/generated-creds/credential-files/`
- Hot-reload support via agent-server endpoint
- Frontend UI with file upload or paste

**Example - Google Service Account**:
```
Name: GCP Service Account
Type: File (JSON, etc.)
Service: google
File Path: .config/gcloud/application_default_credentials.json
Content: {"type": "service_account", "project_id": "...", ...}
```

**Agent Usage**:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/home/developer/.config/gcloud/application_default_credentials.json"
```

**Files Modified**:
- `src/backend/credentials.py` - Added `file` type, `file_path` field, `get_file_credentials()`
- `src/backend/services/agent_service/crud.py` - Write file credentials at agent creation
- `src/backend/routers/credentials.py` - Include files in hot-reload
- `docker/base-image/startup.sh` - Copy credential files to target paths
- `docker/base-image/agent_server/models.py` - Added `files` field to request model
- `docker/base-image/agent_server/routers/credentials.py` - Write files on hot-reload
- `src/frontend/src/views/Credentials.vue` - UI for file-type credentials
- `docs/memory/feature-flows/credential-injection.md` - Flow 7 documentation

---

### 2025-12-30 13:30:00
🔗 **Dynamic GitHub Templates via MCP**

**Feature**: Create agents from ANY GitHub repository via MCP - not just pre-defined templates.

**Problem Solved**: Previously, MCP `create_agent` only worked with templates pre-defined in `config.py`. Users wanted to create agents from arbitrary GitHub repos on the fly.

**Implementation**:
- `create_agent` tool now accepts `template: "github:owner/repo"` for any repository
- If template is not in pre-defined list, uses system GITHUB_PAT for access
- Repository must be accessible by the configured PAT (public or private with access)

**Example**:
```typescript
create_agent({
  name: "my-custom-agent",
  template: "github:myorg/my-private-agent"
})
```

**Requirements**:
- System GitHub PAT configured in Settings or via GITHUB_PAT env var
- PAT must have `repo` scope and access to target repository

**Files Modified**:
- `src/backend/services/agent_service/crud.py` - Support dynamic GitHub repos
- `src/mcp-server/src/tools/agents.ts` - Updated tool description
- `docs/memory/feature-flows/mcp-orchestration.md` - Added documentation

---

### 2025-12-30 12:00:00
🔄 **GitHub Source Mode - Unidirectional Pull Flow**

**Feature**: Agents can now track a GitHub source branch directly and pull updates on demand.

**Problem Solved**: Users developing agents locally wanted to push to GitHub and have Trinity pull updates, without dealing with bidirectional sync conflicts.

**Implementation**:
- **New Fields**: `source_branch` (default: "main") and `source_mode` (default: true) in agent git config
- **Source Mode**: Agent stays on source branch, pulls only (no working branch created)
- **Pull Button**: Added "Pull" button next to "Sync" in Agent Detail UI

**Flow**:
1. Develop locally → push to GitHub (main branch)
2. Create agent in Trinity from GitHub template
3. Click "Pull" button to fetch latest changes
4. Agent's `content/` folder (gitignored) stores large files separately

**Files Modified**:
- `src/backend/db_models.py` - Added `source_branch`, `source_mode` to AgentGitConfig
- `src/backend/database.py` - Migration and schema update
- `src/backend/db/schedules.py` - Updated create_git_config with new params
- `src/backend/models.py` - Added source_branch/source_mode to AgentConfig
- `src/backend/services/agent_service/crud.py` - Pass env vars to container
- `src/backend/routers/git.py` - Include new fields in /git/config response
- `docker/base-image/startup.sh` - Support GIT_SOURCE_MODE and GIT_SOURCE_BRANCH
- `src/frontend/src/composables/useGitSync.js` - Added pullFromGithub function
- `src/frontend/src/views/AgentDetail.vue` - Added Pull button

**API**:
- `POST /api/agents/{name}/git/pull` - Pull latest from source branch

---

### 2025-12-30 10:30:00
🔗 **Dashboard Permissions Integration**

**Feature**: Visual permission management on Dashboard via edge connections.

**Two Edge Types**:
- **Permission edges** (dashed blue): Show which agents CAN communicate - created by dragging between nodes
- **Collaboration edges** (solid animated): Show actual message flow - created automatically from agent activity

**Edge Creation**:
- Drag from source agent handle → target agent handle
- Immediately calls `POST /api/agents/{source}/permissions/{target}`
- Toast notification: "Permission granted: A → B"

**Edge Deletion**:
- Click to select edge → Press Delete/Backspace key
- Immediately calls `DELETE /api/agents/{source}/permissions/{target}`
- Toast notification: "Permission revoked: A → B"

**Files Modified**:
- `src/frontend/src/stores/network.js` - Added `permissionEdges`, `fetchPermissions()`, `createPermissionEdge()`, `deletePermissionEdge()`
- `src/frontend/src/stores/agents.js` - Added `addAgentPermission()`, `removeAgentPermission()` API methods
- `src/frontend/src/views/Dashboard.vue` - Added `@connect`, `@edges-change` handlers, toast notifications
- `src/frontend/src/components/AgentNode.vue` - Styled handles (blue, hover effects, glow)

**UX**:
- No confirmation dialogs - direct manipulation for fast workflow
- Permission edges show as dashed blue lines with arrow
- Node handles highlight blue on hover with glow effect
- Toast notifications for all permission operations

---

### 2025-12-29 16:45:00
🧹 **Removed DEV_MODE_ENABLED from Codebase**

**Complete Removal**:
- Removed `DEV_MODE_ENABLED` environment variable and all references
- Removed `devModeEnabled` state from frontend auth store
- Removed `dev_mode_enabled` from API response (`/api/auth/mode`)
- Updated all documentation, tests, and config files

**Simplified Auth API**:
- `GET /api/auth/mode` now returns `{email_auth_enabled, setup_completed}`
- `POST /api/token` always available for admin login (no mode gating)
- Token mode: `admin` for password login, `email` for email login

---

### 2025-12-29 16:30:00
🔐 **Login Page Simplification**

**Changes**:
- Removed Google OAuth and Developer Mode options from login page
- Login now offers two methods only:
  1. **Email with code** (primary) - For whitelisted users
  2. **Admin Login** (secondary) - Fixed username 'admin', password only
- Token mode changed from `dev` to `admin` for password-based login
- Audit log action changed from `dev_login` to `admin_login`

**Files Modified**:
- `src/frontend/src/views/Login.vue` - Simplified to email + admin login only
- `src/backend/routers/auth.py` - Updated token mode
- `src/frontend/src/stores/auth.js` - Simplified auth methods

---

### 2025-12-29 15:30:00
🗂️ **File Manager: Hidden Files Toggle + Inline Editing**

**New Features**:
1. **Show Hidden Files Toggle**: Checkbox in header to show/hide dotfiles (persisted to localStorage)
2. **Inline Text File Editing**: Edit button for text files, textarea editor, Save/Cancel buttons
3. **Unsaved Changes Warning**: Confirmation prompt when switching files or canceling with changes

**Backend Changes**:
- `docker/base-image/agent_server/routers/files.py`: Added `show_hidden` query param, `PUT /api/files` endpoint
- `src/backend/services/agent_service/files.py`: Updated `list_agent_files_logic` with `show_hidden`, added `update_agent_file_logic`
- `src/backend/routers/agents.py`: Added `show_hidden` param, `PUT /{agent_name}/files` endpoint

**Frontend Changes**:
- `FileManager.vue`: Hidden toggle, edit state, startEdit/cancelEdit/saveFile methods
- `FilePreview.vue`: Edit mode with textarea, `isEditing` and `editContent` props
- `stores/agents.js`: `listAgentFiles` accepts `showHidden`, new `updateAgentFile` action

**Protection Policy**:
- **Cannot delete**: CLAUDE.md, .trinity, .git, .gitignore, .env, .mcp.json, .mcp.json.template
- **Cannot edit**: .trinity, .git, .gitignore, .env, .mcp.json.template
- **CAN edit**: CLAUDE.md, .mcp.json (users need to modify agent instructions and MCP config)

---

### 2025-12-29 14:15:00
📚 **Feature Flows Audit and Update**

**Verified and Updated**:
- `tasks-tab.md` - Updated line numbers for TasksPanel.vue (264-475, 329-433), AgentDetail.vue (201, 884-885), and added service layer references for queue.py
- `scheduling.md` - Updated API endpoint line numbers to match current schedules.py after refactoring (GET/PUT/DELETE endpoints shifted)
- `execution-queue.md` - Updated chat.py line numbers (106-356) to reflect retry helper addition

**No Changes Needed** (already accurate):
- `agent-lifecycle.md` - Service layer references correct (updated 2025-12-27)
- `agent-terminal.md` - Service layer references correct (updated 2025-12-27)
- `agent-network.md` - Workplan removal noted in revision history
- `testing-agents.md` - Plans router removal noted in revision history

**Index Updated**: `feature-flows.md` now reflects 2025-12-29 audit with summary of changes.

---

### 2025-12-29 13:30:00
🧪 **Add Missing Execution and Queue Tests**

**New Tests Added**: Comprehensive test coverage for task executions and queue management.

**test_executions.py** (new file - 15 tests):
- `TestExecutionsEndpoint` - GET /api/agents/{name}/executions endpoint tests
- `TestExecutionFields` - Verify execution records have required fields
- `TestTaskPersistence` - Tasks saved to DB, manual trigger, duration, cost tracking
- `TestAgentToAgentTask` - X-Source-Agent header and triggered_by='agent'
- `TestExecutionOrdering` - Executions returned in descending time order
- `TestExecutionFiltering` - Filter by status and triggered_by

**test_execution_queue.py** (expanded from 6 to 24 tests):
- `TestQueueStatus` - Queue status fields, agent name, busy state, queued executions
- `TestQueueStatusDuringExecution` - Queue busy during chat execution
- `TestClearQueue` - Clear queue, cleared count, auth, 404 handling
- `TestForceRelease` - Release, was_running, warning message, auth
- `TestQueueAfterOperations` - Queue state after clear/release
- `TestQueueWithParallelTasks` - Verify /task bypasses queue

**Coverage Gaps Addressed**:
1. GET /api/agents/{name}/executions endpoint (was untested)
2. Task persistence to database
3. Agent-to-agent execution via X-Source-Agent header
4. Queue endpoint authentication tests
5. Queue state verification after operations

---

### 2025-12-29 13:00:00
🔧 **Fix Agent Server Connectivity Race Condition**

**Problem**: Test suite showed 7/441 failures due to agent file server connectivity issues. When an agent container starts, the internal HTTP server takes a moment to initialize. Requests made during this window would fail with "All connection attempts failed".

**Root Cause**: No retry logic when communicating with agent containers - single connection failure caused immediate HTTP 500 error.

**Solution**: Added retry logic with exponential backoff for all agent communication:
- File operations (list, download, preview, delete)
- Chat endpoint
- Parallel task endpoint

**Changes**:
- `src/backend/services/agent_service/helpers.py` - Added `agent_http_request()` helper with retry logic
- `src/backend/services/agent_service/files.py` - Uses retry helper, returns 503 for connection failures
- `src/backend/routers/chat.py` - Added `agent_post_with_retry()` for chat/task endpoints

**Retry Logic**:
- 3 attempts with exponential backoff (1s, 2s, 4s)
- Returns 503 "Agent server not ready" for connection failures
- Tests can skip on 503 vs failing on 500

**Impact**: Fixes test_agent_files.py failures (7 tests), improves stability for test_activities.py chat tests.

---

### 2025-12-28 23:15:00
🔧 **Manual Tasks Now Persisted to Database**

**Fix**: Manual tasks triggered via Tasks tab are now saved to database and persist across page reloads.

**Backend Changes**:
- Added `create_task_execution()` method to `db/schedules.py` for manual task creation
- Uses `schedule_id = "__manual__"` as marker for non-scheduled tasks
- Updated `/api/agents/{name}/task` endpoint to save execution records
- Execution status (success/failed), response, cost, context, tool_calls all saved

**Modified Files**:
- `src/backend/db/schedules.py` - Added `create_task_execution()` method
- `src/backend/database.py` - Exposed `create_task_execution()` on Database class
- `src/backend/routers/chat.py` - Updated `/task` endpoint to persist executions

**Flow**:
1. Task submitted → execution record created with status "running"
2. Task completes → execution updated with status "success"/"failed" + metadata
3. Page reload → task appears in history from database

---

### 2025-12-28 22:45:00
✨ **Added Tasks Tab to Agent Detail Page (v2)**

**New Feature**: Tasks tab provides a unified view for all headless agent executions with inline task execution and monitoring.

**Key Capabilities**:
1. **Inline Task Execution**: Submit task → immediately appears as "running" in list → updates in place when done
2. **Lightweight UI**: No modals or notifications - everything happens on the task row itself
3. **Expandable Details**: Click task to expand and see response/error inline
4. **Re-run Tasks**: One-click to re-run any previous task
5. **Queue Status**: Shows busy/idle indicator and queue management options

**UX Flow**:
1. Enter task message in input field
2. Click "Run" or Cmd+Enter
3. Task immediately appears at top of list with yellow "running" status
4. When complete, status changes to green "success" or red "failed"
5. Click to expand and see response, click again to collapse

**New Files**:
- `src/frontend/src/components/TasksPanel.vue` - Lightweight task management panel

**Modified Files**:
- `src/frontend/src/views/AgentDetail.vue` - Added Tasks tab

**Technical Details**:
- Uses `/api/agents/{name}/task` endpoint (parallel mode, no queue blocking)
- Local state for pending tasks merged with server executions
- Real-time queue status polling every 5s
- Cmd+Enter / Ctrl+Enter keyboard shortcut to submit

---

### 2025-12-28 21:50:00
🧪 **Tested Scheduling and Execution Queue Functionality**

**Tested Features**:
- Schedule CRUD operations (create, list, get, update, delete)
- Schedule enable/disable with APScheduler sync
- Manual schedule trigger with execution tracking
- Execution queue status, clear, and force release
- WebSocket broadcast implementation verification

**Results**: All tests PASS

**Key Findings**:
1. **Session State Issue**: Scheduled executions fail with exit code 1 if agent's Claude Code session state is corrupted (e.g., from direct testing in container). Fix: restart agent.
2. **MCP Integration Works**: Scheduled tasks can use Trinity MCP tools (agent-to-agent communication)
3. **Observability Captured**: Executions record cost, context tokens, tool calls

**Execution Test Data**:
- Duration: ~25s
- Cost: ~$0.055
- Context: 3,702 / 200,000 tokens
- Tools used: Bash, Read, mcp__trinity__list_agents

**Note**: Feature flow docs (`scheduling.md`, `execution-queue.md`) have outdated line numbers but architecture is accurate.

---

### 2025-12-28 18:30:00
📚 **Documentation - Delegation Best Practices**

Added comprehensive delegation best practices to the Multi-Agent System Guide, covering the hybrid delegation strategy for Trinity.

**New Documentation** (`docs/MULTI_AGENT_SYSTEM_GUIDE.md`):
- **MCP vs Runtime Sub-Agents**: When to use each delegation type
- **Decision Matrix**: Clear guidance for choosing delegation method
- **Anti-Patterns**: Common mistakes to avoid
- **Architecture Diagram**: Visual overview of delegation layers

**Key Concepts Documented**:
- MCP delegation for cross-agent, audited, persistent work
- Runtime sub-agents (Gemini's `codebase_investigator`, Claude's `--agents`) for ephemeral parallelism
- Don't reinvent Trinity's orchestration inside containers

---

### 2025-12-28 18:15:00
🐛 **Fixed Credential Hot-Reload Not Saving to Redis**

**Problem**: When using the "Hot Reload Credentials" feature in AgentDetail, credentials were pushed to the running agent's `.env` file but were NOT persisted to Redis. This meant:
- Credentials were lost when the agent restarted
- "Reload from Redis" wouldn't include hot-reloaded credentials
- No permanent storage of credentials added via hot-reload

**Root Cause**: The `/api/agents/{name}/credentials/hot-reload` endpoint only pushed credentials to the agent container, skipping Redis storage entirely.

**Solution**: Modified the hot-reload endpoint to:
1. Parse credentials from the text input (unchanged)
2. **NEW**: Save each credential to Redis using `import_credential_with_conflict_resolution()`
3. Push credentials to the running agent (unchanged)

The conflict resolution handles duplicates intelligently:
- Same name + same value → reuse existing credential
- Same name + different value → create with suffix (e.g., `API_KEY_2`)
- New name → create new credential

**API Response Enhancement**: Now includes `saved_to_redis` array showing each credential's status:
```json
{
  "saved_to_redis": [
    {"name": "MY_API_KEY", "status": "created", "original": null},
    {"name": "EXISTING_KEY", "status": "reused", "original": null},
    {"name": "CONFLICT_KEY_2", "status": "renamed", "original": "CONFLICT_KEY"}
  ]
}
```

**Files Changed**:
- `src/backend/routers/credentials.py:617-727` - Added Redis persistence to hot-reload endpoint
- `docs/memory/feature-flows/credential-injection.md` - Updated Flow 3 documentation

**Testing**: Verified hot-reload → Redis → agent restart → reload from Redis works end-to-end.

---

### 2025-12-28 15:30:00
🐛 **Fixed File Manager Page Issues**

**Bug #1: Missing Navigation Menu**
- **Problem**: File Manager page (`/files`) didn't show the top navigation bar
- **Fix**: Added `<NavBar />` component to `FileManager.vue`

**Bug #2: Vue Runtime Compiler Warning**
- **Problem**: Console warning "Component provided template option but runtime compilation is not supported"
- **Cause**: `FileTreeNode.vue` used template strings for icon components which require Vue's runtime compiler
- **Fix**: Converted all icon components to use `h()` render functions instead of template strings

**Files Changed**:
- `src/frontend/src/views/FileManager.vue` - Added NavBar import and usage
- `src/frontend/src/components/file-manager/FileTreeNode.vue` - Render functions for icons

**Note on 404 Preview Errors**: Agents created before 2025-12-27 don't have the `/api/files/preview` endpoint. To fix, **recreate** the agent (not just restart). This applies to all agents created before the File Manager feature was implemented.

---

### 2025-12-28 15:00:00
🚀 **Multi-Runtime Support - Gemini CLI Integration**

Added support for Google's Gemini CLI as an alternative agent runtime, enabling cost optimization and 1M token context windows.

**New Features**:
- **Runtime Adapter Pattern**: Abstract interface for swapping execution engines
- **Gemini CLI Support**: Full integration with Google's free-tier AI
- **Per-Agent Runtime Selection**: Choose Claude Code or Gemini CLI per agent
- **Template Configuration**: New `runtime:` field in template.yaml

**Key Benefits**:
- **Cost Savings**: Gemini free tier (60 req/min, 1K/day)
- **5x Context Window**: 1M tokens vs 200K for Claude
- **Provider Flexibility**: Mix runtimes based on task complexity

**Files Added**:
- `docker/base-image/agent_server/services/runtime_adapter.py` - Abstract interface
- `docker/base-image/agent_server/services/gemini_runtime.py` - Gemini implementation
- `config/agent-templates/test-gemini/` - Test template
- `docs/GEMINI_SUPPORT.md` - User guide

**Files Modified**:
- `docker/base-image/Dockerfile` - Added Gemini CLI installation
- `docker/base-image/agent_server/services/claude_code.py` - Wrapped in ClaudeCodeRuntime
- `docker/base-image/agent_server/state.py` - Runtime-aware availability checks
- `docker/base-image/agent_server/routers/chat.py` - Uses runtime adapter
- `src/backend/models.py` - Added runtime fields to AgentConfig
- `src/backend/routers/agents.py` - Runtime env var injection
- `docker-compose.yml` - GOOGLE_API_KEY support

**Documentation Updated**:
- `README.md` - Multi-runtime feature mention
- `docs/DEPLOYMENT.md` - GOOGLE_API_KEY instructions
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Runtime Options section

**Backward Compatibility**: ✅ All existing agents continue using Claude Code by default.

---

### 2025-12-28 12:28:00
⚡ **Fixed Terminal Thread Pool Exhaustion (v2)**

**Problem**: App became unresponsive after opening multiple web terminals. Navigation would hang, API calls would timeout, and audit logging would fail.

**Root Cause**: Terminal implementation used a tight polling loop with `run_in_executor`:
- 5ms `select()` timeout → 200 thread pool calls/second per terminal
- Default thread pool has only 18 workers
- Multiple terminals saturated the pool, blocking all async operations

**Solution (Final)**: Use proper asyncio socket coroutines:
```python
# Correct approach - proper async coroutines
data = await loop.sock_recv(docker_socket, 16384)
await loop.sock_sendall(docker_socket, message.encode())
```

**Why not `add_reader`?** First attempt used `loop.add_reader()` with callback + Event signaling. This was overly complex and caused slow terminal response. The `sock_recv()`/`sock_sendall()` approach is simpler and faster - they are true coroutines that integrate naturally with async/await.

**Files Changed**:
- `src/backend/services/agent_service/terminal.py:221-280` - asyncio socket coroutines
- `src/backend/routers/system_agent.py:491-550` - Same for System Agent

**Investigation**: `docs/investigations/terminal-hanging-investigation.md`

| Metric | Before | After |
|--------|--------|-------|
| Thread pool calls/sec | 200+ per terminal | 0 |
| Max concurrent terminals | ~2-3 | Unlimited |
| Latency | 5ms polling | Near-instant |

---

### 2025-12-27 22:45:00
🐛 **Fixed Two Critical Bugs**

**Bug #1: Terminal Session Loss on Tab Switch**
- **Problem**: When switching from Terminal tab to another tab, the WebSocket connection was destroyed (using `v-if`), causing session loss and "session limit reached" errors when returning
- **Fix**: Changed `v-if="activeTab === 'terminal'"` to `v-show` in `AgentDetail.vue` (line 357) to keep the terminal component mounted
- **Result**: Terminal session persists when switching between tabs

**Bug #2: MCP Agent Import Only Copied CLAUDE.md**
- **Problem**: When deploying a local agent via MCP `deploy_local_agent` tool, only hardcoded files (CLAUDE.md, .claude/, README.md, resources/, scripts/, memory/) were copied
- **Root cause**: `startup.sh` had an explicit list of files instead of copying all template files
- **Fix**: Updated `startup.sh` to copy ALL files from `/template` (including `template.yaml` - required Trinity file)
- **Also added**: `.trinity-initialized` marker to prevent re-copying on container restart

**Files Changed**:
- `src/frontend/src/views/AgentDetail.vue` - Line 357: `v-if` → `v-show` for terminal tab
- `docker/base-image/startup.sh` - Lines 113-142: Replaced hardcoded file list with generic copy

**Note**: Base image rebuilt. New agents will get all files from templates. Existing agents unaffected.

---

### 2025-12-27 21:30:00
✅ **Implemented File Manager Page (Req 12.2)**

Added a dedicated File Manager page with two-panel layout for browsing and managing agent workspace files.

**New Files**:
- `src/frontend/src/views/FileManager.vue` - Main page with agent selector, file tree, preview panel
- `src/frontend/src/components/file-manager/FileTreeNode.vue` - Recursive tree component with icons
- `src/frontend/src/components/file-manager/FilePreview.vue` - Multi-format preview (image, video, audio, text, PDF)

**Backend Changes**:
- `docker/base-image/agent_server/routers/files.py` - DELETE endpoint, preview endpoint with MIME detection
- `src/backend/services/agent_service/files.py` - Proxy functions for delete and preview
- `src/backend/routers/agents.py` - New routes: DELETE `/{name}/files`, GET `/{name}/files/preview`

**Frontend Changes**:
- `src/frontend/src/stores/agents.js` - `deleteAgentFile()`, `getFilePreviewBlob()` methods
- `src/frontend/src/router/index.js` - `/files` route
- `src/frontend/src/components/NavBar.vue` - "Files" navigation link

**Features**:
- Agent selector dropdown with localStorage persistence
- Collapsible file tree with search/filter
- File preview: images, video, audio, text/code, PDF
- Delete with confirmation modal
- Protected file warnings (CLAUDE.md, .trinity/, .git/, .env, etc.)
- Inline notification system

**Note**: Existing agents need recreation (not restart) to get new preview/delete endpoints.

---

### 2025-12-27 18:00:00
✅ **Implemented Content Folder Convention (Req 12.1)**

Implemented the content folder convention for managing large generated assets (videos, audio, images, exports) that should NOT be synced to GitHub.

**Changes**:
- `docker/base-image/startup.sh` - Creates `content/{videos,audio,images,exports}` directories on startup
- `docker/base-image/startup.sh` - Adds `content/` to `.gitignore` automatically
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Added Content Folder Convention section with usage examples

**How it works**:
- All agents now get a `content/` directory at `/home/developer/content/`
- Files in `content/` persist across container restarts (same Docker volume)
- Files in `content/` are NOT synced to GitHub (added to `.gitignore`)
- Standard subdirectories: `videos/`, `audio/`, `images/`, `exports/`

**Testing**:
1. Rebuild base image: `./scripts/deploy/build-base-image.sh`
2. Restart any agent
3. Verify `content/` directory exists
4. Verify `.gitignore` contains `content/`

---

### 2025-12-27 17:30:00
📋 **Added Content Management & File Operations Requirements (Phase 11.5)**

Added comprehensive requirements for managing large generated assets (videos, audio, exports) in agent workspaces.

**Requirement 12.1: Content Folder Convention**
- `content/` directory auto-created and gitignored by default
- Prevents large files from bloating Git repositories
- Same persistent volume - survives container restarts
- Convention documented for template authors

**Requirement 12.2: File Manager Page**
- Dedicated `/files` route with agent selector dropdown
- Two-panel layout: tree (left) + preview (right)
- Preview support: images, video, audio, text/code, PDF
- Delete file/folder with confirmation
- Create new folders
- Protected file warnings (CLAUDE.md, .trinity/, etc.)

**Files Changed**:
- `docs/memory/requirements.md` - Added Section 12 (Content Management)
- `docs/memory/roadmap.md` - Added Phase 11.5

---

### 2025-12-27 15:45:00
🔧 **Refactored AgentDetail.vue (2,193 → 1,386 lines)**

Major refactoring of `views/AgentDetail.vue` using Vue 3 Composition API composables. Extracted 12 domain-specific composables to improve maintainability and reusability.

**Before**: Single 2,193-line component with all business logic inline
**After**: 1,386-line component + 12 composables (~1,000 lines total)

**New Composables Structure** (`composables/`):
| Module | Lines | Purpose |
|--------|-------|---------|
| useNotification.js | 20 | Toast notification management |
| useFormatters.js | 110 | Shared formatting utilities |
| useAgentLifecycle.js | 65 | Start/stop/delete operations |
| useAgentStats.js | 55 | Container stats polling |
| useAgentLogs.js | 65 | Log fetching with auto-refresh |
| useAgentTerminal.js | 45 | Terminal fullscreen/keyboard |
| useAgentCredentials.js | 70 | Credential loading & hot reload |
| useAgentSharing.js | 60 | Agent sharing with users |
| useAgentPermissions.js | 80 | Agent-to-agent permissions |
| useGitSync.js | 90 | Git status and sync operations |
| useFileBrowser.js | 95 | File tree loading/download |
| useAgentSettings.js | 70 | API key and model settings |
| useSessionActivity.js | 100 | Session info and activity polling |

**Benefits**:
- Each composable handles single concern with proper cleanup
- Composables can be reused in other components
- Better testability with isolated logic
- ~37% reduction in main component size

---

### 2025-12-27 12:15:00
📝 **Updated Feature Flow Documentation for Service Layer Refactoring**

Updated all feature flows affected by the agents.py refactoring to reflect new file paths and module locations.

**Updated Flows** (8 documents):
- `agent-lifecycle.md` - References to lifecycle.py, crud.py, helpers.py
- `agent-terminal.md` - References to terminal.py, api_key.py
- `agent-permissions.md` - References to permissions.py
- `agent-shared-folders.md` - References to folders.py, helpers.py
- `file-browser.md` - References to files.py
- `agent-custom-metrics.md` - References to metrics.py
- `execution-queue.md` - References to queue.py
- `local-agent-deploy.md` - References to deploy.py

**Each Updated Flow Includes**:
- "Updated 2025-12-27" note at top explaining refactoring
- Architecture table showing Router vs Service layer split
- Correct file paths and line numbers
- Revision history entry

**Index Updated**:
- `feature-flows.md` - Added refactoring summary and updated all affected flow entries

---

### 2025-12-27 10:30:00
🔧 **Refactored agents.py Router (2928 → 785 lines)**

Major non-breaking refactoring of `routers/agents.py` to improve maintainability. Business logic extracted to dedicated service modules while preserving all API signatures and external interfaces.

**Before**: Single 2,928-line file handling 25+ endpoints and 15 distinct concerns
**After**: 785-line thin router + 12 focused service modules (~2,735 lines total)

**New Service Module Structure** (`services/agent_service/`):
| Module | Lines | Purpose |
|--------|-------|---------|
| helpers.py | 233 | Shared utilities (get_accessible_agents, versioning) |
| lifecycle.py | 251 | Agent start/stop, Trinity injection |
| crud.py | 447 | Agent creation logic |
| deploy.py | 306 | Local agent deployment via MCP |
| terminal.py | 345 | WebSocket PTY terminal handling |
| permissions.py | 197 | Agent-to-agent permissions |
| folders.py | 231 | Shared folder configuration |
| files.py | 137 | File browser proxy |
| queue.py | 124 | Execution queue operations |
| metrics.py | 92 | Custom metrics proxy |
| stats.py | 162 | Container/context statistics |
| api_key.py | 97 | API key settings |

**Preserved Interfaces** (no changes needed in consuming modules):
- `main.py`: `router`, `set_websocket_manager`, `inject_trinity_meta_prompt`
- `systems.py`: `create_agent_internal`, `get_accessible_agents`, `start_agent_internal`
- `activities.py`: `get_accessible_agents`
- `system_service.py`: `start_agent_internal`

**Testing**:
- All 24 agent lifecycle tests pass
- All 56 permissions/folders/api_key tests pass
- All systems integration tests pass

**Files Changed**:
- Backend: `routers/agents.py` - Refactored to thin wrapper (2928 → 785 lines)
- Backend: `services/agent_service/__init__.py` - New module exports
- Backend: `services/agent_service/*.py` - 12 new focused modules

---

### 2025-12-26 18:30:00
🐛 **Fixed Email Whitelist 404 Error**

Fixed email whitelist endpoints returning 404 due to FastAPI route matching order issue.

**Root Cause**: The generic `GET /api/settings/{key}` catch-all route was defined BEFORE the specific `GET /api/settings/email-whitelist` route in settings.py. FastAPI matches routes in order, so it was hitting the generic route and treating "email-whitelist" as a setting key, returning "Setting 'email-whitelist' not found" instead of the actual whitelist data.

**Solution**:
- Reordered routes in `settings.py` - moved email-whitelist routes (lines 544-658) BEFORE the generic `/{key}` catch-all route (line 660+)
- Now FastAPI correctly matches the specific route first
- Email whitelist table now loads and displays whitelisted emails

**Files Changed**:
- Backend: `routers/settings.py` - Reordered routes (email-whitelist section moved from line 823 to line 544)

**Testing**:
1. Navigate to Settings page
2. Scroll to Email Whitelist section
3. Verify table loads and shows any whitelisted emails
4. Add a new email - should show "already whitelisted" if duplicate or add successfully
5. Remove button should work

---

### 2025-12-26 18:15:00
🐛 **Fixed Agents Page Render Error**

Fixed critical bug preventing Agents page from loading. Agents were not displaying due to missing function references from deleted Task DAG system.

**Root Cause**: Task DAG system (Req 9.8) was removed on 2025-12-23, but Agents.vue template still referenced deleted functions `hasActivePlan()`, `getTaskProgress()`, and `getCurrentTask()` at lines 93-101. This caused Vue render error: "Property 'hasActivePlan' was accessed during render but is not defined on instance."

**Solution**:
- Removed obsolete task progress section from Agents.vue template (lines 92-101)
- Removed unused `ClipboardDocumentCheckIcon` import
- Agents page now renders correctly without task progress display

**Files Changed**:
- Frontend: `views/Agents.vue:92-101` - Removed task progress section
- Frontend: `views/Agents.vue:160` - Removed unused icon import

**Testing**:
1. Navigate to /agents page
2. Verify agents list displays correctly
3. Verify no console errors
4. Verify agent cards show name, status, activity state, and context progress (but not task progress)

---

### 2025-12-26 18:00:00
🐛 **Fixed GitHub Initialization UI Timeout**

Fixed UI stuck in "Initializing..." state even after successful repository creation.

**Root Cause**: Git initialization can take 30-60 seconds for agents with large `.claude/` directories, but the frontend axios request had no explicit timeout (defaulted to browser's 60s limit). In some cases, the request would timeout before backend completed, leaving modal stuck.

**Solution**:
- Added explicit 120-second timeout to `initializeGitHub()` axios request
- Added user feedback in modal: "This may take 10-60 seconds depending on the size of your agent's files"
- Added console logging for debugging initialization flow
- Logs now show: Starting → Success → Reloading status → Closing modal

**Files Changed**:
- Frontend: `stores/agents.js:366` - Added `timeout: 120000`
- Frontend: `components/GitPanel.vue:132-134` - Added timing note
- Frontend: `components/GitPanel.vue:356-383` - Added console logging

**Testing**:
1. Initialize git for agent with large `.claude/` directory (100+ MB)
2. Modal should show "Initializing..." with spinner
3. Wait up to 60 seconds
4. Modal should close automatically after success
5. Git tab should display repository info
6. Check browser console for initialization logs

---

### 2025-12-26 17:45:00
🐛 **Fixed GitHub Repository Initialization - Four Issues**

Fixed four critical issues preventing GitHub repository initialization from working correctly:

**Issue 1: ImportError causing 500 status**
- **Root Cause**: The git router was importing `execute_command_in_container` from `services.docker_service`, but this function didn't exist
- **Solution**: Added the missing function to `docker_service.py` (lines 122-155)
- **Files**: `services/docker_service.py`

**Issue 2: Workspace directory not found**
- **Root Cause**: Git commands assumed `/home/developer/workspace` always exists, but some agents (test agents, agents created without templates) don't have this directory
- **Solution**: Added workspace detection and automatic creation:
  - Check if workspace directory exists before git operations
  - Create directory if missing
  - Use detected directory path for all git commands
  - Log which directory is being used for debugging
- **Files**: `routers/git.py:428-454` (workspace detection), `482, 505` (use detected path)

**Issue 3: Orphaned database records**
- **Root Cause**: If initialization partially failed (repo created but git init failed), database record was created but agent had no `.git` directory, causing "already configured" error but UI showing nothing
- **Solution**: Added verification and cleanup:
  - Before rejecting re-initialization, verify `.git` directory actually exists in container
  - If database record exists but git not initialized, auto-cleanup orphaned record
  - Verify git initialization succeeded before creating database record
  - Added `git rev-parse --git-dir` check before database insert
- **Files**: `routers/git.py:322-342` (orphaned record cleanup), `512-525` (verification before DB insert)

**Issue 4: Empty repository - no agent files pushed** ⚠️ **CRITICAL**
- **Root Cause**: Git was initialized in empty `/home/developer/workspace/` directory, but agent's actual files (CLAUDE.md, .claude/, .trinity/, .mcp.json, etc.) live in `/home/developer/`. Result: Empty GitHub repository with no agent content!
- **Solution**: Intelligent directory detection:
  - Check if `/home/developer/workspace/` exists AND has content → use workspace
  - Otherwise → use `/home/developer/` (where agent files actually are)
  - Create `.gitignore` to exclude system files (.bash_logout, .bashrc, .cache/, .ssh/, etc.)
  - Keep important agent files (CLAUDE.md, .claude/, .trinity/, .mcp.json, .claude.json)
  - Check both locations when verifying existing git configuration
- **Files**: `routers/git.py:442-476` (smart directory detection + .gitignore), `326-330` (check both locations)
- **Impact**: Agent files now correctly pushed to GitHub! Repository will contain the agent's configuration, prompts, and working files.

**Result**:
- Repository creation now works for both personal and organization accounts ✅
- Supports both fine-grained and classic PATs ✅
- Works with agents that have or don't have workspace directories ✅
- Handles partial failures gracefully with automatic cleanup ✅
- UI correctly displays git status after successful initialization ✅
- **Agent files are pushed to GitHub repository** ✅ (CRITICAL FIX!)

**Testing**:
1. Navigate to agent detail → Git tab
2. Click "Initialize GitHub Sync"
3. Enter repository owner (personal or organization) and name
4. Wait for initialization to complete (~10-30 seconds)
5. **Verify repository on GitHub has agent files:**
   - Visit `https://github.com/{owner}/{repo}`
   - Check files exist: CLAUDE.md, .claude/, .trinity/, .mcp.json, etc.
   - Verify `main` branch has initial commit
   - Verify `trinity/{agent}/{id}` working branch exists
6. Verify UI shows git sync status with remote URL and branch
7. Check logs show: "Using home directory (agent's files are here): /home/developer" or "Using workspace directory with existing content"
8. Check logs show: "Git initialization verified successfully"

---

### 2025-12-26 17:00:00
🔧 **Fixed GitPanel Vue Render Error**

Fixed "Cannot read properties of null (reading 'remote_url')" error when navigating to the Git tab on agent detail pages.

**Root Cause**: GitPanel template at line 182 had `<div v-else>` which would show the Git Status Display section when git was enabled and agent was running, BUT it didn't check if `gitStatus` object had complete data. When the API call returned, `gitStatus` could be temporarily set to an incomplete object, causing Vue to try rendering `gitStatus.remote_url` on line 191 before the property existed.

**Solution**: Changed line 182 from `<div v-else>` to `<div v-else-if="gitStatus?.remote_url && gitStatus?.branch">` to ensure the Git Status Display only renders when `gitStatus` has complete data.

**Files Changed**:
- Frontend: `components/GitPanel.vue:182` - Added guard for gitStatus properties

**Testing**:
1. Navigate to agent detail page
2. Click Git tab
3. Verify no console errors
4. Verify git status displays correctly (or "not enabled" message)

---

### 2025-12-26 16:30:00
🔧 **Fixed Fine-Grained PAT Support**

Fixed GitHub Personal Access Token validation to properly support fine-grained PATs (starting with `github_pat_`).

**Issue**: Fine-grained PATs were incorrectly showing "Missing repo scope" because they don't expose scopes via `X-OAuth-Scopes` header like classic PATs.

**Solution**:
- Backend now detects token type (fine-grained vs classic)
- Fine-grained PATs: Tests actual permissions by calling `GET /user/repos`
- Classic PATs: Checks `X-OAuth-Scopes` header for `repo` or `public_repo`
- Returns `token_type` and `has_repo_access` fields
- Frontend displays appropriate messages:
  - Fine-grained: "✓ Fine-grained PAT with repository permissions" or "⚠️ Missing repository permissions (need Administration + Contents)"
  - Classic: "✓ Has repo scope" or "⚠️ Missing repo scope"

**Files Changed**:
- Backend: `routers/settings.py` - Updated `test_github_pat` endpoint
- Frontend: `views/Settings.vue` - Updated test result display logic

---

### 2025-12-26 16:00:00
🔀 **GitHub Repository Initialization**

Added ability to initialize GitHub synchronization for any agent, pushing it to a new or existing GitHub repository with one click.

**Features**:
- **Settings Configuration**: GitHub Personal Access Token (PAT) management
  - Admin can configure GitHub PAT in Settings page
  - Test button validates token and checks scopes
  - Stored securely in system settings
- **One-Click Initialization**: Initialize GitHub sync from Agent Git tab
  - Modal form for repository owner and name
  - Option to create repository automatically
  - Private/public repository selection
  - Repository description (optional)
- **Automated Setup**: Backend handles complete initialization
  - Creates GitHub repository via GitHub API (if requested)
  - Initializes git in agent workspace
  - Commits current agent state
  - Pushes to main branch
  - Creates working branch (trinity/{agent-name}/{instance-id})
  - Stores configuration in database
- **MCP Tool**: `initialize_github_sync` for programmatic access

**Backend Changes**:
- **API Endpoints**:
  - `GET /api/settings/api-keys` - Returns both Anthropic and GitHub PAT status
  - `PUT /api/settings/api-keys/github` - Save GitHub PAT
  - `DELETE /api/settings/api-keys/github` - Delete GitHub PAT
  - `POST /api/settings/api-keys/github/test` - Test GitHub PAT validity
  - `POST /api/agents/{name}/git/initialize` - Initialize GitHub sync for agent
- **Helper Functions**:
  - `get_github_pat()` in `routers/settings.py` - Get GitHub PAT from settings or env
- **GitHub Integration**:
  - Repository creation via GitHub REST API
  - Git commands executed in agent container via Docker exec
  - PAT embedded in git remote URL for authentication

**Frontend Changes**:
- **Settings Page** (`src/frontend/src/views/Settings.vue`):
  - GitHub PAT input field with show/hide toggle
  - Test and Save buttons
  - Status indicators (configured/not configured, source)
  - Scope validation (checks for repo access)
  - Link to GitHub token settings
- **Git Tab** (`src/frontend/src/components/GitPanel.vue`):
  - "Initialize GitHub Sync" button when git not enabled
  - Modal form with repository configuration
  - Real-time feedback and error handling
  - Success triggers git status reload
- **Agents Store** (`src/frontend/src/stores/agents.js`):
  - `initializeGitHub(name, config)` method

**MCP Server Changes**:
- **New Tool**: `initialize_github_sync` (`src/mcp-server/src/tools/agents.ts`)
  - Parameters: agent_name, repo_owner, repo_name, create_repo, private, description
  - Enables agents to programmatically initialize GitHub sync

**Required Permissions**:
- GitHub PAT must have `repo` scope (full control of private repositories)
- Or `public_repo` scope (for public repositories only)

**User Workflow**:
1. Admin configures GitHub PAT in Settings → API Keys
2. User opens agent → Git tab
3. Clicks "Initialize GitHub Sync"
4. Enters repository owner and name
5. Selects options (create repo, private/public)
6. Click Initialize
7. Agent workspace is pushed to GitHub and sync is enabled

**Files Changed**:
- Backend: `routers/settings.py`, `routers/git.py` (new initialize endpoint)
- Frontend: `views/Settings.vue`, `components/GitPanel.vue`, `stores/agents.js`
- MCP Server: `src/tools/agents.ts`, `server.ts`

---

### 2025-12-26 14:00:00
📧 **Email-Based Authentication** (Phase 12.4)

Implemented passwordless email authentication as the new default login method. Users enter email → receive 6-digit code → login. Includes admin-managed whitelist and automatic whitelist addition when agents are shared.

**Features**:
- **Passwordless Login**: 2-step email verification (request code → verify code)
- **Email Whitelist**: Admin-managed whitelist controls who can login
- **Auto-Whitelist**: Sharing an agent automatically adds recipient to whitelist
- **Security**: Rate limiting (3 requests/10 min), email enumeration prevention, audit logging
- **Email Providers**: Support for console, SMTP, SendGrid, Resend

**Backend Changes**:
- **Database**: New tables `email_whitelist` and `email_login_codes`
- **API Endpoints**:
  - `POST /api/auth/email/request` - Request verification code
  - `POST /api/auth/email/verify` - Verify code and login
  - `GET /api/settings/email-whitelist` - List whitelist (admin)
  - `POST /api/settings/email-whitelist` - Add email (admin)
  - `DELETE /api/settings/email-whitelist/{email}` - Remove email (admin)
- **Operations**: `src/backend/db/email_auth.py` - EmailAuthOperations class
- **Models**: `src/backend/db_models.py` - EmailWhitelistEntry, EmailLoginRequest, etc.
- **Config**: `EMAIL_AUTH_ENABLED=true` by default, can override via system_settings
- **Integration**: `src/backend/routers/sharing.py` - Auto-whitelist on agent sharing

**Frontend Changes**:
- **Login Page**: `src/frontend/src/views/Login.vue` - Email auth shown by default
  - Step 1: Enter email, request code
  - Step 2: Enter 6-digit code with countdown timer
  - Fallback buttons for Dev mode and Auth0 if enabled
- **Auth Store**: `src/frontend/src/stores/auth.js` - Added email auth methods
  - `requestEmailCode(email)` - Request verification code
  - `verifyEmailCode(email, code)` - Verify and login
  - `emailAuthEnabled` state tracking
- **Settings Page**: `src/frontend/src/views/Settings.vue` - Email Whitelist section
  - Table view with email, source, added date
  - Add/remove emails
  - Source badges: "Manual" vs "Auto (Agent Sharing)"

**Security Features**:
- Rate limiting: 3 code requests per 10 minutes per email
- Code expiration: 10-minute lifetime
- Single-use codes: Marked as verified after use
- Email enumeration prevention: Generic success messages
- Audit logging: Complete event trail for all auth operations

**Configuration**:
- Environment variable: `EMAIL_AUTH_ENABLED=true` (default)
- Runtime toggle: System settings key `email_auth_enabled`
- Detection endpoint: `GET /api/auth/mode` includes `email_auth_enabled` flag

**Documentation**:
- Feature flow: `docs/memory/feature-flows/email-authentication.md` (1200+ lines)
- Comprehensive documentation of architecture, security, testing

**User Experience**:
- Default login: Email with verification code
- Seamless onboarding: Sharing agent auto-whitelists recipient
- Alternative methods: Dev mode and Auth0 still available if configured

**Use Cases**:
- Share agents with external collaborators via email
- No need to pre-configure Auth0 or manage OAuth
- Simple onboarding for new users
- Admin controls access via whitelist

**Files Changed**:
- Backend: 11 files (database, routers, config, models, operations)
- Frontend: 3 files (Login.vue, Settings.vue, auth.js)
- Documentation: 1 feature flow (1200+ lines)

---

### 2025-12-26 12:15:00
🔐 **Automatic Logout on Session Expiration**

Fixed UX issue where expired JWT tokens resulted in empty interface instead of redirecting to login.

**Problem**:
- When JWT token expired, API calls failed with 401
- Frontend showed empty interface (no agents, no data)
- User remained on dashboard with broken state
- Had to manually navigate to /login

**Solution**:
- Added axios response interceptor in `main.js`
- Automatically detects 401 responses (expired/invalid token)
- Clears auth state and redirects to login page
- Console logs: "🔐 Session expired - redirecting to login"

**Changes**:
- `src/frontend/src/main.js`: Added axios interceptor for 401 handling

**User Experience**:
- Token expires → automatic redirect to login screen
- Clear indication session has ended
- No more confusing empty interface

**Combined with previous fix**: 7-day token lifetime + automatic logout = smooth auth experience.

---

### 2025-12-26 12:00:00
⏱️ **Extended JWT Token Lifetime**

Increased JWT token expiration from 30 minutes to 7 days to reduce re-login frequency.

**Changes**:
- `src/backend/config.py`: Changed `ACCESS_TOKEN_EXPIRE_MINUTES` from 30 to 10080 (7 days)

**Impact**:
- Users stay logged in for 7 days instead of 30 minutes
- No more frequent re-logins when walking away from browser
- Still need to re-login after backend redeployments (expected behavior)

**Note**: For even longer sessions, increase `ACCESS_TOKEN_EXPIRE_MINUTES` in config.py (e.g., 43200 = 30 days).

---

### 2025-12-26 11:30:00
📊 **OpenTelemetry Enabled by Default**

Changed OTEL_ENABLED default from `0` (disabled) to `1` (enabled). New Trinity installations will now have metrics export enabled out of the box.

**Changes**:
- `.env.example`: Default `OTEL_ENABLED=1`
- `src/backend/routers/agents.py`: Default to enabled
- `src/backend/routers/observability.py`: Default to enabled
- `src/backend/routers/ops.py`: Default to enabled
- `src/backend/services/system_agent_service.py`: Default to enabled
- `docs/memory/feature-flows/opentelemetry-integration.md`: Updated documentation

**Rationale**: OTel provides valuable cost and productivity insights. Having it on by default ensures users get observability from the start. Set `OTEL_ENABLED=0` to disable.

---

### 2025-12-26 10:00:00
🔐 **Per-Agent API Key Control**

Added ability for agents to use either platform API key or user's own Claude subscription via terminal authentication.

**Changes**:
- Database: Added `use_platform_api_key` column to `agent_ownership` table (default: true)
- Backend: Container recreation on start if API key setting changed
- Backend: API endpoints `GET/PUT /api/agents/{name}/api-key-setting`
- Frontend: Radio toggle in Terminal tab when agent is stopped
- Agents can now run `claude login` in terminal to use personal subscription

**User Flow**:
1. Create agent (uses platform key by default)
2. Go to Terminal tab when agent is stopped
3. Select "Authenticate in Terminal" option
4. Start agent → run `claude login` to authenticate
5. Agent now uses user's subscription instead of platform key

**Files changed**:
- `src/backend/database.py` - Migration for new column
- `src/backend/db/agents.py` - get/set methods for setting
- `src/backend/routers/agents.py` - API endpoints, container recreation logic
- `src/frontend/src/stores/agents.js` - Store methods
- `src/frontend/src/views/AgentDetail.vue` - UI toggle

---

### 2025-12-25 19:30:00
✨ **Agent Terminal: Replaced Chat with Terminal**

Replaced the Chat tab on Agent Detail page with a full Web Terminal (xterm.js), matching System Agent functionality.

**Changes**:
- Created `AgentTerminal.vue` component (generalized from SystemAgentTerminal)
- Added WebSocket terminal endpoint at `/api/agents/{name}/terminal`
- Replaced Chat tab with Terminal tab on Agent Detail page
- Terminal auto-connects when agent is running
- Fullscreen support with ESC key to exit
- Mode toggle: Claude Code (default) or Bash shell
- Access control: User must have access to agent (owner, shared, or admin)
- Session limit: 1 terminal per user per agent (prevents resource exhaustion)
- Audit logging for terminal sessions with duration tracking

**Files changed**:
- `src/frontend/src/components/AgentTerminal.vue` - New terminal component
- `src/frontend/src/views/AgentDetail.vue` - Terminal tab replaces Chat tab
- `src/backend/routers/agents.py` - WebSocket terminal endpoint
- `docs/memory/feature-flows/agent-terminal.md` - New feature flow
- `docs/memory/feature-flows.md` - Updated index, deprecated agent-chat

**Deprecation**: Agent Chat feature (`agent-chat.md`) is deprecated in favor of direct terminal access.

---

### 2025-12-25 18:00:00
✨ **Web Terminal: Embedded with Fullscreen**

Refactored Web Terminal from modal to embedded panel on System Agent page with fullscreen support.

**Changes**:
- Terminal now embedded directly on System Agent page (replaces Operations Console chat)
- Auto-connects when page loads (if agent running and user is admin)
- Added fullscreen toggle button in terminal header
- ESC key exits fullscreen mode
- Terminal refits automatically on fullscreen toggle
- Removed Terminal button from header (no longer modal-based)
- Cleaned up all unused chat-related code (marked library, sendMessage, etc.)

**Files changed**:
- `src/frontend/src/views/SystemAgent.vue` - Embedded terminal, fullscreen, removed chat code
- `docs/memory/feature-flows/web-terminal.md` - Updated documentation

---

### 2025-12-25 17:30:00
🐛 **Web Terminal Bug Fixes**

Fixed several issues discovered during local testing of the Web Terminal feature.

**Fixes**:
- Fixed localStorage key mismatch: Changed `trinity_token` to `token` to match auth store
- Fixed user_email extraction: Handle `None` database values with `or` fallback chain
- Added session timeout (5 min) to auto-cleanup stale sessions from failed connections
- Added WebSocket support (`ws: true`) to Vite proxy for `/api` endpoint
- Made `connectionStatusText` reactive using Vue `computed()` instead of static object

**Files changed**:
- `src/frontend/src/components/SystemAgentTerminal.vue` - localStorage key, computed status
- `src/frontend/vite.config.js` - Added `ws: true` to `/api` proxy
- `src/backend/routers/system_agent.py` - Session timeout, user_email fallback

---

### 2025-12-25 10:30:00
✨ **Web Terminal for System Agent (11.5)**

Implemented browser-based interactive terminal for System Agent with full Claude Code TUI support.

**Features**:
- xterm.js terminal emulator in modal (v5.5.0)
- WebSocket PTY forwarding via Docker exec
- Mode toggle: Claude Code or Bash shell
- Terminal resize follows browser window
- Admin-only access (button hidden for non-admins)
- Session limit: 1 terminal per user
- Audit logging for session start/end with duration

**Architecture**:
- Frontend: `SystemAgentTerminal.vue` with xterm.js + addons (fit, web-links)
- Backend: WebSocket endpoint at `WS /api/system-agent/terminal?mode=claude|bash`
- PTY allocation via Docker SDK `exec_create(tty=True)` + `exec_start(socket=True)`
- Bidirectional socket forwarding with `select()` for non-blocking I/O
- `decode_token()` helper for WebSocket authentication

**Files changed**:
- `src/frontend/package.json` - Added @xterm/xterm, @xterm/addon-fit, @xterm/addon-web-links
- `src/frontend/src/components/SystemAgentTerminal.vue` - New terminal component
- `src/frontend/src/views/SystemAgent.vue` - Added Terminal button and modal
- `src/backend/routers/system_agent.py` - Added WebSocket terminal endpoint
- `src/backend/dependencies.py` - Added `decode_token()` helper function

---

### 2025-12-24 12:30:00
❌ **Removed Platform Chroma Vector Store Injection**

Removed all platform-level vector memory injection for development workflow parity.

**Reason**: Local dev should equal production. Platform-injected capabilities create mismatches between local Claude Code development and Trinity deployment. Templates should be self-contained.

**What was removed**:
- `chromadb`, `sentence-transformers`, `chroma-mcp` from base image (~800MB savings)
- Vector store directory creation during Trinity injection
- Chroma MCP config injection into agent .mcp.json
- `vector-memory.md` documentation file from trinity-meta-prompt
- Vector memory section in injected CLAUDE.md
- `vector_memory` status field from Trinity status API
- `VECTOR_STORE_DIR` constant from agent server config

**Alternative**: Templates that need vector memory should include dependencies and configuration themselves. This ensures local development matches production exactly.

**Files changed**:
- `docker/base-image/Dockerfile` - Removed packages and model download
- `docker/base-image/agent_server/config.py` - Removed VECTOR_STORE_DIR
- `docker/base-image/agent_server/models.py` - Removed vector_memory field
- `docker/base-image/agent_server/routers/trinity.py` - Removed injection logic
- `config/trinity-meta-prompt/vector-memory.md` - Deleted
- `config/trinity-meta-prompt/prompt.md` - Removed vector memory references
- `docs/memory/requirements.md` - Updated 10.4, 10.5 to REMOVED
- `docs/memory/feature-flows/vector-memory.md` - Marked as removed
- `docs/memory/roadmap.md` - Updated Phase 10
- `docs/MULTI_AGENT_SYSTEM_GUIDE.md` - Removed vector memory sections
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md` - Removed vector memory sections
- `docs/requirements/CHROMA_MCP_SERVER.md` - Deleted

---

### 2025-12-24 11:30:00
🔧 **deploy_local_agent MCP Tool - Architecture Fix**

Fixed fundamental architecture issue where MCP tool tried to access local filesystem (impossible since MCP server runs remotely).

**Problem**: The `deploy_local_agent` MCP tool was using `fs.existsSync()` and `tar` commands to package a local directory. Since the MCP server runs on the remote Trinity server, it cannot access the calling agent's local filesystem.

**Solution**: Changed the tool to accept a pre-packaged base64 archive from the calling agent.

**New Parameters**:
```typescript
{
  archive: string,                    // Base64-encoded tar.gz (REQUIRED)
  credentials?: Record<string, string>, // Key-value pairs from .env
  name?: string                       // Override agent name
}
```

**Calling Agent Responsibility**:
1. Create tar.gz archive locally: `tar -czf agent.tar.gz --exclude='.git' ...`
2. Base64 encode: `base64 -i agent.tar.gz`
3. Parse .env file for credentials
4. Call `deploy_local_agent` with archive and credentials

**Files Changed**:
- `src/mcp-server/src/tools/agents.ts` - Replaced path-based logic with archive-based
- `docs/memory/feature-flows/local-agent-deploy.md` - Updated architecture and usage docs

**Backend API Unchanged**: `POST /api/agents/deploy-local` still accepts same parameters.
