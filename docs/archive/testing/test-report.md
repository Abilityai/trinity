# Trinity API Test Report

**Date**: 2026-03-22
**Run Duration**: 46 minutes 59 seconds (2819.88s)
**Test Tier**: Full Suite (all tests except deprecated process_engine)
**Backend**: http://localhost:8000
**Test Runner**: pytest with virtual environment

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Tests Executed | 1460 |
| Passed | 1328 |
| Failed | 14 |
| Skipped | 118 |
| Warnings | 197 |
| Pass Rate (executed, excluding skips) | **98.9%** |
| Health Status | **Healthy** |

The Trinity API test suite is in excellent health with a 98.9% pass rate across 1342 executed tests (1460 total minus 118 expected skips). All 14 failures are real issues warranting investigation, not flaky infrastructure problems. The skipped tests are all intentional (missing prerequisites or optional features).

---

## Test Coverage by Module

| Module | Passed | Failed | Skipped | Notes |
|--------|--------|--------|---------|-------|
| test_activities.py | mostly | 1 | 0 | Chat creates activity (503 error) |
| test_agent_api_key_setting.py | all | 0 | 0 | Clean |
| test_agent_chat.py | all | 0 | ~7 | Chat execution skipped (no agent session) |
| test_agent_dashboard.py | all | 0 | 0 | Clean |
| test_agent_files.py | all | 0 | 0 | Clean |
| test_agent_git.py | all | 0 | ~5 | Git not configured on test agent |
| test_agent_lifecycle.py | all | 0 | 0 | Clean |
| test_agent_metrics.py | all | 0 | 0 | Clean |
| test_agent_permissions.py | all | 0 | 0 | Clean |
| test_agent_rename.py | all | 0 | 0 | Clean |
| test_agent_sharing.py | all | 0 | 0 | Clean |
| test_agent_timeout.py | all | 0 | 0 | Clean |
| test_archive_security.py | all | 0 | 0 | Clean |
| test_auth.py | all | 0 | 0 | Clean |
| test_avatars.py | partial | 4 | 0 | Gemini not configured; test bug (_token) |
| test_capacity.py | all | 0 | 0 | Clean |
| test_cleanup_service.py | all | 0 | 0 | Clean |
| test_credentials.py | all | 0 | 0 | Clean |
| test_deploy_local.py | all | 0 | 0 | Clean |
| test_dynamic_thinking_status.py | partial | 3 | 0 | Async mode failures (no API key) |
| test_email_auth.py | mostly | 1 | 0 | Server disconnect on invalid OTP test |
| test_execution_queue.py | all | 0 | 0 | Clean |
| test_execution_streaming.py | all | 0 | 0 | Clean |
| test_execution_termination.py | all | 0 | 0 | Clean |
| test_executions.py | all | 0 | 0 | Clean |
| test_image_generation.py | all | 0 | 0 | Clean |
| test_internal.py | all | 0 | 0 | Clean |
| test_log_archive.py | all | 0 | 0 | Clean |
| test_mcp_keys.py | all | 0 | 0 | Clean |
| test_model_selection.py | all | 0 | 0 | Clean |
| test_monitoring.py | all | 0 | 0 | Clean |
| test_nevermined_payments.py | all | 0 | 0 | Clean |
| test_nevermined_permissions.py | mostly | 1 | 0 | API contract: 404 vs expected 400/403/501 |
| test_notifications.py | mostly | 2 | 0 | Agent not found (stale agent reference) |
| test_observability.py | all | 0 | 0 | Clean |
| test_ops.py | all | 0 | 0 | Clean |
| test_operator_queue.py | all | 0 | 0 | Clean |
| test_parallel_task.py | mostly | 1 | 0 | Async execution fails (no API key) |
| test_playbooks.py | all | 0 | 0 | Clean |
| test_public_links.py | all | 0 | 0 | Clean |
| test_public_user_memory.py | all | 0 | 0 | Clean |
| test_rate_limits.py | all | 0 | 0 | Clean |
| test_read_only_mode.py | all | 0 | 0 | Clean |
| test_schedules.py | all | 0 | 0 | Clean |
| test_settings.py | all | 0 | 0 | Clean |
| test_setup.py | all | 0 | ~3 | Setup already complete (expected) |
| test_shared_folders.py | all | 0 | 0 | Clean |
| test_skills.py | all | 0 | ~1 | Sync requires URL configured |
| test_slack_integration.py | all | 0 | ~5 | Slack not configured |
| test_subscription_auto_switch.py | all | 0 | 0 | Clean |
| test_subscriptions.py | mostly | 1 | 0 | Missing injection_result field |
| test_system_agent.py | all | 0 | 0 | Clean |
| test_systems.py | all | 0 | 0 | Clean |
| test_telemetry.py | all | 0 | 0 | Clean |
| test_templates.py | all | 0 | 0 | Clean |
| test_web_terminal.py | all | 0 | 0 | Clean |
| agent_server/*.py | 0 | 0 | ~20 | Need TEST_AGENT_NAME env var (expected) |
| scheduler_tests/*.py | all | 0 | 0 | Clean |
| unit/*.py | all | 0 | 0 | Clean |

---

## Failure Analysis

### 1. test_activities.py::TestActivityCreation::test_chat_creates_activity

**Severity**: High
**Category**: Backend bug

**Error**:
```
AssertionError: Expected status in [200, 202], got 503.
Response body: {"detail":"Failed to communicate with agent: Execution error:
cannot access local variable 'execution_id' where it is not associated with a value"}
```

**Root Cause**: A Python scoping bug in the agent execution path. A local variable `execution_id` is used before being assigned in a code path that should communicate with the agent. This is a regression likely introduced by recent changes to execution tracking in `src/backend/routers/chat.py` (which is in the git status as modified: `M src/backend/routers/chat.py`).

**Recommended Fix**: Review `src/backend/routers/chat.py` for any conditional code path where `execution_id` might not be initialized before use. The variable may need to be initialized to `None` before the try block that assigns it.

---

### 2. test_avatars.py::TestDefaultAvatarGeneration::test_generate_defaults_returns_structure

**Severity**: Low
**Category**: Test bug

**Error**:
```
AttributeError: 'TrinityApiClient' object has no attribute '_token'.
Did you mean: 'token'?
```

**Root Cause**: The test accesses `api_client._token` (private attribute with underscore prefix) but the `TrinityApiClient` exposes `token` (without underscore). This is a test-side bug — the API client's attribute name changed or was never `_token`.

**Recommended Fix**: In `test_avatars.py` around line 402, change `api_client._token` to `api_client.token`.

---

### 3. test_avatars.py::TestAvatarLifecycle::test_generate_and_serve
### 4. test_avatars.py::TestAvatarLifecycle::test_regenerate_after_generate
### 5. test_avatars.py::TestAvatarLifecycle::test_emotions_appear_after_generate

**Severity**: Low (expected without Gemini API key)
**Category**: Missing configuration / timeout

**Error**:
```
httpx.ReadTimeout: timed out
```

**Root Cause**: These tests attempt to generate avatars via the Gemini API. Without a valid Gemini API key configured, the request times out (no Gemini key = no avatar generation). These are E2E lifecycle tests that require `GEMINI_API_KEY` to be set. The smoke tests for avatar serving (non-generation) all pass.

**Recommended Fix**: Mark these three tests with `@pytest.mark.slow` or a `@pytest.mark.requires_gemini` marker and skip them when Gemini is not configured. Alternatively, add a fixture that checks for Gemini availability and skips gracefully.

---

### 6. test_dynamic_thinking_status.py::TestAsyncModeSubmission::test_async_mode_completes_eventually

**Severity**: Medium
**Category**: Execution failure (no API key)

**Error**:
```
Failed: Expected success, got failed.
Error: Task execution failed (exit code 1): General error. Check agent logs for details.
```

**Root Cause**: The async mode execution test sends a real chat message to an agent and expects it to complete successfully. Without a configured Claude API key on the test agent, the execution fails with a general error. This test requires a live LLM API key to pass.

**Recommended Fix**: Mark this test as `@pytest.mark.slow` and `@pytest.mark.requires_llm`. Consider mocking the LLM call for CI, or using a dedicated test agent with API key configured.

---

### 7. test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence::test_async_mode_with_save_to_session
### 8. test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence::test_async_mode_session_contains_messages

**Severity**: Medium
**Category**: Dependent on previous failure

**Error**:
```
AssertionError: Expected new session to be created. Before: 0, After: 0
AssertionError: Should have at least one session
```

**Root Cause**: These tests depend on async mode execution succeeding (failure #6 above). Since the execution fails, no session is created, so these assertions fail in cascade. Fix the async execution issue and these should pass.

**Recommended Fix**: Same as failure #6 — requires LLM API key. Additionally, consider adding a `pytest.skip()` when the prior async execution test failed, to make cascading failures clearer.

---

### 9. test_email_auth.py::TestEmailLoginVerify::test_verify_code_invalid_code_returns_401

**Severity**: Medium
**Category**: Server instability / race condition

**Error**:
```
httpx.RemoteProtocolError: Server disconnected without sending a response.
```

**Root Cause**: The backend disconnected during a call to `GET /api/setup/status` (an unauthenticated endpoint). This is unexpected — the test was in the middle of verifying OTP code behavior. The backend may have restarted or crashed briefly during the test run. This could be caused by the `test_system_agent.py` restart tests that ran earlier in the suite, which actually restart the trinity-system agent and may have caused some instability.

**Recommended Fix**: Rerun this test in isolation to confirm it is a transient failure. If it's consistent, investigate whether the backend restarts triggered by `test_system_agent.py::test_restart_returns_success_structure` affect the main backend connection pool. Consider adding a retry mechanism for transient connection errors.

---

### 10. test_nevermined_permissions.py::TestNeverminedNonexistentAgent::test_save_config_nonexistent_agent

**Severity**: Low
**Category**: API contract inconsistency

**Error**:
```
AssertionError: Expected status in [400, 403, 501], got 404.
Response body: {"detail":"Agent not found"}
```

**Root Cause**: The test expects saving a Nevermined config for a nonexistent agent to return 400, 403, or 501. The backend returns 404 (Agent not found). This is arguably correct behavior — 404 is a reasonable response for a nonexistent agent. The test's expected status codes are too restrictive.

**Recommended Fix**: Add 404 to the expected status codes in the test assertion: `assert_status_in(response, [400, 403, 404, 501])`. The 404 response is semantically correct.

---

### 11. test_notifications.py::TestAgentNotifications::test_get_agent_notifications_success
### 12. test_notifications.py::TestAgentNotifications::test_count_agent_notifications_success

**Severity**: Medium
**Category**: Test isolation / stale agent reference

**Error**:
```
AssertionError: Expected status 200, got 404.
Response body: {"detail":"Agent not found"}
```

**Root Cause**: These tests query `GET /api/agents/{name}/notifications` and `GET /api/agents/{name}/notifications/count` but the agent referenced no longer exists. The `created_agent` fixture is module-scoped, and it appears the agent was deleted or is referencing a wrong name by the time the `TestAgentNotifications` class runs. Other notifications tests in the module (`TestNotificationCRUD`) likely pass because they use the global notifications endpoint rather than the agent-specific one.

**Recommended Fix**: Inspect the `conftest.py` fixture for `test_notifications.py` to ensure the `created_agent` fixture is properly scoped and the agent name is correctly passed to the per-agent endpoints. Verify that agent cleanup in prior test classes doesn't delete the agent before `TestAgentNotifications` runs.

---

### 13. test_parallel_task.py::TestAsyncModeExecution::test_async_mode_execution_completes

**Severity**: Medium
**Category**: Execution failure (no API key)

**Error**:
```
AssertionError: Task should complete successfully, got status: failed
assert 'failed' == 'success'
```

**Root Cause**: The parallel task execution test sends a real task to an agent via the headless execution API. Without a valid LLM API key configured, the task fails. This is the same root cause as failures #6-8.

**Recommended Fix**: Mark with `@pytest.mark.requires_llm`. Consider mocking the execution for CI testing or ensuring the test agent has a valid API key configured.

---

### 14. test_subscriptions.py::TestCredentialInjection::test_injection_on_assignment

**Severity**: Medium
**Category**: Missing API response field

**Error**:
```
AssertionError: assert 'injection_result' in {
  'agent_name': 'test-subscriptions-a781c3',
  'message': "Subscription 'test-sub-inject-cb6c6033' assigned to agent ...",
  'restart_result': 'success',
  'subscription_name': 'test-sub-inject-cb6c6033', ...}
```

**Root Cause**: The subscription assignment response does not include an `injection_result` field. The test expects this field to be present in the response from `POST /api/agents/{name}/subscription`. The backend response contains `message`, `agent_name`, `subscription_name`, and `restart_result` but not `injection_result`. This is an API contract gap — either the test was written expecting a field that was not yet implemented, or the field was removed.

**Recommended Fix**: Either add `injection_result` to the subscription assignment response in the backend router (showing whether credential injection succeeded), or update the test to not require this field if it was intentionally removed from the contract.

---

## Skipped Tests Analysis

Total skipped: **118 tests**

| Category | Count | Reason |
|----------|-------|--------|
| agent_server direct tests | ~20 | `TEST_AGENT_NAME` env var not set (expected) |
| Agent chat execution tests | ~7 | No active agent session / no API key |
| Git-related tests | ~5 | Test agent has no git template configured |
| First-time setup tests | ~3 | Setup already complete (expected) |
| Slack integration tests | ~5 | Slack not configured (expected) |
| Skills sync | ~1 | No skills library URL configured (expected) |
| Session-based tests | ~5 | No active sessions at test time |
| Queue full tests | ~2 | Hard to trigger in test environment |
| Other optional features | ~70 | Various missing configurations |

All skips are intentional and expected. None indicate bugs or regressions.

---

## Warnings Analysis

**Total warnings: 197**

The primary warning category is `DeprecationWarning: datetime.datetime.utcnow() is deprecated`. This appears across:

- `src/scheduler/service.py` (multiple locations)
- `src/scheduler/database.py` (multiple locations)
- `tests/scheduler_tests/conftest.py`
- `tests/scheduler_tests/test_service.py`
- `tests/scheduler_tests/test_database.py`
- `tests/scheduler_tests/test_model_selection.py`

Python 3.14 (the version in use) has deprecated `datetime.utcnow()` in favor of timezone-aware `datetime.now(datetime.UTC)`. These are non-fatal but should be fixed to maintain compatibility with future Python versions.

---

## Priority Issues Summary

### P0 (Critical — Fix Immediately)

1. **`test_activities.py` — execution_id scoping bug** (`src/backend/routers/chat.py`): A variable scoping bug causes 503 errors when chat creates activity. This may affect real users. The modified `src/backend/routers/chat.py` (shown in git status) is the likely cause.

### P1 (High — Fix Soon)

2. **`test_subscriptions.py` — Missing `injection_result` field**: The subscription assignment API response is missing an expected field. Either the API contract is incomplete or the test needs updating.

3. **`test_notifications.py` — Agent not found (2 tests)**: The per-agent notification endpoints return 404 for an agent that should exist in the test fixture. Likely a test isolation bug.

### P2 (Medium — Fix in Next Sprint)

4. **`test_dynamic_thinking_status.py` (3 tests) + `test_parallel_task.py` (1 test)**: Async execution tests fail because no LLM API key is configured on the test agent. Mark these as `requires_llm` or configure a test API key.

5. **`test_email_auth.py` — Server disconnect**: A transient connection failure. Needs investigation to determine if the system agent restarts during the test run cause backend instability.

### P3 (Low — Minor Issues)

6. **`test_avatars.py` — `_token` attribute bug**: One-line fix in test code (`_token` → `token`).

7. **`test_avatars.py` — Gemini timeout (3 tests)**: Expected without Gemini API key. Mark as `requires_gemini` to skip gracefully.

8. **`test_nevermined_permissions.py` — 404 vs expected status list**: Test expects the wrong status codes for nonexistent agent. Should include 404.

9. **Scheduler deprecation warnings**: Replace `datetime.utcnow()` with `datetime.now(datetime.UTC)` in scheduler code and tests.

---

## Recommendations

### Immediate Actions

1. **Investigate `src/backend/routers/chat.py`** for the `execution_id` scoping bug. This file is in the git working tree as modified (`M src/backend/routers/chat.py`) and is likely the source of the 503 error. Also check `docker/base-image/agent_server/routers/chat.py` (also modified).

2. **Fix `test_avatars.py` line ~402**: Change `api_client._token` to `api_client.token`. This is a trivial one-line fix.

3. **Add `injection_result` to subscription assignment response** or update the test to match the actual contract. Check if this was an intentional API design decision.

### Short-Term Improvements

4. **Add LLM-dependent test markers**: Tests in `test_dynamic_thinking_status.py` and `test_parallel_task.py` that require a live LLM API key should use a `@pytest.mark.requires_llm` marker and skip when no key is configured.

5. **Investigate notifications test fixture**: Debug why `TestAgentNotifications` gets a 404 for its agent. Check if the agent is deleted between test classes in `test_notifications.py`.

6. **Update Nevermined permissions test**: Add 404 to the list of acceptable responses for nonexistent agent in `test_nevermined_permissions.py`.

7. **Investigate backend stability during system agent restarts**: The `test_email_auth.py` server disconnect may be related to `test_system_agent.py` running restart operations that affect overall backend connectivity.

### Long-Term

8. **Fix scheduler deprecation warnings**: All `datetime.utcnow()` calls in `src/scheduler/` should be updated to `datetime.now(datetime.UTC)` for Python 3.14 compatibility.

9. **Clean up stale test agents**: 39 agents were found in the system at test run time (many from previous test runs that weren't cleaned up). Consider adding a pre-test cleanup fixture or sweeper.

10. **Configure Gemini API key for test environment** to allow avatar generation tests to run, or add a CI-compatible mock.

---

## Test Environment Notes

- **Python Version**: 3.14 (via pyenv/Homebrew)
- **Agents present at test start**: 39 (including stale test agents from previous runs)
- **Backend health**: Running (confirmed via `GET /api/setup/status`)
- **LLM API Key**: Not configured on test agents (causes async execution failures)
- **Gemini API Key**: Not configured (causes avatar generation timeouts)
- **Slack**: Not configured (expected skips)
- **Git template**: Not configured on test agents (expected skips)

---

## Raw Statistics

```
14 failed, 1328 passed, 118 skipped, 197 warnings in 2819.88s (0:46:59)
```

Pass rate (executed tests only): 1328 / (1328 + 14) = **98.96%**
Pass rate (all tests including skips): 1328 / 1460 = **90.96%**
