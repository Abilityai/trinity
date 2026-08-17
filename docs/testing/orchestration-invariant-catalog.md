# Trinity Orchestration Invariant Catalog

> **Purpose**: Catalog of system invariants that must hold across Trinity's orchestration layer, and the design approach for a continuous canary harness that verifies them on staging/dev.
>
> **Status**: Proposal — pending research & implementation (see tracking issue).

---

## Motivation

Trinity has accumulated a class of orchestration bugs that unit tests don't catch:

- **PR #378/#403** — *phantom stale-slot failures*: cleanup service's Phase 3 marked executions FAILED based on a stale Phase 0 snapshot, then the real SUCCESS arrived and overwrote it. User saw a failure flash that wasn't real.
- **PR #407/#410** — *subprocess reaping*: Claude subprocess children kept stdout pipes open; reader thread hung forever; agent-server spun at 83% CPU until container restart.
- **Issue #129** — *orphaned executions*: DB rows stuck in `running` because the agent completed the task but never reported back. No passive cleanup caught this.
- **Issue #219/#226** — *slot TTL vs. execution timeout mismatch*: slots expired while legitimate long tasks were still running.

Each of these violated a property that was *obvious in hindsight*:
- "A terminal execution state must be immutable."
- "Every subprocess must be reapable on every exit path."
- "Every `running` execution must map to either an agent registry entry or a cleanup action within one cycle."
- "Slot TTL must be ≥ execution timeout."

These are **invariants** — properties that must *always* hold. When written down, they become testable, and a continuous test harness can verify them 24/7 against a live staging instance.

This document catalogs those invariants and proposes the harness that tests them.

---

## Framework

Testing orchestration systematically rests on five ideas, in order of leverage:

### 1. Invariants-first, not assertions-first
Before writing any test code, write the catalog. Invariants are properties of the *system state*, not properties of *one flow's outcome*. A test suite built on assertions catches known bugs; one built on invariants catches structural drift.

### 2. Black-box canary harness
A pytest suite running against the staging API as an ordinary user: create agent → schedule → trigger → observe → assert invariants from DB + Vector logs. Treats Trinity as a box. Runs on cron or as a scheduled Trinity agent. Cheap, catches ~80% of real issues.

### 3. Property-based scenario generation (Hypothesis stateful)
Model the agent lifecycle as a state machine (idle/queued/running/completed/failed + slot + backlog state). Hypothesis generates random valid command sequences — finds the weird interleavings humans never write by hand. Best fit for orchestration bugs.

### 4. Chaos layer (opt-in)
Pumba/toxiproxy to kill agent containers mid-run, drop Redis connections, slow the Docker socket. The gate: does cleanup reconcile? do slots release? does reaping hold? This is where #407-class bugs hide.

### 5. Load/concurrency harness
k6 or Locust driving parallel chat + schedule triggers — exposes slot leaks, race conditions in the queue.

**Design principle**: treat staging as a permanent tenant of a "chaos-canary" system — synthetic fleet, synthetic workloads, real infra. The goal isn't pass/fail on a feature, it's **proving invariants hold under adversarial conditions 24/7**.

---

## Invariant tiering

Invariants are grouped by subsystem. Each entry has a **tier** and **severity**:

- **Tier A (always)** — must hold at every observable instant. Violation = bug.
- **Tier B (eventually ≤ T)** — must reconcile within SLA T (driven by cleanup cycles). Violation beyond T = bug.
- **Severity**:
  - 🔴 **critical** — corruption, loss, or stuck state
  - 🟡 **major** — user-visible wrong state
  - 🟢 **minor** — drift, eventual self-healing

Each invariant is expressed so it is directly checkable — as a SQL predicate, Redis query, or Docker state diff. "Signal" is the exact query the canary harness runs.

---

## 1. Execution lifecycle (`schedule_executions`)

State machine: `queued → running → {success, failed, cancelled}`. See `src/backend/services/task_execution_service.py:288-598`, `src/backend/models.py:TaskExecutionStatus`.

**E-01** Terminal-state closure *(Tier B ≤ timeout + 5 min, 🔴)*
Every execution reaches a terminal state within its timeout + slot buffer.
Signal: `status='running' AND started_at < now() - (timeout_seconds + 300s)
AND (lease_expires_at IS NULL OR lease_expires_at < now() - 600s)` → must be 0.
⚠️ **The lease clause is load-bearing (#1990), and so is its 600s bound.** A #1081 pull-CLAIMED row is
`running` but owned **exclusively** by the lease-reaper, which re-queues or poison-parks it at
`MAX_REDELIVERY` — so its age is not evidence of a stuck execution. The windows are in fact identical
(`claim_next_queued` stamps the lease at `started_at + timeout + SLOT_TTL_BUFFER`), so without the clause E-01
fired at the *instant* the reaper became eligible to act, with zero head-room, on every re-delivery — a
**critical** stream indistinguishable from the real lights-out condition during the #1766 soak. Mirrors S-01,
E-05 (#1982), and the six `lease_expires_at IS NULL` sweep exclusions in `db/schedules/` — including
`mark_stale_executions_failed`, the sweep whose failure E-01 exists to detect. NULL-lease (push) rows are
unaffected.
**The grace is bounded, not an exclusion.** `600s` = 2 × `cleanup_service.CLEANUP_INTERVAL_SECONDS`, the loop
the reaper runs in: a healthy reaper resolves an overdue lease within one 300s cycle (`requeue_expired_lease`
clears the lease and resets `started_at` in one atomic UPDATE; `park_expired_lease` goes terminal), so
observable overdue-ness tops out at one interval and the second interval is head-room. Past 600s the reaper has
skipped multiple of its own cycles and the row fires — a **different diagnosis**, carried in `observed_state`
as `lease_expires_at` / `lease_overdue_seconds` and split in the Slack runbook hint: the *lease-reaper* has
failed, not `cleanup_service`'s stale-row watchdog. This is the automated owner of §9 **M4** in
`PULL_MIGRATION_TESTING.md` (a #1766 abort criterion); a blanket exclusion would have left it to a human
pasting SQL. **The constant is coupled to `CLEANUP_INTERVAL_SECONDS`** and
`tests/unit/test_1990_e01_lease_awareness.py` asserts the 2× relation so the coupling cannot drift.

**E-02** No phantom reversal *(Tier A, 🔴)* — the #378/#403 invariant.
Once an execution is in a terminal state, its `status` is immutable for the rest of its life.
Signal: audit-log every status transition; any `{success|failed|cancelled} → *` after that = violation.
🚫 **No lease exclusion here — deliberately (#1990).** E-02 is the fourth reader of the running-row set and the
only one that keeps seeing leased rows: a terminal→non-terminal reversal is corruption regardless of who owns
the row, the reaper cannot produce one (`requeue_expired_lease` / `park_expired_lease` both CAS on
`status='running'` with a past lease, so a terminal row is unreachable to them), and re-delivery *preserves*
the `execution_id` (#1084/#525 are execution_id-scoped). Pull is **more** exposed to this bug class than push —
a late worker result races a reaper pass for the same row — so copying #1990's grace here "for consistency"
would blind E-02 for 600s on exactly the path #1081 adds.

**E-03** Completed rows are fully populated *(Tier A, 🟡)* — ✅ **SHIPPED Phase 4, #1077** (registry id `E-03`).
`status IN (success, failed, cancelled)` ⇒ `completed_at IS NOT NULL AND duration_ms IS NOT NULL`.
⚠️ **Implemented predicate deviates: `completed_at IS NOT NULL` ONLY.** The `+ duration_ms IS NOT NULL`
clause was dropped because it false-fires in bulk on healthy queue-terminated rows — `cancel_queued_for_agent`
/ `fail_queued_for_agent` / `expire_stale_queued` (`db/schedules.py`) set `completed_at` but never `duration_ms`
(only ran-to-completion `update_execution_status` computes it). Windowed on `started_at` (`max timeout + 300s`,
`LIMIT 5000`) — a leading-edge regression tripwire, not a 90-day backfill auditor. `skipped` rows are excluded
by the collector (they legitimately have no `completed_at`).

**E-04** Queued rows have metadata *(Tier A, 🟡)* — ✅ **SHIPPED Phase 4, #1077** (registry id `E-04`; stacked on #1450).
`status='queued'` ⇒ `queued_at IS NOT NULL AND backlog_metadata IS NOT NULL AND json_valid(backlog_metadata)`
(the implemented predicate uses `json.loads`, catching `JSONDecodeError`/`TypeError`). Protects
`backlog_service.drain_next` against `json.JSONDecodeError`. Reads the queued-row metadata `_collect_executions`
captures, scoped strictly to `status='queued'` rows (never terminal — #1449-safe). Older-image DDL without the
`queued_at`/`backlog_metadata` columns → skips the eid (fail-open). **SECURITY:** `observed_state`/`signal_query`
report only the failed-predicate reason code (`queued_at_null`/`backlog_metadata_null`/`backlog_metadata_invalid_json`)
+ ids — never the raw `backlog_metadata` (may carry credentials; violations persist to `canary_violations`).

**E-05** Dispatched rows have session *(Tier B ≤ 60 s, 🟡)* — Issue #106 guard.
`status='running' AND started_at < now() - 60s` ⇒ `claude_session_id IS NOT NULL` (even just `'dispatched'`). If not, `mark_no_session_executions_failed` should have fired.

**E-06** No stuck "completed-on-agent-but-not-reported" *(Tier B ≤ 5 min, 🔴)* — Issue #129 invariant.
For every `status='running'` row with `started_at < now() - 60s`, the agent's `/api/executions/running` must report the `execution_id`. If not, watchdog must mark it failed within one cycle.
Signal: cross-check DB × agent registry; violations older than one cycle are true orphans.
> ⚠️ **Catalog-id ≠ registry-id drift:** the shipped registry id `E-06` is a *different* invariant — "no overdue `next_run_at`" (#1472) — not this catalog #129 check (which remains unimplemented). When stamping catalog entries "shipped", map to the registry id explicitly (as E-03/G-03 above do) rather than assuming a 1:1 correspondence.

**E-07** Retry chain integrity *(Tier A, 🟢)*
`retry_of_execution_id IS NOT NULL` ⇒ referenced row exists and has same `agent_name` and `schedule_id`.

**E-08** Cancellation is sticky *(Tier A, 🔴)*
Once `status='cancelled'`, no service writes a different terminal state (see `task_execution_service.py:498, 548`). Check: watchdog's `mark_execution_failed_by_watchdog` refuses to overwrite `cancelled`.

---

## 2. Slots ↔ executions (`slot_service.py` / Redis ZSET)

**S-01** Slot–row bijection *(Tier A, 🟠 — downgraded 🔴→🟠 major by #1082: redundant under single-owner status, retires with the slot ZSET in #1081 Phase 5)* — THE core orchestration invariant.
For every agent A: `ZMEMBERS(agent:slots:A)` = `{row.id for row in schedule_executions where agent_name=A and status='running'}` ∪ `{sentinel drain tokens < ~5s old}`.
Signal: symmetric diff of both sets. Drift here = the #219/#226/#378 class of bugs.

**S-02** No overbooking *(Tier A, 🔴)*
`ZCARD(agent:slots:A) ≤ agent_ownership.max_parallel_tasks` at all times. Violation ⇒ `acquire_slot` bypass.

**S-03** Slot TTL ≥ execution timeout *(Tier A, 🔴)* — TIMEOUT-001/#226.
For every member of `agent:slots:A`, the companion `agent:slot:A:{eid}` HASH has `TTL ≥ timeout_seconds + SLOT_TTL_BUFFER(300)`.
Signal: `redis TTL agent:slot:A:{eid}` < `timeout_seconds` ⇒ premature-expiry bug.

**S-04** Metadata–membership consistency *(Tier A, 🟡)*
`eid ∈ ZSET(agent:slots:A)` ⇔ `EXISTS agent:slot:A:{eid}` HASH.
Signal: asymmetric presence ⇒ `acquire`/`release` path forgot a step.

**S-05** Release is idempotent and ordered *(Tier A, 🔴)*
After `release_slot(A, eid)`: (a) `eid ∉ ZSET(agent:slots:A)`, (b) `agent:slot:A:{eid}` deleted, (c) execution row is in terminal state.
Signal: released-but-row-still-running triples ⇒ slot leak.

**S-06** No slot resurrection *(Tier A, 🔴)*
Once a slot is released for `eid`, no subsequent `ZADD agent:slots:A eid` should ever occur (ids are single-use).
Signal: audit slot ops; duplicate `ZADD` with same `eid` ⇒ bug.

**S-07** Sentinel lifetime bounded *(Tier A, 🟢)* — BACKLOG-001 drain.
Any `drain-{agent}-{ts}` sentinel in a slots ZSET lives < 10 s. (The drain in `backlog_service.py:158-189` holds it only briefly.) Stale sentinels ⇒ drain crash mid-claim.

---

## 3. Backlog ↔ queued executions (`backlog_service.py`)

**B-01** Queue-status coherence *(Tier A, 🔴)*
`backlog_service.get_queued_count(A) = COUNT(schedule_executions WHERE agent_name=A AND status='queued')`. Backlog never has its own table — queued rows ARE the queue.

**B-02** No queued without slots-full *(Tier B ≤ 60 s, 🔴)*
If `COUNT(status='queued' AND agent_name=A) > 0`, then either (a) `ZCARD(agent:slots:A) = max_parallel_tasks` or (b) a drain callback/maintenance tick is pending (≤60 s SLA).
Signal: queued rows while slots have free space ⇒ drain callback failed. `drain_orphans_all` is the backstop every 60 s.

**B-03** Claim atomicity *(Tier A, 🔴)*
At most one drain wins for a given queued row (enforced by single-row `UPDATE … RETURNING` in `claim_next_queued`). Check: count of transitions `queued → running` per execution_id = exactly 1.

**B-04** Claim-release pairing *(Tier A, 🔴)*
If `claim_next_queued` returns a row but the subsequent real `acquire_slot` fails, `release_claim_to_queued` must flip status back to `queued` (see `backlog_service.py:196-199`). Signal: rows stuck in non-`queued`, non-terminal state with no active slot.

**B-05** Stale expiry *(Tier B ≤ 60 s + 24 h, 🟢)*
`status='queued' AND queued_at < now() - 24h` ⇒ gets FAILED by `expire_stale_queued` within 60 s.

**B-06** Backlog cap honored *(Tier A, 🟡)*
`COUNT(queued WHERE agent_name=A) ≤ max_backlog_depth(A)` at time of enqueue. Race exists; treat as Tier B ≤ next drain if briefly violated.

**B-07** Agent deletion drains backlog *(Tier A on delete, 🔴)*
After agent delete, `COUNT(status='queued' AND agent_name=A) = 0` (cancelled with reason). See `backlog_service.cancel_all_backlog`.

---

## 4. Activities ↔ executions ↔ chat

**AC-01** Every execution has a start activity *(Tier B ≤ 5 s, 🟡)*
For every `schedule_executions` row, there exists an `agent_activities` row with `related_execution_id = <row.id> AND activity_type IN (chat_start, schedule_start) AND activity_state = 'started'` within 5 s of creation.

**AC-02** Activity terminal mirrors execution terminal *(Tier A on update, 🟡)*
If activity `related_execution_id = X` and execution X has terminal state, the activity is `completed` or `failed` (not `started`).
Signal: activities in `'started'` whose linked execution is terminal > 1 min old ⇒ bug in `complete_activity` path.

**AC-03** No stale started activities *(Tier B ≤ 120 min, 🟢)*
`activity_state='started' AND started_at < now() - 120min` ⇒ `mark_stale_activities_failed` ran.

**AC-04** Parent activity lifetime ≥ child lifetime *(Tier A, 🟢)*
If `child.parent_activity_id = P`, then `parent.started_at ≤ child.started_at` and parent only closes after all children closed (or closes with `failed`).

**AC-05** Chat session aggregates consistent *(Tier B ≤ next message, 🟢)*
`chat_sessions.message_count = COUNT(chat_messages WHERE session_id=X)` and `total_cost = SUM(cost)`.

**AC-06** One active session per (agent, user) *(Tier A, 🟡)*
`COUNT(chat_sessions WHERE agent_name=A AND user_id=U AND status='active') ≤ 1`.

**AC-07** Chat message → session FK intact *(Tier A, 🔴)*
Every `chat_messages.session_id` resolves to a `chat_sessions.id` (same `agent_name`, `user_id`). Never orphaned after agent delete — deletion must cascade.

---

## 5. Agent lifecycle (Docker ↔ DB)

**L-01** DB row ⇔ container presence *(Tier A post-op, 🔴)*
For every `agent_ownership` row, exactly 0 or 1 Docker containers labeled `trinity.platform=agent, trinity.agent-name=<name>` exist (0 = stopped, 1 = any running/stopped state). And: every Trinity-labeled container has a matching row.
Signal: left/right join between `agent_ownership` and `docker ps -a --filter label=trinity.platform=agent`.

**L-02** Create is atomic *(Tier A, 🔴)*
After a create request, either (row AND container) both exist, or neither does. No dangling container, no dangling DB row.
Test: kill backend mid-create; after restart, reconcile finds neither.

**L-03** Delete cascades *(Tier A post-delete, 🔴)*
After `DELETE /api/agents/{name}`, ALL these are 0: rows in `agent_ownership`, `agent_sharing`, `agent_schedules`, `schedule_executions (non-terminal)`, `agent_permissions` (as source OR target), `agent_event_subscriptions` (as subscriber OR source), `mcp_api_keys (scope='agent')`, `slack_channel_agents`, `agent_shared_folder_config`, `chat_sessions (status='active')`, and Redis `agent:slots:{name}` + metadata keys.

**L-04** No orphan container outlives DB row *(Tier B ≤ 60 s, 🔴)*
Container exists without `agent_ownership` row ⇒ cleanup stops/removes it.

**L-05** Running container ⇒ agent-server responsive *(Tier B ≤ 30 s of start, 🟡)*
Container `status='running'` ⇒ `GET http://agent-{name}:8000/api/health` returns 200 within 30 s.

**L-06** Credential injection precedes first chat *(Tier A, 🟡)*
Any successful `POST /api/task` on agent A implies `.env` (or `.credentials.enc` auto-import) has been materialized. Signal: task rejection if `credentials-status='missing'` on cold start.

---

## 6. Agent-runtime subprocess hygiene (PR #407)

**R-01** No zombie Claude processes *(Tier A, 🔴)*
Inside every running agent container: `ps -eo stat,comm | grep ' Z.*claude' | wc -l = 0`.

**R-02** No orphan process groups *(Tier B ≤ 10 s post-exit, 🔴)*
When agent registry shows `/api/executions/running = []`, no process in the container has a pgid matching any recently-tracked execution. (The #407 invariant: subprocess pgroup must be reaped.)

**R-03** Pipe FDs closed *(Tier B ≤ 10 s, 🟡)*
No pipe FDs remain open to a completed claude process. Proxy signal: agent-server `RSS` and `FD count` return to baseline within 10 s of execution completion.

**R-04** CPU baseline *(Tier B ≤ 10 s, 🟡)*
Idle agent (no running executions) shows `agent-server` CPU < 5 %. (Was 83 % with reader thread stuck pre-#407.)

---

## 7. Permissions, sharing, access control

**P-01** Permission edge FK integrity *(Tier A, 🔴)*
Every row in `agent_permissions` points to two existing agents. Dangling edges = cascade bug in delete.

**P-02** MCP-layer enforcement matches DB *(Tier A, 🟡)*
Agent A's MCP `list_agents` returns exactly `{A} ∪ {B : exists agent_permissions(A→B)}`. Agent A's `chat_with_agent(B)` succeeds iff edge exists or A is system.

**P-03** Sharing → access symmetry *(Tier A, 🟡)*
User U can chat via web/Slack/Telegram with A iff one of: U is owner, U is admin, `agent_sharing(A, U.email)` exists, or `open_access(A)=1 AND U.email verified`.

**P-04** Access request closes cleanly *(Tier A on decide, 🟢)*
Approving `access_requests(A, email)` inserts `agent_sharing(A, email)` in the same transaction and flips status to `approved`.

**P-05** First-login role respects whitelist *(Tier A, 🟡)* — #314.
New email user's `users.role` = `email_whitelist.default_role` for their email, or `'user'` if no row. Never silently promoted to `creator`.

---

## 8. Schedules

**SCH-01** Schedule → executions linkage *(Tier A, 🟢)*
Every `schedule_executions.schedule_id` (when non-null) resolves to a live `agent_schedules.id`. Schedule delete ⇒ associated non-terminal executions are cancelled; historical terminal rows are either preserved with null FK or kept for history (pick one — today's answer: preserved).

**SCH-02** `last_run_at` ≤ max(completed_at) *(Tier B ≤ 5 s, 🟢)*
`agent_schedules.last_run_at` equals `MAX(schedule_executions.completed_at WHERE schedule_id=X)`.

**SCH-03** Next-run sanity *(Tier A, 🟡)*
Every `enabled=1` schedule has `next_run_at IS NOT NULL AND next_run_at > now() - 1min`. (Past by >1 min ⇒ scheduler stuck.)

**SCH-04** Disabled schedule ⇒ no new queued rows *(Tier B ≤ 1 cron-tick, 🟡)*
After `enabled=0`, no new `schedule_executions` rows created beyond the currently-running one.

**SCH-05** Autonomy toggle is all-or-nothing *(Tier A, 🟢)*
`PUT /agents/{name}/autonomy` flips every schedule atomically — partial state violates the user contract.

---

## 9. Operator queue (`OPS-001`)

**OQ-01** Agent-file ↔ DB coherence *(Tier B ≤ 10 s, 🟡)*
Every pending item in `~/.trinity/operator-queue.json` appears in DB within 2 sync cycles (≤10 s). Every `status='responded'` DB row is written back to the agent file within the same window.

**OQ-02** Monotonic state progression *(Tier A, 🟡)*
`pending → responded → acknowledged` (or `pending → cancelled|expired`). No transition out of terminal.

**OQ-03** Expiry freedom *(Tier B ≤ expires_at + 60 s, 🟢)*
`status='pending' AND expires_at < now()` ⇒ marked `expired` within 60 s.

**OQ-04** Responder is authorized *(Tier A, 🔴)*
`responded_by_id` refers to a user with access to the agent at the time of response.

---

## 10. Channel adapters (Slack / Telegram)

**CH-01** Verified email is a stable principal *(Tier A, 🔴)* — #311.
Every channel message reaching `message_router` carries a verified email (or is classified as anonymous and rejected). Signal: `normalized_message.verified_email IS NULL AND adapter ≠ 'public_link' ⇒ rejection`.

**CH-02** Thread → agent binding is stable *(Tier A, 🟡)*
Once a Slack thread is bound to agent A, all subsequent messages in that thread route to A unless the binding is explicitly changed. Agent deletion ⇒ binding row deleted; new messages return a clear rejection (not 500).

**CH-03** Adapter never calls a non-running agent *(Tier A, 🟡)*
`message_router` checks container state before dispatch. A routed task never hits a stopped/missing container.

**CH-04** Rate limiter bounds enforced *(Tier A, 🟢)*
Per-email, per-agent rate limits from `message_router` are the single gate; bypass paths (e.g. group chats) are explicitly documented.

---

## 11. Event subscriptions (EVT-001)

**EV-01** Subscription FK integrity *(Tier A, 🔴)*
Every `agent_event_subscriptions` row has both `source_agent` and `subscriber_agent` existing. Deletion cascades both ways.

**EV-02** Emit → fan-out exactly once *(Tier A, 🟢)*
`emit_event` produces exactly one entry in `agent_events` and triggers exactly N executions where N = `COUNT(enabled subscriptions matching source+type)`. `subscriptions_triggered` field = N.

**EV-03** Subscriber permission or self-own *(Tier A, 🟡)*
If subscription crosses owners, permission edge must exist from subscriber → source (same as chat gating).

---

## 12. MCP keys & cross-surface sync

**MCP-01** Exactly one active agent-scoped key per agent *(Tier A, 🔴)*
`COUNT(mcp_api_keys WHERE agent_name=A AND scope='agent' AND is_active=1) = 1` for every live agent.

**MCP-02** Key-agent deletion symmetry *(Tier A, 🔴)*
Agent delete ⇒ agent-scoped keys deleted. Agent-scoped key cannot be independently revoked (only user-scoped can).

**MCP-03** Three-surface sync — backend/MCP/agent-server *(Tier A, 🟡)* — architectural invariant #13.
For every MCP tool that proxies a backend endpoint, the route exists on both. Drift test: MCP tool list ∪ backend router diff should be empty against a known manifest.

---

## 13. Audit log (SEC-001)

**AU-01** Append-only enforced *(Tier A, 🔴)*
`UPDATE audit_log` returns an error; `DELETE` on rows `< 365 days` returns an error. (SQLite triggers — verify they're installed on every startup.)

**AU-02** Lifecycle events emitted *(Tier A, 🟡)* — Phase 2a.
Every agent create/delete produces an `audit_log` row with matching `target_id` and `event_type='agent_lifecycle'`. Signal: count parity between domain events and audit rows per hour.

---

## 14. Global / cross-cutting

**G-01** No resource leak on restart *(Tier B ≤ startup + 5 min, 🔴)*
After backend restart, cleanup-service startup sweep + `recover_orphaned_executions` leaves no `status='running'` executions without matching agent registry entries. (Violation = stuck-forever states across restart — a real-world class of bug.)

**G-02** Cleanup cycle completes within SLA *(Tier A, 🟡)*
Every cleanup cycle completes within `poll_interval - 30s` (270 s). Exceeding = unresponsive agent starving watchdog → cascading invariant failures. Signal: `last_run_at - previous_last_run_at > 330s`.

**G-03** Clock monotonicity on ordering fields *(Tier A, 🟢)* — ✅ **SHIPPED Phase 4, #1077** (registry id `G-03`).
`created_at ≤ started_at ≤ completed_at` on every row where all three exist. Protects against clock-drift / mis-assignment bugs.
⚠️ **Reduced to `started_at ≤ completed_at`** — `schedule_executions` has no `created_at` column. Fires only past a
~1s tolerance (cross-worker / NTP jitter is not a bug). UTC-aware parse so a #1474 mixed naive/`Z` pair compares
without raising; E-03 owns the NULL-`completed_at` case.

**G-04** No credential leakage into backlog / logs *(Tier A, 🔴)* — ✅ **SHIPPED Phase 4, #1077** (registry id `G-04`; stacked on #1450, rides E-04's read).
`backlog_metadata` never contains raw credential values. Regex-scans each queued row's `backlog_metadata` for
common secret prefixes (`sk-`, `ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `xoxb-`, `xoxp-`, `AKIA`, `AIza`,
`sk_live_`), word-boundary anchored so common substrings (e.g. "task-") don't false-fire; fires on any match.
Scope note: the implemented check covers the **backlog** half of the catalog title (queued `backlog_metadata`) —
log-line credential scanning is out of scope for #1077. Folded into #1077 per gate decision. **SECURITY:** one
violation per row, reporting only the matched pattern NAME + ids — never the matched secret, surrounding bytes, or
raw `backlog_metadata`.

**G-05** Watchdog idempotence *(Tier A, 🔴)*
Running cleanup twice back-to-back produces an empty second report. Failure here ⇒ oscillation / double-failing bug.

---

## 15. Harness health (self-check)

Every invariant above answers *"is the system broken?"*. This family answers
*"is the harness blind?"* — and carries its own `H-` prefix so a detector outage
is never triaged as a platform defect. An `H-` violation invalidates every other
green in the same cycle.

**H-01** Collector not blind *(Tier A, 🔴 critical / 🟡 major)* — ✅ **SHIPPED Phase 5, #1813** (registry id `H-01`; follow-up to #1540).
The SQL roster read (`_collect_known_agents`) must not return zero rows — or raise — while an **independent,
non-SQL** source proves the fleet is alive. #1540 repointed the collectors at the configured engine but left the
failure *shape* intact: a collector reading an empty or unreachable source returns zero rows, and zero rows is
indistinguishable from a genuinely clean fleet, so both produce a green cycle. Signal: `known_agents == ∅`
(or a `sqlite.agent_ownership` entry in `sources_unavailable`) **AND** `docker_agent_names ∪ orphan_redis_slots`
non-empty.
- **Evidence must not be circular.** Docker container presence (from the container LIST, before any `exec_run` —
  `zombie_counts` is keyed by exec success and silently thins on a degraded container) and Redis slot keys.
  Docker is collected **before** the roster read, so it is available even on the arm where the roster read raises
  and `collect_snapshot` returns early.
- **Redis is corroborating only, and that is enforced in the severity ladder, not just in prose.** Slot keys exist
  solely while an execution holds a slot, so an idle fleet has none — and `orphan_redis_slots` is by definition
  keys whose agent is ABSENT from `agent_ownership`, i.e. the leaked-slot state L-03 reports. Docker evidence is
  required for `critical`; Redis-only evidence reports `roster_empty_unverifiable` (major), so a correct roster
  plus one leaked slot key cannot page critical claiming the harness is blind.
- **Source availability is tri-state**, not boolean: ran-and-fine / ran-and-failed / **never ran**. A skipped
  collector writes nothing to `sources_unavailable`, which is byte-identical to success — so `Snapshot.collectors_ran`
  carries the third state and H-01 renders it as `not read`. Without it the `roster_read_failed` arm reported
  `docker=up · redis=up` on a cycle where neither source had been consulted.
- **Confirmation on elapsed wall-clock** (`CONFIRMATION_MIN_SECONDS`, marker `canary:h01:suspect_since`, following
  E-02's cross-cycle-state precedent) so the last-agent delete race — DB row deleted, container still tearing
  down — cannot false-fire. Deliberately NOT "a second cycle": prod runs `--workers 2` and `canary_service` holds
  no leader lease, so the two loops share the marker and worker B would confirm worker A's sighting seconds later,
  collapsing the gate to nothing. Costs at most one extra cycle to alarm; irrelevant for a config regression that
  persists until a human fixes it. The gate applies to **every** firing arm, `roster_read_failed` included — that
  arm has no delete race to ride out, but a raised roster read is very often a momentary DB blip (connection
  reset, PG restart, pool exhaustion), and paging critical on one of those is how a safety net gets muted.
  The marker carries a **24h TTL, refreshed on every suspicious cycle**: `_clear_marker` is best-effort and a
  `run-cycle` filtered to other `invariant_ids` never reaches it, so without an expiry an orphaned marker stays
  armed forever and the next genuine episode confirms on its first cycle. Refreshing makes it an idle timeout
  rather than an absolute lifetime, so a long episode cannot silently re-arm and re-alert.
- **Fail-loud:** an unreadable marker fires *unconfirmed* rather than skipping, and an unavailable evidence source
  fires `roster_empty_unverifiable` (major) rather than staying quiet — a dead smoke detector should chirp.
  A whole-database outage reaches the check too: `_run_cycle_inner`'s pre-cycle latest-violation read is fail-open
  (it used to raise before `collect_snapshot` ran, so H-01 never executed on the most total blindness there is),
  with transition detection falling back to `canary:last_cycle_red` so a persistent outage still chirps once.
- **Scope:** the roster read ONLY. On a live-but-quiet fleet `terminal_rows`, `enabled_schedules`, `orphan_refs`
  and `terminal_exec_statuses` are all legitimately empty, so a general "any SQL collector reads zero" rule would
  false-alarm on every idle install.
- **Residual:** an entirely stopped fleet has no containers and no slots, so no evidence is available and H-01 can
  only reach `roster_empty_unverifiable`. Partial blindness (roster returns 1 of 20) is out of scope — a count
  comparison would false-fire on legitimate create/stop races between the two reads.

---

## Design notes

- **Every Tier-A invariant is a single SQL/Redis query** — make the canary compute it continuously. Tier-B invariants run every SLA window.
- **S-01, E-02, E-06, L-03, G-01 are the five "must never break" invariants** — these encode the fixes from #378/#403/#407/#129 and agent-delete cascades. Put them in a red-alert dashboard. (**E-06 here is the _catalog_ id** — the #129 check, still unimplemented. The shipped registry `E-06` is a different invariant; see the drift note above.)
- **Invariants with Redis ↔ SQLite ↔ Docker triplets** (S-01, L-01, L-03, G-01) are the highest-leverage targets for chaos testing — they fail under partition/crash, not under ordinary load.
- **Audit log** (AU-01/02) gives you retroactive reasoning when a live invariant fires — without it, a Tier-A violation has no forensic trail.
- **Gaps to fill next**: chat-session cascade on user-delete (no such path today); soft-delete vs hard-delete of shared-with-me agents; per-subscription quota invariants (SUB-004 path); fan-out (`fan_out_id`) completion aggregation.

---

## Recommended starting subset

Twelve invariants cover ~80% of orchestration risk:

| ID | Invariant | Why |
|----|-----------|-----|
| S-01 | Slot–row bijection | Core orchestration consistency |
| S-02 | No overbooking | Capacity guarantee |
| S-03 | Slot TTL ≥ execution timeout | #226 |
| E-01 | Terminal-state closure | No stuck executions |
| E-02 | No phantom reversal | #378/#403 |
| E-05 | Dispatched rows have session | #106 |
| E-06 † | No completed-but-not-reported | #129 |
| B-01 | Queue-status coherence | Backlog integrity |
| B-02 | No queued without slots-full | Drain liveness |
| L-03 | Delete cascades | Prevents dangling references |
| G-01 | No resource leak on restart | Recovery correctness |
| R-01 | No zombie Claude processes | #407 |

† **Catalog id, not the registry id.** Catalog `E-06` is the #129
"completed-but-not-reported" check, which remains unimplemented. The shipped
registry `E-06` is "no overdue `next_run_at`" (#1472) — semantically **SCH-03**
in this catalog. Do not source an alert name or runbook for registry `E-06`
from this row; the invariant module's own docstring title is the source of
truth (`src/backend/canary/invariants/*.py`). See the drift note in §Executions.

Start here. Expand as the harness stabilizes.

---

## Harness design (proposal)

### Topology
- **Staging Trinity instance** — dedicated, isolated. Synthetic fleet of 3–5 agents (pre-seeded templates).
- **Canary agent** — a Trinity agent running on the same instance, scheduled every 5–15 minutes, holding the test scripts.
- **Read-only observer** — queries DB (via backend API or direct SQLite read), Redis (via a bastion MCP tool), Docker (via labels), Vector logs. Asserts invariants. Reports violations.

### Layers (bottom-up)
1. **Invariant library** — each invariant is a pure function `(state_snapshot) → ViolationReport`. Snapshots include: SQL result sets, Redis key dumps, agent registry calls, Docker `ps`.
2. **Snapshot collector** — single function that captures all sources at roughly the same instant and returns a typed snapshot.
3. **Scenario runner** — pytest-style tests that perform actions (create agent, trigger task, delete agent, kill container) and assert invariants after each step.
4. **Continuous canary** — runs the read-only invariant subset every N minutes. Writes violations to a `canary_violations` table + Slack/Telegram alert.
5. **Chaos layer** — opt-in, label-gated. Introduces failure (container kill, Redis disconnect, Docker socket lag) and asserts that Tier-B invariants re-converge within SLA.

### Reporting
- Violations emit a structured event: `{invariant_id, tier, severity, signal_query, observed_state, snapshot_time}`.
- Dashboard: green/red per invariant ID, time-series of violations.
- Trend alerts: "invariant X violated >3 times in 24h" → Slack channel.

### Rollout phases
1. **Phase 1** — static library + snapshot collector + read-only subset (10 invariants above). No scenarios, just observation of real staging traffic.
2. **Phase 2** — scenario runner (create/delete/trigger flows) exercising all Tier-A invariants.
3. **Phase 3** — Hypothesis stateful testing for scenario generation.
4. **Phase 4** — chaos injection. Only after Phase 1–3 are stable.

---

## Open questions (for research)

1. **Source of truth for state snapshots**: direct SQLite read (faster, no auth) vs. backend API (enforces real code path). Recommended: API for Phase 1, direct for Phase 3+.
2. **Clock skew across sources**: Redis time vs. backend time vs. Docker time. How wide should the "simultaneity window" be on snapshots?
3. **Isolating canary traffic from real usage**: synthetic email domain (e.g. `@canary.trinity.local`), synthetic agent name prefix (`canary-*`), dedicated user.
4. **Retention**: how long to keep violation records for trend analysis? 30 days proposed.
5. **Self-test**: how does the harness test itself? A pair of deliberately-broken invariants (known to fire) as liveness probes.
6. **Staging DB access**: do we expose a read-only SQLite bind mount on staging, or route everything through backend API? Security vs. fidelity trade-off.
7. **Reuse with existing testing**: overlap with `docs/testing/` existing frameworks (`MODULAR_TESTING_STRUCTURE.md`, `UI_INTEGRATION_TEST.md`). Where does this fit?

---

## References

- `src/backend/services/cleanup_service.py` — watchdog, Phase 0/1/3 logic
- `src/backend/services/slot_service.py` — slot ZSET, TTL logic
- `src/backend/services/backlog_service.py` — enqueue/drain/claim protocol
- `src/backend/services/task_execution_service.py` — full lifecycle
- `docs/memory/architecture.md` — architectural invariants (15 listed)
- PRs #378, #403, #407, #410; Issues #129, #219, #226, #106, #311, #314
