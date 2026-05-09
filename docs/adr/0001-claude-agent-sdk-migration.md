# ADR 0001 — Claude Agent SDK migration evaluation

- **Status:** Proposed
- **Date:** 2026-05-09
- **Deciders:** Trinity engineering (per [#409](https://github.com/abilityai/trinity/issues/409))
- **Recommendation:** **DEFER migration.** Complete the [#122](https://github.com/abilityai/trinity/issues/122) split on the current architecture; revisit when the SDK gaps listed in §9 close.

---

## 1. Context

`docker/base-image/agent_server/services/claude_code.py` (currently 2137 lines, up from ~1003 lines when [#122](https://github.com/abilityai/trinity/issues/122) was filed in March 2026) is the agent-side runtime that:

1. Shells out to the `claude` CLI as a subprocess
2. Parses its `--output-format stream-json` output line-by-line
3. Manages process-group lifecycle (pgid capture, draining, signal handling)
4. Classifies error conditions from stdout/stderr/exit code (rate limits, auth failures, model access, signal exits, empty results)
5. Recovers truncated turns from on-disk JSONL transcripts
6. Wires Trinity guardrails into CLI flags (`--max-turns`, `--disallowedTools`, `--append-system-prompt`, `--dangerously-skip-permissions`)

A stream of bugs has been traceable to this "shell out + parse stream-json" architecture:

| Bug | Symptom | Architectural root cause |
|---|---|---|
| [#285](https://github.com/abilityai/trinity/issues/285) | Expired-token executions zombie for full timeout | No structured auth-failure signal; backend depends on stderr regex |
| [#407](https://github.com/abilityai/trinity/issues/407) | Agent-server spins ~83% CPU after CLI subprocess defunct | `readline()` never returns EOF when hook subprocess inherits stdout FD |
| [#516](https://github.com/abilityai/trinity/issues/516) | SIGKILL exit misclassified as auth failure | Exit-code disambiguation depends on stderr scanning order |
| [#630](https://github.com/abilityai/trinity/issues/630) | Result event missed when one bad line kills reader thread | Per-line parser exceptions abort the reader |
| [#640](https://github.com/abilityai/trinity/issues/640) | Empty-result diagnostic surface needed | Exit 0 + missing result line indistinguishable from successful no-op |
| [#728](https://github.com/abilityai/trinity/issues/728) | Drain reader threads deadlock on `TextIOWrapper` lock | Buffered I/O lock conflicts with concurrent `pipe.close()` |

[Issue #409](https://github.com/abilityai/trinity/issues/409) asks: would migrating this module to the **Anthropic Claude Agent SDK (Python)** eliminate this bug class and remove material complexity?

This ADR answers that question and chooses a path forward.

---

## 2. Decision drivers

In order of weight:

1. **Bug-class closure** — does the SDK eliminate, by design, the symptom class that produced #285 / #407 / #516 / #630 / #640 / #728?
2. **Observability preservation** — Trinity ships UI and analytics that depend on per-tool duration, per-call cache-read tokens, `compactMetadata`, and JSONL recovery. Migration must not regress these.
3. **Auth-mode compatibility** — agent containers authenticate via `CLAUDE_CODE_OAUTH_TOKEN` (subscription tokens), not `ANTHROPIC_API_KEY`. The SDK must support this.
4. **LOC reduction** — material code deletion is a benefit, but second-order to the above.
5. **Migration risk** — incremental rollout behind the existing `AgentRuntime` ABC seam (`runtime_adapter.py:18`) is feasible; the question is whether the destination is worth the trip.

---

## 3. Current architecture inventory

What `claude_code.py` owns today, scoped so the SDK has a complete coverage target.

### 3.1 Stream-json parser

`parse_stream_json_output` ([line 410](../../docker/base-image/agent_server/services/claude_code.py#L410)) and `process_stream_line` ([line 580](../../docker/base-image/agent_server/services/claude_code.py#L580)) route on the `"type"` field and consume:

| Message type | Fields extracted |
|---|---|
| `system` / `init` | `session_id`, establishes session identity |
| `system` / `compact_boundary` | `compactMetadata.{trigger, preTokens, postTokens, durationMs}`, timestamp — captured into `metadata.compact_events` |
| `result` | `total_cost_usd`, `duration_ms`, `num_turns`, `result` (response text), `session_id`, `usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`, `modelUsage.<model>.{contextWindow, inputTokens, outputTokens}`, `is_error`, `terminal_reason`, `subtype`, `errors[]` (max-turns detection) |
| `assistant` / `user` | `message.content[]`, per-call `usage` block (cache-read/creation for THIS API call only — distinct from cumulative totals on `result`), `error` field on assistant messages |
| `assistant.content[].tool_use` | `id`, `name`, `input` |
| `assistant.content[].tool_result` | `tool_use_id`, `is_error`, `content[].type=text` |
| `assistant.content[].text` | `text` (response accumulation) |

**Token-accounting semantics worth preserving** ([lines 658–671](../../docker/base-image/agent_server/services/claude_code.py#L658)): the cumulative `usage` on the `result` message is **not** used for context-pressure estimation; only the latest assistant message's per-call `usage` is tracked, since that represents the actual prompt size of the final API call. This nuance is invisible from the SDK's `total_cost_usd` / `get_context_usage()` surface.

### 3.2 Subprocess lifecycle

| Concern | Implementation | Relevant issue |
|---|---|---|
| Process group capture | `Popen(start_new_session=True)` ([line 921](../../docker/base-image/agent_server/services/claude_code.py#L921)) + `_capture_pgid` ([line 925](../../docker/base-image/agent_server/services/claude_code.py#L925)) before `wait()` reaps the parent | #407 |
| Daemon reader threads | `iter(process.stdout.readline, '')` in dedicated threads with per-line try/except | #630 |
| Drain budget | `_drain_bounded` ([line 50](../../docker/base-image/agent_server/services/claude_code.py#L50)) with hard 90 s ceiling wrapping `_drain_reader_threads` | #728 |
| Timeout enforcement | Inner `process.wait(timeout=...)` + outer `asyncio.wait_for(timeout + 60)` belt-and-braces | GUARD-003 |
| Termination escalation | `_terminate_process_group` graceful → SIGKILL with pgid | #407 |
| Pipe cleanup | `_safe_close_pipes` in exception paths | #728 |
| Registry hand-off | `process_registry.register/unregister` for live streaming + cancellation | OPS-001 |

### 3.3 Error classification (8 helpers)

| Helper | Line | Detects |
|---|---|---|
| `_is_rate_limit_message` | [1130](../../docker/base-image/agent_server/services/claude_code.py#L1130) | "out of usage", "usage limit", "rate limit", "exceeded your", "quota exceeded", "resets " |
| `_is_model_access_error` | [1147](../../docker/base-image/agent_server/services/claude_code.py#L1147) | "not available on your subscription", "don't have access", "model not found", "not supported by your plan", … |
| `_is_auth_failure_message` | [1164](../../docker/base-image/agent_server/services/claude_code.py#L1164) | "token expired/revoked/invalid", "authentication failed", "setup-token", "oauth token", "unauthorized", "invalid credentials" |
| `_format_rate_limit_error` | [1193](../../docker/base-image/agent_server/services/claude_code.py#L1193) | Renders actionable message — wait / use API key / reassign subscription |
| `_diagnose_exit_failure` | [1204](../../docker/base-image/agent_server/services/claude_code.py#L1204) | Exit code + metadata → user-facing failure reason (codes 1, 2, 126, 127, 137, 139, 143) |
| `_classify_signal_exit` | [1264](../../docker/base-image/agent_server/services/claude_code.py#L1264) | SIGINT/SIGKILL/SIGTERM via negative exit codes or shell encoding (130, 137, 143); disambiguates from auth failures (#516) |
| `_recover_metadata_from_raw_messages` | [1305](../../docker/base-image/agent_server/services/claude_code.py#L1305) | Scans raw_messages for missed `result`, back-fills cost/duration/tokens (#630) |
| `_classify_empty_result` | [1381](../../docker/base-image/agent_server/services/claude_code.py#L1381) | Exit 0 + empty metadata → estimates tools/turns from raw_messages, surfaces parse-failure count (#640) |

### 3.4 JSONL recovery

| Helper | Line | Failure mode it covers |
|---|---|---|
| `_recover_response_from_jsonl` | [115](../../docker/base-image/agent_server/services/claude_code.py#L115) | Stdout pipe wedged mid-turn, result event lost, `response_parts == []`. Walks back to last `user`-string boundary in `~/.claude/projects/-home-developer/<session>.jsonl`, collects assistant text. 10 MB cap; tail-seek if larger. |
| `_extract_compact_events_from_jsonl` | [214](../../docker/base-image/agent_server/services/claude_code.py#L214) | `compact_boundary` events arrive on stdout but stripped of `compactMetadata`. Re-reads JSONL, filters by `since_iso` timestamp, returns full `CompactEvent` list. |

The 10 MB cap and string-boundary heuristic are load-bearing — they're how Trinity recovers turns where the CLI subprocess died ungracefully.

### 3.5 Guardrails wiring

`_load_guardrails` ([line 311](../../docker/base-image/agent_server/services/claude_code.py#L311)) reads `/opt/trinity/guardrails-runtime.json` (fallback `/opt/trinity/guardrails-baseline.json`) on every invocation:

| Setting | CLI flag | Default |
|---|---|---|
| `max_turns_chat` / `max_turns_task` | `--max-turns` | 50 |
| `disallowed_tools` | `--disallowedTools` (comma-separated) | `[]` |
| `execution_timeout_sec` | (timeout bound, not CLI) | 1800 s |
| (always) | `--append-system-prompt` | (Trinity platform prompt) |
| (always) | `--dangerously-skip-permissions` | (containers run with bypass) |

### 3.6 External contracts (must be preserved across migration)

- **`activity_tracking`** — `start_tool_execution(tool_id, tool_name, tool_input)` + `complete_tool_execution(tool_id, success, tool_output)` on every `tool_use` / `tool_result` block. Powers `/api/activity` and the execution-log UI.
- **`process_registry`** — `register(execution_id, process, metadata)` / `publish_log_entry(execution_id, raw_msg)` / `unregister(execution_id)`. Powers live streaming + operator-cancel termination.
- **`credential_sanitizer`** — three entrypoints (`sanitize_text`, `sanitize_dict`, `sanitize_subprocess_line`). Applied before logging stderr lines, raw_msg dicts, and stdout lines.
- **`AgentRuntime`** (abstract) — `runtime_adapter.py:18`. Trinity already abstracts the runtime; `ClaudeCodeRuntime` is one implementation, `GeminiRuntime` is another. **A hypothetical `ClaudeAgentSdkRuntime` slots in here cleanly** — this is the migration seam.

### 3.7 Public seams (call sites)

| Function | Callers |
|---|---|
| `execute_claude_code` | `ClaudeCodeRuntime.execute` → `chat.py` router (interactive chat path) |
| `execute_headless_task` | `ClaudeCodeRuntime.execute_headless` → MCP, fan-out, webhooks (one-shot task path) |
| `parse_stream_json_output` | Batch path inside `execute_headless_task` |
| `get_execution_lock` | `chat.py:POST /api/chat` (chat serialization) |
| `get_claude_runtime` | `runtime_adapter.get_runtime()` singleton |

---

## 4. SDK feature audit

Source: [`/anthropics/claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python) via Context7 as of 2026-05-09.

| # | Capability | Coverage | Notes |
|---|---|---|---|
| a | Streaming message chunks | **Partial** | `async for msg in client.receive_response()` yields typed objects (`AssistantMessage`, `ResultMessage`). No raw line-by-line stream-json access. |
| b | Per-tool-call events with name/input/output/duration | **Partial** | `ToolUseBlock` exposes `name`, `input`. **No explicit duration**, no discrete output event — tool results appear as message blocks. |
| c | Token usage (input/output) per turn | **Partial** | `total_cost_usd` available on `ResultMessage`. **No public per-turn input/output/cache breakdown**. |
| d | Cost reporting per turn | **Yes** | `ResultMessage.total_cost_usd`, `max_budget_usd`. |
| e | Context window size + tokens used | **Yes** | `client.get_context_usage()` returns `{percentage, totalTokens, maxTokens}`. |
| f | Model name + `permissionMode` from init | **Partial** | Set via `ClaudeAgentOptions.model` and `permission_mode`. No explicit init event surfacing both. |
| g | Cancellation / timeout / SIGINT | **Yes** | `await client.interrupt()` + `asyncio.timeout()`. |
| h | Sub-process cleanup (zombie reaping, hook FDs) | **Not documented** | SDK still wraps the `claude` CLI. Cleanup contract not specified. |
| i | Auth-failure detection mid-stream | **Partial** | Generic `ClaudeSDKError` / `ProcessError(exit_code, stderr)`. **No auth-specific exception type.** Falls back to stderr string-matching. |
| j | Rate-limit / quota / subscription-expired error types | **Not documented** | No specific exception types in public API. |
| k | `--append-system-prompt` equivalent | **Yes** | `system_prompt: {"type": "preset", "append": "..."}` or plain string. |
| l | `--disallowed-tools` equivalent | **Yes** | `ClaudeAgentOptions.disallowed_tools=[...]`. |
| m | `--max-turns` equivalent | **Yes** | `ClaudeAgentOptions.max_turns=N`. |
| n | `bypassPermissions` permission mode | **Yes** | `permission_mode="bypassPermissions"`. |
| o | Subscription-token auth (`CLAUDE_CODE_OAUTH_TOKEN`) | **Not documented** | SDK presumes API key or CLI credential store. **Empirically unverified.** |
| p | Session resume / `--resume <uuid>` equivalent | **Yes** | `session_id`, `resume="..."`, `fork_session=True`. |
| q | JSONL transcript on disk (CLI-compatible format) | **Partial** | SDK supports pluggable session backends (filesystem, Postgres, Redis, S3). **Default filesystem layout is not byte-compatible with CLI JSONL** — incompatible with our current recovery paths (§3.4). |

---

## 5. Bug closure analysis

| Bug | Closed by SDK migration? | Reasoning |
|---|---|---|
| **#285** (zombie on expired token) | **NO** | SDK still shells to the same CLI; CLI's behavior on expired tokens is non-deterministic. SDK has no structured auth-failure exception type (§4 row i) — we'd still string-match stderr, same fragility. Closing #285 by design requires an auth-failure exception type the SDK doesn't ship. |
| **#407** (CPU spin on undrained subprocess) | **UNCERTAIN** | The bug is in our reader-thread loop. SDK owns its own subprocess management — but cleanup semantics are undocumented (§4 row h). Could persist or surface as a different bug. Cannot answer without an empirical spike. |
| **#516** (SIGKILL misclassified as auth) | **NO** | This is a classification bug in our `_classify_signal_exit`. SDK doesn't expose stronger exit-code disambiguation. |
| **#630** (per-line parser kills reader) | **YES** | SDK consumers iterate typed objects; a malformed message becomes a structured exception, not a dead reader thread. |
| **#640** (empty-result diagnostic) | **NO** | Our diagnostic operates on raw_messages, which the SDK abstracts away. The class of bug ("CLI exited 0 but produced nothing useful") still exists, just observable through different telemetry. |
| **#728** (TextIOWrapper deadlock during drain) | **YES** | We don't manage the buffered-I/O lifecycle if we use the SDK. SDK's own implementation likely avoids this specific Python stdlib pitfall. |

**Net:** 2/6 bugs closed by design (#630, #728). 1 uncertain (#407). 3 either persist or move (#285, #516, #640). The two strongest motivators in #409 — #285 and #407 — are *not* cleanly resolved by migration.

---

## 6. LOC reduction estimate

#409 estimated ~1200 lines deleted. Our inventory shows the realistic figure is materially smaller because several modules cannot migrate:

| Section | Lines (approx) | Migratable? |
|---|---|---|
| Subprocess lifecycle (§3.2) — drain, terminate, pgid, threads | ~250 | **Yes** — SDK owns this |
| Stream-json parser (§3.1) — `parse_stream_json_output` + `process_stream_line` | ~400 | **Yes** — SDK owns this |
| Error classification (§3.3) — 8 helpers | ~280 | **No** — SDK has no equivalent (rows i, j) |
| JSONL recovery (§3.4) — `_recover_*` / `_extract_*` | ~170 | **No** — SDK uses incompatible storage (row q); recovery semantics gone |
| Guardrails wiring (§3.5) | ~50 | **Reduces** — `ClaudeAgentOptions` replaces flag-stringing, ~30 lines saved |
| `execute_claude_code` (chat path) | ~330 | **Yes** — `ClaudeSDKClient` + `query()` replace it |
| `execute_headless_task` (one-shot path) | ~640 | **Partial** — process management goes; image handling + log-entry construction stay; ~300 lines saved |
| Token-accounting helpers, runtime singleton, lock | ~100 | Mixed |

**Realistic deletion: 600–800 lines** (parser + stream readers + drain helpers + chat path savings). The 8 error-classification helpers (~280 lines) and JSONL recovery (~170 lines) **stay** because the SDK has no equivalent — they'd just classify SDK exceptions and SDK on-disk artifacts instead of stream-json/JSONL.

---

## 7. Regressions if we migrate

Each is independent and additive. Listed worst-first:

1. **Auth-failure detection regresses to stderr string-matching.** The SDK has no auth-specific exception (§4 row i). We currently scan stderr (`_is_auth_failure_message`, [line 1164](../../docker/base-image/agent_server/services/claude_code.py#L1164)) and Issue #285 hardened that path. Migration moves us back to the same fragility, with the SDK as one more layer of indirection between us and the symptom.
2. **Per-tool-call duration is lost.** `ToolUseBlock` exposes `name` and `input` but no duration timestamp. The execution-log UI (`/api/activity`) currently shows per-tool duration; this becomes unavailable until the SDK adds it.
3. **Per-message cache-read tokens are lost.** Our session messages persist `cache_read_tokens` ([architecture.md §"Session Tab Features"](../memory/architecture.md)) precisely so we can analyse prompt-cache hit-rate across `--resume` turns ([SESSION_TAB_2026-04](../planning/SESSION_TAB_2026-04.md)). The SDK's `total_cost_usd` does not surface cache-read tokens (§4 row c).
4. **`compactMetadata` is lost.** We extract `compact_boundary.compactMetadata.{preTokens, postTokens, durationMs, trigger}` and persist it for the compact-event timeline. The SDK exposes typed messages, not raw stream events; this metadata is not in the public surface.
5. **JSONL recovery is unavailable.** The SDK's pluggable session backends are not byte-compatible with the CLI's JSONL files (§4 row q). Our 10 MB-capped recovery walk-back ([line 115](../../docker/base-image/agent_server/services/claude_code.py#L115)) would have nothing to read. Turns currently saved by recovery would just be lost.
6. **Subscription-token auth is unverified.** Trinity's primary auth mode in agent containers is `CLAUDE_CODE_OAUTH_TOKEN` (subscription-mode, not API-key). The SDK does not document support for this (§4 row o). **This alone is a hard gate** — until empirically verified, migration cannot proceed.

---

## 8. Migration risk

- **Migration seam exists** — `AgentRuntime` ABC at `runtime_adapter.py:18` already abstracts `ClaudeCodeRuntime` from callers. A new `ClaudeAgentSdkRuntime` slots in cleanly behind a feature flag.
- **Empirical spike needed** to clear hard gates (subscription-token auth, subprocess cleanup behaviour) before any milestone plan is binding. Sketch in §11.
- **Test-suite gap** — current integration tests assume CLI-subprocess behaviours (e.g., signal exit codes, stderr line scanning). A migration introduces a parallel test surface during the deprecation window.

---

## 9. Recommendation: **DEFER**

**Reason:** The strongest bugs motivating #409 (#285 and #407) are *not* closed by design under the current SDK feature set, and migration introduces five concrete observability regressions plus one hard auth-mode gate. The benefit (cleaner code, ~600–800 fewer lines) is real but does not outweigh the cost.

**Conditions for revisiting** (any one of these flips the decision; all four would make migration unambiguous):

1. SDK ships a structured **auth-failure exception type** distinguishable from generic `ProcessError` — closes #285 by design.
2. SDK documents its **subprocess cleanup contract** (zombie reaping, hook FD inheritance behaviour) — clarifies #407.
3. SDK exposes **per-tool duration + per-call cache-read tokens** in its typed events — eliminates regressions 2 and 3.
4. SDK confirms **subscription-token auth (`CLAUDE_CODE_OAUTH_TOKEN`)** support, either via docs or empirical verification — clears the hard gate.

**Until then:** complete [#122](https://github.com/abilityai/trinity/issues/122) on the current architecture. The split has standalone value (reviewability, test isolation, hotspot reduction) and is independent of the SDK question.

---

## 10. Concrete next step — rescoped #122 split

The original #122 plan (4 modules, dated 2026-03-13) is stale: the file has more than doubled since (1003 → 2137 lines) and gained responsibilities the original split didn't account for (JSONL recovery, signal-exit classification, empty-result diagnostics, drain budgeting, GUARD-003 timeout layering).

Recommended target structure (≤350 lines per file, matches today's file):

| Proposed module | Contents | Approx LOC |
|---|---|---|
| `claude_code.py` | `execute_claude_code`, `get_execution_lock`, `get_claude_runtime`, `ClaudeCodeRuntime` | ~450 |
| `headless_executor.py` | `execute_headless_task` (likely needs further internal extraction; today it's 644 lines) | ~650 |
| `stream_parser.py` | `parse_stream_json_output`, `process_stream_line` | ~400 |
| `error_classifier.py` | `_is_rate_limit_message`, `_is_model_access_error`, `_is_auth_failure_message`, `_format_rate_limit_error`, `_diagnose_exit_failure`, `_classify_signal_exit`, `_recover_metadata_from_raw_messages`, `_classify_empty_result` | ~280 |
| `jsonl_recovery.py` | `_recover_response_from_jsonl`, `_extract_compact_events_from_jsonl`, `_MAX_JSONL_BYTES_FOR_RECOVERY` | ~170 |
| `subprocess_lifecycle.py` | `_drain_bounded`, `_drain_reader_threads`, `_terminate_process_group`, `_capture_pgid`, `_safe_close_pipes`, `_DRAIN_BUDGET_SECONDS` | ~150 |

The `headless_executor.py` file would still be over the 350-line target. Splitting `execute_headless_task` into setup / stream-loop / finalise sub-functions is part of the work.

This is informational — whoever claims #122 can disagree.

---

## 11. Sketch — migration milestone plan (only if §9 conditions are met)

Hidden in this section so it doesn't feel like the recommended path. If migration is later reopened, this is a starting point, not a binding plan.

**Pre-condition spike** (1 sprint): Implement a minimal `ClaudeAgentSdkRuntime.execute_headless` (no chat, no streaming) behind a feature flag. Goals: verify subscription-token auth works, observe SDK subprocess cleanup behaviour under hook-laden agents, measure per-tool duration availability via typed events.

If spike clears all gates:

| Milestone | Scope | Risk |
|---|---|---|
| M1 | `execute_headless` migrated; `execute_claude_code` (chat) still on CLI path | Low — headless callers tolerate latency variance better |
| M2 | `execute_claude_code` migrated; activity-tracking + process-registry contracts ported to SDK callbacks | Medium — chat path has lower latency budget and live-streaming UI is exposed |
| M3 | CLI path deprecated; remaining JSONL recovery + error classifiers either removed (if SDK closes the gap) or rewritten against SDK exceptions | Low — once both paths are SDK-only, the legacy code is dead |

Estimated total: 2 sprints after spike.

---

## 12. References

- [#409](https://github.com/abilityai/trinity/issues/409) — this evaluation's parent issue
- [#122](https://github.com/abilityai/trinity/issues/122) — the split that becomes the recommended next step
- [#285](https://github.com/abilityai/trinity/issues/285), [#407](https://github.com/abilityai/trinity/issues/407), [#516](https://github.com/abilityai/trinity/issues/516), [#630](https://github.com/abilityai/trinity/issues/630), [#640](https://github.com/abilityai/trinity/issues/640), [#728](https://github.com/abilityai/trinity/issues/728) — symptom bugs
- [`claude_code.py`](../../docker/base-image/agent_server/services/claude_code.py) — the module under evaluation
- [`runtime_adapter.py`](../../docker/base-image/agent_server/services/runtime_adapter.py) — the migration seam
- [Anthropic Claude Agent SDK (Python)](https://github.com/anthropics/claude-agent-sdk-python) — the proposed destination
- [SESSION_TAB_2026-04](../planning/SESSION_TAB_2026-04.md) — context on `cache_read_tokens` observability
- [docs/memory/architecture.md](../memory/architecture.md) — current system invariants
