# Pull / Work-Stealing Migration (#1081) — Testing, Findings & Pilot Record

**Branch:** `feature/target-arch-pull-migration` · **Dates:** 2026-07-07 → 07-09
**Source design:** `docs/planning/TARGET_ARCHITECTURE.md` (v2) · **Status:** `docs/planning/PULL_MIGRATION_STATUS.md`

> Consolidates the former `PULL_MIGRATION_TEST_PLAN.md` (tiered, defect-first QA plan) and
> `PULL_MIGRATION_RECONCILED_TESTING.md` (reconciliation + what execution actually found). One doc:
> **the plan (§3 tiers), what it found (§2 findings + §5 real-PG results), and the live pilot (§6).**

---

## 1. Status snapshot

**Everything pull-related is dark:** `PULL_MODE_PILOT_AGENTS=""` (default) ⇒ no agent injects the pull env ⇒
no worker runs ⇒ push path byte-for-byte unchanged. That splits risk in two, and the priority is
counterintuitive:

- **Ships to 100% of clients regardless of the flag** (highest blast radius): the two new nullable column
  sets; the `lease_expires_at IS NULL` exclusion grafted onto **6 existing production sweeps**; the
  lease-reaper sweep running **every cleanup cycle**; the `claim_token` param on the shared
  `update_execution_status` CAS; the canary S-01 lease-exclusion; the capacity **shadow meter** summed into
  `get_all_states`/`get_slot_state`. A bug here regresses the *existing push fleet* — no flag needed.
- **Only pilot agents hit** (staged): claim/CAS endpoints, worker pool, redelivery/poison-park, the capacity
  over-subscription vector.

**Reconciled execution order** (run the cheap, high-blast-radius work first; treat the live pilot as an
integration stage gated on defect confirmation):

1. ✅ **P0 defect confirmation** — B1–B6 via code + DB/unit repro (§2). All six CONFIRMED.
2. ✅ **P0 dark-regression baseline** — existing suite green + live-PG dark-schema check (§4).
3. ✅ **P0 concurrency on real PG** — T2.1 double-claim → surfaced + fixed **C1** (§5).
4. ✅ **P0 canary lease-awareness** — T3.6 (E-05, closed by #1766) / T3.7 (E-01, closed by #1990). Every
   invariant that reads the running-row set is now lease-aware except E-02, which deliberately is not.
5. ✅ **P1 correctness** — Tiers 1/4/5; B1/B2/B3/B5 fixed + verified; B6 fixed (static).
6. ⬜ **Opt-in gate** — Tier 6 side-effect safety (`effect_guard` fail-open when `execution_id` absent). Open.
7. ✅ **Integration** — live pilot (#946), pull PROVEN end-to-end on `gtm-synthesizer` (§6).
8. ⬜ **Soak** — #856 fleet-scale ≥2-week zero-orphan (Phase-5 gate). Not laptop-reproducible.

**Environment that moved this plan:** the local stack is now **PostgreSQL** (pull revisions `0016`/`0017`,
`0012` as originally tested; dark columns verified on live PG), and live turns are locally exercisable with a
working out-of-tree `ANTHROPIC_API_KEY`.
Consequence: the concurrency-race and PG-migration tiers move from "needs infra you don't have" to "runnable
locally today." SQLite serializes writes and *hid* the double-claim race; PG does not.

---

## 2. Findings

### Headline (severity-ordered)

| ID | Severity | One-liner | Status |
|----|----------|-----------|--------|
| **C1** | ⛔ Critical | `claim_next_queued` **double-claims → double-RUNS** a row up to N× under real-PG contention (subquery InitPlan + no outer status re-check). Token CAS single-values only the terminal write, not the executions. | ✅ **FIXED + VERIFIED** — `FOR UPDATE SKIP LOCKED` (PG) + outer `status='queued'` re-check; re-run **0/25** double-claims (was 25/25); SQLite green. |
| **PG1** | ⛔ Ship-blocker | Over-length Alembic revision IDs (41 chars) truncation-fail on default `version_num VARCHAR(32)` → **fresh PostgreSQL deploy can't boot**. Live DB survives only by a pre-widened column. | ✅ **RESOLVED (superseded)** — dev fixed the identical class as **#1420** (`0008a_widen_alembic_version` + `env.py` `version_table_column_type=String(255)`). On rebase onto dev the branch inherits that fix; the session's duplicate `env.py` widening was dropped and the pull revisions renumbered `0016`/`0017`. Originally verified independently (env.py widening → fresh empty PG reaches head; PG suite **47/47**). |
| **B1–B6** | 🔴 High | All six suspected defects confirmed (below). | ✅ **B1/B2/B3/B5-lost-result FIXED + unit-verified**; **B6 fixed, static-verified**; **B4 + B5-double-run deferred by design**. |

C1 and PG1 were surfaced by *executing* the plan (concurrency + real-PG tiers), not predicted. C1 is the
concrete, worse-than-predicted form of B4.

### Confirmed defects (DB/unit repro — no Claude auth, no rebuild)

| # | Defect | Root evidence |
|---|--------|---------------|
| **B1** | **De-pilot doesn't stop pulling.** `lifecycle.py` `env_vars.update(pull_mode_env_vars())` returns `{}` for a de-piloted agent with **no `pop`** (contrast the adjacent guardrails/stall-limit `else: pop`) → baked `TRINITY_PULL_MODE=true` survives recreate; `internal.py::_pull_authorized` has **no allowlist term**. |
| **B2** | **Parked row can't be fixed by a late SUCCESS.** `park_expired_lease` set `claim_token=None`; the result CAS requires `claim_token == T` → NULL never matches → the genuine late SUCCESS is swallowed as `replayed`, row stuck FAILED. Contradicted the `pull_coordination_service` docstring. |
| **B3** | **Silent invisible poison.** Reaper committed row→FAILED, *then* inserted the operator-queue park item (separate txn). Crash between → poison row with no alert; `find_expired_leases` requires `status='running'` so a parked (FAILED) row is never re-found. |
| **B4** | **2N over-subscription.** Pull claims bypass the Redis slot counter; push-drain and pull-pool share **no admission budget** → up to **2×`max_parallel_tasks`** concurrent turns. The capacity meter is **shadow-only** (observes, does not gate). |
| **B5** | **Slow-but-alive duplicate run.** Reaper has **no liveness probe** — trusts `lease_expires_at < now` only. A turn overrunning `timeout+300s` is re-queued and re-run **concurrently** with the still-live original; side-effects guarded only by `effect_guard` (fail-open when `execution_id` absent). |
| **B6** | **In-flight terminal dropped on shutdown.** `_stop_pull_workers` was cancel-only (no drain/await); the completed-turn body delivered outside the `try`, no disk persistence / startup-resend. A completed, already-billed turn discarded → row reaped → re-run. |

**Shared root cause (B2 + B5):** both `park_expired_lease` and `requeue_expired_lease` nulled `claim_token` on
every transition, and the CAS treats a NULL token as unmatchable. The "late SUCCESS overwrites a reaper
LEASE_EXPIRED" promise held ONLY for a worker-reported FAILED (token preserved) — *inverted* for every
reaper-touched row. The fix targets this single point.

### Fix status

| # | Fix | Files | Verified |
|---|-----|-------|----------|
| **C1** | Claim subquery `FOR UPDATE SKIP LOCKED` (PG) + outer `status='queued'` re-check. | `db/schedules.py::claim_next_queued` | ✅ real-PG multi-process 0/25 double-claims; SQLite green |
| **PG1** | Superseded by dev's **#1420** (`0008a_widen_alembic_version` + `env.py` `String(255)`); inherited on rebase. Session's duplicate `env.py` widening dropped; pull revisions renumbered `0016`/`0017`. | dev `migrations/env.py` (#1420) | ✅ fresh empty PG reaches head; PG suite 47/47 |
| **B1** | Recreate pops `PULL_MODE_ENV_KEYS` before re-applying; the claim seam's agent-key path is allowlist-gated in `_pull_authorized` (internal-secret + result paths intentionally ungated so an in-flight result still lands). | `pull_mode.py`, `lifecycle.py`, `routers/internal.py` | ✅ unit |
| **B2** | `park_expired_lease` keeps `claim_token` → a late SUCCESS CAS-overwrites the parked FAILED row. | `db/schedules.py` | ✅ unit |
| **B3** | Reaper creates the operator alert **before** the park write, parks **only if the alert persisted** → a failed alert leaves the row reapable, not invisible poison. | `lease_reaper_service.py` | ✅ unit |
| **B5** (lost-result) | `requeue_expired_lease` keeps `claim_token` → the original worker's late SUCCESS finalizes the SAME queued row. | `db/schedules.py` | ✅ unit |
| **B6** | `pull_worker.py` persists the terminal to `~/.trinity/pending-pull-results/<eid>.json` before delivery, deletes on 2xx/permanent-4xx, re-sends leftovers on startup + bounded shutdown drain (mirrors #1083 `result_callback.py`). | `pull_worker.py`, `agent_server/main.py` | ⚠️ static + unit; runtime needs base-image rebuild |
| **B4** | **Deferred by design.** Physical admission (shared push+pull budget) is Phase-5 work (the ZSET retires then; unifying now violates additive-until-Phase-5). C1's fix removed the double-**RUN**; only budget-doubling remains, which the shadow meter observes. | — | ⏸️ deferral |
| **B5** (double-run) | **Deferred by design.** Fires only when a turn exceeds its own `timeout+300s` (anomalous — `execute_headless` enforces the timeout). Lease-renewal rejected (a hung turn would renew forever). Side-effect safety under any re-run belongs to the effect_guard/Tier-6 prereq (BLOCKING before default-ON). | — | ⏸️ deferral |

Full suite after all fixes: **237 passed, 1 skipped** (`test_1081_*` incl. `test_1081_pull_mode.py` + regression,
status-guard, cleanup, canary 89, result_callback 27). Fixes are dialect-agnostic.

---

## 3. Test tiers

Priority: **P0** ship-blocker / confirms a suspected defect · **P1** core correctness · **P2** edge · **P3** observability.

### TIER 0 — Dark-change regression (must be a true no-op for all clients) — P0

Run on **both** engines (SQLite supported until EOS 2026-09-01), even though local is now PG.

| ID | Test | Expected |
|----|------|----------|
| T0.1 | Non-pull rows still swept identically for all 6 amended selectors | Every legacy `lease_expires_at IS NULL` row swept exactly as pre-#1081. Diff vs `dev@0d3e58b7`. |
| T0.2 | Reaper inert with zero leases — real cleanup cycle | `find_expired_leases → []`; `CleanupReport` unchanged; no exception/latency. |
| T0.3 | Migration idempotency — fresh + upgrade, both engines | 3 columns present, nullable, correct defaults; `ADD COLUMN IF NOT EXISTS` no-ops on re-run; SQLite `migrations.py` and Alembic `0016`/`0017` agree. |
| T0.4 | Legacy CAS path unchanged (`claim_token=None`) | Push terminal writes byte-identical. |
| T0.5 | Status-projection guard covers new sites (AST guard) | `requeue_expired_lease` + `park_expired_lease` classified in `_EXPECTED_UPDATE_SITES`. |
| T0.6 | Pull columns never populated with flag OFF | All 3 pull columns stay NULL on every row. |
| T0.7 | Capacity meter inert with empty allowlist | `count_active_leased_by_agent` returns 0; `get_slot_state`/`get_all_states` numerically identical to pre-#1081. |

### TIER 1 — Claim / CAS correctness (flag-ON, single agent, no concurrency) — P1

| ID | Test | Expected |
|----|------|----------|
| T1.1 | Claim stamps token+lease+worker, flips `queued→running` atomically | One UPDATE; lease = `3600 + 300 = 3900s`, ISO-Z. |
| T1.2 | FIFO — oldest `queued_at` first | Ordered claims. |
| T1.3 | Empty queue | 200 `{"envelope": null}`. |
| T1.4 | Result CAS matrix | applied→200; duplicate SUCCESS→replayed 200; wrong/stale token→409; unknown id→404. |
| T1.5 | B2 — parked-row late SUCCESS with original token | Overwrites FAILED→SUCCESS (post-fix). |
| T1.6 | Late SUCCESS over a token-intact FAILED row | Overwrites FAILED. |
| T1.7 | Auth-mislabeled cancel (`status=cancelled, error_code=auth`) | Forced to FAILED, never a clean cancel. |
| T1.8 | User-CANCELLED blocks late SUCCESS (#671) | Replayed, stays cancelled. |
| T1.9 | Dual-auth boundary | internal-secret→200; agent key matching→200; other-agent key→403 (next-task)/404-then-403 (result); user/system→403; no creds→403. |
| T1.10 | Payload validation | empty `claim_token`→422; unknown `status`/`error_code`→422; oversized body → confirm a 413 cap. |

### TIER 2 — Concurrency & races (runnable on local PG) — P0

> Every existing "no double-claim / idempotent" proof is **sequential single-thread**. These require real
> threads/processes and a **PostgreSQL** engine. **T2.1 is the single highest-value newly-unblocked test.**

| ID | Test | Expected |
|----|------|----------|
| T2.1 | **Postgres double-claim** — N threads call `next-task` for one agent with M queued rows | Exactly one claimant per row. (Pre-fix: 25/25 double-claimed — see §5.) |
| T2.2 | Concurrent reaper passes — two cleanup processes reap the same expired lease | Exactly one requeue **or** one park; `redelivery_count` increments exactly once. |
| T2.3 | B5 — slow worker vs reaper | Reaper re-queues same `execution_id`; original worker's late result → 409/replayed; **count the duplicate side-effect**. |
| T2.4 | Duplicate delivery, same token twice concurrently | One applied, one replayed; single terminal. |
| T2.5 | Claim vs backend push-drain race for one overflow row | Exactly one path runs it. |
| T2.6 | Reaper re-queue vs a just-arriving valid result (token still valid) | First commit wins; the other a clean no-op. |

### TIER 3 — Capacity, shadow meter & canary — P0/P1

| ID | Test | Priority | Expected |
|----|------|----------|----------|
| T3.1 | B4 — 2N over-subscription | P0 | Saturate N push slots, drive overflow, pilot pool ON → peak concurrent turns reaches 2N (admission bypasses ZSET). |
| T3.2 | Shadow meter counts leased turns, disjoint from ZSET | P1 | `get_slot_state` total = `ZCARD(slots)` + `count_active_leased_by_agent`, no execution in both sets. |
| T3.3 | Admission is byte-identical (shadow-only proof) | P0 | `acquire`/`acquire_slot`/`release`/`slot_service.py` do **not** consult the meter. |
| T3.4 | Meter disjoint under re-delivery | P2 | A re-queued (`queued`) row counted by neither meter nor ZSET until re-claimed; a parked (FAILED) row by neither. |
| T3.5 | Canary S-01 fix verified | P1 | Excludes `lease_expires_at IS NOT NULL` rows → no false-fire on a pilot; still fires for a genuine non-pull mismatch. |
| T3.6 | **Canary E-05 false-fire (KNOWN pre-opt-in blocker)** | P0 | A pull-`running` row >60s with `claude_session_id IS NULL` trips E-05. Apply the same lease-exclusion **before any pilot opt-in**. |
| T3.7 | Canary E-01 fire on a legitimately-running pull turn | P1 | ✅ **CLOSED by #1990** — fixed via a bounded lease grace, not accepted and not a blanket exclusion. Not merely a re-delivery transient: `claim_next_queued` stamps the lease at `started_at + timeout + SLOT_TTL_BUFFER`, the **identical** window, so E-01 fired at the instant the reaper's recovery window opened and stayed red until its next sweep, every re-delivery, per execution, at **critical**. A leased row is now silent only while its lease is overdue by ≤ 600s (2 × the `cleanup_service` interval the reaper runs in); past that it fires as a *lease-reaper* failure — see M4 in §9. A NULL-lease control of the same age still fires. |

### TIER 4 — Lease reaper / redelivery / poison-park — P1

| ID | Test | Expected |
|----|------|----------|
| T4.1 | Under-cap requeue | `redelivery_count 0→1`, same id, lease columns cleared, status `queued`. |
| T4.2 | 4-total-runs semantics | 1 original + redeliveries at counts 1,2,3; park at `count≥3` (`MAX_REDELIVERY=3`). |
| T4.3 | At-cap park | FAILED, error contains `poison_lease`, operator item `poison-{eid}`; `on_conflict_do_nothing` idempotent. |
| T4.4 | `MAX_REDELIVERY` env override (0, 1, 100) | Honored globally (per-agent deferred). `0` doesn't infinite-loop / brick crash-recovery. |
| T4.5 | B3 — crash between park-CAS and operator insert | Poison remains discoverable (post-fix: alert-first). |
| T4.6 | Governor pause holds off reaper (#1085) | With pause armed, `_sweep_expired_leases` returns early; lease survives (pause TTL 300s < lease window). |
| T4.7 | Lingering lease on normal terminal | Worker SUCCESS doesn't clear `lease_expires_at` → terminal row keeps a lease; assert no sweep mis-flags it. |
| T4.8 | Non-normalized `lease_expires_at` on PG | TEXT column, `< now` is lexicographic. Guard all writes via `to_utc_iso` (Invariant #16). Run on PG. |

### TIER 5 — Agent worker lifecycle & flag operations — P1

| ID | Test | Expected |
|----|------|----------|
| T5.1 | Default no-op | Empty allowlist → no env → no handlers → no `[#1081] pull pool started`. |
| T5.2 | Enable requires recreate not restart | Add to allowlist + `POST /start` → still no workers; recreate → pool starts (size = ceiling-clamped `max_parallel_tasks`). |
| T5.3 | B1 — disable path | Remove from allowlist, recreate → workers stop (post-fix). |
| T5.4 | Mixed fleet | old-image+piloted → inert; new-image+not-piloted → no workers; new-image+creds-missing → no workers. Only new∧piloted∧creds runs. |
| T5.5 | B6 — shutdown drain | Stop container mid-turn → a completed turn's terminal is delivered (post-fix). |
| T5.6 | Turn crash → typed error | 503→auth, 504→timeout, 502/500→agent_error, 429→billing, 422→max_turns; generic→agent_error. Terminal carries `claim_token`. |
| T5.7 | Result POST retries to lease deadline → reaper backstop | Always-500 → give up at deadline, row stays leased, reaper recovers. |
| T5.8 | Persistent 403 (rotated/bad key) | Worker idle-loops silently at `logger.debug`. Recommend an operator-visible WARN/alert. |
| T5.9 | Pool-size clamp | `TRINITY_MAX_PARALLEL_TASKS` clamps `[1,32]`; 0→1, 999→32, junk→default 3. |

### TIER 6 — Side-effect safety (gates default-ON) — P0 for opt-in

| ID | Test | Expected |
|----|------|----------|
| T6.1 | Re-delivery preserves `execution_id` | Requeue and park both keep the row id. |
| T6.2 | effect_guard dedup across re-delivery | Same `execution_id` re-run → `send_message`/`create_share`/`voip` de-duped. |
| T6.3 | **effect_guard fail-open when `execution_id` absent** | Without trusted injection a re-run **double-emits**. Must close before default-ON for side-effect agents. |
| T6.4 | Nevermined settle exactly-once | Duplicate re-delivery → single settle on native `agent_request_id` token. |

### TIER 7 — PostgreSQL (now the LOCAL backend) — P0

| ID | Test | Expected |
|----|------|----------|
| T7.1 | Run the whole #1081 suite on live PG | The double-claim race (T2.1) only manifests on PG. Make PG the default CI leg for this feature. |
| T7.2 | Alembic `0016`→`0017` on live PG | Columns created; `down_revision` chain clean (re-parented onto dev head `0015_enterprise_connectors`); pre-Alembic PG stamped at baseline. |
| T7.3 | SQLite↔PG parity | `schema-parity` + `check_alembic_parity.py` green for the 4 new columns. |
| T7.4 | Column-type assertion on PG | `lease_expires_at`/`claim_token`/`claimed_by_worker` TEXT; `redelivery_count` INTEGER DEFAULT 0. |

### TIER 8 — Observability / operator experience — P2/P3

| ID | Test | Expected |
|----|------|----------|
| T8.1 | Poison-park item renders in Operating Room | `poison-{eid}` visible + actionable. |
| T8.2 | Feature-flag surface | `mcp_agent_chat_pull_enabled` / `redelivery_governor_enabled` correct in `/api/settings/feature-flags`. |
| T8.3 | Standalone scheduler blind spot | `src/scheduler/database.py` writes the same table with raw non-CAS SQL, outside the #1082 AST guard. Verify it never touches leased/pull rows. |

---

## 4. Dark-regression baseline — GREEN (but shallow)

Existing suite on the host (SQLite): **199 passed / 0 failed / 1 intentional skip** (`test_1081_pull_endpoints`
37, `_pull_worker` 15, `_lease_reaper` 10, `_lease_sweep_exclusion` 9, `_physical_meter` 14,
`test_schedule_status_observability` 19+1skip, `test_cleanup_inner_sweeps` 6, `test_canary_invariants` 89).

**Two caveats that make this green shallow:**
1. **Zero PostgreSQL coverage.** `TEST_POSTGRES_URL` unset ⇒ all 47 DB-param tests ran SQLite-only. The engine
   where the double-claim race manifests is unrun.
2. **Every no-double-claim / CAS / idempotency test is sequential single-thread.** They claim with `w1` then
   `w2` *in sequence* — proving the `WHERE status='queued'` precondition, not atomicity under contention. On
   SQLite's single-writer lock a real race can't be constructed.

→ The safety-critical invariant (exactly-one-claimant under contention) is **not** tested by the baseline.
That is Tier 2 / T2.1.

---

## 5. Real-PG + concurrency results

Run against throwaway test DBs via a socat forwarder (every connection asserted `current_database()` ≠ live
`trinity`). **Test rig torn down when done**; the scratch race harness lives only in the session scratchpad —
promote it to a committed real-PG multi-process regression test when C1 lands.

### T7.1 / T7.2 — full suite on real PostgreSQL → **PG1 (ship-blocker)**

> **Resolution note (post-rebase):** dev fixed this identical class independently as **#1420**
> (`0008a_widen_alembic_version` migration + `env.py` `version_table_column_type=String(255)`). Rebasing
> onto dev inherits that fix, so the session's own `env.py` widening was dropped and the pull revisions were
> renumbered `0016`/`0017` (re-parented onto dev head `0015_enterprise_connectors`). The finding below is the original session
> diagnosis, kept as the historical record.

Alembic's `alembic_version.version_num` defaults to **`VARCHAR(32)`**, but the descriptive `NNNN_<table>_<change>`
revision IDs are **41 chars** (e.g. `0009_agent_ownership_public_channel_model`, and the pull revisions, then
`0011`/`0012`, now `0016`/`0017`). On a clean PG bootstrap, `init_database()` → `upgrade_to_head()`
truncation-fails at the first over-length id with `StringDataRightTruncation`; Alembic runs the whole upgrade
in one transaction, so it **rolls back entirely → no schema → backend can't start.**

- The live `trinity` DB is unaffected only by accident (its `version_num` is already `VARCHAR(255)`).
- SQLite hides it (bespoke `schema_migrations` `INSERT OR IGNORE`, no width cap).
- Blast radius: 38 of 47 PG variants fail; the 9 that pass are pure DB-layer tests using the harness schema.
- **The pull DDL itself is correct** — with the column pre-widened, `upgrade head` runs clean and the 4
  columns land exactly as specified.
- **Fix:** the version column is widened to `String(255)` pre-upgrade — now carried by dev's #1420
  (`0008a_widen_alembic_version` + `env.py`), which this branch inherits on rebase. `tests/unit/test_alembic_revision_id_length.py` guards the ≤255 bound.

**No logic-level SQLite↔PG divergence in the pull code.** `lease_expires_at < now` is a same-format ISO-Z
lexicographic compare (Invariant #16); NULL `redelivery_count` handled by `row[...] or 0`; CAS/token-match
pass on PG unpatched.

### T2.1 — double-claim race → **C1 (critical): rows double-RUN on real PostgreSQL**

Real PG 16, READ COMMITTED, M=50 queued rows, N=16 claimer processes (multiprocessing `spawn`,
`Barrier`-synchronized), calling the real `claim_next_queued`. **25/25 iterations double-claimed** — the head
row observed claimed by **all 16 workers, each with a distinct `claim_token`**; `unclaimed=0`, `errors=0`.
Because `claim_next_queued` is exactly what `GET /api/internal/next-task` returns, **a single queued row is
handed to up to N workers and each runs the turn.**

**Mechanism.** The claim UPDATE's subquery is an **uncorrelated scalar subquery → InitPlan, evaluated once →
constant `X`**. Under READ COMMITTED, N workers' InitPlans resolve to the same head id `X`. First UPDATE
commits `X`; the others block on the row lock, then EvalPlanQual re-checks the outer qual `id = X` — which
stays TRUE for the now-`running` row (the InitPlan isn't re-run and **there is no status predicate in the
outer WHERE**). Each blocked updater re-applies to `X`, re-stamps a fresh token, RETURNs `X`.

| variant (M=50/N=16/20 iters) | iters w/ double | double rows | events/iter |
|---|---|---|---|
| exact repo SQL | **20/20** | 979 | 235 |
| `+ FOR UPDATE SKIP LOCKED` | 0/20 | 0 | 50 (exactly-once) |
| `+ outer AND status='queued'` | 0/20 | 0 | 50 (exactly-once) |

**Fix (landed):** `FOR UPDATE SKIP LOCKED` on the claim subquery (losers skip to the next row). **Token CAS
does NOT save it** — the stale-token loser's terminal write → `won=False`, but the N executions already ran.
`effect_guard`/#1084 is the only side-effect backstop (fail-open-when-absent). Latent today (empty pilot list)
but a **blocking correctness bug for any pull enablement**.

### T2.2 / T2.6 — reaper transitions → **PASS (atomic)**

`requeue_expired_lease`/`park_expired_lease` carry `status='running' AND lease_expires_at < now` in the
**outer** WHERE → EvalPlanQual fails the loser. Concurrent reaper passes: exactly one transition wins,
`redelivery_count` increments exactly once. Reaper-vs-result: first-commit-wins, loser a clean no-op.

> **The asymmetry is the lesson:** every reaper/result transition that puts its precondition in the outer
> WHERE is atomic under real-PG contention; `claim_next_queued` — the one path with a subquery-only status
> check and no outer re-check — was the single non-atomic writer, and it double-ran rows.

---

## 6. Live pull pilot (#946) — executed + PROVEN (2026-07-08)

Ran on `gtm-synthesizer` (rebuilt image, `AGENT_AUTH_SECRET` fixed; chosen because its container stored
`Config.Image=trinity-agent-base:latest` and carried a real `TRINITY_MCP_API_KEY`).

**End-to-end proof:**
- Worker pool up: `[#1081] pull pool started: 3 worker(s) for agent gtm-synthesizer`.
- **Happy path:** `queued → running` (claim_token + lease set) → run → CAS result → terminal `success`, cost
  $0.036 — with **`ZCARD(agent:slots)=0` the whole run** (pure SQL lease, no ZSET slot). `execution_id` preserved.
- **Reaper + fixes:** requeue ×3 (`redelivery_count` 0→3, same id, **claim_token kept** — B2/B5) → park at cap
  (row `failed` + `poison-{eid}` operator alert — B3 alert-first). **Late SUCCESS after park with the kept
  token → `{"applied":true}`** (failed→success) — B2 live.
- **Canary:** S-01 lease-exclusion holds (leased row not flagged; non-leased control *does* fire); E-05 fires
  (known open gate, §3 T3.6).

### Gaps the pilot surfaced

| ID | Gap | Impact | Fix |
|----|-----|--------|-----|
| **G1** | `docker-compose.yml` didn't forward `PULL_MODE_PILOT_AGENTS` to the backend (explicit `environment:` list, not `env_file`). | The documented pilot opt-in (`.env` + restart) is a **no-op** out of the box. | ✅ **FIXED** — added `PULL_MODE_PILOT_AGENTS=${PULL_MODE_PILOT_AGENTS:-}` to the backend `environment:`. |
| **G2** | Recreate dropped `TRINITY_BACKEND_URL` — injected only on fresh create; `recreate_container_with_updated_config` preserved old env but never re-added it. | A **legacy agent** opted into pull recreates without a backend URL → worker refuses to start. | ✅ **FIXED** — recreate now `setdefault`s `TRINITY_BACKEND_URL` (matching the #1098 TMPDIR idiom). |
| **G3** | Canary collector was **SQLite-only** — `db/connection.py::get_db_connection` hardcodes `sqlite3`, ignoring the PG `DATABASE_URL`. On a PG instance `POST /api/canary/run-cycle` read a frozen/empty `/data/trinity.db` → vacuously green. | The canary safety net didn't watch live PG state (S-01/E-05/E-01/E-02/B-01 never saw real rows). Material to any default-ON decision. | ✅ **CLOSED by #1540** — all six SQL-tier collectors (`_collect_known_agents`, `_collect_executions`, `_collect_terminal_executions`, `_collect_terminal_rows`, `_collect_enabled_schedules`, `_collect_orphan_refs`) now read through the `get_engine()`/`DATABASE_URL` seam; verified against a real `postgres:16-alpine` (`sources_unavailable == []`, S-01/L-03 fire on synthetic mismatch). `db/connection.py` stays the sqlite-only maintenance seam; the canary is routed around it. |

---

## 7. Test infrastructure to build

1. **A real concurrency harness** — OS threads/processes hammering `claim_next_queued` and
   `reap_expired_leases` on **PostgreSQL**. SQLite hides every write race. (Promote the scratch C1 harness.)
2. **A backend↔worker↔DB integration rig** — nothing today wires the real `pull_worker` to the real router to
   the real DB; the claim→run→result round-trip is never exercised across the seam.
3. **A flag-ON live-turn env** — local PG + an out-of-tree `ANTHROPIC_API_KEY` make T5.5/T5.6/T6 runnable.
4. **A fleet-scale soak leg (#856)** — Tiers 3–5 at scale over ≥2 weeks; the Phase-5 default-ON gate; not
   laptop-reproducible.

## 8. Net remaining before a default-ON decision

~~Canary lease-awareness E-05 (§3 T3.6)~~ ✅ **closed by #1766** — E-05 now excludes leased rows, mirroring
S-01 and the `mark_no_session_executions_failed` sweep it watches (which already carried
`lease_expires_at IS NULL`); without it E-05 fired on every pull turn past 60s ·
~~canary lease-awareness E-01 (§3 T3.7)~~ ✅ **closed by #1990** — the same lease-awareness, on the invariant
with the worst overlap: the lease is stamped at exactly `timeout + SLOT_TTL_BUFFER`, so E-01 fired **critical**
the moment the lease-reaper became eligible and stayed red until its next sweep. E-01's is a **bounded grace**
(600s = 2 × the `cleanup_service` interval the reaper runs in), not a blanket exclusion, so an overdue lease
the reaper never touches still fires — that is M4's automated owner (§9). Canary lease-awareness is now
complete: S-01 and E-05 exclude leased rows, E-01 grace-bounds them; E-02 deliberately does neither (a
terminal→non-terminal reversal is corruption regardless of ownership, and pull is the more exposed path) ·
Tier-6 `effect_guard` `execution_id` injection · ~~G3 canary-on-PG (#1540)~~ ✅ closed ·
B6 runtime-verify on the rebuilt image · the ≥2-week soak (#856 / #1766, measurement set in §9).

**Also closed by #1766:** the pilot flag was purely additive, so a pilot ran push AND pull concurrently —
the producer never force-queued (a free slot still meant a push, so rows only queued on overflow) and the
backend's own `drain_next` raced the agent's worker for whatever did queue. Two independent capacity
counters meant up to 2x `max_parallel_tasks`, invisible to S-02. The flag is now a true either/or for
autonomous triggers; interactive turns keep the synchronous path (Open Question 7 scope cut).

## 9. Soak measurement set (#1766)

The queries that turn "we ran it for a few days" into a verdict. **Nothing else
currently covers this**: `instance-monitor` has zero references to leases,
`claim_token`, `redelivery_count`, or queue depth — its probes report generic
health and will stay green whether pull carries the fleet or does nothing at all.
Run these instead (or fold M1/M3/M5 into the monitor's deep-probe).

PostgreSQL flavour. All timestamp columns are `Text` holding ISO-Z strings
(Invariant #16), hence the explicit `::timestamptz` casts. Substitute the pilot
name for `'<agent>'`.

### Pre-flight — pick a viable pilot, then capture a baseline

**First, check the candidate can be piloted at all.** Five triggers reach the
durable queue — `agent`, `event`, `schedule`, `webhook`, `reminder` — and four
do not (`loop`, `fan_out`, `a2a`, `operator_response`). Run the
pull-eligible-volume query under M1 below and pick from its `pull_eligible`
column *before* flipping anything; a week of soak on an agent whose traffic is
all loops and fan-out measures nothing.

> **Changed by #2391.** Before it, `task_execution_service` dispatched with
> `overflow_policy="reject"` unconditionally, so **no** cron, webhook or reminder
> row could ever be claimed — the pilot flag was inert for the fleet's dominant
> traffic class, and a cron-driven agent was explicitly not a viable pilot. It
> now dispatches with `"queue_persistent"` when `pull_owns_dispatch` says a pilot
> owns the trigger, and with `"reject"` otherwise. **A cron-driven agent is a
> viable pilot now** — see "Choosing a pilot" below.

Then run M4 + M6 + M7 + M9 on the pilot for the 7 days *preceding* the flip and
keep the output. Without a baseline, a 4% success-rate dip during the soak is
unattributable and the write-up degrades to "felt fine".

**Record the baseline as a breakdown by failure class, never as one percentage.**
A single "7.7% failed" is unusable, because the classes move independently and
only one of them is about pull:

```sql
SELECT CASE
         WHEN error LIKE 'Subscription usage limit%'      THEN 'quota'
         WHEN error LIKE 'Task execution timed out%'      THEN 'caller_timeout'
         WHEN error LIKE '%recovered by watchdog%'        THEN 'lost_result'
         ELSE COALESCE(LEFT(error, 60), '(none)')
       END AS failure_class,
       COUNT(*) AS n
FROM schedule_executions
WHERE agent_name = '<agent>' AND status = 'failed'
  AND started_at::timestamptz > now() - interval '7 days'
GROUP BY 1 ORDER BY 2 DESC;
```

- **`lost_result`** — the class pull exists to remove (the agent finished, the
  result never landed, the watchdog wrote the row off hours later). This count is
  the number the soak is trying to drive to zero. Under pull the same event
  surfaces as M5 instead: lease expiry → re-queue of the same `execution_id` →
  poison-park at cap.
- **`caller_timeout`** — see the M6 warning below. Do **not** expect these to
  reappear under treatment; their disappearance proves nothing.
- **`quota`** — exogenous. Exclude from the M6 comparison entirely, and check M1
  volume mid-window: a quota-exhausted stretch starves the treatment arm and you
  end up soaking a near-idle agent. Don't open the window right before a weekly
  subscription reset.

### The set

| ID | Answers | AC | When to run |
|----|---------|-----|-------------|
| M1 | Is pull actually carrying load? | coverage (the #1766 premise) | once + mid-window |
| M2 | Push/pull split over time | coverage | once at end |
| M3 | Is the agent over its configured concurrency? | no slot/backlog drift | **sampled, at peak** |
| M4 | Lost or phantom executions | no lost/phantom | **sampled, hourly** |
| M5 | Re-delivery + poison-park behaviour | leases behave as designed | once at end |
| M6 | Outcome regression vs baseline | no regression | baseline + end |
| M7 | Canary state | canary green throughout | once at end |
| M8 | Queue starvation | no drift | once at end |
| M9 | Caller-side timeouts (M6 counter-check) | no regression | baseline + end |

**M3 and M4 must be sampled, not run once.** M3 counts *concurrent* leased rows,
so a single daily run lands off-peak and never sees the overbooking it exists to
catch. M4 is subtler: its predicate is `status IN ('queued','running')`, and
`cleanup_service`'s stale sweep eventually fails stranded rows to `failed` — at
which point they leave the predicate and M4 reads clean. **A soak that genuinely
lost executions reports zero rows if M4 is only run at the end.**

**M1 — is the pull path carrying anything?** The single most important query: if
`pulled` is ~0 the soak is measuring nothing and everything downstream is void.

```sql
SELECT COUNT(*) FILTER (WHERE claimed_by_worker IS NOT NULL) AS pulled,
       COUNT(*) FILTER (WHERE claimed_by_worker IS NULL)     AS pushed
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND started_at::timestamptz > now() - interval '24 hours';
```

Expected after the #1766 gate + #2391: **`agent`, `event`, `schedule`, `webhook`
and `reminder` rows ~100% `pulled`; `loop`, `fan_out`, `a2a` and
`operator_response` 100% `pushed`.** That second half is correct behaviour, not a
fault — read the reach table below before concluding anything from it. Confirm
the split per trigger with:

```sql
SELECT triggered_by,
       COUNT(*) FILTER (WHERE claimed_by_worker IS NOT NULL) AS pulled,
       COUNT(*) FILTER (WHERE claimed_by_worker IS NULL)     AS pushed
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND started_at::timestamptz > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;
```

#### Which triggers can be pulled at all (#2048, widened by #2391)

`capacity_manager` offers a row to the durable queue solely when its producer
passed `overflow_policy="queue_persistent"`. Two of the three producers can:

| Producer | Carries | `overflow_policy` | Pullable? |
|---|---|---|---|
| `task_execution_service` | scheduler — **all cron** — webhooks, reminders, loops, fan-out, A2A, operator resumes | `queue_persistent` **when `pull_owns_dispatch` is true**, else `reject` | **Yes**, for `schedule` / `webhook` / `reminder` |
| `dispatch_admission_service` | sequential `chat_with_agent`, human chat | `queue_in_memory` | **No** |
| `chat_execution_service` (`POST /task`) | parallel `chat_with_agent`, MCP/manual task | `queue_persistent` | **Yes**, for `agent` / `event` |

`POST /task` can only derive `triggered_by ∈ {self_task, agent, mcp, manual,
event}`, contributing `agent` + `event`. `task_execution_service` contributes the
autonomous triggers with **no synchronous result consumer** — `schedule`,
`webhook` and `reminder`, all three of which reach it from the scheduler, which
dispatches `async_mode=True` and then polls the DB for the terminal. The pullable
set is therefore **`{agent, event, schedule, webhook, reminder}`**
(`pull_pilot.PULL_REACHABLE_TRIGGERS`).

The four that stay pushed are stranded on their **caller**, not on the
producer's policy — a queued row is claimed and run later and returns nothing for
any of them to read. Three are structural: `loop` (renders the next iteration
from `result.response`), `fan_out` (builds each `FanOutTaskResult` from the
returned result — the async join is #1081 Phase 4) and `a2a` (turns the result
into the JSON-RPC artifact it hands a remote caller). `operator_response` is out
by **choice**: it dispatches through the same producer #2391 widened, but the
respond endpoint records `result.status` as the dispatch receipt (the
`operator_resume_dispatch` audit row + the #525 idempotency completion) for a
turn that spends money on a person's answer (ent#329). Widening it is a
follow-up, not a blocker.

**Reading a pushed autonomous row.** Two different situations, previously
indistinguishable:

- `triggered_by` ∈ `{loop, fan_out, a2a, operator_response}` → **expected.**
  The flag is applied and correct; the dispatch topology is the limit. The
  backend logs this once per (agent, trigger) — grep `[#2048]` to confirm:
  ```bash
  docker logs trinity-backend 2>&1 | grep '\[#2048\]'
  ```
- `triggered_by` ∈ `{agent, event, schedule, webhook, reminder}` → **a real
  fault.** This is the case where the old advice applies: check the backend
  actually has `PULL_MODE_PILOT_AGENTS` in its env (and see G1 in §6 — compose
  must forward it).

#### Choosing a pilot: cron-driven agents are viable since #2391

Verify pull-eligible volume *before* selecting a pilot, not after a week of soak:

```sql
SELECT agent_name,
       COUNT(*)                                          AS total,
       COUNT(*) FILTER (WHERE triggered_by = 'schedule') AS cron,
       COUNT(*) FILTER (WHERE triggered_by IN
         ('agent','event','schedule','webhook','reminder'))     AS pull_eligible,
       COUNT(*) FILTER (WHERE triggered_by IN
         ('loop','fan_out','a2a','operator_response'))          AS not_pullable
FROM schedule_executions
WHERE started_at::timestamptz > now() - interval '7 days'
GROUP BY 1 ORDER BY pull_eligible DESC;
```

Pick from the `pull_eligible` column. **The advice this replaces was the
opposite**: before #2391 cron could not reach the queue, so a cron-driven agent
read ~0 `pulled` on M1 no matter how the flag was set, and the only viable pilot
was a **fan-in hub fed by parallel `chat_with_agent`** — on `eu2` exactly one
agent (`cornelius-oracle`, 329/329) qualified, while the cron-driven oracles sat
at 2–5 and `brier-hq` at 0. Under the same query today those cron-driven oracles
score ~128 each. Prefer one of them for the next soak arm: they exercise the
traffic class the fleet actually runs, which `cornelius-oracle` never did.

`tests/unit/test_2048_pull_pilot_reach.py` pins both this producer's policies
*and* that the wider one is gated on `pull_owns_dispatch`, so this section
cannot go stale silently.

#### What #2391 changed under capacity pressure — and what it did not

The gate is `pull_owns_dispatch` and nothing else, so:

- **Flag OFF (every agent, by default): unchanged, byte-for-byte.** The policy
  stays `reject`; a scheduled fire arriving at capacity still fails fast with
  `Agent at capacity (N/N parallel tasks running)` and a FAILED row. Giving this
  producer an *unconditional* persistent queue would have changed that for the
  whole fleet whether or not pull was enabled — the reliability-spine change
  #2048 declined to bundle.
- **Flag ON: rejection stops existing for that agent's pullable triggers.** The
  row is never offered a slot (`acquire`'s `pull_exclusive` branch skips the
  ZADD), so "at capacity" is not a state that can reject it. Capacity becomes
  physical — the agent's worker pool. Backpressure moves to
  `agent_ownership.max_backlog_depth`: a full backlog still raises
  `CapacityFull`, at a deeper threshold, and the row's error names the backlog
  rather than parallel slots. **Watch `max_backlog_depth` on a cron-driven
  pilot**: cron fires on a fixed cadence regardless of whether the agent is
  keeping up, so a slow agent accumulates queue depth that a `reject` policy
  used to discard. M8 (queue starvation) is the query to sample for it.
- **#1083 fire-and-forget does not stack.** `schedule` and `webhook` are eligible
  for both, but a pull-queued row is never dispatched, so no 202 ACK can arrive
  and `async_result` is never sent. Pull wins by construction, not precedence.
  On a non-pilot agent #1083 behaves exactly as before. The two share the
  machinery that matters anyway — the eid-keyed slot lease, the lease reaper, and
  the `claim_token`-gated CAS terminal write.
- **The scheduler polls through `queued`.** `_poll_execution_completion` treated
  anything but `running` as an outcome, so a queued row would have been published
  as `schedule_execution_completed(status=queued)`, classified a failure, and
  handed to `_maybe_schedule_retry` — duplicating work that was queued and about
  to run. `queued` is now in `_NON_TERMINAL_POLL_STATES`. If the poll deadline
  (task timeout + buffer) expires while a row is still queued, the scheduler logs
  it and stops; recovery belongs to the lease reaper and the backlog
  maintenance sweep, not to `cleanup_service`'s stale-`running` sweep.
- **#2392 becomes load-bearing.** This moves the fleet's dominant traffic class
  onto a mechanism built on re-running the same execution, and `effect_guard`
  still fails open when the agent omits the execution id. Do not run a
  side-effect-bearing cron pilot before reading it.

**M2 — split over time.** Confirms the flip took effect at the moment you think
it did, and shows drift.

```sql
SELECT date_trunc('hour', started_at::timestamptz) AS hr,
       COUNT(*) FILTER (WHERE claimed_by_worker IS NOT NULL) AS pulled,
       COUNT(*) FILTER (WHERE claimed_by_worker IS NULL)     AS pushed
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND started_at::timestamptz > now() - interval '7 days'
GROUP BY 1 ORDER BY 1;
```

**M3 — overbooking.** Concurrent leased rows must never exceed the agent's
`max_parallel_tasks`. This is the 2x-concurrency failure the #1766 gate closes;
canary S-02 cannot see it (it counts `ZCARD` only), so it needs its own check.
Sample on a short interval during peak, not once.

```sql
SELECT o.agent_name, o.max_parallel_tasks AS cap,
       COUNT(*) FILTER (WHERE e.lease_expires_at IS NOT NULL) AS leased_running,
       COUNT(*) FILTER (WHERE e.lease_expires_at IS NULL)     AS pushed_running
FROM agent_ownership o
LEFT JOIN schedule_executions e
       ON e.agent_name = o.agent_name AND e.status = 'running'
WHERE o.agent_name = '<agent>'
GROUP BY 1, 2;
```

Verdict: `leased_running <= cap` always. `leased_running > 0 AND pushed_running > 0`
for an autonomous workload means coexistence is still live.

**M4 — lost / phantom executions.** Any non-terminal row older than the lease
window is the headline AC failing.

```sql
SELECT id, status, triggered_by, started_at, lease_expires_at,
       claimed_by_worker, redelivery_count,
       EXTRACT(epoch FROM (now() - started_at::timestamptz))::int AS age_s
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND status IN ('queued', 'running')
  AND started_at::timestamptz < now() - interval '2 hours'
ORDER BY started_at;
```

Expect **zero rows**. A `running` row past its `lease_expires_at` that the reaper
has not touched is a reaper failure; a `queued` row aging with idle workers is a
claim failure.

**M5 — re-delivery + poison-park.** Proves the lease machinery behaves under real
load rather than in the 2026-07-08 synthetic pilot.

```sql
SELECT redelivery_count, COUNT(*)
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND started_at::timestamptz > now() - interval '7 days'
GROUP BY 1 ORDER BY 1;
```

Healthy: overwhelmingly `0`, a thin tail at 1–2, nothing at `MAX_REDELIVERY`
without a matching operator alert. Cross-check the parks:

```sql
SELECT id, agent_name, title, created_at, status
FROM operator_queue
WHERE id LIKE 'poison-%' AND created_at::timestamptz > now() - interval '7 days'
ORDER BY created_at DESC;
```

Every row at the cap in the first query must have a `poison-` item here. A park
with no alert is a silent drop — treat as a blocker.

**M6 — outcome regression.** Compare against the pre-flight baseline. Bound this
on the flip timestamp — **not** a rolling `interval '7 days'`, which straddles the
flip and blends control rows into the treatment number, diluting the exact
regression the query exists to find.

```sql
-- :flip_ts = the moment the pilot agent was RECREATED (not the backend restart)
SELECT status, COUNT(*),
       ROUND(AVG(duration_ms)::numeric, 0) AS avg_ms,
       ROUND(SUM(cost)::numeric, 4)        AS cost
FROM schedule_executions
WHERE agent_name = '<agent>'
  AND started_at::timestamptz >= :flip_ts
GROUP BY 1 ORDER BY 2 DESC;
```

Watch for a `failed` share above baseline and for `duration_ms` inflation (pull
adds up to one poll interval of latency by design — the worker idles up to 15s
between claims, so a small p50 rise is expected, a p95 blow-up is not).

> **M6 alone will over-credit pull. Do not read a lower `failed` share as a win.**
>
> Under push, a caller that gives up (`chat_with_agent` sequential, or any `/task`
> caller with a short budget) kills the turn and the row is written `failed` with
> `Task execution timed out after N seconds`. Under pull that budget no longer
> governs the row: the lease is `execution_timeout_seconds + SLOT_TTL_BUFFER`, so
> the work runs to its natural end and the row lands **`success`** — while the
> caller still got nothing in time. Nothing improved; the failure moved off the
> row.
>
> The row cannot tell you this happened. `schedule_executions` never records the
> caller's timeout budget, so no query over it can separate "pull fixed it" from
> "pull outran the caller." A pilot whose baseline carries a meaningful
> `caller_timeout` share can therefore book a large, entirely fake M6 improvement.
>
> **The check is M9 (caller-side), and it must be baselined before the flip** —
> afterwards the information no longer exists.

**M7 — canary.** With E-05 lease-excluded (this branch), violations should be
genuinely rare.

```sql
SELECT invariant_id, severity, COUNT(*), MAX(snapshot_time) AS last_seen
FROM canary_violations
WHERE snapshot_time::timestamptz > now() - interval '7 days'
GROUP BY 1, 2 ORDER BY 3 DESC;
```

Any **S-01 / S-02 / E-01 / E-02 / L-03** hit on the pilot is an abort signal.
E-05 hits mean the lease exclusion is not deployed — verify the build.

**M8 — starvation.** Queue depth should oscillate, not climb monotonically.

```sql
SELECT COUNT(*) AS queued,
       EXTRACT(epoch FROM (now() - MIN(queued_at)::timestamptz))::int AS oldest_s
FROM schedule_executions
WHERE agent_name = '<agent>' AND status = 'queued';
```

A steadily rising `oldest_s` with healthy workers means claims are failing;
with busy workers it just means the agent is undersized — check M3 before
concluding.

**M9 — caller-side timeouts (the M6 counter-check).** The only observable that
survives the flip. Under push, a caller giving up produces BOTH a
`[Chat Timeout Recovery]` line in the MCP server and a `failed` row; under pull it
produces the log line and a `success` row. So the two counts **diverging** is the
signature of the M6 trap, and neither count alone is interpretable.

```bash
# Durable copy (Vector, survives container log rotation — #1871 bounds docker logs)
docker exec trinity-vector sh -c \
  "grep -h 'Chat Timeout Recovery' /data/logs/platform.json" \
  | jq -r 'select(.message | contains("<agent>")) | .timestamp' | sort | uniq -c

# Live tail equivalent
docker logs trinity-mcp-server 2>&1 | grep -c "Chat Timeout Recovery.*<agent>"
```

Read it against M6's `caller_timeout` baseline class:

| M9 log lines | M6 `caller_timeout` rows | Reading |
|---|---|---|
| ~baseline | ~0 (was N) | **The trap.** Callers still time out; the rows just stopped recording it. No improvement. |
| ~0 | ~0 | Genuine — callers now get answers inside their budget. |
| ↑ above baseline | ~0 | Regression: pull's poll interval pushed more callers past their budget. |

**Limitation, stated so it isn't rediscovered mid-analysis:** true
receipt→terminal wall-clock — the number the calling orchestrator actually feels
(`PULL_PILOT_946_SOAK.md` §3a) — is recorded **nowhere** on the platform. The row
carries no caller budget and no receipt timestamp. If that number is wanted, the
caller has to log it, and that instrumentation must exist *before* the flip.

### Abort criteria

Stop the soak and revert if any of these hold: a non-empty **M4**; **M3**
`leased_running > cap`; a critical canary violation on the pilot (**M7**); a
park with no operator alert (**M5**); or a `failed` share materially above the
pre-flight baseline (**M6**), **excluding the `quota` class** — a subscription
limit is exogenous and reverting pull will not fix it.

M9 is **not** an abort criterion; it is the interpretation guard on M6. A soak
that ends with a *better* M6 and an unchanged M9 has not demonstrated anything
and must not be written up as a pass.

**A pre-existing critical violation on the candidate pilot has to be resolved or
explicitly carved out before the flip**, not during the window — otherwise the
soak trips its own abort rule at t=0, or worse, trains the operator to ignore the
one signal the "canary green throughout" AC rests on.

### Rollback

Remove the agent from `PULL_MODE_PILOT_AGENTS` → restart backend → **recreate the
agent** (the flag only clears at create/recreate; `PULL_MODE_ENV_KEYS` is popped
on recreate precisely so de-piloting takes effect). Queued rows are picked up by
the backend drain again as soon as the pilot guard stops matching, so in-flight
work is not stranded.

---

## Appendix — key file references

| Concern | File(s) |
|---------|---------|
| Claim + lease stamp / C1 fix | `db/schedules.py::claim_next_queued` |
| Result CAS | `db/schedules.py::update_execution_status`; `services/pull_coordination_service.py::apply_task_result` |
| Pull endpoints + dual-auth | `routers/internal.py` (`pull_router`) |
| Lease reaper | `services/lease_reaper_service.py`; `services/cleanup_service.py::_sweep_expired_leases` |
| Requeue / park | `db/schedules.py::requeue_expired_lease`, `park_expired_lease` |
| Sweep exclusions (6 selectors) | `db/schedules.py` |
| Capacity shadow meter | `db/schedules.py::count_active_leased_by_agent`; `services/capacity_manager.py` |
| Agent worker pool | `docker/base-image/agent_server/services/pull_worker.py`; `agent_server/main.py` |
| Env injection + gating | `services/agent_service/pull_mode.py`; `crud.py`, `lifecycle.py`; `config.py` |
| Migrations | `migrations/versions/0020_schedule_executions_pull_claim_lease.py`, `0021_schedule_executions_redelivery_count.py`; SQLite `db/migrations.py` (version-column widening carried by dev #1420 `env.py`) |
| Tests | `tests/unit/test_1081_*.py`, `test_cleanup_inner_sweeps.py`, `test_schedule_status_observability.py`, `tests/test_canary_invariants.py` |
