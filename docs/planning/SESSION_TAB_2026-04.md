# Session Tab — `--resume`-default chat surface

**Date:** 2026-04-30
**Status:** Proposed. Implementation has not started; supersedes the local-only spike branch `spike/chat-resume-default` (rolled back, learnings preserved in Appendix A).
**Owner:** TBD
**Tracking issue:** TBD (open after this doc lands on `dev`)

---

## TL;DR

We are adding a new agent tab — **Session** — that lives alongside the existing **Chat** tab. The Chat tab keeps its current stateless behavior (each turn = a fresh `claude --print` invocation with the last 10 message-pairs prepended as text). The Session tab is a **separate surface** built on `claude --print --resume <uuid>`, so every turn in a session reattaches to the same Claude Code session — preserving tool-result memory, mid-skill state, and reasoning state across turns.

This is a **new surface, not a behavior change to Chat**. Chat is untouched. Users who want today's behavior keep it; users who want stateful sessions get a new tab. Schedules, MCP `chat_with_agent`, fan-out, and webhook triggers stay on text-replay (concurrency hazards make `--resume` unsafe there).

We proved the thesis end-to-end in a local spike on 2026-04-29 (Scenarios A & B in the *Validation* section below). This doc is the implementation plan, with every spike learning baked in so we don't repeat the four failure modes we hit the first time.

---

## Why a new tab instead of changing Chat

Three reasons:

1. **Different value prop.** Chat is "ask one question, get one answer, repeat." Session is "have an ongoing relationship with the agent where it remembers what it did." The UX framing should reflect that.
2. **Concurrency surface is genuinely different.** `--resume` writes to a JSONL on disk; concurrent writes corrupt it (Anthropic #20992). Chat's stateless model has no such hazard. Forcing the two into one surface means the more-restrictive concurrency rules apply to everyone.
3. **Rollback is binary.** If Session has a regression, Chat is untouched and continues serving every existing user. No mixed-mode confusion, no flag races between two behaviors of the same UI element.

**What we are explicitly NOT doing**: replacing Chat, deprecating Chat, or hiding Chat. Both tabs exist forever (until/unless we later choose to converge).

---

## User-facing design

### Tab placement

Add a `Session` tab between `Chat` and `Schedules` in `AgentDetail.vue`'s tab row. Order: `Tasks | Chat | Session | Schedules | Playbooks | Credentials | Payments | Sharing | Permissions | Git | Files | Folders | Info`.

### Visual parity with Chat (day 1)

The Session tab is a **structural copy** of `ChatPanel.vue`. Same skeleton:

- Top-left: session selector dropdown ("Just now" / "2 hours ago" / "Yesterday" / explicit names)
- Top-right: model picker + **+ New Session** button
- Message list: same bubble rendering, same role layout
- Input box: same multiline, same mic, same `/` slash-command discovery
- SSE streaming, "Thinking…" labels, all of it identical

Users coming from Chat should feel zero learning curve in the first 30 seconds. Behavior diverges below the surface.

### What's different

| Element | Chat | Session |
|---|---|---|
| Model picker | "Default model" | "Default model" — same |
| New-session button | "+ New Chat" | **"+ New Session"** |
| Empty state copy | "Start a new chat with `<agent>`" | "Start a new session with `<agent>` — your conversation, tool results, and reasoning state will persist between turns." |
| Session selector subtitle | (none) | small "{N} turns · {context}% used · model {x}" badge per row, so users can see which sessions are getting heavy |
| Per-session menu (right-click or `…`) | "Delete chat" | **"Delete session"** + **"Reset memory"** (kills the JSONL, keeps the message history visible — see *Reset memory action* below) |

### Multi-session model (long-term memory)

Session sessions work the **same way Chat sessions do today** for storage and switching: each `agent_sessions` row is one independent conversation, listed in the session selector, switchable via the dropdown, and **never auto-deleted by switching away**. Click "Yesterday" → see yesterday's session messages in the list, type → continue that conversation with full Claude memory restored.

The new dimension vs. Chat: each `agent_sessions` row owns a `cached_claude_session_id` pointing at its private Claude Code JSONL on the agent disk. So switching from Session A to Session B does two things in lockstep:

1. Frontend re-fetches `agent_session_messages` for B and renders them.
2. Backend now resolves B's `cached_claude_session_id` for the next outgoing turn → `claude --resume <B's uuid>` reattaches to B's working memory.

```
agent_sessions (DB row)              .claude/projects/.../<uuid>.jsonl (Claude memory)
─────────────────────────────────    ────────────────────────────────────────────
s001 "Just now"        ─── owns ───→  3abcc2e4-...jsonl  ← full reasoning state
s002 "Yesterday"       ─── owns ───→  7f1b9d20-...jsonl  ← full reasoning state
s003 "Mon AM"          ─── owns ───→  b8e54c11-...jsonl  ← full reasoning state
```

This is **strictly more capable than Chat's long-term memory today**. Today, switching to an old chat lets you read the messages but Claude has zero memory of the prior reasoning when you continue. With Session, switching back to a 3-week-old session and typing → Claude truly picks up where it left off, with full tool context and intermediate calculations intact.

### "+ New Session" semantics

Creates a new `agent_sessions` row with empty `cached_claude_session_id`. The first turn of the new session will be a cold turn (no resume) but `persist_session=True`, so the JSONL is written and turn 2 onward can resume. **The old session's JSONL is NOT touched** — it stays on disk, owned by its row, available for future continuation.

### "Delete session" semantics

Deletes the `agent_sessions` row, all `agent_session_messages` rows for it, **and** marks the JSONL on the agent for cleanup (see *Retention & cleanup* below). This is the only path that removes Claude memory.

### "Reset memory" semantics (new — not in Chat)

Keeps the `agent_sessions` row and the message history visible to the user, but clears `cached_claude_session_id` and removes the JSONL. The next turn becomes a cold turn again. Useful when a session has accumulated context-window pressure but the user wants to keep the message log readable.

---

## Data model

### New tables (parallel to existing chat tables — no FK, no shared state)

```sql
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY,                           -- urlsafe token
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,                      -- ISO-Z
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',                  -- active | archived | reset
    subscription_id TEXT,
    cached_claude_session_id TEXT,                 -- THE primitive — Claude Code session UUID for --resume
    last_resume_at TEXT,                           -- when we last successfully resumed (observability)
    consecutive_resume_failures INTEGER DEFAULT 0, -- triggers cache-clear after 2 consecutive (#6 fallback)
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_agent_sessions_agent_user ON agent_sessions(agent_name, user_id);
CREATE INDEX idx_agent_sessions_status ON agent_sessions(status);

CREATE TABLE agent_session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,                            -- user | assistant
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,
    context_used INTEGER,
    context_max INTEGER,
    cache_read_tokens INTEGER,                     -- new vs. chat_messages — track prompt-cache hits
    tool_calls TEXT,                               -- JSON
    execution_time_ms INTEGER,
    claude_session_id TEXT,                        -- per-message UUID Claude actually ran under (audit)
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_agent_session_messages_session ON agent_session_messages(session_id);
CREATE INDEX idx_agent_session_messages_user ON agent_session_messages(user_id);
```

### What we're NOT doing

- We are **not** adding `cached_claude_session_id` to `chat_sessions`. The spike did this; the new approach keeps the two systems strictly separate.
- We are **not** adding columns to `public_chat_sessions` in Phase 1. Public/Slack/Telegram/WhatsApp keep text-replay (Phase 3 — see *Rollout phases*).
- We are **not** sharing migrations with the chat schema.

---

## Backend architecture

### New router

`src/backend/routers/sessions.py` — mirrors the structure of `routers/chat.py` for the `/task` flow but on a separate URL prefix. Same auth model (`AuthorizedAgent` + `get_current_user`), separate DB ops module.

```
POST   /api/agents/{name}/session                    Create new session row
GET    /api/agents/{name}/sessions                   List sessions for current user
GET    /api/agents/{name}/sessions/{id}              Get one session w/ recent messages
POST   /api/agents/{name}/sessions/{id}/message      Send a message — THE turn endpoint
POST   /api/agents/{name}/sessions/{id}/reset        Reset memory (clear cache + JSONL)
DELETE /api/agents/{name}/sessions/{id}              Delete session entirely
```

The turn endpoint internally:
1. Looks up the session, reads `cached_claude_session_id`.
2. Calls `task_execution_service.execute_task(...)` with `resume_session_id=cached_id` (or None for cold turn) and `persist_session=True`.
3. Extracts the real Claude UUID from `result.raw_response.execution_log` (workaround for the agent-server parser bug — see Appendix B).
4. Updates `cached_claude_session_id` if changed.
5. Persists user + assistant messages to `agent_session_messages`.
6. Returns the response.

### New DB ops module

`src/backend/db/sessions.py` → `SessionOperations` class. Methods:
- `create_session`, `get_session`, `list_sessions`, `delete_session`
- `get_session_messages`, `add_session_message`
- `get_cached_claude_session_id`, `update_cached_claude_session_id`, `clear_cached_claude_session_id`
- `mark_resume_failure` (increments `consecutive_resume_failures`) / `mark_resume_success` (resets it + sets `last_resume_at`)

Wired into `database.py` facade as `db.create_session`, etc. — same pattern as `ChatOperations`.

### `task_execution_service.execute_task` extension

Add **one** new optional parameter:

```python
async def execute_task(
    self,
    ...
    resume_session_id: Optional[str] = None,    # already exists (EXEC-023)
    persist_session: bool = False,              # NEW — passed to agent payload
) -> TaskExecutionResult:
    ...
    payload = {
        "message": message,
        ...
        "resume_session_id": resume_session_id,
        "persist_session": persist_session,     # NEW
    }
```

Existing callers (Chat router, schedules, MCP, fan-out, webhooks) **all keep `persist_session=False`** — zero behavior change. Only `routers/sessions.py` passes True.

This is the *only* change to a shared service. Everything else (router, store, frontend component, DB tables) is parallel and additive.

### Agent server (base image) changes

| File | Change |
|---|---|
| `docker/base-image/agent_server/models.py` | Add `persist_session: Optional[bool] = False` to `ParallelTaskRequest`. |
| `docker/base-image/agent_server/routers/chat.py` | Pass `persist_session` from request → runtime. |
| `docker/base-image/agent_server/services/runtime_adapter.py` | Add `persist_session: bool = False` to `execute_headless` ABC. |
| `docker/base-image/agent_server/services/claude_code.py` | In `execute_headless_task`: gate `--no-session-persistence` on `not persist_session`. When `persist_session=True` and not resuming, still pass `--session-id <uuid>` for unique namespace per task. |
| `docker/base-image/agent_server/services/gemini_runtime.py` | Accept `persist_session` parameter (ignore — Gemini CLI doesn't support resume). |

**Base image rebuild required** — `./scripts/deploy/build-base-image.sh`. Test agent containers must be recreated to pick up the new image. This is a one-time migration step.

### Frontend

| File | Change |
|---|---|
| `src/frontend/src/components/SessionPanel.vue` | New — copy of `ChatPanel.vue`, then swap API endpoints, store, terminology. |
| `src/frontend/src/stores/sessions.js` | New — copy of the chat store (whatever its current name is), swap endpoints. |
| `src/frontend/src/views/AgentDetail.vue` | Add `<Session>` tab between Chat and Schedules. Behind feature flag (see *Rollout phases*). |

**Important:** the frontend should NOT prepend `### Previous conversation:` text-replay context for Session turns. It sends only the bare current user message. The backend doesn't need that block on resume turns, and including it would double-replay.

---

## Touchpoints (sequenced)

### Phase 1 — Foundation (no UI yet, behind a flag)

| # | Component | What | Why |
|---|---|---|---|
| **1.1** | `db/schema.py` + `db/migrations.py` | Add `agent_sessions` + `agent_session_messages` tables. New idempotent migration `agent_sessions_tables`. | Clean schema separation from chat. |
| **1.2** | `db/sessions.py` + `database.py` | `SessionOperations` class + facade methods. | Match the existing class-per-domain invariant. |
| **1.3** | Agent server (base image) — parser fix | Recognize `{"type": "system", "subtype": "init"}` in `parse_stream_json_output` and the streaming parser, so `metadata.session_id` captures the real Claude Code UUID instead of falling back to the Trinity execution id. Also fall back to capturing `session_id` from the `result` event if init was missed. See Appendix B. | The agent-server parser bug we worked around in the spike. Fixing it at the source means Session, EXEC-023, and all `schedule_executions.claude_session_id` values become correct. |
| **1.4** | Agent server (base image) — persist_session | `persist_session` parameter through `ParallelTaskRequest` → router → runtime adapter → `execute_headless_task`. Gate `--no-session-persistence` on `not persist_session` (still pass `--session-id <uuid>` for unique namespace). | The hard dependency we hit in the spike. Must land before any session turns can succeed. |
| **1.5** | `services/task_execution_service.py` | Add `persist_session: bool = False` parameter. Pass to agent payload. Default false → all existing callers unaffected. | One shared touchpoint. |
| **1.6** | `services/settings_service.py` | Add `is_session_tab_enabled()` flag. Default false in code. Env override `SESSION_TAB_ENABLED`. | Kill-switch for staged rollout. |
| **1.7** | Tests | Unit tests for `SessionOperations` CRUD, migration idempotency, `task_execution_service` plumbing of `persist_session`, parser-fix coverage (system/init event yields the right UUID), and an agent-server test that JSONL is written when `persist_session=True` and not when False. | TDD bar — every touchpoint above gets a test before merge. |

### Phase 2 — Backend turn endpoint

| # | Component | What | Why |
|---|---|---|---|
| **2.1** | `routers/sessions.py` | The 6 endpoints listed above. Reuses `task_execution_service.execute_task`. | The actual feature. |
| **2.2** | `routers/sessions.py` | Resume-failure fallback: catch the `No conversation found` error from agent stderr → clear `cached_claude_session_id` → retry once with `resume_session_id=None`. Log structured warning `event=session_resume_fallback`. | The spike's biggest UX gap (touchpoint #6 from the original plan). |
| **2.3** | `routers/sessions.py` | Redis lock per `(agent_name, claude_session_id)` with 5-min TTL on resume turns. Wait-and-retry on contention (chat UX). | Anthropic #20992 mitigation. |
| **2.4** | Tests | Integration tests against a running testfix container: cold→resume happy path; resume against a missing JSONL → fallback fires; concurrent double-POST → second request waits on lock and serializes. | High-leverage tests — every spike-era bug gets one. |
| **2.5** | `main.py` | Mount the sessions router. | Wiring. |

### Phase 3 — Frontend

| # | Component | What | Why |
|---|---|---|---|
| **3.1** | `components/SessionPanel.vue` | Copy `ChatPanel.vue`, then: rename, swap API calls, drop the `buildContextPrompt` text-replay (send bare `user_message` directly), update placeholder copy. | The day-1 UI. |
| **3.2** | `stores/sessions.js` | Copy chat store, swap endpoints. | State management for the new surface. |
| **3.3** | `views/AgentDetail.vue` | Add Session tab gated on `is_session_tab_enabled()` (fetched via `/api/settings/session-tab` or similar). | Tab visibility. |
| **3.4** | `SessionPanel.vue` | "Reset memory" button + confirmation modal → `POST /sessions/{id}/reset`. | New affordance vs. Chat. |
| **3.5** | `SessionPanel.vue` | Per-session selector subtitle: turn count + context % + model. | Make session-health visible to the user. |
| **3.6** | Tests | Playwright e2e: create session → 3 turns → switch sessions → original session resumes correctly. Mark `@smoke`. | Existing test runner harness. |

### Phase 4 — Hardening + observability

| # | Component | What | Why |
|---|---|---|---|
| **4.1** | `routers/sessions.py` | Surface `cache_read_tokens` from each turn → write to `agent_session_messages.cache_read_tokens`. | Observe whether prompt caching is engaging. |
| **4.2** | New service: `session_cleanup_service.py` | Periodic job (every 6h): for sessions with `status='deleted'` or older than retention threshold, delete JSONL files in agent containers via the agent server. | Bounded disk growth. |
| **4.3** | Cross-session contamination test | Manual + automated test for Anthropic #26964: create session A, run a tool call that produces a known unique string; create session B (different UUID, same agent, same cwd); verify session B has no recall of A's tool result. **If contamination is observed, abort Phase 4 and re-evaluate.** | The single biggest unanswered security question. |
| **4.4** | `architecture.md` | Document Session tab + invariants. | Architectural-invariants compliance. |
| **4.5** | `feature-flows/session-tab.md` | Vertical-slice flow document. | Per Trinity Rules of Engagement (tiered docs). |

### Phase 5 — Rollout

| # | Component | What | Why |
|---|---|---|---|
| **5.1** | Internal QA | Flag-on for admin user only. Run for one week. Watch error rate, cost, fallback frequency. | Soak. |
| **5.2** | Limited release | Flag-on for `creator` role and above. Two-week soak. | Wider exposure, controlled. |
| **5.3** | General availability | Flag default true in code. Documented in user-docs. | Default behavior. |
| **5.4** | Channel adapters (separate doc) | If GA goes well, plan a Phase C adding `--resume` to single-user DM channels (Slack/Telegram/WhatsApp). Group chats and public chat stay on text-replay. | Out of scope for this doc. |

---

## Edge cases & failure modes (lessons baked in from the spike)

This is the section that pays for itself by preventing repeats. Each row maps to a specific failure we hit on 2026-04-29 in the spike, with the prevention baked into the plan.

| # | What broke in the spike | Symptom | Fix in this plan |
|---|---|---|---|
| **L1** | Agent-server parser misses `system/init` | `metadata.session_id` stays None → `final_session_id = task_session_id` (execution id with `EX-` prefix). We cached the bogus value, `claude --resume EX-...` errored "not a UUID". | Phase 2.2: backend extractor scans `execution_log` for `system/init` UUID. Doesn't trust `result.session_id`. Also Appendix B: file an issue to fix the parser at the source. |
| **L2** | Cold turn passed `--no-session-persistence` | JSONL was created with only the auto-title, no conversation body. Turn 2's `--resume` errored "No conversation found." | Phase 1.3: `persist_session` parameter through the stack. Phase 2.1: turn endpoint always passes `persist_session=True`. |
| **L3** | First turn of a brand-new session has no `chat_session_id` (frontend hasn't received one yet) | Resolver bailed at `if not request.chat_session_id` before checking the flag. Cold turn skipped persistence. Turn 2 cached the stub UUID, resume failed. | Phase 2.1: the turn endpoint creates the `agent_sessions` row **before** calling `execute_task`, so `session_id` always exists when the cold turn fires. Cleaner separation than the spike's frontend-first model. |
| **L4** | We removed the agent-testfix container before Trinity could recreate it | `Agent not found` 404 from start endpoint. Required manual placeholder container with all env vars + correct SSH port. | Operational note: rolling back the new base image should always go through Trinity's recreate flow, not raw `docker rm`. Add a runbook entry in Phase 4.4. |
| **L5** | SSH port 2222 was held by `agent-trinity-system`, not free | Trinity's recreate failed to bind. | Operational note: when manually creating placeholder containers for testing, query `docker ps --filter label=trinity.platform=agent --format '{{.Label "trinity.ssh-port"}}'` for free ports first. |
| **L6** | Anthropic API key dumped into terminal via `docker inspect` | Sensitive value visible in agent env. Not a leak (local-only) but a prompt to flag. | Operational note: when debugging containers, prefer `docker inspect <c> | jq 'del(.Config.Env)'`. Doc this in the runbook. |
| **L7** | Audit row stored `request.message` (frontend's full text-replay block) instead of `effective_message` (what Claude actually got) | Confusing for debugging. | Phase 2.1: `agent_session_messages.content` stores the bare user message. The execution row keeps `request.message` as audit history but we add a clear UI indicator: "session resumed — claude saw bare message + prior session memory". |

### New edge cases we identified during the spike review (not yet hit)

| # | Edge case | Mitigation |
|---|---|---|
| **E1** | JSONL exceeds context window after many turns | Phase 4.5 telemetry: track `context_used / context_max` per turn. UI warns at 75%. At 90%, suggest "Reset memory" (touchpoint 3.4). At 100%, the next turn fails — fallback (2.3) clears cache and starts cold. |
| **E2** | Anthropic's `cleanupPeriodDays` deletes the JSONL behind our back (#39667) | Fallback (2.3) catches the resume failure, clears cache, retries cold. User sees a one-time "this session's working memory expired — starting fresh" inline notice. |
| **E3** | Claude Code CLI upgrade breaks existing JSONLs (#53417) | Same fallback. Plus: pin Claude Code version in the base-image Dockerfile and document the upgrade procedure (test before bumping). |
| **E4** | User opens Session tab in two browser tabs simultaneously | Phase 2.4 Redis lock serializes. UI in the second tab shows "another turn is in progress" until the lock releases. |
| **E5** | Cross-session contamination (Anthropic #26964) | Phase 4.3 test gates GA. If contamination is real, Session must run with one cwd per session — significant re-architecture. |
| **E6** | User shares an agent with a colleague; both open Session simultaneously | `agent_sessions.user_id` keys per-user. Each user has their own session list. Within a single user's sessions, lock applies. |
| **E7** | Subscription change mid-session (e.g. user moves agent from API key to Claude Max OAuth) | The cached UUID is from the previous auth context. First turn after auth change might fail. Fallback handles it. Optional polish: clear cache on subscription change in `recreate_container_with_updated_config`. |
| **E8** | Agent recreated (e.g. resource-limit change) — new container, new workspace volume | Workspace volume is named and survives recreation. JSONLs persist. ✓ |
| **E9** | Workspace volume migrated to new host (DR scenario) | Backups of `~/trinity-data/` cover the DB but not Docker volumes. **Open question:** include named workspace volumes in the backup script? Phase 4.4 task. |
| **E10** | User pastes a very long message (e.g. 50K-token document) | Persisted into the JSONL forever. Same context-saturation pressure as E1, but immediate. UI warns at message-paste time if input would exceed 25K tokens. |
| **E11** | Slash command (`/playbook`) called mid-session | The slash command's expansion goes into Claude's working memory. Subsequent turns see the expansion. Behavior matches existing slash-command UX, just persistent. |
| **E12** | Operator queue interaction (mid-skill approval gate) | The primary user-value scenario. Scenario B in *Validation* below validated it. Worth a dedicated test in Phase 2.5. |

---

## Validation

### Spike evidence (already collected, 2026-04-29)

**Scenario A — Sequential web chat, 3 turns:**
- Used `agent-testfix` with the chat-resume-default flag enabled.
- 3 turns of trivial messages.
- Result: all 3 executions reattached to **same** Claude session UUID `2a499a28-b640-4cc9-87c2-9a7900155fcc`.
- `cache_read_tokens=11636` on every turn (steady prompt-cache hit).

**Scenario B — Mid-skill approval gate (the actual user-value claim):**
- Custom `/test-approval` skill: step 2 picks a random N=4827391, **does not reveal it**; step 3 asks "yes/no?"; step 4 writes the value to disk.
- After "yes", agent wrote `widget-4827391` to `spike-output.txt`.
- Verification: line 16 of the JSONL captured turn 1's thinking: `"Let me pick: 4827391. Combined value: widget-4827391."` Timestamped `12:01:06.595Z` — **before** turn 2's user message at `12:01:57.808Z`.
- The number `4827391` does not appear in turn 1's user-visible reply; text-replay alone could not have produced it on turn 2.
- Same Claude UUID across both turns: `3abcc2e4-c815-4a71-ae40-caf49cb9d71f`.
- **The thesis is empirically validated.** Implementation work is engineering, not science.

### Tests required before each phase ships

#### Phase 1 — unit + agent-server tests

- `tests/unit/test_session_operations.py` — CRUD round-trip for sessions and messages.
- `tests/unit/test_agent_session_persistence.py` — agent server: with `persist_session=True`, JSONL exists with > 1 line after a cold turn; with `persist_session=False`, JSONL either doesn't exist or has only the title stub (today's behavior).
- `tests/unit/test_task_execution_persist_session.py` — service plumbs `persist_session` to the agent payload; existing callers unaffected.

#### Phase 2 — integration tests against live testfix

- **Scenario A** automated: 3-turn chat → assert same `claude_session_id` in `agent_session_messages`.
- **Scenario B** automated: skill that picks a hidden random, assert post-turn-2 file contents match grep of turn-1 thinking in JSONL.
- **Scenario C — fallback:** delete the JSONL between turn 1 and turn 2 → turn 2 succeeds via fallback, `consecutive_resume_failures` resets after subsequent success.
- **Scenario D — concurrency:** two POSTs simultaneously to the same session id → second waits on Redis lock, both succeed in order.
- **Scenario E — switching sessions:** create A, send 2 turns; create B, send 2 turns; switch back to A, send 1 more turn → A's claude_session_id matches across turns 1, 2, and 3 (the post-switch one).

#### Phase 3 — frontend e2e

- Playwright test: open Session tab → "+ New Session" → send 3 messages → switch sessions → verify message lists are separate → switch back → continue.

#### Phase 4 — security/contamination

- **The cross-session contamination test (#26964 mitigation, Phase 4.3):**
  - Create session A. Send: "remember this secret password: PURPLE-DRAGON-9173. Don't write it anywhere."
  - Verify in JSONL: secret is in turn-1 thinking, not in any tool input.
  - Create session B (new UUID, same agent, same cwd `/home/developer`).
  - Send to session B: "what's the secret password from another session?"
  - **Expected**: agent doesn't know — "I have no information about a secret password from another session."
  - **Fail condition**: agent leaks `PURPLE-DRAGON-9173`. If this happens, `--resume` is unsafe in shared-cwd mode and we abort GA.

#### Phase 5 — soak metrics

- `consecutive_resume_failures > 2` rate < 1% of sessions
- Resume-fallback fire rate < 5% of turns
- p99 turn latency within 1.2× of Chat tab baseline
- Cost per turn average within ±15% of Chat tab baseline (cache hits should make Session cheaper on average)

---

## Retention & cleanup policy

### What lives forever

- `agent_sessions` rows (until user deletes the session) — backed up via `~/trinity-data/trinity.db` snapshot
- `agent_session_messages` rows — same

### What needs cleanup

- JSONL files in `/home/developer/.claude/projects/<cwd-hash>/<uuid>.jsonl` inside each agent container

### Cleanup triggers

| Trigger | Action |
|---|---|
| User clicks "Delete session" | Backend deletes DB rows + calls agent server `/api/sessions/cleanup` with the JSONL UUID. Agent server deletes the file. |
| User clicks "Reset memory" | Same JSONL deletion, but DB rows remain. `cached_claude_session_id` cleared. |
| Periodic cleanup (every 6h via `session_cleanup_service.py`) | For all `agent_sessions.status='deleted'` rows, attempt JSONL deletion (best-effort, retry on failure). |
| Per-session age limit | **Phase 5+.** Sessions inactive for > 90 days warn user; > 180 days auto-archive (clear JSONL but keep messages). Configurable via settings. |

### What we are explicitly **NOT** doing

- We are NOT relying on Anthropic's `cleanupPeriodDays` — it's silent and unreliable (#39667).
- We are NOT auto-deleting JSONLs on container restart — the workspace volume persists by design.
- We are NOT compacting JSONLs in place. If a session hits context-window pressure, the user must "Reset memory" or "Delete session" — explicit user action only.

---

## Observability

### New metrics

| Metric | Source | Why |
|---|---|---|
| `session_resume_success_total{agent}` | Increment in `routers/sessions.py` after resume succeeds | Baseline rate |
| `session_resume_failure_total{agent,reason}` | Increment in fallback path with reason label (`session_not_found`, `corrupt_jsonl`, `lock_timeout`, `other`) | Detect Anthropic regressions per-CLI-version |
| `session_cache_read_tokens{agent}` | Per-message `cache_read_tokens` field | Did caching engage? |
| `session_jsonl_bytes{agent}` | Per-session JSONL size, sampled hourly | Disk growth |
| `session_turn_latency{agent,phase}` | Per-turn duration broken down by `cold | resume | fallback` | Compare regimes |

### New audit events (`audit_log` table — SEC-001)

| Event | Action | Reason |
|---|---|---|
| `session_lifecycle` | `create` | Session created |
| `session_lifecycle` | `delete` | Session deleted (user-initiated) |
| `session_lifecycle` | `reset` | Memory reset (user-initiated) |
| `session_lifecycle` | `auto_archive` | Periodic cleanup deleted a JSONL |
| `session_resume` | `success` / `fallback` / `failure` | Per-turn outcome (sampled — full would be too verbose) |

### UI surfaces

- Per-session subtitle in selector: turns / context / model (touchpoint 3.5)
- Optional admin-only "show session JSONL diff" link in the execution detail view (deferred to a follow-up PR)

---

## Security checklist

| Concern | Mitigation in this plan |
|---|---|
| Cross-session contamination (#26964) | Phase 4.3 test gates GA. |
| Persistent prompt-injection | The JSONL retains injected content forever within a session. Mitigations: (a) "Reset memory" button gives users a clear out, (b) we don't share JSONLs across sessions, (c) document the risk in user docs. |
| Persistent PII | User-pasted PII goes into the JSONL. Same retention policy as `chat_messages` today (per-tenant volumes, encryption-at-rest via Docker volume + LUKS if configured). |
| Cross-tenant access | Agent containers are single-tenant by design. Volumes are per-agent. Resume can't cross agents. |
| Prompt-cache poisoning | `cached_claude_session_id` is only ever resolved from the same `agent_sessions` row that owns it. Lookup is keyed on `(session_id, user_id)` — no row hopping. |
| Audit completeness | `agent_session_messages.content` stores user-visible content. JSONL stores reasoning. Both are attributable to a user via `agent_sessions.user_id`. |
| Workspace volume DR | Phase 4.4 doc task: extend backup script to cover named workspace volumes. |
| Local-machine `docker inspect` exposing API keys | Operational hygiene; flag in runbook (lesson L6). |
| Public/anonymous chat exposure | Out of scope — Phase 1–4 is web-Chat-tab only. Public chat stays on text-replay. |

---

## Rollout phases (revisited from end-to-end perspective)

```
Phase 1 — Foundation        ← schema, agent base image, service plumbing, all behind flag (default off)
Phase 2 — Backend turn       ← turn endpoint, fallback, lock, parser-bug workaround
Phase 3 — Frontend           ← SessionPanel.vue, sessions store, tab gated on flag
Phase 4 — Hardening          ← observability, cleanup service, contamination test, docs
Phase 5 — Rollout            ← admin-only → creator+ → GA → channel adapters (next doc)
```

Each phase ends with a merged PR. Phases are NOT collapsed into one mega-PR. Reviewers can land Phase 1 even before Phase 2 is started — the flag-off default means no user-facing change.

---

## Branch naming

- Spike branch (rolled back, learnings captured here): `spike/chat-resume-default` — to be deleted.
- Implementation branch: **`feature/session-tab`** — branched from `dev` after this doc lands.
- Sub-PRs: `feature/session-tab-phase-1`, `feature/session-tab-phase-2`, etc., each merging back to `feature/session-tab`. The integration branch merges to `dev` once Phase 5 is ready.

---

## Open questions (need answers during the local build)

1. **Workspace volume backup**: extend the backup script in Phase 4 or treat as a follow-up? (Recommendation: follow-up — not on the critical path.)
2. **Subscription-change cache invalidation (E7)**: ship in Phase 2 or defer? (Recommendation: defer — fallback handles it.)
3. **JSONL access for admins via UI**: ship in Phase 4 or defer? (Recommendation: defer — `docker exec` is sufficient until then.)
4. **Branch strategy during local build**: single integration branch with sequential commits, or sub-branches per phase that merge into `feature/session-tab`? (Recommendation: single branch with phase-tagged commits — simpler since nothing leaves local until validation passes.)

---

## Appendix A — Spike learnings (preserved from `spike/chat-resume-default`, 2026-04-29)

The spike branch validated the thesis end-to-end but was kept local-only and rolled back per the agreed plan. Key technical learnings, in chronological order of discovery:

1. **Agent-server stream parser misses `system/init`** (Appendix B). Workaround: backend extractor reads `result.raw_response.execution_log` and pulls the UUID from the `system/init` event. This works around the bug for the *write* side (caching) but doesn't fix EXEC-023's "Continue Execution as Chat" which has the same broken assumption.

2. **`--no-session-persistence` blocks resume.** The agent server unconditionally added this flag on cold turns, so the JSONL was created but empty — turn 2's `--resume` then found "no conversation found." Fix: gate on a new `persist_session` flag through the stack.

3. **First turn of a new chat has no `chat_session_id` yet** (frontend creates it server-side after the first response). Resolver must not bail before checking the persist requirement. New plan creates the `agent_sessions` row in the turn endpoint *before* invoking `execute_task`, so the row always exists.

4. **Removing an agent container manually breaks Trinity's start endpoint**, which requires the container to exist for `recreate_container_with_updated_config` to read its old config. Must always go through Trinity's stop/start flow.

5. **SSH port allocation isn't tracked in DB**, only in container labels. Manually re-creating a placeholder container requires picking a free port (e.g. `2223+` since `2222` is held by `agent-trinity-system`).

6. **`docker inspect` exposes Anthropic API keys.** Not a security incident — the user already controls the local Docker socket — but worth a runbook entry to redact env when sharing inspect output.

7. **Cost savings are real but smaller than the spike doc claimed.** With short user turns, the system prompt dominates and prompt-cache wins are modest. The win compounds over long, multi-turn reasoning sessions — exactly the use case Session is built for.

8. **`task_execution_service.execute_task` already had `resume_session_id` plumbing from EXEC-023.** We only need to add `persist_session`. No major shared-service refactor.

The diff from the spike (~238 insertions across 12 files) was stashed under `git stash@{0}: spike/chat-resume-default WIP` for reference. It will be dropped after the implementation branch's Phase 1 lands.

---

## Appendix B — Agent-server stream parser fix (folded into Phase 1.3)

**Severity:** Medium (silently corrupts session ID metadata; affects EXEC-023 "Continue Execution as Chat" in production today, and would block Session tab if left unfixed).
**File:** `docker/base-image/agent_server/services/claude_code.py` lines 189 + 327.
**Root cause:** Parser checks `if msg_type == "init":` but Claude Code emits `{"type": "system", "subtype": "init", "session_id": "..."}`. So `metadata.session_id` stays None, and `final_session_id = task_session_id` (line 1466) returns the Trinity execution id (with `EX-` prefix).

**Fix (lands in Phase 1.3):**
```python
if msg_type == "system" and msg.get("subtype") == "init":
    metadata.session_id = msg.get("session_id")
elif msg_type == "result":
    # The result event also carries session_id — useful as a fallback
    # in case the init message wasn't seen (e.g. truncated stream).
    if not metadata.session_id:
        metadata.session_id = msg.get("session_id")
```

Both `parse_stream_json_output` (line ~189) and the streaming variant (line ~327) need the same change.

**Impact of fix:**
- Session tab can trust `result.session_id` directly — no backend extractor needed.
- EXEC-023 "Continue Execution as Chat" stops returning bogus UUIDs.
- `schedule_executions.claude_session_id` becomes correct for all callers (schedule executions, parallel tasks, MCP, fan-out).

**Test in Phase 1.7:** unit test that feeds a synthetic stream with the real `system/init` shape and asserts `metadata.session_id` is the embedded UUID. Also test the result-event fallback path (init absent, result has session_id) to guard against truncated streams.

---

## Appendix C — File index of expected changes

### Phase 1
- `src/backend/db/schema.py` (+ ~30 lines: new tables)
- `src/backend/db/migrations.py` (+ ~25 lines: new migration)
- `src/backend/db/sessions.py` (new file, ~150 lines)
- `src/backend/database.py` (+ ~10 lines: facade methods)
- `src/backend/services/task_execution_service.py` (+ ~3 lines: new param + payload)
- `src/backend/services/settings_service.py` (+ ~15 lines: flag accessor)
- `docker/base-image/agent_server/models.py` (+ ~1 line)
- `docker/base-image/agent_server/routers/chat.py` (+ ~2 lines)
- `docker/base-image/agent_server/services/runtime_adapter.py` (+ ~2 lines: ABC update)
- `docker/base-image/agent_server/services/claude_code.py` (+ ~10 lines: gate + new branch)
- `docker/base-image/agent_server/services/gemini_runtime.py` (+ ~2 lines: param accept-and-ignore)
- `tests/unit/test_session_operations.py` (new, ~200 lines)
- `tests/unit/test_agent_session_persistence.py` (new, ~100 lines)
- `tests/unit/test_task_execution_persist_session.py` (new, ~80 lines)

### Phase 2
- `src/backend/routers/sessions.py` (new, ~400 lines)
- `src/backend/main.py` (+ ~3 lines: router mount)
- `tests/integration/test_session_turns.py` (new, ~300 lines)

### Phase 3
- `src/frontend/src/components/SessionPanel.vue` (new, ~600 lines via copy)
- `src/frontend/src/stores/sessions.js` (new, ~250 lines via copy)
- `src/frontend/src/views/AgentDetail.vue` (+ ~10 lines: tab + flag check)
- `tests/e2e/session-tab.spec.ts` (new, ~120 lines)

### Phase 4
- `src/backend/services/session_cleanup_service.py` (new, ~150 lines)
- `src/backend/main.py` (+ ~5 lines: register service)
- `docs/memory/architecture.md` (+ ~80 lines: Session tab section + invariants)
- `docs/memory/feature-flows/session-tab.md` (new, ~300 lines)
- `tests/integration/test_session_cross_contamination.py` (new, ~100 lines)

**Total new code estimate: ~2700 LOC across all phases.** Comparable to other vertical slices in the codebase (e.g. SLACK-002 was ~2400 LOC).

---

## Sign-off

This plan supersedes the spike branch. The implementation runs **entirely locally** until validation passes; only then do we engage the standard SDLC.

### Pre-implementation prerequisites

1. Spike branch deleted from local (no remote presence to clean up).
2. Stock `dev` agent base image rebuilt and `agent-testfix` recreated to reset spike state.
3. New branch `feature/session-tab` cut from the current `dev` tip.

### Pre-push prerequisites (after Phase 5 local validation passes)

1. Doc committed to `dev` (or to the feature branch — taste call at that point).
2. GitHub issue opened referencing this doc.
3. Branch pushed to origin.
4. PR opened against `dev`, validated via `/validate-pr`, reviewed.

The split keeps GitHub interaction focused on the cleaned-up, validated outcome rather than the in-progress build.
