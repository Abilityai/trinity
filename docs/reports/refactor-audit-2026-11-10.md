# Refactor Audit Report

**Generated**: 2026-11-10 21:30
**Scope**: `src/` (backend + frontend + mcp-server + scheduler + cli)
**Tool**: `/refactor-audit` (radon + vulture + AST analysis)
**Previous audit**: [`refactor-audit-2026-06-02.md`](refactor-audit-2026-06-02.md)

> **What is counted**: the raw scan returned 5,090 findings over 1,446 files.
> This report covers **first-party OSS code only: 4,430 findings across 690
> files**. Excluded: the private submodule mounted at `src/backend/enterprise`
> (623 findings, out of scope for a public report), co-located test files (34)
> and `node_modules` (3).
>
> **File sizes are logical lines** (the analyzer skips blanks and comments), so
> they read lower than `wc -l`: `db/migrations.py` is 5,326 here and 6,267 raw.
>
> **Coverage gap**: on the frontend the analyzer reads `.vue` files only. Plain
> `.js` modules are not scanned, so the largest stores and utilities are missing
> from the counts — see "Not covered by the analyzer" below.

## Summary

| Severity | Count | June 2026 | Description |
|----------|------:|----------:|-------------|
| 🚨 Critical | 229 | 75 | Must fix — blocks AI maintenance |
| 🔴 High | 521 | 120 | Strongly recommended |
| ⚠️ Medium | 1,232 | 404 | Recommended |
| 📝 Low | 2,448 | 762 | Nice to have |

**Total issues**: 4,430 (June: 1,361 — 3.3× in five months)
**AI-Refactorable**: effectively all of them. Every finding is a size, complexity or signature-width problem that is cleared by mechanical extraction. The limit is test coverage on the execution path, not the nature of the change.

### Issue type distribution

| Type | Count | June |
|------|------:|-----:|
| `long_function` | 2,158 | 722 |
| `high_complexity` | 898 | 217 |
| `too_many_parameters` | 835 | 218 |
| `large_file` | 263 | 83 |
| `large_script_section` | 141 | 61 |
| `large_component` | 132 | 54 |
| `dead_code` | 3 | 6 |

### Where the debt sits

| Area | Critical | High | Medium | Low | Total |
|------|---------:|-----:|-------:|----:|------:|
| `backend/services` | 118 | 289 | 619 | 1,118 | 2,144 |
| `backend/db` | 12 | 48 | 173 | 372 | 605 |
| `backend/routers` | 19 | 52 | 171 | 326 | 568 |
| `frontend` (`.vue` only) | 49 | 51 | 51 | 122 | 273 |
| `backend/client_portal` | 10 | 18 | 64 | 155 | 247 |
| `backend/` top-level modules | 5 | 10 | 46 | 142 | 203 |
| `backend/adapters` | 4 | 17 | 31 | 59 | 111 |
| `scheduler` | 2 | 13 | 20 | 33 | 68 |
| `backend/shared_sessions` | 3 | 3 | 14 | 35 | 55 |
| `backend/canary` | 3 | 8 | 15 | 24 | 50 |
| `backend/utils` | 1 | 5 | 12 | 27 | 45 |
| `mcp-server` | 3 | 4 | 9 | 22 | 38 |
| `cli`, `backend/migrations`, `helper-mcp` | 0 | 3 | 7 | 13 | 23 |

`backend/services` holds 48% of all findings and 52% of the criticals.

### What changed since June

**Paid down** (the June recommendations that were acted on):

| June finding | Then | Now |
|--------------|-----:|----:|
| `routers/chat.py` | 1,752 lines; handlers of 601 and 551 lines | 984 lines; `chat_with_agent` 178, `execute_parallel_task` 159 |
| `db/schedules.py` | 1,987 lines | package of mixins; largest is `executions.py` at 883 |
| `routers/settings.py` | 1,196 lines | package; largest module 705 |
| `services/git_service.py`, `agent_client.py` | 831 / 891 lines | split into packages, off the high list |
| `create_agent_internal` | 714 lines, complexity 111 | 288 lines, complexity 13 |
| `main.py::lifespan` | 414 lines, complexity 46 | no longer flagged |
| `_run_cleanup_inner` | 303 lines, complexity 53 | 65 lines |
| `_handle_message_inner` | 391 lines, complexity 65 | 207 lines, complexity 24 |
| `public_chat`, `deploy_system` handlers | 268 / 187 lines | moved to services; routers no longer flagged |

**Relocated rather than removed**: several of those extractions moved the weight into a service that is now itself critical. `chat_execution_service.py` (2,874 lines) took the chat handlers, `agent_service/crud.py` (2,712) took the pieces of `create_agent_internal`, and `cleanup_service.py` grew from 1,037 to 2,777 lines even as its main function shrank.

**Grew**: `task_execution_service.py` 764 → 2,593 lines and `execute_task` 25 → 39 parameters; `mcp-server/src/client.ts` 1,504 → 3,744; `database.py` 1,381 → 3,170 with 47 → 113 wide delegating signatures; `Settings.vue` 3,061 → 3,744; `MobileAdmin.vue` 1,941 → 2,606.

**New since June**: `client_portal/` (247 findings, including the longest function in the codebase), `system_service.py`, `skill_service.py` / `skill_gate_service.py`, `ask_service.py`, `routers/a2a.py` / `a2a_client.py`, `shared_sessions/`, and the portal Vue components.

---

## 🚨 Critical Issues (229)

### Backend and MCP — files over 1,000 lines (44)

| File | Lines | Recommendation |
|------|------:|----------------|
| `backend/db/migrations.py` | 5,326 | Append-only log, low real risk. Split by era into modules loaded by an index; retires with #746. |
| `backend/client_portal/service.py` | 4,418 | Split by concern (chat turn, roster, sessions, files). **Hotspot.** |
| `backend/models.py` | 4,140 | Centralised by Invariant #14. Convert to a `models/` package re-exporting the public surface (the Invariant #1 package rule). |
| `mcp-server/src/client.ts` | 3,744 | Split the HTTP client by domain to mirror `tools/*.ts`. |
| `backend/services/system_service.py` | 3,511 | Split validate / export / plan / deploy / snapshot. **Hotspot.** |
| `backend/database.py` | 3,170 | Facade is intentional; the 113 wide delegating signatures are the smell. |
| `backend/services/chat_execution_service.py` | 2,874 | Split sync turn, pulled turn and persistence. **Hotspot.** |
| `backend/services/cleanup_service.py` | 2,777 | One module per sweep family, composed by the runner. |
| `backend/services/agent_service/crud.py` | 2,712 | Split template resolution, fork-to-own and container create. |
| `backend/services/task_execution_service.py` | 2,593 | Orchestration core — see "Requires tests first". |
| `backend/services/skill_service.py` | 2,497 | Split library sync, injection and assignment. |
| `backend/db/operator_queue.py` | 2,345 | Apply the `db/schedules/` mixin split (Invariant #2). |
| `backend/dependencies.py` | 2,339 | Auth core (Invariant #8). Package it, with the static guards updated in the same change. |
| `backend/db/schema.py` | 2,124 | DDL store; acceptable, could split per table group. |
| `backend/services/skill_gate_service.py` | 1,792 | |
| `backend/client_portal/router.py` | 1,764 | Sub-routers per resource; preserve registration order (Invariant #4). |
| `scheduler/service.py` | 1,668 | Extract lock acquisition, dispatch and status polling. |
| `backend/routers/a2a.py` | 1,642 | Move JSON-RPC dispatch into a service. |
| `backend/db/tables.py` | 1,638 | MetaData store; acceptable. |
| `backend/services/template_service.py` | 1,590 | |
| `backend/services/ask_service.py` | 1,546 | |
| `backend/services/subscription_auto_switch.py` | 1,487 | |
| `backend/services/gemini_voice.py` | 1,459 | |
| `backend/routers/agents.py` | 1,429 | |
| `backend/services/compatibility/static_checks.py` | 1,398 | One module per check family. |
| `backend/shared_sessions/service.py` | 1,353 | |
| `backend/adapters/telegram_adapter.py` | 1,329 | Extract `parse_message` (complexity 30) by message kind. |
| `backend/client_portal/db.py` | 1,310 | |
| `backend/main.py` | 1,302 | |
| `backend/services/a2a_client.py` | 1,301 | |
| `backend/db/subscriptions.py` | 1,232 | |
| `backend/services/agent_service/lifecycle.py` | 1,178 | |
| `backend/services/platform_prompt_service.py` | 1,152 | |
| `backend/db_models.py` | 1,150 | |
| `backend/services/metric_read_service.py` | 1,145 | |
| `backend/services/settings_service.py` | 1,134 | |
| `backend/services/objective_join_service.py` | 1,131 | |
| `backend/services/agent_service/deploy.py` | 1,046 | |
| `backend/adapters/message_router.py` | 1,045 | |
| `backend/routers/git.py` | 1,042 | |
| `backend/canary/snapshot.py` | 1,019 | |
| `backend/services/canary_alerts.py` | 1,004 | |
| `mcp-server/src/tools/agents.ts` | 998 | |
| `mcp-server/src/tools/chat.ts` | 938 | |

June had 9 files in this band.

### Frontend — components too large (33 components, 16 oversized `<script>` blocks)

| Component | Total lines | `<script>` lines | Recommendation |
|-----------|------------:|-----------------:|----------------|
| `views/Settings.vue` | 3,744 | 1,538 | Still the largest. One child component and composable per tab. |
| `components/portal/PortalConversation.vue` | 3,539 | 2,627 | Largest script block in the app. Extract stream handling, composer and message list. |
| `views/Portal.vue` | 3,239 | 2,305 | Move orchestration into `stores/clientPortal.js` consumers and composables. |
| `views/MobileAdmin.vue` | 2,606 | 928 | Per-section panels. |
| `components/FleetGrid.vue` | 1,768 | 596 | |
| `components/SchedulesPanel.vue` | 1,658 | 776 | Extract schedule form and cron editor. |
| `views/AgentDetail.vue` | 1,652 | 1,231 | Tabs are already panels; move the remaining orchestration into a composable. |
| `components/TasksPanel.vue` | 1,272 | 731 | |
| `components/AgentListPanel.vue` | 1,241 | 374 | |
| `components/ReplayTimeline.vue` | 1,133 | 676 | `useReplay()` composable. |
| `views/Dashboard.vue` | 1,096 | 494 | |
| `components/portal/PortalRoom.vue` | 1,073 | 686 | |
| `components/AgentTile.vue` | 970 | 317 | |
| `views/PublicChat.vue` | 904 | 611 | |
| `components/settings/SubscriptionsPanel.vue` | 898 | 333 | |
| `views/ExecutionDetail.vue` | 833 | 438 | |
| `components/CredentialsPanel.vue` | 810 | 490 | |
| `components/portal/PortalInbox.vue` | 787 | 576 | |
| `components/ChatPanel.vue` | 762 | 605 | |

Fourteen more sit between 600 and 750 lines: `settings/McpKeysTab`, `DashboardPanel`, `portal/PortalAsks`, `portal/PortalSidebar`, `AgentHeader`, `FilesPanel`, `skills/SkillsTab`, `PublicLinksPanel`, `NavBar`, `operator/NotificationsPanel`, `AgentAssignmentsSection`, `agent-settings/GeneralPane`, `canvas/CanvasPanel`, `CreateAgentModal`.

### Functions over 200 lines (50) or complexity over 30 (72)

| Function | Lines | Complexity | Params | Location |
|----------|------:|-----------:|-------:|----------|
| `portal_chat` | 658 | 58 | 18 | `client_portal/service.py:2937` |
| `deploy_local_agent_logic` | 531 | 62 | | `services/agent_service/deploy.py:913` |
| `_sync_agent` | 527 | 106 | | `services/operator_queue_service/ingest.py:87` |
| `execute_task` | 497 | 35 | 39 | `services/task_execution_service.py:1579` ⚠️ orchestration core |
| `deploy_manifest` | 479 | 70 | | `services/system_service.py:2202` |
| `update_setting` | 454 | 36 | | `routers/settings/generic.py:176` |
| `_call_agent_with_retries` | 420 | 56 | 8 | `services/task_execution_service.py:2457` ⚠️ |
| `_render_forensic` | 407 | 123 | | `services/canary_alerts.py:466` |
| `apply_result` | 404 | 66 | | `services/task_execution_service.py:3273` ⚠️ |
| `recreate_container_with_updated_config` | 391 | 49 | 7 | `services/agent_service/lifecycle.py:984` |
| `run_paid_turn` | 380 | 25 | 13 | `services/paid_turn_service.py:180` |
| `start_agent_internal` | 352 | 45 | | `services/agent_service/lifecycle.py:382` |
| `export_manifest` | 349 | 68 | | `services/system_service.py:1430` |
| `restart_fleet_impl` | 315 | 32 | | `services/fleet_ops_service.py:191` |
| `read_apply_snapshot` | 315 | 84 | | `services/system_service.py:2929` |
| `voice_websocket` | 312 | 34 | | `routers/voice.py:304` |
| `trigger_webhook` | 304 | 40 | | `routers/webhooks.py:72` |
| `_resolve_template` | 298 | 38 | | `services/agent_service/crud.py:1671` |
| `raise_ask` | 298 | 47 | 11 | `services/ask_service.py:820` |
| `run_public_chat` | 292 | 34 | | `services/public_chat_service.py:223` |
| `create_agent_internal` | 288 | 13 | 7 | `services/agent_service/crud.py:3615` |
| `handle_terminal_session` | 285 | 29 | | `services/agent_service/terminal.py:58` |
| `_inject_skills_locked` | 273 | 60 | | `services/skill_service.py:2016` |
| `initialize_git_in_container` | 270 | 21 | | `services/git_service/provisioning.py:471` |
| `system_agent_terminal` | 269 | 23 | | `routers/system_agent.py:321` |
| `_reconcile_orphaned_executions` | 261 | 40 | | `services/cleanup_service.py:2643` |
| `process_file_uploads` | 257 | 38 | 12 | `services/upload_service.py:121` |
| `_wake_agent_locked` | 257 | 42 | | `shared_sessions/service.py:1857` |
| `collect_snapshot` | 256 | 25 | | `canary/snapshot.py:1402` |
| `_write_responses_to_agent` | 240 | 56 | 10 | `services/operator_queue_service/write_back.py:60` |
| `apply_task_result` | 237 | 44 | 10 | `services/pull_coordination_service.py:727` |

Three of these are router handlers holding business logic, which Invariant #1 puts in a service: `update_setting` (454 lines), `voice_websocket` (312) and `trigger_webhook` (304).

Shorter functions that are critical on complexity alone: `_validated_ask` (63, `ask_service.py:1170`), `resolve_apply_plan` (59, `system_service.py:3980`), `_render_message` (53, `canary_alerts.py:922`), `validate_manifest` (53, `system_service.py:319`), `verify_request` (50, `aauth/verifier.py:120`), `build` (48, `client_portal/suggestions/service.py:208`), `_plan_existing_member` (45, `system_service.py:3650`), `_dispatch_jsonrpc` (44, `routers/a2a.py:1628`), `_row_to_schedule_execution` (43, `db/schedules/executions.py:39`), `upsert_endpoint` (42, `a2a_outbound.py:489`), `_metric_row` (42, `objective_join_service.py:627`).

### Too many parameters (14 at 15 or more)

| Function | Params | Location |
|----------|-------:|----------|
| `execute_task` | 39 | `services/task_execution_service.py:1579` |
| `upsert` | 28 | `db/sync_state.py:163` |
| `build_pull_queue_payload` | 21 | `services/task_execution_service.py:688` |
| `create_loop` | 20 | `db/loops.py:107` |
| `list_items` | 20 | `db/operator_queue.py:1367` |
| `start_loop` | 20 | `services/loop_service.py:282` |
| `portal_chat` | 18 | `client_portal/service.py:2937` |
| `_list_conditions` | 18 | `db/operator_queue.py:1200` |
| `enqueue` | 18 | `services/backlog_service.py:82` |
| `run_chat_turn` | 18 | `services/chat_execution_service.py:1133` |
| `log` | 17 | `services/platform_audit_service.py:92` |
| `execute` | 16 | `services/fan_out_service.py:298` |
| `run_pulled_chat_turn` | 15 | `services/chat_execution_service.py:1415` |
| `_dispatch_all` | 15 | `services/fan_out_service.py:458` |

`execute_task` went from 25 to 39 parameters since June, when a `TaskExecutionRequest` model was first recommended. `execute_task`, `build_pull_queue_payload`, `enqueue`, `run_chat_turn`, `run_pulled_chat_turn` and `fan_out_service.execute` all carry overlapping subsets of one execution request; a single request object would shrink all six.

---

## 🔴 High Priority Issues (521)

### Large files (800–1,000 lines) — 18

`services/canary_service.py` (991), `routers/chat.py` (984), `client_portal/asks/service.py` (978), `services/pull_coordination_service.py` (976), `services/subscription_headroom_service.py` (971), `db/public_links.py` (906), `routers/agent_files.py` (901), `routers/public.py` (885), `db/schedules/executions.py` (883), `services/loop_service.py` (877), `services/telemetry_sharing_service.py` (849), `scheduler/database.py` (824), `routers/skills.py` (814), `services/agent_mcp_key_service.py` (804); MCP: `tools/operator_queue.ts` (787), `tools/schedules.ts` (638), `tools/skills.ts` (620), `types.ts` (610).

### Large Vue components (400–600 lines) — 28

`HelpChatWidget` (588), `OverflowTabs` (587), `SharingPanel` (573), `views/Library` (552), `OverviewPanel` (525), `portal/PortalRailFiles` (518), `LoopsPanel` (505), `views/Login` (504), `DeclaredMetricsTiles` (499), `chat/VoiceOverlay` (497), `portal/PortalInboxPane` (483), `AgentTerminal` (481), `chat/ChatInput` (478), `GitPanel` (477), `MonitoringPanel` (475), `views/Operations` (472), `ExecutionsPanel` (468), `AgentPermissionsMatrix` (462), `onboarding/FirstRunOverlay` (457), `InfoPanel` (444), `portal/PortalRail` (437), `AccessPanel` (433), `tiles/ExecutionsTile` (430), `A2aPanel` (427), `MetricDeclarationModal` (424), `TelegramChannelPanel` (424), `views/SetupPassword` (417), `canvas/CanvasKit` (410).

### Functions

246 functions of 100–200 lines, 139 at complexity 20–30, 67 with 10–14 parameters. Named in June and still here: `check_network_health` (197 lines), `paid_chat` (182), `perform_health_check` (177), `rename_agent` (167), `_execute_schedule_with_lock` (161, down from 194).

---

## ⚠️ Medium Priority Issues (1,232)

707 functions of 50–100 lines, 232 signatures with 8–9 parameters, 174 functions at complexity 15–20, 68 files of 500–800 lines, 51 Vue findings.

Files nearest the 800-line line, which graduate to high with one more feature: `agent_service/helpers.py` (799), `agent_service/files.py` (792), `services/monitoring_service.py` (791), `routers/schedules.py` (784), `client_portal/models.py` (754), `routers/subscriptions.py` (743), `services/memory_service.py` (743), `services/agent_call_limiter.py` (739), `services/idempotency_service.py` (737), `db/telegram_channels.py` (721), `db/schedules/stats.py` (719), `services/db_backup_service.py` (719).

Address these when touching the file, not as dedicated work.

## 📝 Low Priority Issues (2,448)

Functions of 30–50 lines, complexity 10–15, 5–7 parameters, files of 300–500 lines. Background debt; no dedicated effort.

### Dead code (3 flagged — none is a safe delete)

| File | Line | Item | Verdict |
|------|-----:|------|---------|
| `backend/routers/ops.py` | 385 | variable `alert_id` | False positive. It is the FastAPI path parameter of `POST /alerts/{alert_id}/acknowledge`. Also flagged in June. |
| `backend/services/metric_declaration_editor.py` | 357 | variable `indentless` | False positive. Parameter of an overridden `increase_indent` signature. |
| `backend/client_portal/service.py` | 1987 | import `_OAUTH_BETA` | Unused inside the module, but test bytecode under `tests/unit/` references the name, so it is probably a patch target. Check those tests before removing. |

The other dead-code items from June are gone.

---

## Hotspots (files with multiple issues)

Severity-weighted: critical 4, high 3, medium 2, low 1.

| Rank | File | Issues | Score | Dominant problem |
|-----:|------|-------:|------:|------------------|
| 1 | `backend/database.py` | 127 | 173 | 113 wide delegating signatures (facade) |
| 2 | `backend/services/chat_execution_service.py` | 78 | 159 | 31 long functions, 27 wide signatures, 19 complex |
| 3 | `backend/client_portal/service.py` | 92 | 142 | 52 long functions including the 658-line `portal_chat` |
| 4 | `backend/services/system_service.py` | 62 | 136 | 20 complex functions, five above complexity 50 |
| 5 | `backend/services/task_execution_service.py` | 52 | 125 | three 400–500 line functions; 39-parameter `execute_task` |
| 6 | `backend/services/agent_service/crud.py` | 61 | 115 | 34 long functions |
| 7 | `backend/db/operator_queue.py` | 59 | 104 | 30 long functions, 21 wide signatures |
| 8 | `backend/services/cleanup_service.py` | 54 | 102 | 34 long functions, 15 complex |
| 9 | `backend/services/skill_service.py` | 52 | 98 | 31 long functions |
| 10 | `backend/db/migrations.py` | 74 | 96 | append-only growth (low real risk) |
| 11 | `scheduler/service.py` | 43 | 74 | 24 long functions |
| 12 | `backend/shared_sessions/service.py` | 42 | 66 | 15 complex functions |
| 13 | `backend/services/skill_gate_service.py` | 39 | 65 | 14 complex functions |
| 14 | `backend/routers/a2a.py` | 41 | 63 | 24 long handlers |
| 15 | `backend/services/metric_read_service.py` | 35 | 62 | 11 complex functions |

Four of June's top ten have left the list: `db/schedules.py`, `routers/chat.py`, `routers/settings.py` and `routers/public.py`.

`database.py` ranks first on volume, not risk: its findings are near-identical pass-through signatures that disappear when the underlying `db/` methods take request objects.

### Not covered by the analyzer

Frontend `.js` modules are not scanned. By raw line count the largest would all be critical under the file threshold:

| File | Raw lines |
|------|----------:|
| `stores/clientPortal.js` | 2,913 |
| `components/portal/portalUtils.js` | 2,509 |
| `stores/network.js` | 2,085 |
| `stores/agents.js` | 1,317 |
| `stores/fleetGrid.js` | 909 |
| `components/portal/portalRail.js` | 892 |
| `components/portal/portalInbox.js` | 888 |
| `components/canvas/canvasUtils.js` | 781 |

Together with `Portal.vue` and `PortalConversation.vue`, the portal frontend is the largest concentration of oversized frontend code, and most of it is invisible to this tool.

---

## Recommendations

### Quick Wins (low risk, high impact)

1. **Teach the analyzer to scan `.js`** under `src/frontend/src`, and to skip a mounted private submodule by default. Both change what the next audit can see.
2. **Add a size ratchet for the files that are already critical.** The raw-color and loading-gate ratchets (#2605, #1927) show the pattern works here: freeze today's line counts in a baseline and fail when a listed file grows. The 3.3× growth since June came mostly from features landing in files already past the threshold.
3. **Move the three oversized router handlers into services** — `update_setting`, `trigger_webhook`, `voice_websocket`. These are Invariant #1 violations as well as size findings, and each has a clear service home.
4. **Split `views/Settings.vue` tab by tab.** Recommended in June and it has grown 680 lines since. The tabs are already conceptually separate.
5. **Split `_render_forensic`** (complexity 123, the highest in the codebase) into one renderer per forensic section. It is pure formatting with no side effects, so it is safe to cover with snapshot tests and then cut up.

### Requires Tests First

1. **`task_execution_service.py`** — `execute_task` (497 lines, 39 parameters), `_call_agent_with_retries` (420) and `apply_result` (404). Introduce the request object first, since that is a signature-only change; then extract admission, dispatch and terminal-write phases. Read `docs/memory/architecture/execution.md` before starting: every terminal side effect is gated on the status CAS, and an extraction that separates a CAS winner from its dispatch-activity close reintroduces the #1804 class.
2. **`chat_execution_service.py`** — `run_chat_turn` and `run_pulled_chat_turn` share most of their parameters and much of their body. Unify the request shape before splitting the file.
3. **`client_portal/service.py::portal_chat`** (658 lines). The roster payload is the only capability channel a portal principal has (`architecture/workspace.md`), so extract behind the existing portal exposure tests.
4. **`operator_queue_service/ingest.py::_sync_agent`** (527 lines, complexity 106). Extract per-item-kind handlers.
5. **`system_service.py`** — `deploy_manifest`, `export_manifest`, `read_apply_snapshot`, `resolve_apply_plan`, `validate_manifest` are five functions above complexity 50 in one file. Splitting the file along those five is the natural seam.
6. **`agent_service/lifecycle.py`** — `recreate_container_with_updated_config` and `start_agent_internal`. The PAT-gate rule in `architecture/agent-lifecycle.md` is pinned by an AST guard on the exact writer set; keep that guard green through any extraction.

### Architectural Changes

1. **Request objects for the execution path.** One model shared by `execute_task`, `build_pull_queue_payload`, `backlog_service.enqueue`, the two chat-turn functions and `fan_out_service.execute`. This also removes most of `database.py`'s 113 wide-signature findings where the facade forwards the same fields.
2. **`models.py` (4,140 lines) and `dependencies.py` (2,339) become packages** that re-export the public surface only, following the rule in Invariant #1. Both are imported nearly everywhere, and both have static guards (`test_models_centralized.py`, `test_1310_auth_wiring.py`) that must move with them.
3. **`db/operator_queue.py` and `db/subscriptions.py`** follow the `db/schedules/` mixin split (Invariant #2).
4. **`mcp-server/src/client.ts`** (3,744 lines, up from 1,504) splits by domain to mirror `tools/*.ts`. First recommended in June.
5. **`cleanup_service.py`** becomes a package with one module per sweep family. `OPS_SETTINGS_DEFAULTS` stays a single definition (`architecture/reliability.md`).
6. **Portal frontend** — `Portal.vue`, `PortalConversation.vue`, `stores/clientPortal.js` and `portalUtils.js` total about 12,000 raw lines. Plan one decomposition across the four, not four separate ones.

### Sequencing

The split-into-package refactors from June worked: every file that was split left the hotspot list and none came back. What did not hold is the total, because new features kept landing in the remaining large files. So the order that pays is the ratchet first (stops growth at no refactoring cost), then the execution-path request object (one change that shrinks six functions and the top hotspot), then one hotspot file per PR.

## Next Steps

1. Run `/refactor-audit --quick` after each fix to confirm the count drops.
2. Add tests for the execution-path functions before refactoring them.
3. Use small, incremental PRs — one hotspot file per PR.
4. Extend `analyze.py` to cover frontend `.js` before the next full audit.
