# Feature: Session Tab — `--resume`-default chat surface

> **Status**: ✅ Implemented (2026-05-01), GA (2026-05-04). Default ON (`session_tab_enabled` flag, settable to false to disable platform-wide).
> **Design doc**: [docs/planning/SESSION_TAB_2026-04.md](../../planning/SESSION_TAB_2026-04.md) — read first if you're touching anything in this flow.

## Overview

A new Agent Detail tab that lives alongside the existing Chat tab. Each turn reattaches to the same Claude Code session via `claude --print --resume <uuid>` so the agent retains tool-result memory, mid-skill state, and reasoning state across messages — strictly more capable than Chat's stateless text-replay model.

The Session surface is **strictly parallel** to Chat: separate router, separate Pinia store, separate Vue component, separate DB tables. Chat is untouched. Schedules, MCP `chat_with_agent`, fan-out, and webhook triggers stay on text-replay (concurrency hazards make `--resume` unsafe there).

## User Story

As a Trinity user running long, multi-turn reasoning tasks against an agent, I want each new message to reattach to the same Claude memory so the agent doesn't have to re-derive what it already figured out — and I want a clear "Reset memory" out when context-window pressure builds up, without losing the visible message log.

## Entry Points

- **UI**: `src/frontend/src/components/SessionPanel.vue` — invoked from `AgentDetail.vue` when the "Session" tab is active and `sessionsStore.sessionTabEnabled` is true.
- **API**: `POST /api/agents/{name}/sessions/{id}/message` (the turn endpoint).
- **Feature flag**: `GET /api/settings/feature-flags` exposes `session_tab_enabled` to non-admin users so the frontend can decide whether to render the tab.

---

## Frontend Layer

### Components

| Component | File | Purpose |
|---|---|---|
| `SessionPanel.vue` | `src/frontend/src/components/SessionPanel.vue` | Main surface — header (session selector + model picker + Reset memory + "+ New Session"), `ChatMessages` for the bubble list, `ChatInput` for input. Reuses chat sub-components for visual parity. |
| `AgentDetail.vue` | `src/frontend/src/views/AgentDetail.vue` | Tab insertion (between Chat and Dashboard/Schedules), gated on `sessionsStore.sessionTabEnabled`. The `isFullscreenTab` computed extends Chat's full-viewport flex layout to the Session tab. |

### State Management

**`stores/sessions.js`** — Pinia store keyed by agent name (no per-agent bleed):

| State | Purpose |
|---|---|
| `sessionsByAgent` | Per-agent session-row arrays (newest first) |
| `activeSessionByAgent` | Currently-selected session id per agent |
| `messagesBySession` | Cached message lists per session id |
| `sessionTabEnabled` | Cached feature-flag value (`loadFeatureFlags()` resolves once) |

| Action | Endpoint |
|---|---|
| `loadFeatureFlags()` | `GET /api/settings/feature-flags` |
| `listSessions(agent)` | `GET /api/agents/{name}/sessions` |
| `createSession(agent)` | `POST /api/agents/{name}/session` |
| `loadSession(agent, id)` | `GET /api/agents/{name}/sessions/{id}` |
| `selectSession(agent, id)` | local-only (active-session pointer) |
| `sendMessage(agent, id, text, opts)` | `POST /api/agents/{name}/sessions/{id}/message` (optimistic user-message insert, **no rollback on failure** — the backend persists the user row at turn start so the optimistic row is canonical; #789) |
| `resetSession(agent, id)` | `POST /api/agents/{name}/sessions/{id}/reset` |
| `deleteSession(agent, id)` | `DELETE /api/agents/{name}/sessions/{id}` |

### Per-session subtitle (Phase 3.5)

Each row in the session selector dropdown shows: turn count, context % used, a colored dot (emerald = has cached memory, gray = cold), and a `consecutive_resume_failures` indicator when nonzero.

### Lean cut for first-visible-surface (Phase 3.1)

Voice mic and SSE dynamic status labels are **deferred** (each requires a backend extension to the turn endpoint — voice writes to the wrong DB tables today, async_mode + SSE on the turn endpoint is THINK-001-shaped work). File upload **shipped in Phase 5.2** (commit `24acbf12`) — `SessionMessageRequest.files` is accepted and the turn handler runs the same `process_file_uploads` helper Chat does, with images fed in as vision blocks. Slash-command playbook autocomplete works automatically because `SessionPanel` reuses `ChatInput` with the agent name + status.

---

## Backend Layer

### Endpoints

`src/backend/routers/sessions.py` — six endpoints, all gated on `is_session_tab_enabled()` (404 when off):

| Method | Path | Handler | DB calls |
|---|---|---|---|
| POST | `/api/agents/{name}/session` | `create_session()` | `db.create_session()` |
| GET | `/api/agents/{name}/sessions` | `list_sessions()` | `db.list_sessions(user_id=current_user.id)` |
| GET | `/api/agents/{name}/sessions/{id}` | `get_session_with_messages()` | `db.get_session()` + `db.get_session_messages()` |
| POST | `/api/agents/{name}/sessions/{id}/message` | `send_session_message()` | the turn pipeline (below) |
| POST | `/api/agents/{name}/sessions/{id}/reset` | `reset_session_memory()` | `db.clear_cached_claude_session_id()` + `session_cleanup_service.reap_jsonl()` (best-effort) |
| DELETE | `/api/agents/{name}/sessions/{id}` | `delete_session()` | `db.delete_session()` + `session_cleanup_service.reap_jsonl()` (best-effort) |

### Turn endpoint pipeline

`POST /api/agents/{name}/sessions/{id}/message`:

1. Resolve session row, enforce per-user ownership (404 on mismatch — not 403, to avoid leaking session-id existence).
2. Persist the user message immediately so it appears even on failure.
3. Read `cached_claude_session_id`.
4. Resolve dynamic lock TTL via `_resolve_lock_ttl(agent_name)` = `db.get_execution_timeout(agent) + 30s`, capped at 7230s. The static 300s constant was removed in #759 because turns running longer than 5 min would silently drop the lock and allow concurrent JSONL writes.
5. SET in-flight sentinel `session_inflight:{session_id}` with the same TTL (#759). Bracketed via `_InflightSentinel` async context manager so DEL fires on success **and** exception. Distinct from the resume lock — the sentinel covers cold turns too, which the lock skips by design. Drives the `turn_in_progress` field on the GET endpoint.
6. Acquire `_ResumeLock(agent, cached_uuid, ttl_seconds=lock_ttl)` — Redis SET NX EX with async wait-and-retry (250 ms tick, 30 s ceiling). Cold turns skip the lock. Lock key constructed via `_session_lock_key(agent, uuid)` helper (shared by producer + any future probe — regression guard against split-brain typos).
7. Call `task_execution_service.execute_task(..., resume_session_id=cached, persist_session=True)`. The persist flag is unconditional — even cold turns must write the JSONL so turn 2's resume succeeds.
8. **Resume-failure fallback** (Phase 2.2): if execute_task returned failed with `"no conversation found"` AND we had a cached UUID, clear the cache, `mark_resume_failure`, retry **once** with `resume_session_id=None`. Log structured warning `event=session_resume_fallback`. Anthropic #39667 (cleanupPeriodDays) and #53417 (CLI upgrade) both produce this signal.
9. On success, trust `result.session_id` directly (Phase 1.3 fixed the agent-server stream parser to recognise `{"type":"system","subtype":"init"}` — no execution_log scan needed). Update `cached_claude_session_id` if changed, `mark_resume_success` to reset the failure counter.
10. Persist the assistant message with `cost`, `context_used/max`, `cache_read_tokens` (from agent metadata), `tool_calls`, and the per-message `claude_session_id` audit field.
11. Release resume lock (Lua script — only releases tokens we own). DEL in-flight sentinel via `_InflightSentinel.__aexit__`.
12. Return `{session, message, response, claude_session_id, fallback_fired, fallback_reason, cost, context_used, context_max, cache_read_tokens}` — the `session` payload now carries `turn_in_progress: false` since the sentinel is already DELed before the response leaves.

### Spike-pitfall defenses

| Pitfall | Where defended |
|---|---|
| **L1** parser cached `EX-...` execution id instead of real Claude UUID | Phase 1.3 fix in `agent_server/services/claude_code.py` — both `parse_stream_json_output` and the streaming variant now match `type=system` + `subtype=init` (with `result` event as fallback). |
| **L2** cold turn passed `--no-session-persistence` → empty JSONL → turn 2 errors | Phase 1.4 added `persist_session` flag through `ParallelTaskRequest → ChatRouter → AgentRuntime ABC → ClaudeCodeRuntime → execute_headless_task`. Turn endpoint passes `persist_session=True` unconditionally. |
| **L3** first turn has no session_id (frontend-first model) | Session row created server-side via `POST /session` BEFORE the turn endpoint runs. Lazy session-creation on first input from empty state also goes through the same store path. |
| **#20992** concurrent `--resume` JSONL writes corrupt the file | Phase 2.3 Redis lock per `(agent, claude_uuid)`. |
| **#26964** cross-session contamination via shared cwd | Phase 4.3 contamination test gates GA — currently green on this Claude Code version. |

### Services

| Service | File | Role |
|---|---|---|
| `task_execution_service.execute_task` | `services/task_execution_service.py` | Shared pipeline (capacity, activity, sanitization, agent HTTP). Phase 1.5 added `persist_session: bool = False`; only the session router opts in. |
| `session_cleanup_service` | `services/session_cleanup_service.py` | Phase 4.2 JSONL reaper. Synchronous best-effort `reap_jsonl()` from the router on reset/delete; periodic 6h sweep with 1h race guard. Uses `execute_command_in_container` (no agent-server endpoint). |
| `settings_service.is_session_tab_enabled()` | `services/settings_service.py` | Resolves DB → env → False. Used by both the router (gate) and the public flag endpoint. |

---

## Data Layer

### Schema

`agent_sessions` and `agent_session_messages` — defined in `db/schema.py` for fresh installs, idempotent migration `agent_sessions_tables` in `db/migrations.py` for upgrades. See `docs/memory/architecture.md` "Database Schema" section for the full DDL.

Three fields unique to this surface vs. `chat_sessions` / `chat_messages`:
- `agent_sessions.cached_claude_session_id` — the Claude Code UUID for `--resume`
- `agent_sessions.consecutive_resume_failures` — drives the fallback path
- `agent_session_messages.cache_read_tokens` — prompt-cache hit observability
- `agent_session_messages.claude_session_id` — per-message audit of which UUID Claude actually ran under

### Operations

`db/sessions.py` → `SessionOperations` class. Wired into `database.py` facade as `db.create_session()`, `db.get_session()`, `db.list_sessions()`, `db.delete_session()`, `db.add_session_message()`, `db.get_session_messages()`, `db.get_cached_claude_session_id()`, `db.update_cached_claude_session_id()`, `db.clear_cached_claude_session_id()`, `db.mark_resume_failure()`, `db.mark_resume_success()`, `db.list_active_claude_session_ids()` (used by the cleanup service).

---

## JSONL lifecycle

```
/home/developer/.claude/projects/-home-developer/<uuid>.jsonl
                                                  ↑
                                                  Claude Code writes this on every persisted turn.
                                                  Filename = the session UUID.
```

| Trigger | Path |
|---|---|
| First turn (cold) | Agent writes a fresh JSONL under a new UUID. Backend captures it from `result.session_id` and updates `cached_claude_session_id`. |
| Subsequent turn (resume) | `--resume <cached_uuid>` appends to the existing JSONL. `cached_claude_session_id` unchanged. |
| Reset memory | `db.clear_cached_claude_session_id()` + `session_cleanup_service.reap_jsonl()` (synchronous best-effort). Next turn becomes cold under a fresh UUID. |
| Delete session | `db.delete_session()` + same reap. Message rows in `agent_session_messages` go too. |
| Resume fallback | `db.clear_cached_claude_session_id()` + `mark_resume_failure()`. Old JSONL stays orphaned (disk reclaim deferred to periodic sweep). |
| Periodic sweep (every 6h) | Diff on-disk UUIDs vs. `db.list_active_claude_session_ids(agent)`; reap orphans with mtime > 1h ago. |

---

## Side Effects

- Turn endpoint creates one row in `schedule_executions` per turn (via `task_execution_service.execute_task`'s standard execution-record contract). Each row carries `claude_session_id` for audit.
- WebSocket events emitted on the turn align with the existing chat path (`agent_collaboration` only fires for inter-agent calls — not used for Session, since this is a user-facing chat).
- Phase 4.2 cleanup service emits structured INFO logs only when there's actual deletion or errors to report (avoids log spam).

---

## Error Handling

| Case | Response |
|---|---|
| Feature flag off | 404 from every endpoint |
| Session not found OR not owned by caller | 404 (not 403 — avoid leaking session-id existence) |
| Agent not running | 503 from upstream `task_execution_service` (translated to 502 by the turn endpoint) |
| Capacity full | 502 (the turn endpoint surfaces `execute_task`'s status==failed) |
| Resume failed with no cached UUID (e.g. truly broken) | 502 from the turn endpoint, NOT a fallback (no point retrying without a UUID) |
| Resume failed with cached UUID | Fallback fires, retry cold, return success with `fallback_fired: true` so UI can render the "memory expired — starting fresh" inline notice |
| Concurrent turn on same session | Second request waits up to 30s for the Redis lock; 429 with `retry_after: 5` if the wait exceeds the ceiling |

---

## Security Considerations

| Threat | Mitigation |
|---|---|
| **Session-id enumeration / ownership leak (E6)** | Every endpoint that takes a session id returns **404 on mismatch, never 403**. A 403 would confirm the id exists and is owned by someone else; 404 hides existence. Sessions are scoped per-user even within the same agent — the agent owner cannot see other users' sessions on their own agent. |
| **JSONL corruption from concurrent `--resume` (Anthropic #20992)** | Two simultaneous `--resume <uuid>` invocations against the same JSONL race on writes and produce a corrupt file that breaks all subsequent resumes for that session. Mitigated by a per-`(agent, claude_uuid)` Redis lock (`SET NX EX 300s`, async wait-and-retry with 250ms tick + 30s ceiling, Lua release script that only releases tokens we own). Cold turns skip the lock — there's no shared file yet. |
| **Cross-session contamination via shared cwd (Anthropic #26964)** | In some Claude Code versions, two sessions running under the same working directory could observe each other's tool outputs through cwd-resident state. Phase 4.3 ships an empirical contamination test (`tests/integration/test_session_cross_contamination.py`) — session B asserts it cannot recall a secret token planted in session A. The test runs as a GA gate on every base-image bump and is currently green on the shipped Claude Code version. |
| **JSONL prompt-injection persistence** | Because every persisted turn is appended to `~/.claude/projects/-home-developer/<uuid>.jsonl`, any prompt-injected instruction or pasted secret in turn N persists into the agent's working memory for every subsequent resume on that session — the blast radius of a single bad turn is the whole session, not a single message. **Mitigation**: the user-facing **Reset memory** action calls `db.clear_cached_claude_session_id()` and synchronously reaps the JSONL via `session_cleanup_service.reap_jsonl()`. Subsequent turns are cold under a fresh UUID. The visible message log in `agent_session_messages` is preserved (it lives in the DB, not the JSONL). |

---

## Testing

### Prerequisites
- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost (Vite dev or nginx)
- [ ] Docker daemon running
- [ ] Logged in as admin
- [ ] Test agent `agent-testfix` running on the rebuilt base image (post-Phase-1.3 parser fix)
- [ ] Feature flag enabled: `PUT /api/settings/session_tab_enabled {"value":"true"}`

### Unit tests (`tests/unit/`)
- `test_session_operations.py` — CRUD round-trip + cached-UUID lifecycle + resume-failure/success counters (9 tests)
- `test_claude_code_session_id_parser.py` — both parsers (batch + streaming): system/init recognition, result fallback, init-wins-over-result, legacy bare-init rejection, permission-mode validation regression guard (8 tests)
- `test_session_persistence_flag.py` — pin the contract: signatures across runtime ABC, ParallelTaskRequest, agent chat router, execute_headless_task, task_execution_service.execute_task, --no-session-persistence gating (8 tests)

### Integration tests (`tests/integration/`)
- `test_session_turns.py` — Scenarios A–E (3-turn happy path, B's recall of A's secret, JSONL deletion → fallback, concurrent serialise via Redis lock, switching sessions preserves UUIDs)
- `test_session_cross_contamination.py` — Phase 4.3 GA gate: session B must not leak session A's secret token (Anthropic #26964)
- `test_session_cleanup.py` — reset reaps JSONL synchronously, delete reaps JSONL synchronously, periodic sweep reaps aged orphan + keeps active + respects 1h age guard

Run inside `trinity-backend` container (Python 3.11):
```bash
docker run --rm -v $PWD:/work -w /work \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e TRINITY_API_URL=http://host.docker.internal:8000 \
  -e TRINITY_TEST_USERNAME=admin -e TRINITY_TEST_PASSWORD=$ADMIN_PASSWORD \
  --add-host host.docker.internal:host-gateway --entrypoint python \
  trinity-backend -m pytest tests/integration/test_session_turns.py tests/integration/test_session_cross_contamination.py tests/integration/test_session_cleanup.py -v
```

### E2E (`src/frontend/e2e/session-tab.spec.js`)
Playwright spec, marked `@interactive` (real Claude call ~10–60s, opt-in via `npm run test:e2e -- session-tab.spec`). Snapshots prior flag in `beforeAll`, restores in `afterAll`. Three cases: tab hidden when off, tab appears + send turn + reset modal, Chat tab switch preserves Session state.

**Last Tested**: 2026-05-01
**Status**: ✅ Working — all 25 unit tests + 9 integration tests pass; UI manually verified in dev server.

---

## Known limitations and user-facing caveats

These items are NOT bugs — they are design boundaries we explicitly chose, deferred features, or platform-level concerns that surface in the Session tab. Source these into the user-facing docs (`docs/user-docs/`) when Phase 5.3 lands.

| Limitation | Detail | Mitigation today |
|---|---|---|
| **Voice mic not wired into Session** | The voice button is hidden on the Session tab. Voice writes to `chat_sessions` / `chat_messages`, not the Session tables, so a voice session inside the Session tab would silently land in the wrong table and not persist `--resume` memory. Deferred from Phase 3.1. | Use the Chat tab for voice; switch to Session for stateful text turns. Will revisit when there's signal users actually want voice + persistent memory together. |
| **Agent restore from backup may require fresh sessions** | `~/trinity-data/` backup covers the SQLite DB (session rows + messages) but does NOT cover the named workspace Docker volumes that hold the `.claude/projects/<uuid>.jsonl` files. After a DR restore, every session row's `cached_claude_session_id` will point at a JSONL that doesn't exist on the new host. | First turn on every restored session will trigger the resume-failure fallback, which clears the cache and starts the session cold under a fresh UUID. The visible message log is preserved (it's in the DB); only the agent's working memory of those turns is lost. Will be addressed at the platform level (separate issue) when the backup script is extended to cover named volumes. |
| **Long Session turns may surface phantom errors in browsers** | The Session turn endpoint is synchronous and may legitimately run for the agent's full execution timeout (up to 7200s). The Axios timeout is set to 7260s, but if the browser tab is suspended or the laptop sleeps mid-turn, the user may see a phantom error toast even though the work succeeded server-side. | Refresh the page after the agent's typical completion time — the assistant message will appear if it landed in the DB. A future change to async-mode + SSE polling (THINK-001 pattern) will decouple browser lifetime from task lifetime. |
| **Stdout pipe race recovery is best-effort** | When a child subprocess inherits Claude Code's stdout, the final `result` event line can be lost. Phase 5.1 added soft-recovery: if `response_parts` accumulated assistant text, the agent server treats the turn as success even without the formal result event. Cost / duration columns will be NULL for these recovered turns. | Recovered turns will not show cost or duration in the Tasks tab. The reply is correct; the metrics are missing. |

---

## Related Flows

- [persistent-chat-tracking.md](persistent-chat-tracking.md) — the older `chat_sessions` / `chat_messages` system this surface is parallel to.
- [authenticated-chat-tab.md](authenticated-chat-tab.md) — the Chat tab that sits next to Session in the AgentDetail tab row.
- [parallel-headless-execution.md](parallel-headless-execution.md) — the `task_execution_service.execute_task` shared pipeline this surface plugs into.
- [continue-execution-as-chat.md](continue-execution-as-chat.md) — the EXEC-023 `resume_session_id` plumbing this surface inherits from.
- [websocket-event-bus.md](websocket-event-bus.md) — RELIABILITY-003 transport for any WebSocket events this flow emits.
