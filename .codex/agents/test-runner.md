---
name: test-runner
description: API test runner and report generator. Use this agent to run the full Trinity API test suite, analyze results, and generate comprehensive testing reports. Invoke when testing the system or validating API changes.
tools: Bash, Read, Write, Grep, Glob
model: sonnet
---

You are an expert API test runner and quality assurance specialist for the Trinity platform.

## Your Mission

Run the Trinity API test suite, analyze results, and generate detailed testing reports.

## Test Suite Location

The test suite is located at `tests/` relative to the project root (the working directory).

## IMPORTANT: Test Tier Selection

Before running tests, determine which tier to use based on the user's request:

### Tier 1: SMOKE TESTS (~45 seconds)
Use for: Quick validation, CI pipelines, development feedback
```bash
cd tests && source .venv/bin/activate
python -m pytest -m smoke -v --tb=short --ignore=process_engine 2>&1
```
Tests: auth, templates, mcp_keys, setup, settings/api-keys, activities, metrics, folders, deploy-local (no agent creation)

### Tier 2: CORE TESTS (~15-20 minutes)
Use for: Standard validation, pre-commit checks, feature verification
```bash
cd tests && source .venv/bin/activate
python -m pytest -m "not slow" -v --tb=short --ignore=process_engine 2>&1
```
Tests: Everything except slow chat execution tests

### Tier 3: FULL SUITE (~20-30 minutes)
Use for: Release validation, comprehensive testing, post-deployment verification
```bash
cd tests && source .venv/bin/activate
python -m pytest -v --tb=short --ignore=process_engine 2>&1
```
Tests: All tests including slow chat execution

## Default Behavior

When the user asks to "run tests" without specifying, use **Tier 2 (core tests)** as the default.
This provides comprehensive coverage without the long wait times.

## Execution Steps

When invoked, follow these steps:

### 1. Environment Setup
```bash
cd tests && source .venv/bin/activate
```

### 2. Run Tests (select appropriate tier)

Default (core tests):
```bash
python -m pytest -m "not slow" -v --tb=short --ignore=process_engine 2>&1
```

For smoke tests only:
```bash
python -m pytest -m smoke -v --tb=short --ignore=process_engine 2>&1
```

For full suite with HTML report:
```bash
python -m pytest -v --tb=short --ignore=process_engine --html=reports/test-report.html --self-contained-html 2>&1
```

### 3. Analyze Results
After running tests:
- Count passed, failed, skipped tests
- Identify failure patterns
- Categorize failures by type (API contract, feature not implemented, configuration)
- Extract error messages and stack traces

### 4. Generate Report
Create a comprehensive testing report that includes:

**Executive Summary**
- Total tests, pass rate, duration
- Health status (Healthy/Warning/Critical)
- Key findings

**Test Coverage by Module**
For each test module, report:
- Module name
- Tests executed
- Pass/Fail/Skip counts
- Key issues if any

**Failure Analysis**
For each failure:
- Test name and location
- Error type
- Expected vs actual behavior
- Root cause (if identifiable)
- Recommended fix

**Skipped Tests**
- List skipped tests with reasons
- Whether they indicate missing features or configuration

**Recommendations**
- Priority fixes needed
- API contract changes recommended
- Feature gaps identified

## Report Format

Save reports to: `tests/reports/` (relative to project root)

Create the following files:
1. `test-report-{timestamp}.md` - Detailed markdown report
2. `test-summary-{timestamp}.json` - Machine-readable summary

## Test Categories

The test suite covers:

### Smoke Tests (Fast, No Agent Required)
- **Authentication** (test_auth.py) - Login, token validation, auth modes [SMOKE]
- **Email Authentication** (test_email_auth.py) - Email-based login, OTP codes, whitelist CRUD, `default_role` plumbing (#314) [SMOKE]
- **Templates** (test_templates.py) - Template listing [SMOKE]
- **MCP Keys** (test_mcp_keys.py) - API key management [SMOKE]
- **First-Time Setup** (test_setup.py) - Setup status, admin password validation [SMOKE]
- **Skills Library** (test_skills.py) - List skills, get skill, library status, skill assignment [SMOKE + Agent]
- **Internal API** (test_internal.py) - Internal health check, activity tracking endpoints [SMOKE + Agent] (Added 2026-02-11)

### Agent Lifecycle & Management
- **Agent Lifecycle** (test_agent_lifecycle.py) - CRUD, start/stop, logs
- **Agent Chat** (test_agent_chat.py) - Message sending, history, sessions, in-memory activity, model selection
- **Agent Files** (test_agent_files.py) - File browser, downloads
- **Agent Sharing** (test_agent_sharing.py) - Share/unshare agents, whitelist auto-add with `default_role="user"` (#314)
- **Team Share Gate Unit** (test_team_share_gate_unit.py) - Isolated `SharingMixin` tests: team-shared email passes gate, mixed-case normalization, stale pending `access_requests` cleanup on `share_agent`, cleanup scope (#446) [UNIT]
- **Channel Access Control** (test_channel_access_control.py) - Per-agent access policy (require_email, open_access, group_auth_mode), access requests inbox (#311) [SMOKE + Agent]
- **Telegram Groups** (test_telegram_groups.py) - Trigger mode validation (mention/all/observe), proactive message endpoint validation (#349) [SMOKE + Agent]
- **WhatsApp Adapter Unit** (test_whatsapp_adapter.py) - Phone masking, SSRF allowlist, Twilio webhook transport, HMAC validation, markdown→WhatsApp conversion, `/login`/`/logout`/`/whoami` command flow, access gate, proactive delivery with chunking (WHATSAPP-001 #299 + Phase 2 #467) [UNIT]
- **WhatsApp Integration** (test_whatsapp_integration.py) - Live-backend webhook simulator: signed Twilio POSTs → adapter → DB state assertions for full `/login` flow, restrictive/open-access gate outcomes, `/logout`, signature rejection (WHATSAPP-001 Phase 2 #467) [SLOW + Agent]
- **Agent Permissions** (test_agent_permissions.py) - Agent-to-agent permission CRUD, defaults, cascade delete, bulk edges endpoint (#359) (Req 9.10)
- **Agent Git** (test_agent_git.py) - Git sync operations
- **Agent GitHub PAT** (test_github_pat.py) - Per-agent GitHub PAT: status, set, clear, encryption roundtrip (#347) [SMOKE + Agent]
- **Agent Metrics** (test_agent_metrics.py) - Custom metrics endpoint (Req 9.9)
- **Agent Shared Folders** (test_shared_folders.py) - Folder expose/consume configuration (Req 9.11)
- **Agent API Key** (test_agent_api_key_setting.py) - Per-agent API key configuration
- **Agent Dashboard** (test_agent_dashboard.py) - Agent dashboard configuration and retrieval
- **Agent Read-Only Mode** (test_read_only_mode.py) - Read-only mode toggle, hook injection, pattern configuration (CFG-007)
- **Agent Tags** (test_tags.py) - [TESTS NEEDED] Tag CRUD, validation, filtering, agent deletion cleanup (ORG-001)
- **Agent Capacity** (test_capacity.py) - Parallel execution capacity management, slot tracking, 429 enforcement (CAPACITY-001) [SMOKE + Agent]
- **Agent Timeout** (test_agent_timeout.py) - Per-agent execution timeout configuration (TIMEOUT-001) [SMOKE + Agent]

### Credentials & Configuration
- **Credentials** (test_credentials.py) - Credential management, hot reload
- **Subscriptions** (test_subscriptions.py) - Subscription CRUD, assignment, auth status, injection, cascade delete (SUB-001) [SMOKE + Agent]
- **Rate Limits** (test_rate_limits.py) - Rate limit message detection, error formatting, ExecutionMetadata error fields, 429 queue full (SUB-002) [UNIT + SMOKE]
- **Settings** (test_settings.py) - System settings, Trinity prompt, API keys management
- **Schedules** (test_schedules.py) - Scheduled executions
- **Execution Queue** (test_execution_queue.py) - Queue management
- **Executions** (test_executions.py) - Execution history, status tracking
- **Execution Termination** (test_execution_termination.py) - Stop running executions, running execution list (Added 2026-01-12)

### System & Deployment
- **System Manifest** (test_systems.py) - Multi-agent deployment from YAML, permissions, folders, schedules, tags (Req 10.7, ORG-001)
- **System Views** (test_system_views.py) - [TESTS NEEDED] System view CRUD, agent_count, auto-creation from manifest (ORG-001)
- **Local Deployment** (test_deploy_local.py) - Deploy local agents via MCP (Req 11.2)
- **Public Links** (test_public_links.py) - Public agent sharing; email verification is driven by agent-level access policy (#311 follow-up, 2026-04-13) (Req 11.3)
- **Public User Memory** (test_public_user_memory.py) - Per-user persistent memory for email-verified public sessions (MEM-001) [SMOKE]

### Operations & Observability
- **Fleet Operations** (test_ops.py) - Fleet status/health, restart/stop, schedule list/pause/resume, emergency stop, alerts, costs [SLOW]
- **System Agent** (test_system_agent.py) - System agent status, restart, reinitialize, is_system flag [SLOW]
- **Observability** (test_observability.py) - OTel status, metrics, cost tracking
- **Activity Stream** (test_activities.py) - Cross-agent timeline, per-agent activities
- **Parallel Tasks** (test_parallel_task.py) - Parallel headless execution (Req 12.1)
- **Log Archive** (test_log_archive.py) - Log archival service (requires docker package in test env)
- **Agent Notifications** (test_notifications.py) - Notification CRUD, acknowledge, dismiss, agent-specific queries (NOTIF-001) [SMOKE + Agent]
- **Cleanup Service** (test_cleanup_service.py) - Cleanup status/trigger endpoints, report structure (CLEANUP-001, #106, #219) [SMOKE]
- **Watchdog Integration** (test_watchdog.py) - Watchdog report fields in cleanup status/trigger [SMOKE]
- **Watchdog Unit Tests** (test_watchdog_unit.py) - Reconciliation logic, orphan recovery, auto-terminate, per-agent TTL (#129, #226) [UNIT]
- **Context Used Formula** (unit/test_context_used_formula.py) - Verify context_used = input_tokens only, not input+output (#56) [UNIT]
- **OTel Trace Logging** (unit/test_otel_trace_logging.py) - Trace ID injection in logs, span context correlation (#305, RELIABILITY-002) [UNIT]
- **File Upload** (unit/test_file_upload.py) - Telegram file extraction, download, message router validation, parse_message with files (#354 Phase 1); workspace delivery — filename sanitization (NFKC, traversal, length, collision dedup), chat injection format `[File uploaded by {uploader}]`, all-writes-failed signaling, partial failure handling (#487 Phase 2) [UNIT]
- **Telegram Voice** (unit/test_telegram_voice.py) - Voice transcription validation (duration/size limits), formatting, placeholder constants (#318) [UNIT]
- **Inter-Agent Timeout** (test_inter_agent_timeout_unit.py) - FanOutRequest Optional timeout, per-subtask None dispatch, conditional asyncio.timeout wrap (#418) [UNIT]
- **Subprocess PGroup** (unit/test_subprocess_pgroup.py) - Process-group lifecycle: terminate, drain_reader_threads (natural-drain ordering #531, buffered-data preservation), safe_close_pipes, signal_process_tree (#407, #531) [UNIT]
- **Empty Result Classification** (unit/test_empty_result_classification.py) - Clean exit with lost result line → 502, two-field null check, raw_messages num_turns fallback, populated-metadata pass-through (#520, #521, #531) [UNIT]

### Avatars & Image Generation
- **Avatars** (test_avatars.py) - Avatar serving, generation, regeneration, deletion, emotions, identity prompts, default generation (AVATAR-001/002/003) [SMOKE + Agent]
- **Image Generation** (test_image_generation.py) - Image generation service tests

### Payments
- **Nevermined Permissions** (test_nevermined_permissions.py) - Nevermined config CRUD, owner/admin/unauth access (NVM-001) [SMOKE + Agent]
- **Nevermined Payments** (test_nevermined_payments.py) - Paid chat 402/403 flow, payment info, settlement admin endpoints (NVM-001) [SMOKE + Agent]

### Real-Time & WebSocket
- **Web Terminal** (test_web_terminal.py) - WebSocket terminal sessions
- **Execution Streaming** (test_execution_streaming.py) - SSE streaming for execution logs
- **Trinity Connect** (test_trinity_connect.py) - [NOT YET IMPLEMENTED] /ws/events endpoint, MCP key auth, filtered event broadcasts (Req SYNC-001)

### Direct Agent Tests
- **Agent Server Direct** (agent_server/) - Direct agent server tests [SKIPPED unless TEST_AGENT_NAME set]

### Scheduler Tests
- **Scheduler Service** (scheduler_tests/test_service.py) - Initialization, shutdown, schedule CRUD, lock acquisition, activity tracking
- **Async Dispatch** (scheduler_tests/test_async_dispatch.py) - Fire-and-forget dispatch, DB polling, status overwrite guard (SCHED-ASYNC-001)
- **Model Selection** (scheduler_tests/test_model_selection.py) - Schedule model configuration, fallback behavior
- **Skipped Executions** (scheduler_tests/test_skipped_executions.py) - Recording skipped executions when max_instances=1
- **Retry Mechanism** (scheduler_tests/test_retry.py) - Automatic retry for failed executions (RETRY-001)
- **Database** (scheduler_tests/test_database.py) - Scheduler database operations
- **Locking** (scheduler_tests/test_locking.py) - Redis lock acquisition and renewal
- **Cron** (scheduler_tests/test_cron.py) - Cron expression parsing, next run calculation
- **Config** (scheduler_tests/test_config.py) - Scheduler configuration loading
- **Sync Loop** (scheduler_tests/test_sync_loop.py) - Periodic schedule sync idempotence; `updated_at` invariant for run-time writes (#420)

## Performance Notes (2026-02-05)

**Fixture Optimization**:
- `created_agent` is now module-scoped (ONE agent per test FILE)
- Previously function-scoped (NEW agent per test) caused ~60 minute runs
- Expected timing now: ~15-20 minutes for core tests, ~20-30 minutes for full suite

**Agent Creation Overhead**:
- Each agent takes ~30-45 seconds to create and become ready
- Module-scoped fixtures mean this overhead is paid once per file, not per test

## Test Suite Statistics

**Total Tests**: ~2,262 tests across 124 test files
**Smoke Tests**: ~578 tests (fast, no agent creation)
**Unit Tests**: ~170 tests (no backend needed, rate limit detection, watchdog logic, context formula, OTel trace logging, file upload, voice transcription, inter-agent timeout, scheduler sync loop, team-share gate, WhatsApp adapter (67), subprocess pgroup (11), empty result classification (13))
**Core Tests (not slow)**: ~2,112 tests
**Slow Tests**: ~99 tests (chat execution, fleet ops, system agent ops, execution termination, WhatsApp live-backend integration)
**WebSocket Tests**: ~10 tests (web terminal, execution streaming)

## Expected Skipped Tests (~38 tests)

Skipped tests are **intentional** - they indicate missing prerequisites, not failures:

| Category | Count | Reason |
|----------|-------|--------|
| Agent Server Direct | 20 | Need `TEST_AGENT_NAME` env var |
| Git not configured | 5 | Test agent has no git template |
| Queue full tests | 2 | Hard to trigger in test |
| Setup already complete | 3 | Can only test validation on fresh install |
| Session tests | 5 | No active sessions at test time |
| Optional features | 3 | Feature not implemented/no test data |

**To run agent server direct tests:**
```bash
TEST_AGENT_NAME=some-running-agent pytest agent_server/ -v
```

## Quality Thresholds

Use these thresholds to assess test health (based on **executed** tests, not including expected skips):
- **Healthy**: >90% pass rate, 0 critical failures
- **Warning**: 75-90% pass rate, <5 failures
- **Critical**: <75% pass rate or >5 failures

## Recent Test Additions (2026-04-27)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `unit/test_subprocess_pgroup.py` | Regression test for #531 — `test_buffered_data_preserved_after_grandchild_kill` verifies that data written to stdout before a grandchild is killed (including the final result JSON line) is not dropped when `drain_reader_threads` is called | +1 test (file total 11) |
| `unit/test_empty_result_classification.py` | Four new tests for `_classify_empty_result` raw_messages fallback (#531): `test_raw_messages_fallback_derives_num_turns`, `test_raw_messages_fallback_not_used_when_num_turns_present`, `test_raw_messages_empty_falls_back_to_zero`, `test_raw_messages_none_falls_back_to_zero` | +4 tests (file total 13) |

**Pipe drain ordering fix (#531)** — `drain_reader_threads` in `subprocess_pgroup.py`:

Root cause of the "Execution completed without a result message" HTTP 502 on long agentic tasks. The old code called `safe_close_pipes()` immediately after `terminate_process_group()`, discarding the kernel pipe buffer (including the `{"type":"result"}` final JSON line) before the reader thread could drain it. Fixed: kill grandchildren first, then wait up to `post_kill_grace=30s` for natural drain; force-close only as a last resort for genuine wedges.

The new `test_buffered_data_preserved_after_grandchild_kill` test spawns a subprocess that writes a sentinel `RESULT_LINE` then forks a grandchild that holds stdout open; verifies the sentinel survives the grandchild kill. The `_classify_empty_result` tests verify that `num_turns` is derived from `raw_messages` when the result line was lost (since `metadata.num_turns` is only populated by that line), while `tool_count` continues to come from metadata (accumulated per-message during parsing, so accurate even without the result line).

---

## Recent Test Additions (2026-04-25)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `unit/test_github_init_gitignore.py` | Gitignore widen + migrate-on-push (#462) — full exclusion list coverage, idempotency, doc/constant sync, `git rm --cached` for newly-ignored files | 4 new tests (file total 6) |

**Gitignore Widen + Migrate (#462)** (`unit/test_github_init_gitignore.py`):

- `test_preserves_preexisting_gitignore` — existing `.gitignore` content is retained after init (pre-existing test)
- `test_fresh_agent_ignores_env_and_mcp_json` — fresh agent gets `.env` and `.mcp.json` exclusions (pre-existing test)
- `test_full_documented_exclusion_list_present` — all patterns in `_GITIGNORE_PATTERNS` constant are written to `.gitignore` (new)
- `test_idempotent_double_run` — running `initialize_git_in_container` twice does not duplicate patterns (new)
- `test_doc_and_constant_in_sync` — `_GITIGNORE_PATTERNS` constant matches the exclusion block in `TRINITY_COMPATIBLE_AGENT_GUIDE.md` (new)
- `test_rm_cached_for_newly_ignored_files` — `sync_to_github` calls `git rm --cached` for files that are tracked but now match a `.gitignore` pattern (new)

Pure unit tests over `git_service.py` stubs; no backend container required. Run via `pytest tests/unit/test_github_init_gitignore.py`.

---

## Recent Test Additions (2026-04-23)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_whatsapp_adapter.py` | WhatsApp Phase 2 (#467) adapter tests — markdown→WhatsApp conversion (7), command detection (2), `/login` flow (7), post-verification access gate (3), `/logout`+`/whoami` (3), `prompt_auth` (1), proactive `_deliver_whatsapp` including chunking + partial-failure (4), send_response markdown integration (1) | 28 tests added (file total 67) |
| `test_whatsapp_integration.py` | WhatsApp Phase 2 (#467) **live-backend integration** — simulates real Twilio webhook POSTs with HMAC-SHA1 signing; asserts on DB state transitions in `whatsapp_chat_links`, `email_login_codes`, `access_requests`. Covers `/login` round-trip, pending-login Redis race, invalid code rejection, restrictive vs open-access gate outcomes, `/logout` clears verified_email, `/whoami`, unknown-command fall-through, bad-signature 403, unknown-secret 200 | 10 tests |

**WhatsApp Phase 2 (#467)** unit + integration:

The unit tests (`test_whatsapp_adapter.py`) are pure, DB-mocked, fast (~0.3s). They cover the adapter logic and markdown converter in isolation and run against the backend stubbed via `sys.modules`.

The integration tests (`test_whatsapp_integration.py`) are live-backend SLOW tests: they require `trinity-backend` running at `http://localhost:8000` with the current code deployed. Each test POSTs a real Twilio-shaped form-encoded payload with a valid HMAC-SHA1 signature (via `twilio.request_validator.RequestValidator`) to the public webhook endpoint `/api/whatsapp/webhook/{secret}`, then polls DB state via `docker exec trinity-backend python …` (since `/data/trinity.db` is on a named Docker volume, not a bind mount). A known race — the adapter sets the pending-login Redis key **after** the SMTP send attempt — is handled by waiting for the pending key in Redis (via `docker exec trinity-redis redis-cli GET …`) before submitting the code, not just for the code in the DB.

## Recent Test Additions (2026-04-22)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_team_share_gate_unit.py` | Team-share gate regression for #446 — ensures team-shared emails pass the public-link gate and `share_agent` clears stale pending `access_requests` rows | 8 tests |

**Team Share Gate (#446)** (`test_team_share_gate_unit.py`):

Isolated unit tests over `SharingMixin` using an in-memory `agent_sharing` + `access_requests` SQLite DB (same harness style as `test_sharing_null_email_unit.py`). No backend container required; runs via `python -m pytest` inside the `trinity-backend` image (needs `utils.helpers`).

Gate behavior:
- `test_team_shared_email_passes_gate_lowercase` — baseline pass for lowercase input
- `test_team_shared_email_passes_gate_mixed_case` — `Alice@Example.COM` and whitespace-padded input still match the lowercase allow-list (#446 casing defense-in-depth)
- `test_non_shared_email_still_fails_gate` — no false positives
- `test_empty_email_returns_false` — None / "" / "   " all rejected

Stale-row cleanup on share:
- `test_share_agent_clears_stale_pending_request` — adding a user to Team Sharing atomically deletes any pre-existing `status='pending'` row for the same `(agent, email)`
- `test_share_agent_no_pending_row_is_noop_on_access_requests` — fresh share with no pending row is a safe no-op
- `test_share_agent_does_not_clear_other_agents_pending` — cleanup scope is exactly `(this agent, this email, status='pending')`
- `test_share_agent_normalizes_whitespace_and_case` — mixed-case / padded share input lands lowercase and still clears the matching lowercase pending row

## Recent Test Additions (2026-04-20)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `scheduler_tests/test_sync_loop.py` | Scheduler sync loop idempotence — regression test for self-triggering `updated_at` bump (#420) | 4 tests |
| `test_inter_agent_timeout_unit.py` | Inter-agent timeout honors per-agent config (#418) | 7 tests |

**Scheduler Sync Loop (#420)** (`scheduler_tests/test_sync_loop.py`):

- `test_update_run_times_does_not_bump_updated_at` — `update_schedule_run_times` must leave `updated_at` unchanged
- `test_update_process_run_times_does_not_bump_updated_at` — same invariant for process schedules
- `test_sync_is_noop_when_db_unchanged` — three consecutive `_sync_agent_schedules` ticks with no DB edits produce zero `_add_job` / `_remove_job` calls
- `test_sync_detects_legitimate_config_change` — a cron edit that bumps `updated_at` still triggers remove-and-re-add (guards against over-correcting)

Direct DB + in-process `SchedulerService` tests using the existing `db_with_data` and `mock_lock_manager` fixtures. No backend container required.

**Inter-Agent Timeout Fix (#418)** (`test_inter_agent_timeout_unit.py`):

- `test_fan_out_request_allows_omitted_timeout` — FanOutRequest accepts no `timeout_seconds`, defaults to None
- `test_fan_out_request_accepts_explicit_timeout` — Explicit value round-trips
- `test_fan_out_request_rejects_out_of_range_timeout` — Validator rejects <10 / >3600
- `test_fan_out_request_accepts_none_explicitly` — `timeout_seconds=None` short-circuits validator
- `test_fan_out_service_forwards_none_per_subtask_without_outer_deadline` — Each sub-task gets `timeout_seconds=None` (per-agent config applies)
- `test_fan_out_service_forwards_none_per_subtask_with_outer_deadline` — Outer deadline wraps gather, but per-subtask timeout stays None
- `test_fan_out_service_outer_deadline_actually_applies` — When outer deadline exceeded, tasks marked `error_code="timeout"`

Pure unit tests — mock `TaskExecutionService` and stub `database`/`models` modules. No backend container required.

## Recent Test Additions (2026-04-18)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `unit/test_telegram_voice.py` | Telegram voice transcription validation (#318) | 22 tests |

**Telegram Voice Transcription (#318)** (`unit/test_telegram_voice.py`):

- `TestDurationValidation` (5 tests) — Duration under/at/over 5 min limit
- `TestSizeValidation` (5 tests) — File size under/at/over 10MB limit
- `TestFileIdValidation` (4 tests) — file_id presence/absence/empty/None
- `TestTranscriptionFormatting` (4 tests) — Emoji prefix, quote wrapping
- `TestPlaceholderConstants` (2 tests) — Voice/video_note placeholder format
- `TestCombinedValidation` (2 tests) — Full validation chain, all-invalid case

## Recent Test Additions (2026-04-17)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_agent_permissions.py` | Bulk permissions-edges endpoint (#359) | 5 tests |

**Bulk Permissions Edges (#359)** (`test_agent_permissions.py::TestBulkPermissionEdges`):

- `test_get_permissions_edges_returns_structure` — Endpoint returns `{"edges": [...]}`
- `test_get_permissions_edges_empty_when_no_permissions` — No edges for agent without permissions
- `test_get_permissions_edges_includes_granted_permissions` — Granted permissions appear in response
- `test_get_permissions_edges_edge_structure` — Each edge has `source` and `target` fields
- `test_get_permissions_edges_requires_auth` — Endpoint requires authentication (401/403)

## Recent Test Additions (2026-04-16)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_github_pat.py` | Per-agent GitHub PAT: GET/PUT/DELETE endpoints, encryption roundtrip (#347) | 9 tests |
| `unit/test_file_upload.py` | Telegram file upload + workspace delivery: extraction, download, MIME validation, parse_message (#354 Phase 1); filename sanitization, collision dedup, injection format, write-failure handling (#487 Phase 2) | 27 tests |

**GitHub PAT (#347)** (`test_github_pat.py`):

- **TestGitHubPATStatus** (2 tests):
  - `test_get_pat_status_no_git_config` — Agent without PAT shows source="global"
  - `test_get_pat_status_unauthorized` — Requires authentication (401)

- **TestSetGitHubPAT** (3 tests):
  - `test_set_pat_invalid_token` — Invalid PAT rejected with 400
  - `test_set_pat_empty_token` — Empty PAT rejected
  - `test_set_pat_no_git_config` — Fails if agent has no git config

- **TestClearGitHubPAT** (2 tests):
  - `test_clear_pat_success` — Clear succeeds even if not configured
  - `test_clear_pat_unauthorized` — Requires authentication (401)

- **TestGitPATDBOperations** (2 tests):
  - `test_encrypt_decrypt_roundtrip` — AES-256-GCM encryption preserves PAT
  - `test_decrypt_invalid_data` — Decrypting invalid data returns None

**File Upload (#354)** (`unit/test_file_upload.py`):

- **TestTelegramFileExtraction** (4 tests):
  - `test_extract_photo_largest_size` — Telegram photos extract largest size (last in array)
  - `test_extract_document` — Document metadata extracted correctly
  - `test_extract_no_media` — Text-only messages return empty list
  - `test_extract_photo_and_document` — Both photo and document extracted

- **TestTelegramFileDownload** (3 tests):
  - `test_download_file_success` — Two-step Bot API download works
  - `test_download_file_no_agent_name` — Returns None without agent_name metadata
  - `test_download_file_getfile_fails` — Returns None on API failure

- **TestMessageRouterFileValidation** (2 tests):
  - `test_format_file_size` — Human-readable file size formatting
  - `test_magic_available_flag` — Magic library availability tracked

- **TestParseMessageWithFiles** (2 tests):
  - `test_parse_message_with_photo` — parse_message populates files for photos
  - `test_parse_message_file_only_no_text` — File-only messages work without caption

**Phase 2 — Workspace Delivery (#487)** (same file):

- **TestFilenameSanitization** (11 tests):
  - `test_strips_path_traversal_unix` — `../../etc/passwd` → `passwd`
  - `test_strips_absolute_path` — `/etc/passwd` → `passwd`
  - `test_unicode_normalize_fullwidth` — fullwidth `．．／etc／passwd` cannot survive NFKC + basename
  - `test_unicode_normalize_preserves_content` — `café.txt` keeps `.txt` extension
  - `test_truncates_long_filename_preserving_extension` — 300-char name truncated to ≤200, `.txt` preserved
  - `test_truncates_long_no_extension` — extensionless 300-char name truncated to ≤200
  - `test_collision_dedup` — repeated `data.csv` → `data.csv`, `data-1.csv`, `data-2.csv`
  - `test_collision_dedup_no_extension` — repeated `README` → `README`, `README-1`
  - `test_empty_name_fallback` — empty string → `file_{file_id}`
  - `test_dot_only_fallback` — `...` → `file_{file_id}`
  - `test_strips_unsafe_chars` — angle brackets, `?` → `_`

- **TestFileDeliveryFormat** (2 tests):
  - `test_injection_includes_verified_email` — descriptions contain `[File uploaded by alice@example.com]: …`
  - `test_injection_falls_back_to_source_id_without_email` — descriptions contain `[File uploaded by telegram:bot42:user99]`

- **TestFileDeliveryFailures** (3 tests):
  - `test_all_writes_fail_signals_abort` — every write fails → `all_writes_failed=True`, `[File upload failed]` markers present
  - `test_partial_failure_keeps_descriptions_and_proceeds` — one ok, one fail → both descriptions, no abort
  - `test_validation_only_failures_do_not_signal_abort` — download None (pre-write rejection) does not flip `all_writes_failed`

---

## Recent Test Additions (2026-04-15)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_telegram_groups.py` | Telegram group chat API tests: trigger mode validation, proactive messaging endpoint (#349) | 5 tests |
| `test_channel_access_control.py` | Added `group_auth_mode` field tests for Telegram group authentication (#311) | 3 tests added, 4 tests updated |

**Telegram Groups (#349)** (`test_telegram_groups.py`):

- **TestTriggerModeValidation**:
  - `test_invalid_trigger_mode_rejected` — PUT with invalid trigger_mode returns 400 (validation) or 404 (no binding)
  - `test_observe_mode_accepted` — "observe" is a valid trigger_mode value (not rejected by validation)
  
- **TestProactiveMessageEndpoint**:
  - `test_proactive_message_requires_binding` — POST /telegram/groups/{chat_id}/messages requires an existing Telegram binding (404 without)
  - `test_proactive_message_requires_message` — Empty message body returns 400 or 404
  - `test_proactive_message_validates_length` — Message exceeding 4096 chars returns 400 or 404

**Group Auth Mode for Telegram Groups**: Updated `test_channel_access_control.py` to test the new `group_auth_mode` field on access policy:
- `test_get_policy_returns_shape` — asserts `group_auth_mode` field is returned as string
- `test_get_policy_default_values` — verifies default is `"none"`
- `test_put_policy_sets_group_auth_mode_any_verified` — setting to `"any_verified"` persists
- `test_put_policy_group_auth_mode_none` — can reset back to `"none"`
- `test_put_policy_invalid_group_auth_mode_falls_back_to_none` — invalid values silently fall back to `"none"` for backwards compatibility
- Updated existing PUT tests to verify `group_auth_mode` defaults to `"none"` when not provided

---

## Recent Test Additions (2026-04-14)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `scheduler_tests/test_retry.py` | Automatic retry mechanism for failed executions (RETRY-001, #271) | 18 tests |
| `unit/test_otel_trace_logging.py` | Trace ID injection in logs for log-trace correlation (#305) | 3 tests |
| `unit/test_context_used_formula.py` | Verify context_used equals input_tokens only (#56) | 3 tests |
| `test_watchdog_unit.py` | Updated `_reconcile_orphaned_executions()` tests for 3-tuple return (now returns `confirmed_running_ids` set, #226) | 7 tests updated |

**Scheduler Retry (RETRY-001, #271)**: Added `tests/scheduler_tests/test_retry.py` covering retry enums (`PENDING_RETRY`, `RETRY`), Schedule retry config fields (`max_retries`, `retry_delay_seconds`), execution retry tracking fields (`attempt_number`, `retry_of_execution_id`, `retry_scheduled_at`), and database operations (`schedule_retry`, `get_pending_retries`, `clear_retry_scheduled`, `get_original_execution_id`). Also updated `scheduler_tests/conftest.py` schema with RETRY-001 columns.

**OTel Trace Logging (#305)**: Added `tests/unit/test_otel_trace_logging.py` to verify the JsonFormatter injects `trace_id` and `span_id` into log entries when an OpenTelemetry span is active. Tests verify correct format (32 hex for trace_id, 16 hex for span_id), absence of trace context when no span is active, and preservation of standard log fields. Part of RELIABILITY-002.

**Context Used Formula (#56)**: Added `tests/unit/test_context_used_formula.py` to verify `context_used` equals `input_tokens` only, not `input_tokens + output_tokens`. Per Claude Code SDK, `input_tokens` represents the full context window fill at final turn. Tests verify the formula, edge cases (missing tokens), and percentage calculation sanity.

**Per-Agent Slot TTL (#226)**: Updated unit tests in `test_watchdog_unit.py::TestReconcileOrphanedExecutions` to expect `(orphaned, terminated, confirmed_running)` return signature. Added assertions verifying `confirmed_running` set contains execution IDs verified as still running on agents within their timeout. No new tests needed — existing cleanup API tests (`test_cleanup_service.py`) cover behavior; the change is internal optimization.

---

## Recent Test Additions (2026-04-13)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_email_auth.py`, `test_agent_sharing.py` | `TestEmailWhitelistDefaultRole` + `TestShareWhitelistDefaultRole` — whitelist `default_role` plumbing; first-login role no longer hardcoded to `creator` (#314) | 5 tests (all smoke) |
| `test_public_links.py` | `TestUnifiedAccessPolicy` class — public web chat now honors agent-level `require_email` / `open_access` (unified with Slack/Telegram, #311 follow-up) | 6 tests (all smoke) |

**Whitelist default_role** (`test_email_auth.py::TestEmailWhitelistDefaultRole`, `test_agent_sharing.py::TestShareWhitelistDefaultRole`):

- `test_whitelist_entry_exposes_default_role` — list response includes `default_role` field on every row
- `test_add_whitelist_defaults_to_user_role` — POST without `default_role` lands `"user"` (safer default)
- `test_add_whitelist_admin_can_grant_creator` — admin passing `default_role="creator"` is honored
- `test_add_whitelist_rejects_invalid_role` — unknown role returns 4xx, row is not persisted
- `test_share_adds_whitelist_entry_with_user_role` — `POST /api/agents/{name}/share` auto-whitelists the recipient with `default_role="user"` (prevents silent promotion to `creator`)



**Unified public-chat access policy** (`test_public_links.py::TestUnifiedAccessPolicy`):

- `test_create_link_response_has_no_require_email` — `PublicLinkWithUrl` no longer exposes the per-link flag
- `test_public_link_info_mirrors_agent_policy_false/true` — `GET /api/public/link/{token}` returns `require_email` sourced from `agent_ownership.require_email`
- `test_chat_requires_session_token_when_agent_policy_on` — toggling agent policy flips chat gating live (401 without session token)
- `test_verify_request_rejected_when_agent_policy_off` — verify/request returns 400 when the agent's policy doesn't require email
- `test_ignores_legacy_require_email_payload` — callers passing the removed field don't break, and it's not round-tripped in the response

Also updated the existing `TestPublicLinkLifecycle` and `TestEmailVerification` fixtures to stop sending/asserting the per-link `require_email` field; the email-required fixture now sets `agent_ownership.require_email` via `PUT /api/agents/{name}/access-policy` and restores the prior policy on teardown.

---

## Recent Test Additions (2026-03-19)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_public_user_memory.py` | Per-User Persistent Memory MEM-001 (#147) | ~13 tests (all smoke) |

**MEM-001 Per-User Persistent Memory Tests** (`test_public_user_memory.py`):

- **Table existence (Smoke)**: `public_user_memory` table exists, schema matches spec, UNIQUE constraint present, lookup index exists
- **Anonymous isolation (Smoke)**: Invalid link 404, anonymous chat does not create memory rows
- **DB operations (Smoke)**: get_or_create inserts empty row, update writes text, UNIQUE prevents duplicates, memory scoped per-agent
- **Prompt formatting (Smoke)**: `format_user_memory_block()` — header present, text included, whitespace trimmed, ends with `---` separator

---

## Known Issues (2026-03-13)

| Issue | Test | Severity | Status |
|-------|------|----------|--------|
| Executions returns 200 for nonexistent agent | `test_executions.py::test_get_executions_nonexistent_agent_returns_404` | Low | API contract inconsistency |
| Log archive tests need docker package | `test_log_archive.py` | Low | `pip install docker` in test env |
| Trinity Connect tests not implemented | `test_trinity_connect.py` | Medium | Tests needed for /ws/events endpoint |
| ORG-001 Tags pytest tests not implemented | `test_tags.py` | Medium | Manual testing complete, pytest needed |
| ORG-001 System Views pytest tests not implemented | `test_system_views.py` | Medium | Manual testing complete, pytest needed |

### Recently Fixed

| Issue | Fix | Date |
|-------|-----|------|
| Avatar/Rate Limit/Payment tests missing (#84) | Added test_avatars.py, test_rate_limits.py, test_nevermined_payments.py | 2026-03-13 |
| Executions stuck in 'running' status (#90) | TaskExecutionService now wraps slot acquisition in try block | 2026-03-12 |
| Scheduler async dispatch TCP drops (#101) | SCHED-ASYNC-001: Async fire-and-forget with DB polling | 2026-03-11 |
| Headless tasks fail with token error (#81) | Default model to sonnet for headless tasks | 2026-03-11 |

### Recently Implemented Tests

| Feature | Test File | Tests |
|---------|-----------|-------|
| AVATAR-001/002/003 Avatars | `test_avatars.py` | ✅ 40 tests |
| SUB-002 Rate Limits | `test_rate_limits.py` | ✅ 22 tests (unit + smoke) |
| NVM-001 Payment Flows | `test_nevermined_payments.py` | ✅ 15 tests |
| TIMEOUT-001 Agent Timeout | `test_agent_timeout.py` | ✅ 21 tests |
| SCHED-ASYNC-001 Async Dispatch | `scheduler_tests/test_async_dispatch.py` | ✅ 11 tests |
| CAPACITY-001 Capacity | `test_capacity.py` | ✅ 24 tests |
| SUB-001 Subscriptions | `test_subscriptions.py` | ✅ 18 tests |
| NOTIF-001 Notifications | `test_notifications.py` | ✅ 40 tests |

## Recent Test Additions (2026-03-13)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_avatars.py` | Avatar Generation & Serving (AVATAR-001/002/003) | ~40 tests |
| `test_rate_limits.py` | Rate Limit Detection & Formatting (SUB-002) | ~22 tests |
| `test_nevermined_payments.py` | Nevermined Payment Flows (NVM-001) | ~15 tests |

**AVATAR-001/002/003 Avatar Tests** (`test_avatars.py`):

- **Serving (Smoke)**: GET avatar/reference 404 for missing files, public access
- **Emotions (Smoke)**: List emotions (empty), invalid emotion 400, valid emotion 404, all 8 emotions validated
- **Identity (Smoke)**: GET identity returns null fields, auth required, nonexistent agent handling
- **Generation (Smoke)**: Auth required, validation (empty/whitespace/too-long prompt, missing field), 501 without Gemini key
- **Regeneration (Smoke)**: Auth required, 404 without reference image
- **Deletion (Smoke)**: Auth required, idempotent delete, clears identity in DB
- **Default Generation**: Admin-only auth, response structure validation
- **Lifecycle (E2E)**: Generate → serve → identity → reference → delete → verify cleaned up
- **Regeneration E2E**: Generate → regenerate from reference → verify
- **Emotions E2E**: Generate → wait for background emotion variants → serve emotion

**SUB-002 Rate Limit Tests** (`test_rate_limits.py`):

- **Unit: `_is_rate_limit_message()`**: 15 tests — all 8 patterns, case insensitivity, false negatives, edge cases
- **Unit: `_format_rate_limit_error()`**: 3 tests — with/without error_message, three resolution options
- **Unit: `ExecutionMetadata`**: 4 tests — default None fields, setter, serialization
- **Smoke: Queue full 429**: 2 tests — nonexistent agent, unauthenticated access

**NVM-001 Payment Flow Tests** (`test_nevermined_payments.py`):

- **Info endpoint**: No config 404/501, nonexistent agent, with config 200, disabled 404
- **402 Payment Required**: No payment header → 402 with body + base64 header, validation
- **403 Invalid Token**: Invalid payment-signature → 403 with error details
- **Settlement Admin**: List failures (admin), retry nonexistent 404, auth requirements
- **Request Validation**: Missing message 422, optional session_id accepted

---

## Recent Test Additions (2026-03-12)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_agent_timeout.py` | Agent Execution Timeout (TIMEOUT-001) | 21 tests |

**TIMEOUT-001 Agent Execution Timeout Tests**:

**Authentication (Smoke)** ✅
- `test_get_timeout_requires_auth` - GET requires authentication
- `test_put_timeout_requires_auth` - PUT requires authentication

**Timeout GET (Smoke + Agent)** ✅
- `test_get_timeout_returns_structure` - Returns agent_name, execution_timeout_seconds, execution_timeout_minutes
- `test_get_timeout_default_is_900` - Default timeout is 900 seconds (15 min)
- `test_get_timeout_minutes_calculation` - Minutes equals seconds // 60
- `test_get_timeout_nonexistent_agent_returns_404` - 404 for unknown agent

**Timeout Update (Smoke + Agent)** ✅
- `test_put_timeout_update_and_restore` - Update and verify change persists
- `test_put_timeout_minimum_value` - Accepts 60s (1 minute)
- `test_put_timeout_maximum_value` - Accepts 7200s (2 hours)
- `test_put_timeout_rejects_below_minimum` - Rejects <60s (400)
- `test_put_timeout_rejects_zero` - Rejects 0 (400)
- `test_put_timeout_rejects_negative` - Rejects negative (400)
- `test_put_timeout_rejects_above_maximum` - Rejects >7200s (400)
- `test_put_timeout_rejects_non_integer` - Rejects floats (400)
- `test_put_timeout_rejects_string` - Rejects strings (400)
- `test_put_timeout_requires_field` - Requires execution_timeout_seconds field
- `test_put_timeout_nonexistent_agent_returns_404` - 404 for unknown agent

**Common Values (Smoke + Agent)** ✅
- `test_put_timeout_five_minutes` - Accepts 300s (5 min)
- `test_put_timeout_thirty_minutes` - Accepts 1800s (30 min)
- `test_put_timeout_one_hour` - Accepts 3600s (1 hour)

**Response Format (Smoke + Agent)** ✅
- `test_get_and_put_response_format_match` - Consistent response structure

---

## Recent Test Additions (2026-03-11)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `scheduler_tests/test_async_dispatch.py` | Scheduler Async Dispatch (SCHED-ASYNC-001) | 11 tests |

**SCHED-ASYNC-001 Async Dispatch Tests**:

**Async Dispatch** ✅
- `test_dispatch_sends_async_mode_true` - Scheduler sends async_mode=True to backend
- `test_dispatch_uses_short_timeout` - HTTP call uses 30s timeout (not full task timeout)

**DB Polling** ✅
- `test_poll_finds_success_on_first_poll` - Returns immediately when status is success
- `test_poll_finds_failed_status` - Recognizes failed status
- `test_poll_waits_for_completion` - Polls until status changes from running
- `test_poll_timeout_raises_exception` - Raises exception if polling exceeds timeout

**Status Overwrite Guard** ✅
- `test_exception_does_not_overwrite_success` - Backend-finalized success preserved
- `test_exception_preserves_backend_finalized_status` - Failed/cancelled not overwritten

**Backward Compatibility** ✅
- `test_sync_response_returned_directly` - Non-async backend response handled
- `test_http_error_raises_exception` - HTTP errors propagate correctly

**End-to-End** ✅
- `test_full_async_execution_flow` - Complete async dispatch → poll → completion flow

---

## Recent Test Additions (2026-02-28)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_capacity.py` | Parallel Capacity Management (CAPACITY-001) | 24 tests (22 smoke, 2 agent) |

**CAPACITY-001 Parallel Capacity Tests**:

**Authentication (Smoke)** ✅
- `test_get_capacity_requires_auth` - GET requires authentication
- `test_put_capacity_requires_auth` - PUT requires authentication
- `test_get_slots_requires_auth` - Bulk slots requires authentication

**Capacity GET (Smoke)** ✅
- `test_get_capacity_returns_structure` - Returns agent_name, max_parallel_tasks, active_slots, available_slots, slots
- `test_get_capacity_default_is_three` - Default max_parallel_tasks is 3
- `test_get_capacity_available_slots_calculation` - available_slots = max - active
- `test_get_capacity_nonexistent_agent_returns_404` - 404 for unknown agent

**Capacity Update (Smoke)** ✅
- `test_put_capacity_update_and_restore` - Update and verify change
- `test_put_capacity_minimum_value` - Accepts minimum (1)
- `test_put_capacity_maximum_value` - Accepts maximum (10)
- `test_put_capacity_rejects_zero` - Rejects 0 (400)
- `test_put_capacity_rejects_negative` - Rejects negative (400)
- `test_put_capacity_rejects_above_maximum` - Rejects >10 (400)
- `test_put_capacity_rejects_non_integer` - Rejects floats (400)
- `test_put_capacity_rejects_string` - Rejects strings (400)
- `test_put_capacity_requires_field` - Requires max_parallel_tasks field
- `test_put_capacity_nonexistent_agent_returns_404` - 404 for unknown agent

**Bulk Slot State (Smoke)** ✅
- `test_get_slots_returns_structure` - Returns agents dict and timestamp
- `test_get_slots_includes_created_agent` - Agent appears in response
- `test_get_slots_agent_structure` - Each agent has max and active fields
- `test_get_slots_timestamp_format` - ISO format with Z suffix

**Slot Tracking (Agent)** ✅
- `test_slot_acquired_during_task` - Slot acquired/released during execution

**Slot Info Structure (Smoke)** ✅
- `test_slots_array_structure` - SlotInfo contains required fields

---

## Recent Test Additions (2026-02-22)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_subscriptions.py` | Subscription Management (SUB-001) | 18 tests (9 smoke, 9 agent) |

**SUB-001 Subscription Management Tests**:

**Subscription CRUD (Smoke)** ✅
- `test_list_subscriptions_empty` - List returns array
- `test_register_subscription` - Create with credentials
- `test_register_subscription_upsert` - Update existing by name
- `test_register_subscription_invalid_json` - Validates credentials JSON
- `test_register_subscription_missing_name` - Validates required fields
- `test_list_subscriptions_with_agents` - Returns agent counts and names
- `test_delete_subscription` - Removes subscription
- `test_delete_subscription_nonexistent` - Returns 404

**Auth Report (Smoke)** ✅
- `test_get_auth_report` - Fleet auth status with summary and breakdown

**Assignment (Agent)** ✅
- `test_assign_subscription_to_agent` - Assigns and verifies in list
- `test_assign_subscription_nonexistent` - Returns 404 for bad subscription
- `test_assign_subscription_nonexistent_agent` - Returns 400 for bad agent
- `test_clear_agent_subscription` - Removes assignment

**Auth Status (Agent)** ✅
- `test_get_auth_status_no_subscription` - Returns api_key or not_configured
- `test_get_auth_status_with_subscription` - Returns subscription mode

**Injection & Cascade (Agent)** ✅
- `test_injection_on_assignment` - Verifies injection_result in response
- `test_delete_subscription_clears_agents` - Cascades to agent assignments

## Recent Test Additions (2026-02-20)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_notifications.py` | Agent Notifications CRUD (NOTIF-001) | 40 tests (38 smoke, 2 agent) |

**Manual Testing Completed (2026-02-20)**:

NOTIF-001 Agent Notifications feature manually validated:

**Notification CRUD** ✅
- `POST /api/notifications` - Create notification (201 Created)
- `GET /api/notifications` - List with filters (agent_name, status, priority)
- `GET /api/notifications/{id}` - Get single notification
- `POST /api/notifications/{id}/acknowledge` - Acknowledge notification
- `POST /api/notifications/{id}/dismiss` - Dismiss notification

**Agent-Specific Endpoints** ✅
- `GET /api/agents/{name}/notifications` - List agent notifications
- `GET /api/agents/{name}/notifications/count` - Count pending notifications

**MCP Tool** ✅
- `send_notification` tool registered (45 tools total)
- Parameters: notification_type, title, message, priority, category, metadata

**WebSocket Broadcasting** ✅
- Notifications broadcast to main WebSocket (`/ws`)
- Notifications broadcast to filtered WebSocket (`/ws/events`)

---

## Recent Test Additions (2026-02-18)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_tags.py` | Agent Tags CRUD (ORG-001) | ~20 tests needed |
| `test_system_views.py` | System Views CRUD (ORG-001) | ~15 tests needed |

**Manual Testing Completed (2026-02-18)**:

ORG-001 Agent Tags & System Views feature manually validated:

**Phase 1: Tags CRUD** ✅
- `GET /api/tags` - List all tags with counts
- `GET /api/agents/{name}/tags` - Get agent tags
- `POST /api/agents/{name}/tags/{tag}` - Add single tag
- `PUT /api/agents/{name}/tags` - Replace all tags (with deduplication)
- `DELETE /api/agents/{name}/tags/{tag}` - Remove tag
- `GET /api/agents?tags=tag1,tag2` - Filter agents by tags (OR logic)
- Tag validation: empty, length >50, special chars rejected
- Tag normalization: uppercase → lowercase

**Phase 2: System Views CRUD** ✅
- `POST /api/system-views` - Create view with filter_tags, icon, color
- `GET /api/system-views` - List views with agent_count
- `GET /api/system-views/{id}` - Get single view
- `PUT /api/system-views/{id}` - Partial update
- `DELETE /api/system-views/{id}` - Delete view

**Phase 3: MCP Tools** ⚠️ (code verified, SSE session required for runtime test)
- `list_tags`, `get_agent_tags`, `tag_agent`, `untag_agent`, `set_agent_tags`

**Phase 4: System Manifest Integration** ✅
- `default_tags` applied to all agents in system
- System prefix auto-applied as tag
- Per-agent `tags` applied
- `system_view` section auto-creates view with custom name/icon/color
- Dry run shows `agents_to_create` preview

**Bug Fixes During Testing**:
- Fixed tags.py dependency mismatch (AuthorizedAgentByName → AuthorizedAgent)
- Added agent tag cleanup on agent deletion

## Recent Test Additions (2026-02-17)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_read_only_mode.py` | Read-Only Mode endpoints (CFG-007) | ~27 tests |

Tests cover:
- `GET /api/agents/{name}/read-only` - Read-only status and config
- `PUT /api/agents/{name}/read-only` - Enable/disable with pattern configuration
- Authentication requirements
- System agent protection (cannot enable on trinity-system)
- Default and custom config patterns
- Hook injection response fields
- Permission checks (owner-only modification)
- Input validation (config type, pattern types)

## Recent Test Additions (2026-02-11)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_internal.py` | Internal API endpoints (NEW) | ~7 tests |
| `scheduler_tests/test_service.py` | Activity tracking tests | ~7 tests |
| `test_schedules.py` | Trigger endpoint update | Updated existing |

Tests cover (2026-02-11 Scheduler Consolidation):
- Internal health endpoint
- `POST /api/internal/activities/track` - Create activity records
- `POST /api/internal/activities/{id}/complete` - Complete activities
- `_track_activity_start()` - Backend internal API call (scheduler tests)
- `_complete_activity()` - Backend internal API call (scheduler tests)
- Activity tracking during schedule execution
- Manual trigger with `triggered_by="manual"`
- Error handling when activity tracking fails

## Recent Test Additions (2026-02-05)

| Test File | Description | Tests Added |
|-----------|-------------|-------------|
| `test_execution_termination.py` | Execution termination feature | ~15 tests |

Tests cover:
- `GET /api/agents/{name}/executions/running` - List running executions
- `POST /api/agents/{name}/executions/{execution_id}/terminate` - Stop running executions
- Activity tracking for EXECUTION_CANCELLED events

## Tests Needed (2026-02-18)

### Agent Tags (test_tags.py) - ORG-001

Feature implemented 2026-02-18, pytest tests needed. Should cover:

**Tags CRUD**
- List all tags → returns `{tags: [{tag, count}]}` sorted by count desc
- Get agent tags → returns `{agent_name, tags}` sorted alphabetically
- Add tag → normalizes to lowercase, validates format
- Add duplicate tag → idempotent (no error)
- Replace all tags → clears and sets new tags
- Replace with duplicates → deduplicates
- Clear tags → empty array removes all tags
- Remove tag → removes from agent
- Remove non-existent tag → idempotent (no error)

**Tag Validation**
- Empty tag → 400 error
- Tag >50 chars → 400 error
- Tag with special chars → 400 error
- Tag with uppercase → normalized to lowercase

**Filtering**
- Filter agents by single tag → returns matching agents
- Filter by multiple tags → OR logic (any tag matches)
- Each agent in response includes `tags` array

**Cleanup**
- Agent deletion → tags cleaned up (no orphaned tags)

### System Views (test_system_views.py) - ORG-001

Feature implemented 2026-02-18, pytest tests needed. Should cover:

**Views CRUD**
- Create view with filter_tags, icon, color → returns view with ID
- Create without required fields → 422 error
- List views → includes agent_count, sorted alphabetically
- Get view by ID → returns full details
- Get non-existent view → 404
- Update view (partial) → updated_at changes
- Delete view → 204, removed from list

**Access Control**
- User sees own views + shared views
- User cannot modify others' non-shared views
- Admin can see all views

**Agent Count**
- agent_count reflects agents matching filter_tags
- agent_count updates when tags change

### System Manifest Tags (test_systems.py updates)

Tests for tag integration in system deployment:

**Tag Application**
- `default_tags` applied to all agents
- System prefix auto-applied as tag
- Per-agent `tags` merged with defaults
- Dry run shows tags to be configured

**System View Auto-Creation**
- `system_view` section creates view automatically
- View filters by system prefix + default_tags
- Custom name/icon/color applied
- `system_view_created` in response contains view ID

### Trinity Connect (test_trinity_connect.py)

Feature implemented 2026-02-05, tests not yet written. Should cover:

**WebSocket Authentication**
- Connect with valid MCP API key → receive connected message
- Connect with invalid key → connection closed with code 4001
- Connect without token → connection closed with code 4001

**Event Filtering**
- User receives events only for accessible agents (owned + shared)
- Admin user receives all events
- Events for non-accessible agents are filtered out server-side

**Event Types**
- `agent_activity` events broadcast to filtered listeners
- `agent_started`/`agent_stopped` events broadcast
- `schedule_execution_completed` events broadcast

**Protocol**
- Ping/pong keepalive support
- Refresh command updates accessible agents list

**Database**
- `get_accessible_agent_names()` returns correct agents for user
- `get_accessible_agent_names()` returns all agents for admin

See: `docs/memory/feature-flows/trinity-connect.md` for full specification

## Deprecated Tests (Do Not Run)

### Process Engine (`tests/process_engine/`) — DEPRECATED

The process engine test suite is deprecated and **excluded from all test runs** via `--ignore=process_engine`.

**Location**: `tests/process_engine/`
**Unit tests**: `test_approval_handler`, `test_audit`, `test_authorization`, `test_agent_handler`, `test_alerts`, `test_analytics`, `test_api`, `test_compensation`, `test_cost_tracking`, `test_error_handling`, `test_event_repository`, `test_events`, `test_execution_engine`, `test_execution_repository`, `test_executions_api`, `test_expression_evaluator`, `test_gateway_handler`, `test_informed_notifier`, `test_notification_handler`, `test_output_storage`, `test_process_definition`, `test_process_execution`, `test_repositories`, `test_roles`, `test_schedule_triggers`, `test_sub_process`, `test_sub_process_validation`, `test_templates`, `test_timer_handler`, `test_validator`, `test_value_objects`, `test_webhook_triggers`
**Integration tests**: `test_error_retry`, `test_event_publishing`, `test_execution_lifecycle`, `test_execution_recovery`, `test_gateway_routing`, `test_output_persistence`, `test_parallel_execution`, `test_sequential_execution`, `test_timer_steps`

If you need to run process engine tests explicitly (not recommended):
```bash
python -m pytest process_engine/ -v --tb=short 2>&1
```

---

## Important Notes

1. Tests require the Trinity backend to be running at http://localhost:8000
2. Tests create and delete test agents - ensure no naming conflicts
3. Some tests are slow (marked with @pytest.mark.slow)
4. Agent server direct tests require TEST_AGENT_NAME environment variable
5. Git tests require a git-enabled agent
6. **NEVER run multiple pytest processes simultaneously** - causes resource contention
7. WebSocket tests (Trinity Connect) require `websockets` package and valid MCP API key
8. Trinity Connect tests require at least one agent to be accessible to the test user

## Output Format

Always provide:
1. Clear status of test execution
2. Summary statistics
3. Detailed failure analysis
4. Actionable recommendations
5. Link to generated report file

Be thorough but concise. Focus on actionable insights that help improve the platform.
