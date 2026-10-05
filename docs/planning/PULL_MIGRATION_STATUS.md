# Pull / Work-Stealing Migration — Status

> **This is the entry point.** Read this file first; it is the one place that says where the
> migration actually is. Everything else is reference material and is listed in §7.
>
> **Umbrella:** [#1081](https://github.com/abilityai/trinity/issues/1081) (Epic #1045) — the ticket
> carries the live to-do list. **Design:** `TARGET_ARCHITECTURE.md` (v2 / Direction B, #1404).

**Last verified against `dev` and the tracker: 2026-09-09.** When you change the migration's state,
change this file in the same PR.

---

## 1. One-line model

The backend owns one durable per-agent queue (`schedule_executions` rows: `queued → claimed/running →
terminal`). Each agent's worker pool **pulls** the next task when it has a free worker, runs it, and POSTs
the result back under a compare-and-set guard. Nothing is pushed at a busy or dead agent. Everything ships
behind **`PULL_MODE_PILOT_AGENTS`** (default empty ⇒ inert).

## 2. Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| #945 | Message-envelope payload schema (spec) | ✅ done |
| **Phase 0** | Dark `claim_token` / `lease_expires_at` / `claimed_by_worker` columns | ✅ done (Alembic `0016`) |
| **Phase 1** | Dark pull endpoints — atomic claim + result CAS | ✅ done |
| **Phase 2** | Agent worker pool behind `PULL_MODE_PILOT_AGENTS`; scoped-key auth | ✅ done |
| **Phase 3** | Lease reaper + `MAX_REDELIVERY` + capacity shadow meter + canary lease-awareness | ✅ done (Alembic `0017`) |
| **Phase 4** | Sync edge adapter + async fan-out join | 🔶 **in review — [#2532](https://github.com/abilityai/trinity/pull/2532)** |
| **Phase 5** | Default-ON + delete ZSET / overflow LIST / dispatch-breaker-gate / canary S-01–S-03 and B-02's push arm (B-02 stays as the pull check, alongside B-08 pull-worker liveness, #2840) | ⬜ blocked — see §4 |

## 3. Trigger reach — which work can actually reach the queue

Dispatch topology, not policy. `pull_pilot.PULL_REACHABLE_TRIGGERS` is the source of truth in code.

| | triggers | state |
|---|---|---|
| **On `dev` today** | `agent`, `event`, `schedule`, `webhook`, `reminder`, `loop` | 6 of 9 |
| **Adds with Phase 4** | `fan_out`, `a2a`, `operator_response` | → 9 of 9 |
| **Interactive (#3114)** | `manual`, `mcp`, `session`, `public`, `voice`, `voip`, `room`, `user`, `paid`, `slack`, `telegram`, `whatsapp`, plus `validation` (`pull_pilot.PULL_REACHABLE_NON_AUTONOMOUS`) | routed on pilots; sync callers wait through `dispatch_and_await_terminal` |
| **Pending** | `chat` — the UI `/chat` path (`dispatch_admission_service` → `chat_execution_service.run_chat_turn`) | still pushes on pilots; routed in a later change |
| **Unreached, unclassified** | `retry` | in neither trigger set — #2845. 4.7% of `eu2` traffic; looks like an oversight, not a decision |

`schedule` / `webhook` / `reminder` landed with #2391; `loop` with #2523. Before #2391 the pilot flag was
inert for the fleet's dominant traffic class, so a cron-driven agent was not a viable pilot. It is now.

## 4. What remains before default-ON

The spec names the gates (`TARGET_ARCHITECTURE.md`, §Re-Delivery and Side-Effect Recovery):

> Default-on for effect-bearing agents is still gated on trace fidelity (#548/#333), `prior_trace`
> injection (#1401), and **fail-closed `execution_id` injection**.

**All three are built** (fail-closed injection by #2392). The full remaining list, in order:

1. **Land Phase 4** — [#2532](https://github.com/abilityai/trinity/pull/2532). Rebased on `dev`, migration
   renumbered to `0059`, full unit suite matched against unmodified `dev` (same single pre-existing
   failure, 21 net new tests). Blocked only on review.
2. **Fail-closed `execution_id` injection** — [#2392](https://github.com/abilityai/trinity/issues/2392).
   The agent's MCP config sends each turn's id as `X-Trinity-Execution-Id`; on a pull-mode agent an effect
   without a usable id is refused with an operator alarm, and a person's terminal session (`manual`) is sent
   and logged. Pilots must run a base image with #2392, or every effect call whose model omits the id is
   refused. *(Trace fidelity #548/#333 closed Aug/Jun; `prior_trace` injection #1401 closed 2026-07-08;
   #1402 closed 2026-07-26.)*
3. **A soak on an agent that actually emits.** eu2 runs three pilot arms: `cornelius-oracle` and
   `oracle-6-science` emit no messages, calls or shares; `oracle-7-ai-semi` (flipped 2026-09-15) is the
   emitting arm, with a 2-hourly schedule that writes a byte-stable file to its public folder and calls
   `share_file`, so a re-delivered execution must emit once. What the gate still needs is the pulled-run rate on
   that arm: effect rows per pulled execution, with no duplicate share on a re-delivery. All three arms share
   one subscription seat, so a quota blackout darkens every arm at once. Mechanics are in the ops repo
   (`trinity-ops-agent:docs/pull-soak-eu2.md`). System of record for the soak is
   [#1766](https://github.com/abilityai/trinity/issues/1766)'s comment thread — read it before measuring.
4. **Phase 5: flip default-ON and delete the legacy machinery** — the 9-path cleanup pyramid, the slot ZSET,
   the overflow LIST, the dispatch-breaker gate, canary S-01–S-03, and canary B-02's push arm — B-02 itself
   stays, as the check that pilots' queued work is being claimed, and B-08 checks the pull workers are alive
   (#2840). Tracked as
   [#429](https://github.com/abilityai/trinity/issues/429). Until this lands, both systems run at once.
5. **Interactive chat joins the queue** — **DECIDED 2026-09-16**
   ([#1989](https://github.com/abilityai/trinity/issues/1989)): the queue carries all traffic and the
   synchronous push path is deleted afterwards. Two pieces gate the move —
   [#2842](https://github.com/abilityai/trinity/issues/2842) (interactive turns jump the queue, so a person's
   wait is no worse than today) and [#2843](https://github.com/abilityai/trinity/issues/2843) (one turn per
   conversation at a time, so two workers never resume one transcript). Both are in the pull claim:
   interactive triggers (`pull_pilot.INTERACTIVE_TRIGGERS`) are claimed first with strict precedence and **no
   anti-starvation rule** (steady chat that fills every worker is answered by raising the worker count); a
   row whose `conversation_key` already has a `running` row is skipped, and the unique index
   `idx_executions_one_running_turn` stops two concurrent claimers. **No worker is reserved** for interactive
   arrivals: one of N held idle is 33% of a 3-worker agent. Multi-container affinity (`replica_count > 1`,
   #927) is out of scope.

   **Routed on pilots (#3114)** for every interactive trigger except `chat` (the UI `/chat` path, still
   pushed). Sync callers (Session tab, Workspace, public links, channels, rooms, paid, MCP key auth,
   validation, internal run-now) go through `task_execution_service.dispatch_and_await_terminal`: it waits up
   to one agent execution timeout for a worker to claim the row (rooms: `ROOM_CLAIM_BUDGET_SECONDS`, 300s,
   via `claim_budget`), stores it FAILED/`CAPACITY` if none does (a caller that went away leaves it
   CANCELLED), then waits for the terminal. Each producer passes a `conversation_key` (`session:`, `public:`, `channel:`,
   `room:`, `paid:`), which is what the claim guard serialises on. Images ride the queue in
   `backlog_metadata`, which the #1449 retention sweep NULLs once the row is terminal. Live-stream proxies hold the SSE connection
   while the row is queued.

   **The Session-tab `ResumeLock` and the Workspace in-flight markers are retained** and span the queued
   wait: their TTLs add `pull_pilot.pull_queue_allowance` (one agent timeout on a pilot). The lock also
   keeps the cached resume id fresh across turns, which the claim guard does not do. Rooms take a
   per-(room, agent) wake lock (`ResumeLock` under `room_wake_lock:<room>:<agent>`) from the cursor read
   through the posted reply, so a second wake reads what the first left; a mention chain back to the same
   agent re-enters it. The room working marker adds the queue allowance (capped at the room claim budget).
   The claim guard is the only serialisation for public links and channels.

   **Report-back and stop.** The pull sink (`apply_task_result`) spawns the channel / portal / room
   completion report on its CAS-won branch, as the push terminals do. The agent's pull worker registers a
   claimed turn as pending in the process registry, so `/api/executions/{id}/terminate` before spawn skips
   the turn and reports it `cancelled`.

### The soak duration requirement is mis-cited — correct it when you touch it

Four documents said the Phase-5 gate was a *"≥2-week zero-orphan soak (#856)"*. **#856 sets no such bar** —
it is a closed 2026-05-15 spike on soak-testing approaches for multi-agent fleet stability (context
exhaustion, retry storms) and says nothing about duration, orphans, or pull. The two-week figure originates
in **#429's own gating condition** — *"#306 must be in production for ≥2 weeks with zero observed orphan
recoveries"* — written for the Redis-Streams event bus, then carried onto #1081 Phase 5. #306 closed
2026-04-21. #1766's own acceptance criterion asks for *"several days of real traffic"*, not fourteen.

Re-derive the bar deliberately rather than inheriting it. Note which measurements pool across windows
(M4 lost/phantom, M5 re-delivery, M3 peak, M7 canary — all event-driven) and which do not (M6/M9 need a
build-matched pre-flip baseline, which is what makes a reset expensive).

## 5. Load-bearing invariants — DO NOT break

1. **Preserve `execution_id` on re-delivery.** The reaper (and any operator-gate resolution) re-queues the
   **same** row — never mints a new `execution_id`. `effect_guard` and #525 idempotency are
   `execution_id`-scoped; a new id would re-emit side effects.
2. **Leased rows (`lease_expires_at IS NOT NULL`) are owned exclusively by the lease reaper.** Every other
   sweep and ZSET-consistency check excludes them.
3. **Everything is flag-gated, default-OFF.** `PULL_MODE_PILOT_AGENTS` empty ⇒ no worker, no lease, meter
   no-op, reaper finds nothing. ⚠️ **Exception, and it is not small:** the loop and fan-out *orchestrators*
   were rewritten unconditionally (#2523, #2524), because neither could be made pullable while it held the
   work in a coroutine. With an empty allowlist those rewrites still change behaviour — see §6.
4. **No master secret in an agent container.** Pull seams use the agent's scoped MCP key.
5. **Additive until Phase 5.** The push path, slot ZSET, overflow LIST, dispatch-breaker gate and canary
   S-01/S-02/S-03 are not deleted or bypassed before Phase 5.
6. **`MAX_REDELIVERY = 3`.** `redelivery_count` ≠ `retry_count` (#678 reader-race).
7. **The pilot flag is a true either/or** (#1982). A pilot agent's autonomous triggers reach it *only* by
   the queue; the backend neither pushes them nor drains them. Before this, a pilot ran both systems with
   two independent capacity counters and could run up to 2× `max_parallel_tasks`, invisible to canary S-02.

## 6. Behaviour that is NOT flag-gated

Shipped with the loop and fan-out rewrites and live on every install regardless of the allowlist. Verified
on a local stack with `PULL_MODE_PILOT_AGENTS=""` and every agent recreated (2026-09-04):

- **Loops are terminal-driven.** `delay_seconds` becomes a park on `agent_loops.next_run_at` cleared by a 5s
  sweep, so granularity is the sweep period, not the exact delay. Measured: park stamped at +15.000s, cleared
  4–6s after due.
- **A loop survives a backend restart** instead of being flipped `interrupted` — `mark_orphan_loops_interrupted`
  is gone. Measured: restarted mid-park at 1/3 runs, loop stayed `running`, resumed, finished `max_runs_reached`,
  zero `interrupted` rows. **This is an operator-visible change and belongs in release notes.**
- **A fan-out deadline reports a still-open subtask as `running`, not `failed`** — a contract change on every
  install. The batch still reports `deadline_exceeded`; the status endpoint is the source of truth and the
  subtask's real terminal lands on the row afterwards.
- **One extra indexed read per execution terminal** (loop) and one PK read (fan-out), fleet-wide.
- **Alembic `0050`/`0051` on the loops track and `0059` with Phase 4.**

Push-path parity was verified for the same paths: every row `claimed_by_worker IS NULL`, no row ever `queued`,
`max_concurrency` still bounds concurrency, and capacity overflow is still **rejected** rather than queued.

## 7. Reference documents

Five superseded planning documents were deleted on 2026-09-09 (their content is folded into the spec, and
git retains them). These six remain:

| file | what it is for |
|---|---|
| `PULL_MIGRATION_STATUS.md` | **this file** — where the migration is |
| `TARGET_ARCHITECTURE.md` | the destination design (v2 / Direction B) |
| `PULL_MIGRATION_TESTING.md` | tiers, confirmed defects, and the §9 soak measurement set |
| `PULL_MIGRATION_ROLLBACK.md` | rollback runbook — flag off-switch, code/DB tiers, detection signals |
| `MESSAGE_ENVELOPE_SCHEMA.md` | the wire contract (#945) |
| `ACTOR_MODEL_POSTCARD.md` | the pinned status / `error_code` taxonomy |

Ops-side operational detail (probe scripts, instance quirks) lives in the ops repo at
`docs/pull-soak-eu2.md`. Results and soak status live on #1766, never in a repo file — a second narrative
does not stay in sync.

## 8. Environment caveats

- **PostgreSQL only** for the pull path at fleet scale. The double-claim race manifests only on PG; SQLite
  serialises writes and hides it. Point PG tests at a throwaway database.
- **Status writers:** any change writing `schedule_executions.status` must pass
  `tests/unit/test_schedule_status_observability.py` (the #1082 `_EXPECTED_UPDATE_SITES` guard).
- **Capacity/slots:** any change near capacity or slots must run `tests/test_canary_invariants.py` — prove
  you did not "fix" a meter by writing pull rows into the ZSET.
- **Making an agent a pilot needs a recreate, not a start.** `TRINITY_PULL_MODE` bakes in at agent
  create/recreate; setting the flag and calling start leaves the container without it, silently never
  pulling.
