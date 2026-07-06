# Feature Flow: Parallel Headless Execution

> **Requirement**: 12.1 - Parallel Headless Execution
> **Status**: Implemented
> **Created**: 2025-12-22
> **Updated**: 2026-05-18 (#873: pipe-drop handler ported to refactored `headless_executor.py` after #122 extraction; prior 2026-05-13 #474: original pipe-drop reclassification in both runtimes)
> **Verified**: 2026-02-05

## Revision History

| Date | Changes |
|------|---------|
| 2026-07-04 | **Issue #1444 - fail-loud + owner-gated `save_to_session` persistence**: the CHAT-001 `/task` persistence writer (`_persist_chat_session`, shared by the sync branch and the async `_run_async_task_with_persistence` wrapper; guarded on a SUCCESS terminal) no longer swallows DB errors with a `logger.warning; return None`. It now logs at ERROR with a stack trace (message carries agent + execution_id + exception-type only — never `user_message`/`user_email`/`result.response`, Security F3), does NOT re-raise past a completed, billed turn, and the sync branch surfaces a `chat_persist_failed` marker on the response. The `chat_session_id` branch is owner-checked (`session.user_id == caller`) before appending — a forged/leaked id falls through to `get_or_create_chat_session` for the caller (IDOR fix). Triage (direct call, wrapper with stubbed `execute_task`, and wrapper with the REAL `execute_task` + terminal-CAS row write) confirmed the in-process path is correct on stock config; the #1083 fire-and-forget callback path (which does not persist) is structurally unreachable by a manual `/task` (`ASYNC_DISPATCH_ELIGIBLE_TRIGGERS`={schedule,webhook}), so callback-path persistence is deferred to the pull-mode epic (#1045/#1081). Fast CI guard: `tests/unit/test_1444_chat_session_persistence.py` (6 tests, incl. a `hide_parameters=True` engine-config guard asserting a real `DBAPIError` can't leak bound values through the fail-loud log); the slow `requires_agent` `test_dynamic_thinking_status.py::TestAsyncModeSessionPersistence` tests now assert `success` before demanding a session. Full flow in [authenticated-chat-tab.md](authenticated-chat-tab.md). |
| 2026-05-18 | **Issue #873 - Pipe-drop handler ported to refactored `headless_executor.py`**: After PR #122 extracted `execute_headless_task()` into its own module (`services/headless_executor.py`), the `except (BrokenPipeError, ConnectionResetError)` handler from #474 needed to be present in the new file's outer `try/except` chain. PR #873 applied the same pattern — handler at `headless_executor.py:856-874`, semantically identical to the `gemini_runtime.py:665-676` sibling — ensuring the pipe-drop → HTTP 502 reclassification and INFO-level logging survive the module split. No behaviour change; pure carry-forward of the #474 fix. `src/frontend/src/composables/useProcessWebSocket.js` was deleted as part of this cleanup (composable was no longer used after the SSE streaming surface was consolidated). |
| 2026-05-13 | **Issue #678 - executor refactor + salvage/auto-retry pipeline**: PR #797 breaks the monolithic `docker/base-image/agent_server/services/claude_code.py` (was ~3000 LOC) into focused modules so the headless path can be reasoned about and tested in isolation: `claude_code.py` itself loses 1278 LOC of orchestration; new `headless_executor.py` (+956) owns `execute_headless_task`, `HeadlessRunContext`, `_finalize_headless_result`, `_attempt_empty_result_recovery`, and subprocess lifecycle; new `error_classifier.py` (+465) owns `_classify_empty_result`, `_classify_signal_exit`, `_is_auth_failure_message`, `_is_rate_limit_message`, `_diagnose_exit_failure`, `_recover_metadata_from_raw_messages`; new `jsonl_recovery.py` (+461) hosts the JSONL session-file readers for cost/context/text recovery; new `stream_parser.py` (+448) owns stdout line parsing; new `subprocess_lifecycle.py` (+79), `_runtime_config.py` (+32), `state.py` (+24), `runtime_adapter.py` (+6) split out the remaining concerns. Behaviorally the most load-bearing shift is that `_classify_empty_result` now returns a structured dict body — `{message, metadata, raw_message_count, parse_failure_count, recovery_attempted}` — instead of the prior plain string (was a string until 2026-05-11, see `error_classifier.py:391` docstring); `message` carries the human-readable diagnostic and `metadata` is the sanitized `ExecutionMetadata.model_dump()`, which lets the backend's HTTPError handler salvage cost/context onto the FAILED row (sibling 2026-05-13 update in [task-execution-service.md](task-execution-service.md)) instead of silently zeroing them out as #520 used to. A new shared helper `_attempt_empty_result_recovery(metadata, raw_messages, response_parts, parse_failure_count, parse_failure_sample, task_start_iso, session_id_fallback)` at `headless_executor.py:67-155` is invoked from both async and sync paths and runs a two-step JSONL salvage: (1) call `_classify_empty_result` and return `None` if metadata is already complete so the caller continues on the success path; (2) call `_recover_metadata_from_jsonl(session_id, since_iso, metadata)` to back-fill `cost_usd`, `duration_ms`, `num_turns`, per-call `usage` (input/output/cache_creation/cache_read tokens), and `model_name` in place; (3) if `response_parts` is empty, try `_recover_response_from_jsonl(session_id)` and set `metadata.recovered_from_jsonl = True` on success; (4) on hard failure return a structured `(502, body)` carrying a refreshed metadata snapshot (since JSONL may have populated `cost_usd` even when no response text was recovered). The `session_id_fallback` argument is the UUID we passed to `claude --session-id` (`HeadlessRunContext.claude_session_uuid`), required for the reader-race window where stdout never produced an `init` line and `metadata.session_id` is unset — without it, recovery would silently no-op. Headless tasks whose `timeout_seconds > 600` now auto-enable JSONL persistence (the `persist_session=True` path) so this stdout-race recovery can actually fire on long-running work; those headless-task UUIDs aren't in `agent_sessions`, so they fall out of the session-cleanup keep set automatically and are reaped by the existing `session_cleanup_service` 6h sweep (1h mtime race guard) — no new reaper required, see `architecture.md` (#678 Option B). The JSONL reader hardens path containment: session-file paths under `/root/.claude/projects/...` are resolved via `Path.resolve()` and validated with `Path.is_relative_to()`, and `session_id` is regex-gated to `^[a-zA-Z0-9_-]+$` before path construction so traversal attempts (`../`, absolute paths, NUL bytes, etc.) are rejected at the gate. New `ExecutionMetadata` token-accounting fields `cache_creation_tokens` and `cache_read_tokens` are populated from each assistant message's `usage` dict in `stream_parser.py`; the accounting invariant is that `input_tokens + cache_read_tokens` represents the actual prompt size and cached tokens are NOT added on top (using them as a fallback would double-count). Together with the in-line auto-retry in [task-execution-service.md](task-execution-service.md) (one retry with the same `execution_id`, 300 s cap, rolled-up cost) this closes the symptom class tracked under #520 and the prior drain reorder fix #531, and ticks the executor-refactor box on the orchestration plan (#408). Tests: `tests/unit/test_auto_retry_reader_race.py` covers the auto-retry gate behavior, `tests/unit/test_error_classifier_dict_body.py` pins the dict-body classifier shape, `tests/unit/test_jsonl_metadata_recovery.py` covers metadata recovery plus 12 parametrized hostile-input path-containment cases, `tests/unit/test_empty_result_classification.py` is migrated to the dict-body shape, and `tests/unit/test_session_persistence_flag.py` asserts `effective_persist = persist_session OR timeout_seconds > 600`. |
| 2026-05-13 | **Issue #474 - Subprocess pipe-drop misclassified as HTTP 500 (and triggering SUB-003 auto-switch on the 503 sibling path)**: When the agent-server tried to write a task request to the Claude (or Gemini) subprocess and the child had already exited — typical causes: OAuth/auth abort, permission-mode validation kill, upstream cancellation by the backend's `terminate_execution_on_agent()`, or any race where the child closes stdin before the parent finishes writing — the resulting `BrokenPipeError` / `ConnectionResetError` fell through to the generic `except Exception` clause and surfaced as HTTP 500 with a misleading `[Errno 32] Broken pipe` ERROR log line. The 503 sibling shape of this failure additionally collided with SUB-003 (`services/task_execution_service.py:628`) which interprets any 503 from an agent endpoint as auth-class failure and auto-rotates the subscription — meaning a benign pipe drop could spuriously burn through subscription rotations. Fix: dedicated `except (BrokenPipeError, ConnectionResetError) as pipe_err` handler inserted *before* the generic `except Exception` clause in both runtimes — `docker/base-image/agent_server/services/headless_executor.py:814-832` (Claude path) and `docker/base-image/agent_server/services/gemini_runtime.py:665-676` (Gemini path) — both raise `HTTPException(status_code=502, detail="Agent subprocess closed before task could complete")`. 502 ("Bad Gateway to Claude subprocess") is semantically correct and collision-free: SUB-003's 503-or-auth-string predicate at `task_execution_service.py:628` ignores 502, so 502 flows through as plain FAILED without rotating the subscription. Log level downgraded to `logger.info()` (not `logger.error()`) because the pipe drop is a known benign race — ERROR-level spam misleads operators into hunting non-existent server-side faults. Sibling of #520 (502 for empty result) and #516 (504 for signal exit) — same pattern of "give the symptom a precise HTTP code so backend classification doesn't conflate it with auth failure." Regression tests: `tests/unit/test_headless_executor_pipe_drop.py` (handler behavior, status code, log level, ordering relative to the generic catch-all) and `tests/unit/test_pipe_close_no_auto_switch.py` (asserts SUB-003 does NOT trigger on 502 from the pipe-drop path). |
| 2026-05-12 | **Issue #808 - orphan-killer daemon thread spins at 100% CPU for ~15 min, starving uvicorn health probes and triggering circuit breaker**: Previous fixes (#649, #730, #747) bounded how long the *caller* waits for the orphan scan, but the daemon thread itself continued running at default (normal) scheduling priority. On 1-CPU containers a thread spinning at 100% preempts all other runnable threads — the uvicorn event loop cannot service health probe requests, probes time out repeatedly, and the circuit breaker (#631) opens and stays dormant for ~40 minutes. Two complementary fixes (`subprocess_pgroup.py`): (1) `_set_idle_priority()` — called at the start of `_run_orphan_killer()`, sets the daemon thread to `SCHED_IDLE` (Linux kernel "run only when nothing else wants CPU") via `os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))`; falls back to `os.nice(19)` on non-Linux POSIX (macOS CI); no-op on other platforms. (2) `_scan_deadline = time.monotonic() + _ORPHAN_SCAN_WALL_SECONDS` (8 s) — passed as `_scan_deadline` kwarg to `_kill_orphan_pipe_writers`; per-iteration `time.monotonic() >= _scan_deadline` check in the `/proc` loop aborts scanning after budget expires, bounding total CPU time regardless of individual `readlink()` delays from D-state processes. 8 s is inside the existing 11 s `asyncio.wait_for` ceiling with 3 s margin. `_kill_orphan_pipe_writers` signature changed from `(pipe_read_fd, our_pgid)` to `(pipe_read_fd, our_pgid, _scan_deadline=None)` — keyword-only, backward-compatible. Tests: mock `_slow_orphan_killer` signature updated (`_scan_deadline=None`); new `TestKillOrphanPipeWriters.test_scan_deadline_stops_scan_early` (passes expired deadline, asserts scan aborts in < 1 s); new `TestSetIdlePriority` (does_not_raise, idempotent). |
| 2026-05-09 | **Issue #728 (follow-up) - `os.stat()` on `/proc/pid/fd/N` blocks indefinitely on D-state processes, causing orphan scan to silently miss orphan writers**: `_kill_orphan_pipe_writers` used `os.stat(f"/proc/{pid}/fd/{fd_name}")` to follow the symlink to the pipe inode and then read `/proc/{pid}/fdinfo/{fd_name}` to check write/read flags. Both operations acquire the inode lock at the kernel level; on a D-state (uninterruptible sleep) process this lock may be held indefinitely, causing the entire 10 s orphan-scan daemon thread to exit its cap without ever reaching the orphan writer. Fix (`subprocess_pgroup.py`): replace `os.stat()` + fdinfo with `os.readlink(f"/proc/{pid}/fd/{fd_name}")`. On Linux `/proc/pid/fd/N` is a symlink whose target text is `"pipe:[inode_number]"` — `readlink()` reads this string from proc's own metadata without following the symlink and without acquiring any inode lock, making it safe to call on D-state processes. The fdinfo write/read flag check is removed entirely: an explicit `our_pid = os.getpid()` self-skip is added so the read-end holder is excluded unconditionally; all remaining holders of the target pipe inode outside the killed pgid are by definition orphan writers. Regression test added: `tests/unit/test_subprocess_pgroup.py` — `TestKillOrphanPipeWriters.test_kills_orphan_even_when_stat_raises_dstate_simulation` monkeypatches `os.stat` to raise `OSError` and verifies the orphan is still found and killed via the `readlink` path (would have FAILED against the old implementation). Complements the `_drain_bounded` 90 s cap added in PR #730. |
| 2026-05-08 | **Issue #728 - `safe_close_pipes` deadlock causes 87–91% CPU spin for up to 7200 s when OAuth token is expired**: After #657 made `drain_reader_threads` async, all four sync call sites in `claude_code.py` wrapped it with `asyncio.run(_drain_reader_threads(...))` in the executor thread. If a Claude Max subscription token is expired and no claude child process ever spawns, the reader threads stay alive waiting for EOF on the stdout/stderr pipes. `safe_close_pipes()` (called inside `drain_reader_threads`) tries to acquire Python's `BufferedReader` internal ENTER_BUFFERED lock — but that lock is already held by `readline()` on the reader thread. The result is a permanent deadlock: `asyncio.run()` never returns, the executor thread wedges, and `agent-server.py` spins at ~90% CPU with no progress and no circuit-breaker recovery until the full `execution_timeout_seconds` (up to 7200 s) elapses. Fix (`claude_code.py`): add `_drain_bounded(process, *threads, grace, pgid)` helper — runs `asyncio.run(_drain_reader_threads(...))` inside a daemon thread, signals a `threading.Event` in its `finally` block, and calls `done.wait(timeout=_DRAIN_BUDGET_SECONDS)` (90 s) on the calling thread. If the budget is exceeded, logs a WARNING with `pid` and returns immediately; leaked reader threads are daemon threads and die with the container. Replaced all 4 `asyncio.run(_drain_reader_threads(...))` call sites (chat timeout, chat normal, headless timeout, headless normal) with `_drain_bounded(...)`. Does NOT modify `subprocess_pgroup.py` to minimise regression risk. `_DRAIN_BUDGET_SECONDS = 90` constant added for test monkeypatching. Regression tests: `tests/unit/test_drain_bounded.py` — 4 tests verify budget enforcement, fast completion, constant value, and grace/pgid forwarding. |
| 2026-05-05 | **Issue #657 - `drain_reader_threads` `t.join(30)` deadlocks asyncio event loop permanently when reader thread is stuck**: When `drain_reader_threads` is called and a CPU-heavy orphan MCP subprocess (that called `setsid()`) survives the process-group kill, the orphan consumes 140–180% CPU, starving the asyncio event loop and potentially preventing OS-level thread timeouts from firing. All three `t.join(timeout=N)` calls in `drain_reader_threads` — initial grace join, natural-drain join, and post-force-close join — as well as the orphan-killer `_orphan_thread.join(timeout=10)` could block indefinitely. Fix (`subprocess_pgroup.py`): convert `drain_reader_threads` to `async def`. Replace all `t.join(timeout=N)` with `await asyncio.wait_for(asyncio.to_thread(t.join, N), timeout=N+1)`. Deadlines are now enforced by the asyncio event loop, not by OS thread scheduling. The orphan killer changes from `_orphan_thread.join(timeout=10)` to: start daemon thread, signal a `threading.Event` in a `finally` block, then `await asyncio.wait_for(asyncio.to_thread(event.wait, 10), timeout=11)` — the `event.wait(10)` always returns within 10s so `asyncio.run()`'s `shutdown_default_executor()` never blocks on the slow `/proc` scan. Sync callers in `claude_code.py` (both the chat and headless-task paths, 4 call sites) wrap with `asyncio.run(drain_reader_threads(...))`, which creates a temporary event loop in the executor thread. Tests updated to call `asyncio.run(drain_reader_threads(...))` — all 12 `TestDrainReaderThreads` / `TestDrainOrphanKillerTimeout` tests pass; `test_drain_bounded_when_orphan_killer_blocks` now completes in ~14s instead of 100s. Companion to #649 (orphan killer 10s cap). |
| 2026-05-04 | **Issue #649 - `_kill_orphan_pipe_writers` blocks drain indefinitely on D-state `/proc` entries**: When `drain_reader_threads` falls into the post-kill path (reader still alive after `post_kill_grace`), it calls `_kill_orphan_pipe_writers` inline on the calling thread to kill npx/npm MCP servers that survived `terminate_process_group`. If any process in `/proc` is in D state (uninterruptible sleep — e.g. an NFS hang), `os.listdir(f"/proc/{pid}/fd")` blocks without a timeout, stalling the drain for tens of minutes instead of the expected ~30s. Fix (`subprocess_pgroup.py`): wrap the `_kill_orphan_pipe_writers` call in a daemon thread with `join(timeout=10)`. If the thread is still alive after 10s, log a warning and proceed — the orphan scan is best-effort. Also add wall-clock accounting: record `drain_start = time.monotonic()` when the WARNING fires, then compute `join_timeout = max(1.0, post_kill_grace - elapsed)` so the remaining budget for the reader join is measured from the same wall-clock origin, not from after the orphan scan. The combination means the total post-kill phase is bounded by `max(10, post_kill_grace)` seconds regardless of `/proc` behavior. Regression test: `TestDrainOrphanKillerTimeout.test_drain_bounded_when_orphan_killer_blocks` in `tests/unit/test_subprocess_pgroup.py` — monkeypatches `_kill_orphan_pipe_writers` to sleep 100s and verifies drain completes in < 20s (10s orphan cap + 5s post_kill_grace). |
| 2026-04-27 (backfill) | **Issue #618 - npx MCP server orphans outside claude pgid hold stdout pipe open**: `terminate_process_group(claude_pgid)` kills only processes inside the claude session's pgid. npm → node chains started by `npx`-based MCP servers call `setsid()` on the child, placing it in a new pgid that `os.killpg(claude_pgid, ...)` never touches. The orphan keeps the pipe write-FD open so the kernel never delivers EOF to the reader thread. Fix: after `terminate_process_group` in `drain_reader_threads`, scan `/proc/{pid}/fd` for any write-end holder of the same stdout pipe inode that is *outside* our pgid and send it `SIGKILL`. Identity confirmed via `fstat.st_ino` match + `fdinfo flags` write-bit check. Returns kill count for logging. Added `_kill_orphan_pipe_writers(pipe_read_fd, our_pgid) -> int` to `subprocess_pgroup.py`. (This fix ships as part of the #649 PR since both live in `subprocess_pgroup.py`; #618 is the motivation, #649 is the safety cap.) |
| 2026-04-27 | **Issue #531 - drain_reader_threads closes stdout pipe before reader can drain backlog**: Root cause of the HTTP 502 "Execution completed without a result message" on long agentic tasks (>5 min, dozens of tool calls). In `drain_reader_threads` (`subprocess_pgroup.py`), the old sequence was: kill grandchildren → `safe_close_pipes()` → join reader (2s). Closing the read end of the pipe before the reader finished discarded the kernel pipe buffer — including the final `{"type":"result"}` JSON line written by claude just before exit. This caused `metadata.cost_usd` and `metadata.duration_ms` to stay `None`, which #521's `_classify_empty_result` correctly identified as a 502. Fix: reorder to kill grandchildren → join reader with `post_kill_grace=30s` (natural drain: grandchildren dead → kernel delivers EOF → reader sees buffered tail → exits cleanly) → `safe_close_pipes()` only as a last resort for genuine wedges. Also: `_classify_empty_result(metadata, raw_message_count, raw_messages)` now accepts `raw_messages` list and derives `num_turns` from it when `metadata.num_turns is None` (which is the case when the result line is lost), so the 502 detail shows an honest turn count. `metadata.tool_count` is accumulated per-message during parsing (not from the result line) and remains reliable. New parameter `post_kill_grace=30` added to `drain_reader_threads`; all call sites use the default. Regression test: `test_buffered_data_preserved_after_grandchild_kill` in `tests/unit/test_subprocess_pgroup.py`. Related: #520 (symptom), #521 (502 classification), #523 (FD_CLOEXEC complement, future). |
| 2026-04-26 | **Issue #520 - Empty-result misreported as success**: When the claude subprocess exits cleanly (`return_code == 0`) but the final `{"type":"result"}` JSON line is dropped before the reader thread captures it (typical cause: a child subprocess inherited stdout, kept the pipe open past claude exit, the reader thread leaked, the pgroup unwind closed the pipe and the result line went with it), `metadata.cost_usd` and `metadata.duration_ms` stay `None`. The success path used to return HTTP 200 anyway — agent-server logged "completed successfully" while backend silently reaped the execution as an orphan minutes later, masking the real failure with a misleading "completed on agent but recovered by watchdog" message. Sibling of #516 (signal-kill path). Added `_classify_empty_result(metadata, raw_message_count)` (`claude_code.py:935-980`) consulted *after* the `return_code != 0` block (#516 + auth heuristics) and *before* response building; when both `cost_usd` and `duration_ms` are `None`, raises HTTP 502 with diagnostic context (tool count, turn count, raw_messages, cause hint). Backend's auth classifier only triggers on 503, so 502 flows through as plain FAILED with the helpful detail preserved — no backend changes. The two-field check is conservative: single-field nullability could be a Claude format quirk; both-None is a strong signal that the terminal `result` message never arrived. 9 unit tests in `tests/unit/test_empty_result_classification.py`. |
| 2026-04-26 | **Issue #516 - Signal kills misclassified as auth failure**: External signal terminations of the claude subprocess (timeout SIGKILL, OOM-kill, parent SIGTERM, operator cancel) used to fall into the auth-fallback heuristics at `claude_code.py:1262-1278` and surface as a misleading "Subscription token may be expired" 503 — same shape as #361 but a different exit path. Added `_classify_signal_exit(return_code, metadata)` helper (`claude_code.py:882-931`) consulted *before* the auth heuristics; it matches Python-native signal exits (`return_code < 0`) and shell-encoded forms (`130, 137, 143` for SIGINT/SIGKILL/SIGTERM) and raises HTTP 504 with a clear "killed by SIGKILL/SIGTERM/SIGINT — likely timeout, OOM, or operator cancel" message. Tightened the zero-token heuristic with `return_code > 0` so signal exits cannot reach it. Backend's `task_execution_service.py:542` only flags AUTH on 503, so 504 falls through to the generic FAILED path — no backend changes needed. The bug became routinely reproducible after #61 (PR #326) added backend-driven `terminate_execution_on_agent()` which makes signal-killed claude subprocesses the common case for any timeout. 21 unit tests in `tests/unit/test_signal_exit_classification.py`. |
| 2026-04-26 | **Issue #498 - Sync long-poll on backlog**: Sync `/task` calls (`async_mode=false`) at capacity used to fail terminally with HTTP 429. They now spill to the same persistent backlog (BACKLOG-001) the async path uses and long-poll on the open HTTP connection until the queued execution reaches a terminal status. Total connection hold capped at `2 × effective_timeout` (queue wait + execution). Router (`chat.py:1007-1144`) pre-acquires the slot mirroring the async path, then on at-capacity calls `backlog.enqueue()` followed by `wait_for_sync_terminal()`. New module `services/sync_waiter.py` owns the in-process registry, `signal_sync_waiter()`, and `wait_for_sync_terminal()` (event + 5s DB-poll fallback). The drain reuses `_run_async_task_with_persistence` unchanged — it now calls `signal_sync_waiter` from its `finally` block to wake any sync waiter. See [persistent-task-backlog.md](persistent-task-backlog.md) for the full backlog flow. |
| 2026-04-20 | **Issue #418 - Inter-agent timeout ceiling fix**: Removed hardcoded 600s timeout assumption from the MCP parallel/task path so per-agent `execution_timeout_seconds` (TIMEOUT-001, default 900s, max 7200s) is honored end-to-end. `src/mcp-server/src/tools/chat.ts` — `chat_with_agent` Zod schema no longer applies `.default(600)` to `timeout_seconds`; when callers omit it, `undefined` now flows through to the backend, which resolves the target agent's configured timeout. `src/mcp-server/src/client.ts:563-565` — `client.task()` HTTP fetch ceiling changed from `(timeout_seconds \|\| 600) + 10` to `(timeout_seconds ?? 7200) + 60`, so the fetch client doesn't abort before a long-running agent-configured task finishes. Async mode still uses a fixed 30s HTTP ceiling (unchanged). |
| 2026-04-17 | **Issue #361 - Max-turns error fix**: Fixed max_turns termination being misclassified as authentication failure. Added detection for `terminal_reason="max_turns"` and `subtype="error_max_turns"` in result messages (`claude_code.py:329-336`). Max-turns errors now return HTTP 422 with clear "Task exceeded turn limit" message instead of HTTP 503 "Authentication failure". Also raised `max_turns_task` default from 20 to 50 in both `claude_code.py:52` and `guardrails-baseline.json:65`. |
| 2026-03-26 | **Line number refresh**: Updated all file/line references to match current codebase after upstream shifts (~92 lines in backend `chat.py`, model extraction in `models.py`, agent server reorganisation). |
| 2026-03-11 | **Issue #81 - Default Model for Headless Tasks**: Fixed misleading "token expired" error when agent's `~/.claude/settings.json` contains a model incompatible with the assigned subscription. `execute_headless_task()` now defaults to `model="sonnet"` when model is None (`claude_code.py:768-770`). Added `_is_model_access_error()` helper (`claude_code.py:657-674`) to detect subscription/model access errors. Enhanced `_diagnose_exit_failure()` (`claude_code.py:685-700`) to provide actionable error messages when model access fails. Terminal WebSocket sessions always passed `model=sonnet` via URL param, but headless tasks didn't specify `--model` flag, causing Claude Code to use agent settings which might be incompatible. |
| 2026-03-07 | **ExecutionMetadata Error Fields**: Added `error_type` and `error_message` fields to `ExecutionMetadata` model (`models.py:91-92`). Populated during stream parsing in `claude_code.py` from two sources: `result` messages with `is_error=true` (line 304-311, classifies as `rate_limit` or `execution_error`) and `assistant` messages with `error` field (line 341-349, uses Claude Code's classification directly). Enables the platform to distinguish rate limits from other failures for better error handling (429 vs 503). |
| 2026-03-08 | **Session ID UUID Fix**: Fixed `--session-id` validation failure. Claude Code requires `--session-id` to be a valid UUID but `execution_id` (from `secrets.token_urlsafe(16)`) is a base64url string. Changed `claude_code.py:799` to always generate `uuid.uuid4()` for `--session-id` instead of reusing `execution_id`. The `execution_id` still tracks the task internally. |
| 2026-03-06 | **Session Isolation + Permission Validation**: Fixed bug where headless tasks could run with `permissionMode: "default"` instead of `bypassPermissions`, causing all tool calls to be silently denied. Added `--no-session-persistence` and unique `--session-id` per headless task to prevent session file collision with interactive `/api/chat` sessions. Added `permissionMode` validation on the `init` stream-json message — kills process immediately and returns HTTP 503 if bypass not active. Flags skipped when `resume_session_id` is provided (EXEC-023 needs persistence). |
| 2026-03-04 | **EXEC-024 Service Extraction**: Sync path of `POST /api/agents/{name}/task` refactored. The ~250 lines of inline execution logic (slot acquisition, activity tracking, agent call with retry, sanitization, execution record updates, error handling, slot release) extracted into `TaskExecutionService.execute_task()` in `src/backend/services/task_execution_service.py`. Sync path in `chat.py:810-827` now delegates to the service. `agent_post_with_retry()` moved to the service module and imported back into `chat.py` for use by `/chat` and `_run_async_task_with_persistence`. Async mode path unchanged (still uses `_run_async_task_with_persistence` inline). |
| 2026-03-02 | **MODEL-001 Model Selection**: `model_used` field now recorded on every execution record via `db.create_task_execution(model_used=request.model)`. Backend `chat.py` passes model to execution record. TasksPanel sends `model` in POST body. See [model-selection.md](model-selection.md). |
| 2026-02-17 | **Added PUB-003 use case**: Public Chat Agent Introduction uses `/api/task` to fetch agent intro. Cross-reference to public-agent-links.md added. |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed `DatabaseManager.update_execution_status()` wrapper in `src/backend/database.py:1295-1299` - was missing `claude_session_id` parameter, causing task executions to fail storing session IDs for the "Continue Execution as Chat" feature. The underlying `db/schedules.py:update_execution_status()` (lines 559-610) already supported the parameter. |
| 2026-02-20 | **Chat Session Persistence (CHAT-001)**: `/task` endpoint now supports `save_to_session`, `user_message`, `create_new_session` parameters for authenticated Chat tab. When `save_to_session=true`, messages persist to `chat_sessions` and `chat_messages` tables. Response includes `chat_session_id`. New `db.create_new_chat_session()` method closes existing sessions before creating new one. |
| 2026-02-16 | **Security Fix (Credential Leakage Prevention)**: Agent-side and backend-side credential sanitization now prevents secrets from appearing in execution logs. Agent sanitizes subprocess output via `sanitize_subprocess_line()` and response text via `sanitize_text()` (claude_code.py:563, 611, 922, 996). Backend provides defense-in-depth via `sanitize_execution_log()` and `sanitize_response()` (now called from `TaskExecutionService` in `services/task_execution_service.py:272-278` for sync path, and from `_run_async_task_with_persistence` in `chat.py:489-499` for async path). |
| 2026-02-15 | **Claude Max subscription support**: Headless task execution now supports Claude Max subscription authentication. If user ran `/login` in web terminal, the OAuth session stored in `~/.claude.json` is used instead of requiring `ANTHROPIC_API_KEY`. The mandatory API key check was removed from `execute_headless_task()` in `docker/base-image/agent_server/services/claude_code.py`. |
| 2026-02-12 | **Test fix**: `test_parallel_task_does_not_show_in_queue` (in `tests/test_execution_queue.py`) now uses `async_mode: True` to avoid 30s timeout. The test verifies that parallel tasks bypass the execution queue. |
| 2026-02-05 | **SSE streaming fix**: Documented nginx configuration required for live execution streaming. Added `proxy_buffering off`, `proxy_cache off`, `chunked_transfer_encoding on` directives. Added frontend implementation details using fetch with ReadableStream. |
| 2026-01-30 | **Async mode (fire-and-forget)**: Added `async_mode` parameter for non-blocking execution. Backend spawns background task, returns immediately with `execution_id`. Poll for results. New section documents implementation, use cases, and API. |
| 2026-01-23 | Verified all line numbers against current codebase. Updated file references. |
| 2026-01-12 | Added max_turns parameter for runaway prevention |
| 2025-12-31 | Added execution log persistence details |
| 2025-12-22 | Initial documentation |

## Overview

This feature enables Trinity agents to execute multiple independent tasks in parallel without blocking the main conversation context. It unlocks agent-to-agent orchestration patterns where an orchestrator can delegate to multiple workers simultaneously.

## Problem Statement

**Prior Limitation**: Trinity agents could only process one request at a time due to:
1. Platform-level execution queue (Redis) that serializes requests
2. Container-level execution lock (`asyncio.Lock`)
3. Use of `--continue` flag which requires sequential execution to maintain conversation context

**Solution**: New parallel task mode that bypasses queue and lock, runs stateless (no --continue).

## Two Execution Modes

| Mode | Endpoint | Context | Parallel | Use Case |
|------|----------|---------|----------|----------|
| **Sequential Chat** | `POST /api/agents/{name}/chat` | Maintains conversation | No | Interactive chat, multi-turn reasoning |
| **Parallel Task** | `POST /api/agents/{name}/task` | Stateless | Yes | Agent delegation, batch processing |

## Architecture Diagram

```
                                    +-----------------------------+
                                    |     Sequential Chat         |
User/Agent --> POST /chat --------->|  Execution Queue (Redis)    |--> claude --continue
                                    |  One at a time              |
                                    +-----------------------------+

                                    +-----------------------------+
                                    |     Parallel Task           |
User/Agent --> POST /task --------->|  No queue, no lock          |--> claude -p (headless)
              (can send N)          |  N concurrent allowed       |    (N processes)
                                    +-----------------------------+
```

## Data Flow

### 1. MCP Tool Call (Orchestrator Agent)

```
Orchestrator Agent
       |
       v
+---------------------------------------------+
| chat_with_agent(                            |
|   agent_name: "worker-1",                   |
|   message: "Process file X",                |
|   parallel: true,                           |
|   timeout_seconds: 300                      |
| )                                           |
+---------------------------------------------+
       |
       v
   MCP Server (src/mcp-server)
       |
       v
   client.task(name, message, options)
       |
       v
   POST /api/agents/{name}/task
```

### 2. Backend Processing

```
POST /api/agents/{name}/task
       |
       v
+---------------------------------------------+
| Router (chat.py:652-917)                    |
| 1. Validate agent exists and is running     |
| 2. Create execution record (early, for     |
|    async mode and tracking)                 |
| 3. Broadcast collaboration event (if        |
|    agent-to-agent)                          |
| 4. If async_mode: spawn background task     |
|    via _run_async_task_with_persistence(), return    |
|    immediately                              |
| 5. If sync: delegate to                     |
|    TaskExecutionService.execute_task()      |
|    (NO EXECUTION QUEUE)                     |
+---------------------------------------------+
       |  (sync path)
       v
+---------------------------------------------+
| TaskExecutionService (EXEC-024)             |
| services/task_execution_service.py          |
|                                             |
| 1. Acquire capacity slot                    |
| 2. Track activity start                     |
| 3. Call agent via agent_post_with_retry()   |
| 4. Sanitize response + execution log        |
| 5. Update execution record (success/fail)   |
| 6. Complete activity                        |
| 7. Release slot (finally)                   |
| 8. Return TaskExecutionResult               |
+---------------------------------------------+
       |
       v
+---------------------------------------------+
| Router (post-service)                       |
| - Complete collaboration activity           |
| - Translate failed status to HTTP errors    |
| - Persist to chat session (save_to_session) |
| - Return response                           |
+---------------------------------------------+
       |
       v
   POST http://agent-{name}:8000/api/task
```

### 3. Agent Container Execution

```
POST /api/task
       |
       v
+---------------------------------------------+
| execute_headless_task()                     |
|                                             |
| - NO execution lock acquired                |
| - Build command:                            |
|   claude -p --output-format stream-json     |
|   --verbose --dangerously-skip-permissions  |
|   --no-session-persistence                  |
|   --session-id {unique-per-task}            |
|   [--model X] [--allowedTools Y]            |
|   [--append-system-prompt Z]                |
|   [--max-turns N]                           |
|   [--mcp-config ~/.mcp.json]                |
|                                             |
| - NO --continue flag (stateless)            |
| - Session isolation flags prevent collision |
|   with /api/chat sessions (skipped when     |
|   resume_session_id is provided)            |
| - Validates permissionMode on init message  |
|   → kills process + HTTP 503 if not         |
|   bypassPermissions                         |
| - Parse streaming JSON output               |
| - Return response with session_id           |
+---------------------------------------------+
```

## Key Files

### Agent Server (docker/base-image/agent_server/)

| File | Line | Purpose |
|------|------|---------|
| `models.py` | 77-93 | ExecutionMetadata model (includes `error_type`, `error_message` fields) |
| `models.py` | 199-218 | ParallelTaskRequest, ParallelTaskResponse models |
| `services/claude_code.py` | 729-1030 | execute_headless_task() — always generates UUID for `--session-id` |
| `services/gemini_runtime.py` | 500-642 | execute_headless() for Gemini CLI |
| `services/runtime_adapter.py` | 103-129 | AgentRuntime.execute_headless() interface |
| `routers/chat.py` | 98-146 | POST /api/task endpoint |

### Backend (src/backend/)

| File | Line | Purpose |
|------|------|---------|
| `models.py` | 83-96 | ParallelTaskRequest model with max_turns and async_mode |
| `services/task_execution_service.py` | 43-54 | `TaskExecutionResult` dataclass (status, response, cost, etc.) |
| `services/task_execution_service.py` | 61-97 | `agent_post_with_retry()` HTTP helper with exponential backoff |
| `services/task_execution_service.py` | 104-417 | `TaskExecutionService.execute_task()` — full sync execution lifecycle |
| `routers/chat.py` | 21-24 | Imports `get_task_execution_service` and `agent_post_with_retry` from service |
| `routers/chat.py` | 438-650 | `_run_async_task_with_persistence()` helper for async mode (still inline) |
| `routers/chat.py` | 652-917 | POST /api/agents/{name}/task endpoint |
| `routers/chat.py` | 810-827 | Sync path delegation to `TaskExecutionService.execute_task()` |
| `routers/chat.py` | 842-857 | Translates `TaskExecutionResult.status == "failed"` to HTTP errors |
| `routers/schedules.py` | 464-477 | GET /api/agents/{name}/executions/{id} endpoint (polling) |
| `routers/schedules.py` | 480-520 | GET /api/agents/{name}/executions/{id}/log endpoint |
| `services/agent_client.py` | 201-287 | AgentClient.task() method |

### MCP Server (src/mcp-server/)

| File | Line | Purpose |
|------|------|---------|
| `src/client.ts` | 511-587 | `task()` method with async_mode option. HTTP fetch ceiling: `(timeout_seconds ?? 7200) + 60` for sync, 30s for async (Issue #418). |
| `src/tools/chat.ts` | 132-211 | `chat_with_agent` tool with parallel and async parameters. `timeout_seconds` Zod schema has no default — undefined flows through to backend (Issue #418). |
| `src/tools/chat.ts` | 180-187 | `timeout_seconds` parameter definition (agent-configured fallback). |
| `src/tools/chat.ts` | 188-195 | `async` parameter definition. |

### Sync vs Async Code Paths (EXEC-024)

As of EXEC-024, the sync and async execution paths diverge:

| Aspect | Sync (`async_mode=false`) | Async (`async_mode=true`) |
|--------|---------------------------|---------------------------|
| Execution logic | `TaskExecutionService.execute_task()` in `services/task_execution_service.py` | `_run_async_task_with_persistence()` inline in `routers/chat.py` |
| Slot management | Router pre-acquires (issue #498); service releases in `finally` (`slot_already_held=True`) | Router pre-acquires; background task releases in `finally` |
| At-capacity behavior | Spills to backlog (BACKLOG-001), long-polls on the open HTTP connection until terminal status (issue #498). Total hold ≤ `2 × effective_timeout`. | Spills to backlog, returns HTTP 202 with `execution_id`, caller polls. |
| Activity tracking | Service tracks start/completion internally | Router tracks start; background task completes activities |
| Result handling | Returns `TaskExecutionResult`; router translates to HTTP | Background task updates DB directly |
| HTTP helper | `agent_post_with_retry()` defined in service, called internally | Same function imported from service into `chat.py` |

The router (`chat.py`) still handles: container validation, execution record creation (early), collaboration tracking (WebSocket events), async mode branching, session persistence (`save_to_session`), and translating `TaskExecutionResult.status == "failed"` to HTTP error codes (429/504/503). For sync at-capacity, the router additionally calls `backlog.enqueue()` and `wait_for_sync_terminal()` (services/sync_waiter.py); on wake it either returns the inline result (drain happy path) or reconstructs a minimal `TaskExecutionResult` from the DB row (poll-fallback for non-drain terminal flips).

## API Specifications

### Agent Server: POST /api/task

**Request**:
```json
{
  "message": "string",           // Required: Task to execute
  "model": "sonnet|opus|haiku",  // Optional: Model override
  "allowed_tools": ["Read"],     // Optional: Tool restrictions
  "system_prompt": "string",     // Optional: Additional instructions
  "timeout_seconds": 900,        // Optional: Timeout. If omitted, backend resolves the target
                                 //   agent's configured execution_timeout_seconds (TIMEOUT-001,
                                 //   default 900s / 15 min, max 7200s / 2h).
  "max_turns": 50,               // Optional: Max agentic turns (runaway prevention)
  "execution_id": "uuid"         // Optional: Database execution ID for process registry
}
```

**Response**:
```json
{
  "response": "string",          // Claude's response
  "execution_log": [...],        // Tool calls and results (also saved to DB)
  "metadata": {
    "cost_usd": 0.01,
    "duration_ms": 5000,
    "input_tokens": 1000,
    "output_tokens": 500,
    "tool_count": 3,
    "error_type": null,
    "error_message": null
  },
  "session_id": "uuid",          // Unique per task
  "timestamp": "2025-12-22T...",
  "chat_session_id": "string"    // Only present when save_to_session=true
}
```

**Note**: The `execution_log` is persisted to the `schedule_executions` database table and can be retrieved via `GET /api/agents/{name}/executions/{execution_id}/log`.

### Backend: POST /api/agents/{name}/task

Same request/response as agent server, with additional parameters:

**Backend-Only Request Parameters** (`src/backend/models.py:83-96`):
| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `async_mode` | bool | false | If true, return immediately with execution_id (fire-and-forget) |
| `save_to_session` | bool | false | If true, persist messages to `chat_sessions` table (for authenticated Chat tab) |
| `user_message` | string | null | Original user message without context prefix (for clean session display) |
| `create_new_session` | bool | false | If true, close existing active sessions and create a new one |

**Backend Features**:
- Authentication via JWT token
- Access control validation
- Activity tracking (sync: via `TaskExecutionService`; async: via `_run_async_task_with_persistence`)
- Audit logging
- **Slot management** - Capacity-limited parallel execution via `SlotService` (CAPACITY-001)
- **Execution log persistence** - Full transcript saved to `schedule_executions.execution_log`
- **Credential sanitization** - `sanitize_execution_log()` and `sanitize_response()` applied to all results
- **Process registry** - Passes `execution_id` to agent for termination tracking
- **Chat session persistence** - When `save_to_session=true`, persists to `chat_sessions` and `chat_messages` tables
- **Service delegation** (EXEC-024) - Sync path delegates to `TaskExecutionService.execute_task()`

### Backend: GET /api/agents/{name}/executions/{execution_id}/log

Retrieve the full execution log for any task execution.

**File**: `src/backend/routers/schedules.py:480-520`

**Response**:
```json
{
  "execution_id": "abc123",
  "agent_name": "worker-1",
  "has_log": true,
  "log": [
    {"type": "assistant", "message": {...}},
    {"type": "tool_use", "name": "Read", "input": {...}},
    {"type": "tool_result", "content": [...]}
  ],
  "started_at": "2025-12-31T10:00:00Z",
  "completed_at": "2025-12-31T10:01:00Z",
  "status": "success"
}
```

### MCP Tool: chat_with_agent

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| agent_name | string | required | Target agent |
| message | string | required | Task to execute |
| parallel | boolean | false | Enable parallel mode |
| model | string | null | Model override (parallel only) |
| allowed_tools | string[] | null | Tool restrictions (parallel only) |
| system_prompt | string | null | Additional instructions (parallel only) |
| timeout_seconds | number | agent-configured (default 900s, max 7200s) | Execution timeout in seconds (parallel only). If omitted, backend resolves target agent's `execution_timeout_seconds` (TIMEOUT-001). Issue #418 removed the prior hardcoded 600s MCP ceiling. |
| max_turns | number | null | Max agentic turns for runaway prevention (parallel only) |
| async | boolean | false | Fire-and-forget mode - return immediately with execution_id (parallel only) |

## Execution Error Classification

**Added**: 2026-03-07

The `ExecutionMetadata` model (`docker/base-image/agent_server/models.py:77-93`) includes two fields for error classification:

| Field | Type | Description |
|-------|------|-------------|
| `error_type` | `Optional[str]` | Error classification (e.g., `"rate_limit"`, `"execution_error"`) |
| `error_message` | `Optional[str]` | Human-readable error message from Claude Code |

### How Errors Are Classified

Errors are detected during stream-json parsing in `docker/base-image/agent_server/services/claude_code.py`:

**Source 1 -- `result` message with `is_error=true`** (line 304-311):
```python
if msg.get("is_error") and not metadata.error_type:
    metadata.error_message = result_text
    if _is_rate_limit_message(result_text):
        metadata.error_type = "rate_limit"
    else:
        metadata.error_type = "execution_error"
```

**Source 2 -- `assistant` message with `error` field** (line 341-349):
```python
if msg_type == "assistant" and msg.get("error"):
    metadata.error_type = msg["error"]  # e.g., "rate_limit"
    # Extract error text from content blocks
    for block in error_content:
        if isinstance(block, dict) and block.get("type") == "text":
            metadata.error_message = block.get("text", "")
```

### How Error Type Drives HTTP Status Codes

After execution completes, `error_type` determines the HTTP response:

| `error_type` | HTTP Status | Handler Location |
|--------------|-------------|------------------|
| `"rate_limit"` | 429 | `claude_code.py:583-589` (chat), `claude_code.py:967-973` (headless) |
| `"execution_error"` | 503 | Falls through to non-zero return code handling |
| `null` | 200 | Normal success path |

**Pipe-drop reclassification (#474 / #873):** When the subprocess stdin write fails because the Claude child already exited (`BrokenPipeError` / `ConnectionResetError`), both `headless_executor.py` (lines 856–874) and `gemini_runtime.py` (lines 665–676) raise HTTP **502** instead of falling through to the generic `except Exception` → 500 path. 502 ("Bad Gateway to Claude subprocess") is semantically correct and avoids the SUB-003 auto-switch predicate in `task_execution_service.py:628`, which only triggers on 503. Log level for this path is `logger.info` (not `logger.error`) because the cause is a benign race (auth abort, permission-mode kill, upstream backend cancel), not a server-side fault.

The `_format_rate_limit_error()` helper (`claude_code.py:674-683`) uses `metadata.error_message` to build an actionable error detail string that suggests resolution steps (wait for reset, set API key, or reassign subscription).

## Key Differences: Chat vs Task

| Aspect | Chat (Sequential) | Task (Parallel) |
|--------|-------------------|-----------------|
| Endpoint | POST /api/chat | POST /api/task |
| Execution Queue | Yes (Redis) | No |
| Execution Lock | Yes (asyncio.Lock) | No |
| --continue flag | Yes | No |
| Conversation context | Maintained | Stateless |
| Session updates | Yes | No |
| Concurrent requests | 1 per agent | N per agent |
| Use case | Interactive chat | Batch processing |

## Runaway Prevention: max_turns Parameter

**Added**: 2026-01-12

The `max_turns` parameter limits the number of agentic turns an agent can take before the CLI exits. This prevents runaway agents that get stuck in infinite loops or continue executing far beyond expected scope.

### How It Works

When `max_turns` is specified:

1. **Agent Server** passes `--max-turns N` to the Claude Code or Gemini CLI command
2. **CLI** counts each agentic turn (tool use + tool result cycle)
3. **At limit**: CLI exits with an error, returning partial results
4. **Response**: Includes whatever work was completed before the limit

### Implementation Details

**Claude Code** (`docker/base-image/agent_server/services/claude_code.py:823-825`):
```python
if max_turns is not None:
    cmd.extend(["--max-turns", str(max_turns)])
    logger.info(f"[Headless Task] Limiting to {max_turns} agentic turns")
```

**Gemini CLI** (`docker/base-image/agent_server/services/gemini_runtime.py:558-560`):
```python
if max_turns is not None:
    cmd.extend(["--max-turns", str(max_turns)])
    logger.info(f"[Headless Task {session_id}] Limiting to {max_turns} agentic turns")
```

### Usage Examples

**Conservative limit for simple tasks**:
```json
{
  "message": "Read the README.md file and summarize it",
  "max_turns": 5
}
```

**Higher limit for complex analysis**:
```json
{
  "message": "Analyze this codebase and create a report",
  "max_turns": 50,
  "timeout_seconds": 1800
}
```

**Combined with tool restrictions**:
```json
{
  "message": "Search for security vulnerabilities",
  "allowed_tools": ["Read", "Grep", "Glob"],
  "max_turns": 100,
  "timeout_seconds": 3600
}
```

### Recommended Values

| Task Type | Recommended max_turns | Rationale |
|-----------|----------------------|-----------|
| Simple queries | 5-10 | Few file reads needed |
| Code analysis | 25-50 | Multiple file traversals |
| Refactoring | 50-100 | Many edit operations |
| Deep research | 100-200 | Extensive exploration |
| Unlimited | null (default) | Trusted orchestrators only |

### When to Use

- **Always** for user-triggered parallel tasks
- **Always** for scheduled tasks without human supervision
- **Recommended** for agent-to-agent delegation
- **Optional** for interactive chat (human can cancel)

### Relationship with timeout_seconds

Both parameters provide safety limits:

| Parameter | Protects Against | Behavior at Limit |
|-----------|-----------------|-------------------|
| `timeout_seconds` | Wall-clock time exceeded | Process killed, HTTP 504 |
| `max_turns` | Too many agentic operations | CLI exits gracefully, HTTP 422 |

Use both together for comprehensive protection:
```json
{
  "message": "Complex task",
  "max_turns": 50,
  "timeout_seconds": 900
}
```

## Async Mode (Fire-and-Forget)

**Added**: 2026-01-30

Async mode enables non-blocking task execution where the API returns immediately with an `execution_id` that can be polled later for results.

### How It Works

When `async_mode=true` is specified:

1. **Backend acquires capacity slot** (CAPACITY-001)
2. **Backend creates execution record** with `status="running"`
3. **Tracks activity** (CHAT_START with async_mode: true)
4. **Spawns background task** via `asyncio.create_task(_run_async_task_with_persistence(...))`
5. **Returns immediately** with `execution_id` for polling
6. **Background task executes** the Claude Code command (via `agent_post_with_retry`)
7. **Updates execution record** when complete (success/failed)
8. **Completes activities** asynchronously
9. **Releases slot** in `finally` block

Note: Async mode on the `/api/agents/{name}/task` endpoint does **not** use `TaskExecutionService`. It uses the inline `_run_async_task_with_persistence()` function because it needs to manage its own activity IDs, collaboration tracking, and slot release timing. However, async mode on `/api/internal/execute-task` (used by the scheduler) **does** use `TaskExecutionService` inside its background coroutine — see [scheduler-service.md](scheduler-service.md).

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  Async Mode Flow                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Orchestrator                                                    │
│       │                                                          │
│       ├── POST /api/agents/worker/task {async_mode: true}       │
│       │                                                          │
│       v                                                          │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Backend (chat.py — async branch)                        │    │
│  │                                                          │    │
│  │  1. Acquire capacity slot (CAPACITY-001)                 │    │
│  │  2. Create execution record (status="running")           │    │
│  │  3. Track activities (CHAT_START)                        │    │
│  │  4. asyncio.create_task(_run_async_task_with_persistence(...))   │    │
│  │  5. Return immediately:                                  │    │
│  │     { status: "accepted", execution_id: "abc123" }       │    │
│  └──────────────────────────┬──────────────────────────────┘    │
│                             │                                    │
│       │<────────────────────┘  (immediate response)              │
│       │                                                          │
│       │  Poll: GET /api/agents/worker/executions/abc123          │
│       │                                                          │
│                        (Background task runs independently)      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  _run_async_task_with_persistence()                               │   │
│  │                                                           │   │
│  │  - Calls agent container /api/task                        │   │
│  │  - On success: db.update_execution_status("success")      │   │
│  │  - On failure: db.update_execution_status("failed")       │   │
│  │  - Completes activities                                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Implementation Details

**Backend Model** (`src/backend/models.py:91`):
```python
class ParallelTaskRequest(BaseModel):
    # ... existing fields ...
    async_mode: Optional[bool] = False  # If true, return immediately with execution_id
    files: Optional[List[WebFileUpload]] = None  # File attachments (#364)
```

**File upload** (#364): `execute_parallel_task` decodes files synchronously (before async/sync fork)
via `upload_service.process_file_uploads()`. Images become vision blocks in `execute_task(images=)`;
text files are written to `/home/developer/uploads/{user_id}/` before the execution ID is returned.

**Background Task Handler** (`src/backend/routers/chat.py:608-800`):
```python
async def _run_async_task_with_persistence(
    agent_name: str,
    request: ParallelTaskRequest,
    execution_id: str,
    collaboration_activity_id: Optional[str],
    x_source_agent: Optional[str],
    user_id: Optional[int] = None,
    user_email: Optional[str] = None,
    subscription_id: Optional[str] = None,
    is_self_task: bool = False,
    self_task_activity_id: Optional[str] = None,
    images: Optional[list] = None,  # (#364) pre-decoded vision blocks
):
    """
    Async /task background wrapper (issue #95).

    Delegates the full execution lifecycle to TaskExecutionService (single
    path for slot / activity / sanitization / retry / release with
    `slot_already_held=True`) and layers on chat-endpoint-specific post-task
    side effects: chat session persistence (THINK-001), `chat_response_ready`
    WebSocket broadcast, collaboration activity completion, and SELF-EXEC-001
    `inject_result` handling.
    """
    # Delegate slot+activity+execution lifecycle to the service
    result = await task_service.execute_task(
        agent_name=agent_name,
        message=request.message,
        execution_id=execution_id,
        slot_already_held=True,  # caller (router or backlog drain) pre-acquired
        parent_activity_id=collaboration_activity_id,
        # ... other passthrough fields
    )

    # Post-task: chat session persistence, WS broadcast, collab completion,
    # self-task activity completion + result injection (if is_self_task).
```

**Endpoint Logic — Async branch** (`src/backend/routers/chat.py:735-808`):
```python
# Async mode: acquire slot here, spawn background task which releases it
if request.async_mode:
    # Acquire slot (CAPACITY-001), track activity, update status to "running"
    # ... slot acquisition, activity tracking ...

    # Spawn background task (slot will be released when task completes)
    asyncio.create_task(
        _run_async_task_with_persistence(
            agent_name=name,
            request=request,
            execution_id=execution_id,
            task_activity_id=task_activity_id,
            collaboration_activity_id=collaboration_activity_id,
            x_source_agent=x_source_agent,
            release_slot=True,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username
        )
    )

    # Return immediately with execution_id for polling
    return {
        "status": "accepted",
        "execution_id": execution_id,
        "agent_name": name,
        "message": "Task accepted. Poll GET /api/agents/{name}/executions/{execution_id} for results.",
        "async_mode": True
    }

# ---- Sync mode: delegate to TaskExecutionService (EXEC-024) ----
task_execution_service = get_task_execution_service()
result = await task_execution_service.execute_task(
    agent_name=name,
    message=request.message,
    triggered_by=triggered_by,
    source_user_id=current_user.id,
    source_user_email=current_user.email or current_user.username,
    source_agent_name=x_source_agent,
    # ... additional params ...
    execution_id=execution_id,
)

# Translate TaskExecutionResult.status == "failed" to HTTP errors
if result.status == "failed":
    if "at capacity" in (result.error or ""):
        raise HTTPException(status_code=429, ...)
    elif "timed out" in (result.error or ""):
        raise HTTPException(status_code=504, ...)
    else:
        raise HTTPException(status_code=503, ...)
```

**MCP Client** (`src/mcp-server/src/client.ts:511-587`):
```typescript
async task(name: string, message: string, options?: {
  // ... existing options ...
  async_mode?: boolean;  // If true, return immediately with execution_id
}, sourceAgent?: string) {
  // Async mode returns immediately; sync mode waits for full execution.
  // Issue #418: When timeout_seconds is omitted, backend resolves the target
  // agent's configured execution_timeout_seconds (TIMEOUT-001, max 7200s).
  // The HTTP fetch ceiling must cover the platform's max per-agent timeout
  // plus a buffer, so fetch doesn't abort before the backend finishes.
  const timeout = options?.async_mode
    ? 30
    : (options?.timeout_seconds ?? 7200) + 60;
  // ...
}
```

Note: Previously this ceiling was `(timeout_seconds || 600) + 10`, which capped inter-agent task duration at ~10 minutes and ignored per-agent `execution_timeout_seconds` whenever the caller omitted the parameter. Issue #418 lifted the ceiling to cover the platform max (7200s / 2h) with a 60s buffer.

**MCP Tool** (`src/mcp-server/src/tools/chat.ts:187-195`):
```typescript
async: z
  .boolean()
  .optional()
  .default(false)
  .describe(
    "If true, return immediately with execution_id (fire-and-forget). " +
    "Only applies when parallel=true. Poll the execution endpoint for results."
  ),
```

### API Specification

**Request**:
```json
{
  "message": "Process large dataset",
  "async_mode": true,
  "model": "sonnet",
  "timeout_seconds": 3600
}
```

**Immediate Response** (HTTP 200):
```json
{
  "status": "accepted",
  "execution_id": "abc123xyz789",
  "agent_name": "worker-1",
  "message": "Task accepted. Poll GET /api/agents/{name}/executions/{execution_id} for results.",
  "async_mode": true
}
```

**Polling Endpoint**: `GET /api/agents/{name}/executions/{execution_id}`

**Response when running**:
```json
{
  "id": "abc123xyz789",
  "agent_name": "worker-1",
  "status": "running",
  "message": "Process large dataset",
  "triggered_by": "manual",
  "started_at": "2026-01-30T10:00:00Z",
  "completed_at": null,
  "response": null,
  "cost": null
}
```

**Response when complete**:
```json
{
  "id": "abc123xyz789",
  "agent_name": "worker-1",
  "status": "success",
  "message": "Process large dataset",
  "triggered_by": "manual",
  "started_at": "2026-01-30T10:00:00Z",
  "completed_at": "2026-01-30T10:05:00Z",
  "response": "Dataset processed successfully. 10,000 records analyzed.",
  "cost": 0.15,
  "context_used": 50000,
  "context_max": 200000
}
```

### Use Cases

#### 1. Fan-Out Pattern
Spawn multiple long-running tasks without blocking:

```python
# Orchestrator spawns 10 workers, doesn't wait
execution_ids = []
for i, batch in enumerate(data_batches):
    result = mcp__trinity__chat_with_agent(
        agent_name=f"worker-{i}",
        message=f"Process batch: {batch}",
        parallel=true,
        async=true
    )
    execution_ids.append(result["execution_id"])

# Continue with other work while workers process...

# Later: collect results
for agent_name, exec_id in zip(worker_names, execution_ids):
    result = poll_execution(agent_name, exec_id)  # Helper to wait for completion
    results.append(result)
```

#### 2. Background Analysis
Trigger analysis that runs independently:

```python
# Start long-running analysis
result = mcp__trinity__chat_with_agent(
    agent_name="analysis-agent",
    message="Analyze codebase for security vulnerabilities",
    parallel=true,
    async=true,
    timeout_seconds=7200  # 2 hours
)

# Don't wait - check results later via UI or API
print(f"Analysis started: {result['execution_id']}")
```

#### 3. Scheduled Cleanup
System agent triggers maintenance without blocking:

```python
# Nightly cleanup across fleet
for agent in get_all_agents():
    mcp__trinity__chat_with_agent(
        agent_name=agent,
        message="Run nightly cleanup: clear temp files, rotate logs",
        parallel=true,
        async=true
    )
# All cleanup jobs run concurrently, system agent continues
```

### Key Differences: Sync vs Async

| Aspect | Sync (async_mode=false) | Async (async_mode=true) |
|--------|------------------------|-------------------------|
| Return time | After task completes | Immediately |
| Response | Full result with execution_log | execution_id only |
| HTTP timeout (MCP client → backend) | `timeout_seconds + 60` when explicit; `7200 + 60` (2h + 60s buffer) fallback when omitted (Issue #418) | 30 seconds (unchanged) |
| Result retrieval | In response | Poll endpoint |
| Error handling | Exception in response | Poll for error status |
| Connection held | Yes, for duration | No, released immediately |

### Limitations and Considerations

1. **No streaming in async mode**: Cannot use SSE streaming with async; poll for final result only
2. **Orphaned tasks**: If backend restarts, background tasks are lost (execution stays "running")
3. **No cancellation via async**: Must use separate termination endpoint
4. **Database required**: execution_id comes from database record; fails if DB unavailable
5. **Activity completion async**: Activities complete after task finishes, not at request time

---

## Live Execution Streaming

Task executions can be monitored in real-time via Server-Sent Events (SSE).

### Endpoint

**Backend**: `GET /api/agents/{name}/executions/{execution_id}/stream`
**File**: `src/backend/routers/chat.py:1496-1563`

**Agent Server**: `GET /api/executions/{execution_id}/stream`
**File**: `docker/base-image/agent_server/routers/chat.py:302-369`

### nginx Configuration (Required for Production)

SSE streaming requires nginx proxy buffering to be disabled. Without this, nginx buffers the SSE response and events don't stream in real-time.

**File**: `src/frontend/nginx.conf`

```nginx
location /api/ {
    proxy_pass http://trinity-backend:8000/api/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_cache_bypass $http_upgrade;
    proxy_read_timeout 86400;

    # SSE (Server-Sent Events) support - REQUIRED for live execution streaming
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}
```

**Key SSE directives**:
| Directive | Purpose |
|-----------|---------|
| `proxy_buffering off` | Disable response buffering so events stream immediately |
| `proxy_cache off` | Prevent caching of SSE stream |
| `chunked_transfer_encoding on` | Enable chunked transfer for streaming |

### Frontend Implementation

**File**: `src/frontend/src/views/ExecutionDetail.vue:519-578`

The frontend uses `fetch` with `ReadableStream` instead of `EventSource` because:
1. `EventSource` doesn't support custom headers (needed for JWT auth)
2. `fetch` with `ReadableStream` allows proper SSE parsing with auth

```javascript
fetch(url, {
  headers: {
    'Authorization': `Bearer ${authStore.token}`,
    'Accept': 'text/event-stream'
  }
}).then(response => {
  const reader = response.body.getReader()
  // Process SSE stream...
})
```

### Process Registry

The process registry (`docker/base-image/agent_server/services/process_registry.py`) enables:
- Tracking running executions
- Terminating executions
- Live log streaming via pub/sub

### SSE Message Format

```
data: {"type": "init", "execution_id": "abc123", "status": "running"}

data: {"type": "assistant", "message": {...}, "timestamp": "..."}

data: {"type": "tool_use", "name": "Read", "input": {...}}

data: {"type": "tool_result", "content": [...]}

data: {"type": "stream_end", "status": "success"}
```

## Testing

Tests are in `tests/test_parallel_task.py`:

| Test Class | Purpose |
|------------|---------|
| TestParallelTaskEndpoint | Endpoint existence, 404/503 handling |
| TestParallelTaskResponse | Response format validation |
| TestParallelTaskOptions | Model, timeout parameter handling |
| TestParallelTaskStateless | Verifies no context pollution |
| TestParallelExecution | Multiple concurrent tasks |

Run tests:
```bash
cd tests && pytest test_parallel_task.py -v
```

### Related Tests in Other Files

**`tests/test_execution_queue.py`**:
- `TestQueueWithParallelTasks::test_parallel_task_does_not_show_in_queue` - Verifies parallel tasks bypass the execution queue. Uses `async_mode: True` to return immediately (avoids waiting for task completion which could timeout).

## Use Cases

### 1. Orchestrator-Worker Pattern
```
Orchestrator spawns 5 parallel workers:
  +-> Worker 1: "Process file A" (parallel=true)
  +-> Worker 2: "Process file B" (parallel=true)
  +-> Worker 3: "Process file C" (parallel=true)
  +-> Worker 4: "Process file D" (parallel=true)
  +-> Worker 5: "Process file E" (parallel=true)

All 5 execute concurrently, no queue blocking.
```

### 2. Batch Processing
```
System Agent runs nightly batch:
  for each agent in fleet:
    chat_with_agent(agent, "Run health check", parallel=true)
```

### 3. Research Tasks
```
Orchestrator gathers info from multiple sources:
  +-> Research Agent 1: "Find info about X" (parallel=true)
  +-> Research Agent 2: "Find info about Y" (parallel=true)
  +-> Research Agent 3: "Find info about Z" (parallel=true)

Combine results after all complete.
```

### 4. Public Chat Agent Introduction (PUB-003)
```
Public user opens /chat/{token}
  +-> Backend calls agent's /api/task endpoint
  +-> Prompt: "Introduce yourself in 2 paragraphs"
  +-> Agent responds with personalized introduction
  +-> Response displayed as first chat message

See: public-agent-links.md (PUB-003 section)
```

## Authentication (Updated 2026-02-15)

Claude Code uses whatever authentication is available in the agent container:

1. **OAuth session** (Claude Pro/Max subscription): If user ran `/login` in web terminal, session stored in `~/.claude.json` is used
2. **API key**: If `ANTHROPIC_API_KEY` environment variable is set (platform key or user-injected)

The mandatory `ANTHROPIC_API_KEY` check was removed from `execute_headless_task()` to support Claude Max subscriptions. This means:
- Agents with "Authenticate in Terminal" setting can run parallel tasks after user logs in via web terminal
- Scheduled tasks and MCP-triggered executions use the subscription instead of API billing
- API key is still supported for agents that have it configured

**Implementation**: `docker/base-image/agent_server/services/claude_code.py:586-590` - Removed mandatory API key check

## Model Selection and Defaulting (Issue #81)

**Added**: 2026-03-11

### Problem

When using Claude Max/Pro subscriptions, headless task execution could fail with a misleading "Subscription token may be expired or revoked" error. The root cause was model incompatibility:

- **Terminal WebSocket sessions**: Always pass `model=sonnet` via URL parameter, ensuring subscription compatibility
- **Headless task execution**: Did not specify `--model` flag when model was None, causing Claude Code to use the agent's `~/.claude/settings.json` which might contain an incompatible model (e.g., `claude-3-opus-20240229`)

### Solution

Both `execute_headless_task()` and `execute_claude_code()` now default to `model="sonnet"` when no model is specified:

**Headless tasks** (Issue #81) — `docker/base-image/agent_server/services/claude_code.py:746-748`:
```python
# Issue #81: Default to "sonnet" when model is not specified.
if model is None:
    model = "sonnet"
    logger.debug("[Headless Task] No model specified, defaulting to 'sonnet' for subscription compatibility")
```

**Chat sessions** (Issue #138) — `docker/base-image/agent_server/services/claude_code.py:463-465`:
```python
# Issue #138: Default to "sonnet" when no model is specified and none is set on state.
if not model and not agent_state.current_model:
    model = "sonnet"
    logger.debug("[Chat] No model specified, defaulting to 'sonnet' for subscription compatibility")
```

### Error Detection

Added `_is_model_access_error()` helper to detect model access/subscription tier errors:

**File**: `docker/base-image/agent_server/services/claude_code.py:623-637`
```python
def _is_model_access_error(text: str) -> bool:
    """Check if a message indicates a model access/subscription tier error."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "model is not available",
        "not available on your subscription",
        "don't have access to",
        "model not found",
        "invalid model",
        "model access",
        "cannot access",
        "not supported by your plan",
    ])
```

Enhanced `_diagnose_exit_failure()` to provide actionable error messages:

**File**: `docker/base-image/agent_server/services/claude_code.py:657-663`
```python
if metadata and metadata.error_message and _is_model_access_error(metadata.error_message):
    return (
        f"Model access error: {metadata.error_message}. "
        f"The agent's configured model may not be available with the current subscription. "
        f"Try using a different model (sonnet, opus) or check subscription settings."
    )
```

### Model Precedence

1. **Explicit `model` parameter** (from request) — always used if provided
2. **Default `"sonnet"`** — used when model is None (ensures subscription compatibility)
3. **Agent settings** (`~/.claude/settings.json`) — only used if Claude Code ignores `--model` flag

## Security Considerations

1. **Access Control**: Same rules as chat - owner, shared, or admin
2. **Rate Limiting**: Consider implementing concurrency limits
3. **Audit Trail**: All parallel tasks logged with user attribution
4. **Tool Restrictions**: `allowed_tools` parameter for sandboxing
5. **Timeout**: Hard limit (`timeout_seconds`) prevents wall-clock runaway
6. **Turn Limit**: `max_turns` prevents infinite agentic loops
7. **Session Isolation**: `--no-session-persistence` + unique `--session-id` (always a UUID — Claude Code rejects non-UUID values) prevent headless tasks from interfering with interactive sessions or each other via shared `~/.claude/projects/` state
8. **Permission Mode Validation**: `permissionMode` in init message validated immediately — process killed and HTTP 503 returned if `bypassPermissions` not active, preventing silent hour-long timeouts with zero work completed

## Chat Session Persistence (CHAT-001)

**Added**: 2026-02-20

The `/task` endpoint now supports optional chat session persistence for the authenticated Chat tab (`ChatPanel.vue`). This enables multi-turn conversations via headless execution while maintaining session history.

### How It Works

When `save_to_session=true` is passed (`src/backend/routers/chat.py:766-807`):

1. **Session Management**:
   - If `create_new_session=true`: Close existing active sessions and create a new one (`db.create_new_chat_session()`)
   - Otherwise: Get or create session for this user+agent (`db.get_or_create_chat_session()`)

2. **Message Persistence**:
   - User message added to `chat_messages` table (uses `user_message` if provided, otherwise `message`)
   - Assistant response added with cost/context metadata

3. **Response Augmentation**:
   - Response includes `chat_session_id` for frontend session tracking

### Database Operations

**File**: `src/backend/db/chat.py:223-263`

**New Method: `create_new_chat_session()`** (lines 233-263):
```python
def create_new_chat_session(self, agent_name: str, user_id: int, user_email: str) -> ChatSession:
    """
    Create a new chat session, closing any existing active sessions for this user+agent.
    Use this when user explicitly wants a new conversation (e.g., "New Chat" button).
    """
    # 1. Close existing active sessions
    # 2. Create new session with token_urlsafe(16) ID
    # 3. Return new ChatSession
```

### Frontend Usage

**File**: `src/frontend/src/components/ChatPanel.vue:299-324`

```javascript
// Send via task endpoint with session persistence
const response = await axios.post(`/api/agents/${props.agentName}/task`, {
  message: contextPrompt,      // Full message with conversation context
  save_to_session: true,       // Persist to chat_sessions table
  user_message: userMessage,   // Original message (for clean session display)
  create_new_session: !currentSessionId.value  // New session if none active
}, { headers: authStore.authHeader })

// Update session ID from response
if (response.data.chat_session_id) {
  currentSessionId.value = response.data.chat_session_id
}
```

### Use Case: Chat Tab

The authenticated Chat tab (`ChatPanel.vue`) uses this to:
1. Track conversations in session dropdown (survives page refresh)
2. Allow switching between past sessions
3. Support "New Chat" button to start fresh conversation
4. Maintain Dashboard activity visibility (unlike Terminal which uses PTY)

See [authenticated-chat-tab.md](authenticated-chat-tab.md) for full Chat tab documentation.

## Future Enhancements

1. **Concurrency Limits**: Agent-level (default 5), platform-level (default 50)
2. **Activity Type**: New `parallel_task` activity type (currently reuses CHAT_START)
3. ~~**Session Persistence**: Optional session persistence with TTL for resume~~ **IMPLEMENTED** (CHAT-001)
4. **Batch API**: `POST /api/agents/{name}/batch` for multiple tasks
5. **Priority Queue**: Priority parameter for urgent tasks
6. **Async mode service migration**: Migrate `_run_async_task_with_persistence()` to use `TaskExecutionService` for consistency (EXEC-024 follow-up)
