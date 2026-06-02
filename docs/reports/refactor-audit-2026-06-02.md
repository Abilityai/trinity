# Refactor Audit Report

**Generated**: 2026-06-02 16:25
**Scope**: `src/` (backend + frontend + mcp-server + scheduler)
**Tool**: `/refactor-audit` (radon + vulture + AST analysis)

> **Noise filtered**: The raw scan flagged 2,635 issues, but 1,274 came from
> `src/backend/venv/.../site-packages/` (bundled third-party packages — docker,
> pydantic, pyasn1, pip vendor, etc.). Those are excluded below. **All numbers
> in this report are first-party Trinity code only (1,361 issues across 367
> files).** Recommend adding `venv/`, `node_modules/`, `site-packages/`,
> `dist/` to the analyzer's ignore list so future runs skip them.

## Summary

| Severity | Count | Description |
|----------|-------|-------------|
| 🚨 Critical | 75 | Must fix — blocks AI maintenance (can't fit file in context / unreviewable function) |
| 🔴 High | 120 | Strongly recommended |
| ⚠️ Medium | 404 | Recommended |
| 📝 Low | 762 | Nice to have |

**Total issues**: 1,361
**AI-Refactorable**: 1,361 / 1,361 (100%) — every finding is a mechanical extraction/split achievable with test coverage in place. None require algorithmic redesign.

### Issue type distribution (first-party)

| Type | Count | What it means |
|------|-------|---------------|
| `long_function` | 722 | Function exceeds line threshold |
| `high_complexity` | 217 | Cyclomatic complexity over threshold |
| `too_many_parameters` | 218 | Signature has too many positional params |
| `large_file` | 83 | File too big to hold in one AI context window |
| `large_script_section` | 61 | Vue `<script>` block too large |
| `large_component` | 54 | Vue SFC too large overall |
| `dead_code` | 6 | Unused imports/variables (vulture, ≥80% confidence) |

---

## 🚨 Critical Issues (75)

### Backend — Files too large to hold in one context (9)

| File | Logical lines | Raw lines | Recommendation |
|------|--------------:|----------:|----------------|
| `backend/db/migrations.py` | 1,998 | 2,375 | Append-only migration log — acceptable to grow, but split into era-based modules (`migrations/0001_xxx.py`) loaded by an index. |
| `backend/db/schedules.py` | 1,987 | 2,363 | Split into `schedules_crud.py` / `schedule_executions.py` / `schedule_webhooks.py`. |
| `backend/routers/chat.py` | 1,752 | 2,173 | Split sync-chat, parallel-fan-out, and async-persistence paths into sub-routers. **Hotspot** (see below). |
| `mcp-server/src/client.ts` | 1,504 | 1,734 | Split the backend HTTP client by domain (agents / schedules / chat / executions). |
| `scheduler/service.py` | 1,465 | 1,947 | Extract lock acquisition, execution dispatch, and status polling. |
| `backend/database.py` | 1,381 | 2,127 | Facade is intentional (Invariant: class-per-domain composed here) but the 47 wide delegating signatures (below) are the real smell. |
| `backend/routers/settings.py` | 1,196 | — | Split per settings domain (Slack transport, resources, feature flags, MCP URL). |
| `backend/db/schema.py` | 1,123 | — | DDL constant store — acceptable, but could be split per table group. |
| `backend/services/cleanup_service.py` | 1,037 | — | Extract each retention sweep (#772/#834) into its own strategy function. |

### Frontend — Components too large (17 `large_component` + 11 oversized `<script>`)

| Component | Total lines | `<script>` lines | Recommendation |
|-----------|------------:|-----------------:|----------------|
| `views/Settings.vue` | 3,061 | 1,269 | **Worst offender.** Each settings tab should be its own child component + composable. |
| `views/MobileAdmin.vue` | 1,941 | 615 | Decompose into per-section panels. |
| `views/Agents.vue` | 1,142 | 446 | Extract list/filter/grid into components; move logic to `stores/agents.js`. |
| `components/TasksPanel.vue` | 1,114 | 606 | Extract row, filters, and polling composable. |
| `components/ReplayTimeline.vue` | 1,095 | 651 | Extract playback engine into a `useReplay()` composable. |
| `views/AgentDetail.vue` | 1,025 | 773 | Tabs already exist as panels — move remaining orchestration logic to a composable. |
| `components/SchedulesPanel.vue` | 1,005 | 422 | Extract schedule form + cron editor. |
| `views/enterprise/Audit.vue` | 973 | 304 | Extract heatmap/calendar/table into sub-components. |
| `views/Dashboard.vue` | 864 | 288 | Extract tiles. |
| `views/AgentWorkspace.vue` | 840 | 544 | Extract panel/canvas logic to composable. |
| `views/ExecutionDetail.vue` | 803 | 422 | Extract observability sections. |
| `views/PublicChat.vue` | 769 | 479 | Extract message list + input. |
| `components/ChatPanel.vue` | 686 | 525 | Extract streaming logic to `useChatStream()`. |
| `components/AgentHeader.vue`, `FilesPanel.vue`, `PublicLinksPanel.vue`, `DashboardPanel.vue` | 603–650 | — | Decompose. |

### Backend — Functions too long / too complex to review safely

| Function | Lines | Complexity | Location |
|----------|------:|-----------:|----------|
| `create_agent_internal` | 714 | 111 | `services/agent_service/crud.py:116` |
| `execute_task` | 663 | 116 | `services/task_execution_service.py:334` ⚠️ orchestration core |
| `chat_with_agent` | 601 | 79 | `routers/chat.py:81` |
| `execute_parallel_task` | 551 | 69 | `routers/chat.py:949` |
| `lifespan` | 414 | 46 | `main.py:307` (startup wiring) |
| `_handle_message_inner` | 391 | 65 | `adapters/message_router.py:255` |
| `deploy_local_agent_logic` | 306 | 40 | `services/agent_service/deploy.py:301` |
| `_run_cleanup_inner` | 303 | 53 | `services/cleanup_service.py:243` |
| `public_chat` | 268 | 33 | `routers/public.py:404` |
| `system_agent_terminal` | 265 | — | `routers/system_agent.py:243` |
| `handle_terminal_session` | 262 | — | `services/agent_service/terminal.py:58` |
| `send_session_message` | 231 | — | `routers/sessions.py:513` |
| `process_file_uploads` | 223 | — | `services/upload_service.py:119` |
| `recreate_container_with_updated_config` | 210 | 39 | `services/agent_service/lifecycle.py:336` |
| `_render_forensic` | 183 | 66 | `services/canary_alerts.py:252` |

> **Note on `task_execution_service.py`**: this is the orchestration reliability
> core (EXEC-024, #678). It's both critically complex *and* on the active
> reliability roadmap. Refactor here needs the canary harness + test suite green
> first — treat as "requires tests before touching," not a quick win.

### Backend — Too many parameters (7 critical, ≥15 params)

| Function | Params | Location |
|----------|-------:|----------|
| `execute_task` | 25 | `services/task_execution_service.py:334` |
| (delegating method) | 16 | `database.py:982` |
| (session insert) | 16 | `db/sessions.py:138` |
| (delegating method) | 15 | `database.py:933` |
| (chat insert) | 15 | `db/chat.py:106` |
| `BacklogService.__init__`/enqueue | 15 | `services/backlog_service.py:57` |
| (audit write) | 15 | `services/platform_audit_service.py:70` |

These should take a dataclass/Pydantic request object instead of positional args. `execute_task(…, 25 params)` is the priority — a `TaskExecutionRequest` model would also simplify the idempotency/retry call sites.

---

## 🔴 High Priority Issues (120)

### Large files (800–1,000 logical lines)
- `backend/services/agent_client.py` (891)
- `backend/routers/public.py` (876)
- `backend/db_models.py` (854)
- `backend/main.py` (834)
- `backend/services/git_service.py` (831)
- `mcp-server/src/tools/agents.ts` (676)

### Large Vue components (high)
`GitPanel.vue` (587), `views/FileManager.vue` (584), `SessionPanel.vue` (512), `settings/McpKeysTab.vue` (508), `CredentialsPanel.vue` (495), `operator/NotificationsPanel.vue` (490), `AgentTerminal.vue` (474), `chat/ChatInput.vue` (437), `AgentNode.vue` (435).

### Long functions (top of 48)

| Function | Lines | Location |
|----------|------:|----------|
| `initialize_git_in_container` | 198 | `services/git_service.py:812` |
| `paid_chat` | 195 | `routers/paid.py:73` |
| `_execute_schedule_with_lock` | 194 | `scheduler/service.py:738` |
| `_run_async_task_with_persistence` | 190 | `routers/chat.py:755` |
| `deploy_system` | 187 | `routers/systems.py:35` |
| `check_network_health` | 185 | `services/monitoring_service.py:198` |
| `perform_health_check` | 177 | `services/monitoring_service.py:580` |
| `initialize_github_sync` | 174 | `routers/git.py:303` |
| `rename_agent` | 173 | `db/agent_settings/metadata.py:66` (touches 17 tables — FILES-001/#816 cascade) |
| `trigger_webhook` | 167 | `routers/webhooks.py:249` |

*(Plus 28 high-complexity and 20 too-many-parameter findings — see hotspots.)*

---

## ⚠️ Medium Priority Issues (404)

Highlights — services/routers in the 500–800 logical-line band that will graduate to "high" if they keep growing:

`routers/ops.py` (793), `scheduler/database.py` (790), `services/task_execution_service.py` (764), `services/monitoring_service.py` (695), `adapters/telegram_adapter.py` (688), `services/gemini_voice.py` (619), `services/agent_service/crud.py` (612), `routers/sessions.py` (608), `services/template_service.py` (587), `routers/git.py` (584), `routers/agents.py` (575), `db/public_links.py` (552).

The bulk of the 404 are functions in the 50–100 line range and complexity 15–20 — normal background debt, address opportunistically when touching the file.

---

## 📝 Low Priority Issues (762)

Functions 30–50 lines, complexity 10–15, signatures with 5–7 params. Not worth dedicated effort; clean up in passing during feature work.

### Dead code (6 — quick wins, near-zero risk)

| File | Line | Item |
|------|-----:|------|
| `backend/routers/agents.py` | 29 | unused import `volume_remove` |
| `backend/routers/ops.py` | 739 | unused variable `alert_id` |
| `backend/routers/slack.py` | 27 | unused import `SlackOAuthInitResponse` |
| `backend/services/event_bus.py` | 44 | unused import `RedisResponseError` |
| `backend/services/subscription_auto_switch.py` | 174 | unused variable `old_subscription_id` |
| `backend/staging-acceptance.py` | 16 | unused import `AsyncMock` |

> Verify each before deletion — `event_bus.py`'s `RedisResponseError` and
> `subscription_auto_switch.py`'s `old_subscription_id` may be intentional
> (re-export / debugging breadcrumb). The other four are safe.

---

## Hotspots (files with the most weighted debt)

Severity-weighted (critical=4, high=3, medium=2, low=1). Prioritize these — fixing one file clears many findings at once.

| Rank | File | Issues | Score | Dominant problem |
|-----:|------|-------:|------:|------------------|
| 1 | `backend/database.py` | 51 | 75 | 47 wide delegating signatures (facade) |
| 2 | `backend/db/schedules.py` | 38 | 70 | 26 long functions + oversized file |
| 3 | `scheduler/service.py` | 33 | 58 | 22 long functions |
| 4 | `backend/routers/chat.py` | 23 | 53 | 4 huge handlers (601/551/190 lines) |
| 5 | `backend/db/migrations.py` | 29 | 43 | append-only growth (lower real risk) |
| 6 | `backend/services/cleanup_service.py` | 18 | 40 | one 303-line / cx-53 sweep function |
| 7 | `backend/routers/settings.py` | 27 | 36 | 21 long handlers |
| 8 | `backend/routers/ops.py` | 20 | 36 | 8 high-complexity functions + dead var |
| 9 | `scheduler/database.py` | 22 | 35 | 7 wide signatures + long functions |
| 10 | `backend/routers/public.py` | 18 | 35 | `public_chat` (268 lines / cx-33) |

---

## Recommendations

### Quick Wins (low risk, high signal)
1. **Delete the 4 safe dead-code items** (`agents.py`, `slack.py`, `staging-acceptance.py`, `ops.py:739`) — one small PR.
2. **Add `venv/` / `node_modules/` / `site-packages/` to the analyzer ignore list** so future audits show only the 1,361 that matter (not 2,635).
3. **Split `views/Settings.vue` (3,061 lines)** tab-by-tab — it's already conceptually tabbed, so extraction is mechanical and each tab becomes independently testable.

### Requires Tests First (orchestration-critical — don't touch blind)
1. `services/task_execution_service.py::execute_task` (663 lines / cx-116 / 25 params) — on the active reliability roadmap. Introduce a `TaskExecutionRequest` model, then extract slot-admit / dispatch / terminal-write phases. Gate on canary harness green.
2. `scheduler/service.py::_execute_schedule_with_lock` and `routers/chat.py` handlers — touch the execution path; extract behind the existing test suite (`/test-runner`).
3. `services/cleanup_service.py::_run_cleanup_inner` (cx-53) — each retention sweep (#772/#834) is already conceptually separable into a strategy function.

### Architectural Changes (plan, don't rush)
1. **`backend/db/schedules.py` (1,987 lines)** → split into `schedules_crud` / `schedule_executions` / `schedule_webhooks` under the existing class-per-domain DB pattern (Invariant #2).
2. **`backend/routers/chat.py` (1,752 lines)** → separate sync, parallel-fan-out, and async-persistence into sub-routers (Invariant #4 — preserve route-registration order).
3. **`mcp-server/src/client.ts` (1,504 lines)** → domain-split the HTTP client to mirror the tool modules.
4. **`database.py` wide signatures** → migrate the 16-param insert paths (`db/sessions.py`, `db/chat.py`) to request objects; the facade itself can stay.

### Sequencing note
Per the current product focus (**Reliability**), the highest-value refactors are
the ones that also reduce orchestration risk: `task_execution_service.py`,
`scheduler/service.py`, `cleanup_service.py`, and `routers/chat.py`. These are
debt *and* on the critical path — but each needs the test/canary safety net
first. The frontend `.vue` decompositions are zero-risk by comparison (no
backend behavior change) and good candidates for incidental cleanup during
UI/UX work (the secondary theme).

## Next Steps

1. `/refactor-audit --quick` after each fix to confirm the count drops.
2. Add tests for `execute_task` / scheduler paths **before** refactoring them.
3. Small, incremental PRs — one hotspot file per PR.
4. Wire `venv`/`node_modules` exclusions into `analyze.py` so the signal stays clean.
