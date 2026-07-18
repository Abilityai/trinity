# Pull / Work-Stealing Migration — Status & Handoff

**Branch:** `feature/target-arch-pull-migration` (based on `origin/dev @ 0d3e58b7`)
**Umbrella:** #1081 (Epic #1045) · **Target design:** `docs/planning/TARGET_ARCHITECTURE.md` (v2 / Direction B, merged #1404)
**Wire contract:** `docs/planning/MESSAGE_ENVELOPE_SCHEMA.md` (#945) · **Taxonomy:** `docs/planning/ACTOR_MODEL_POSTCARD.md`
**Testing + findings:** `docs/testing/PULL_MIGRATION_TESTING.md` · **Rollback:** `docs/planning/PULL_MIGRATION_ROLLBACK.md`

> Consolidates the former `ORCHESTRATOR_SESSION_CONTEXT.md`, `PULL_MIGRATION_SESSION_HANDOFF_2026-07-07.md`,
> and `PRD.json` into one status doc. The work was built by an orchestration session that fanned out
> sub-agents and verified their test evidence; a separate human-interactive session owned the design
> decisions. This file is the durable record of **what was built, what is load-bearing, and what remains**.

---

## 1. One-line model

The backend owns one durable per-agent queue (`schedule_executions` rows: `queued → claimed/running →
terminal`). Each agent's worker pool **pulls** the next task when it has a free worker, runs it, and POSTs
the result back under a compare-and-set guard. Nothing is pushed at a busy or dead agent. Everything ships
behind **`PULL_MODE_PILOT_AGENTS`** (default empty ⇒ inert), on read-only agents first.

## 2. Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| #945 | Message-envelope payload schema (spec) | ✅ done (`MESSAGE_ENVELOPE_SCHEMA.md`) |
| **Phase 0** | Dark nullable `claim_token` / `lease_expires_at` / `claimed_by_worker` on `schedule_executions` | ✅ done (dual-track: SQLite `migrations.py` + Alembic `0016`) |
| **Phase 1** | Dark pull endpoints — atomic claim + result CAS | ✅ done |
| **Phase 2** | Agent worker-pool behind `PULL_MODE_PILOT_AGENTS`; scoped-key auth | ✅ done (build + **live pilot proven**, §6) |
| **Phase 3** | Lease-reaper + `MAX_REDELIVERY` + capacity **shadow** meter + canary S-01 lease-exclusion | ✅ done (Alembic `0017` adds `redelivery_count`) |
| **Phase 4** | Sync edge adapter (`/chat`, `chat_with_agent`) + async fan-out join | ⬜ not started |
| **Phase 5** | Default-ON + delete legacy (ZSET / overflow LIST / dispatch-breaker-gate / canary S-01–S-03) | ⬜ blocked on ≥2-wk soak (#856) + side-effect gates |

**Prerequisite — PostgreSQL at fleet scale (#1183/#746/#1278, SQLite EOS 2026-09-01):** the local instance
now runs on PostgreSQL; the dark migrations (`0016`, `0017` — originally `0011`/`0012`, renumbered on rebase
onto dev) are applied and verified on live PG. Fleet-scale carry + single-source consolidation remain. Not
needed for the local pilot.

## 3. What was built (the increment)

1. **#945 message-envelope schema** (`MESSAGE_ENVELOPE_SCHEMA.md`): field-level payload per boundary
   message kind (chat/task/event/reply + next-task claim + result POST); `status` + `error_code` taxonomy
   byte-identical to `ACTOR_MODEL_POSTCARD.md`.
2. **Phase 0 — dark schema** (Alembic `0016`, SQLite `schedule_executions_pull_claim_lease`): three nullable
   columns on `schedule_executions`. Dual-track (`migrations.py` + Alembic + `schema.py`/`tables.py`/`db_models.py`).
3. **Phase 1 — dark pull endpoints** (`routers/internal.py` → `services/pull_coordination_service.py` →
   `db/schedules.py`):
   - `GET /api/internal/next-task` — atomic claim of the oldest `queued` row for an agent; stamps
     `claim_token` / `lease_expires_at` (= `execution_timeout + SLOT_TTL_BUFFER`) / `claimed_by_worker`;
     status → `running`. Returns the claim envelope; empty ⇒ `{envelope: null}`.
   - `POST /api/internal/tasks/{id}/result` — CAS-applies a terminal (status-precondition + `claim_token`
     match); idempotent replay.
   - **Dark:** no production caller; push path unchanged.
4. **Phase 2 — agent worker-pool** (`agent_server/services/pull_worker.py`, wired in agent-server `main.py`):
   bounded pool (N = `max_parallel_tasks`) short-polling `next-task`, running the turn, POSTing the result.
   Gated per-agent by the backend `PULL_MODE_PILOT_AGENTS` allowlist (`services/agent_service/pull_mode.py`
   injects `TRINITY_PULL_MODE` / `TRINITY_MAX_PARALLEL_TASKS` at create/recreate). **Default OFF ⇒ no-op.**
5. **Pull-seam auth hardening**: the two pull seams accept either a valid `X-Internal-Secret` (backend) **or**
   the agent's own scoped MCP key with agent_name/ownership match (via `authorize_heartbeat`). No master
   secret in an agent container; the worker auths with `Bearer ${TRINITY_MCP_API_KEY}` (respects #1159/#307).
6. **Phase 3 — reaper + `MAX_REDELIVERY`** (Alembic `0017` adds `redelivery_count`, distinct from `retry_count`):
   `services/lease_reaper_service.py` + additive `cleanup_service._sweep_expired_leases`. Finds
   `status='running' AND lease_expires_at < now`; under cap → **re-queue the SAME `execution_id`** (status →
   queued, clear lease/claim/worker, `redelivery_count++`); at cap (`>= MAX_REDELIVERY`, default 3) → FAIL +
   park to the OPS-001 operator queue. CAS-guarded.
   - **Sweep-exclusion:** `lease_expires_at IS NULL` added to 6 non-reaper selectors so leased rows are owned
     **exclusively** by the reaper.
   - **#1082 status-guard:** `park_expired_lease` / `requeue_expired_lease` registered in `_EXPECTED_UPDATE_SITES`.
7. **Phase 3 — capacity shadow meter** (`db/schedules.py::count_active_leased_by_agent` → `CapacityManager`
   `get_all_states` / `get_slot_state`): read-only physical-occupancy term (`active = zcard + leased`) merged
   into the capacity **meter** for pilot agents. **SHADOW only** — admission stays on the ZSET
   (`acquire`/`acquire_slot`/`release` and `slot_service.py` untouched). Physical **admission** is Phase 5.
8. **Canary S-01 lease-exclusion**: leased rows excluded from the slot–row bijection (additive
   `snapshot.running_lease_expires_at`) so a pilot does not false-fire `in_sql_only`.

## 4. Load-bearing invariants — DO NOT break

1. **Preserve `execution_id` on re-delivery.** The reaper (and any operator-gate resolution) re-queues the
   **same** row — never mints a new `execution_id`. `effect_guard` (#1084) and #525 idempotency are
   `execution_id`-scoped; a new id would re-emit side effects. (Decision session's hard cross-cutting
   invariant, #1402.)
2. **Leased rows (`lease_expires_at IS NOT NULL`) are owned exclusively by the lease-reaper.** Every other
   sweep / ZSET-consistency check excludes them (`lease_expires_at IS NULL`). Applies to the 6 cleanup
   selectors AND canary S-01.
3. **Everything is flag-gated, default-OFF.** `PULL_MODE_PILOT_AGENTS` empty ⇒ no worker, no lease, meter
   no-op, reaper finds nothing. Populating it is the ONLY switch that activates pull for an agent.
4. **No master secret in an agent container.** Pull seams use the agent's scoped MCP key; the internal secret
   is a backend-only alternate.
5. **Additive until Phase 5.** The push path, Redis slot ZSET, overflow LIST, dispatch-breaker-gate, and
   canary S-01/S-02/S-03 are NOT deleted or bypassed until Phase 5 (gated on ≥2-week zero-orphan soak, #856).
6. **`MAX_REDELIVERY = 3`**, per-agent override deferred (seam = `lease_reaper_service.get_max_redelivery`).
   `redelivery_count` column ≠ `retry_count` (#678 reader-race).

## 5. Design decisions (side-effect-gate track)

The v2 target-arch pivot reframes side-effect handling from a single universal `effect_guard` to **per-effect**
gating. Decisions below were resolved by the human-interactive decision session and settled by v2 (#1404).

- **#1401 — structured recovery trace + injection** (`decided`, not yet built). Irreversible effects through
  un-confineable channels (own key / gh / curl): **best-effort trace + fall back to the #1402 async operator
  human-gate**. The continue / verify-before-redo / fail-gracefully branch is **prompt-advisory** — the agent
  decides (Principle #8). Injection = bounded system-prompt summary + full record in a context file.
- **#1402 — `MAX_REDELIVERY` cap + async operator-queue human-gate** (`decided`; reactive-park half built).
  Operator resolution **re-queues the SAME `execution_id`** (park = a non-terminal state; lease/worker
  released, identity kept), injecting the #1401 trace + the operator's answer — one code path for both
  auto-retry and human-gated resume. Cap default **3**, per-agent overridable (the poison-task cap, NOT the
  un-confineable lever). Park reuses the OPS-001 operator_queue create path. Counter = a new
  `schedule_executions` column (built), distinct from `retry_count`.
- **#1408 — deterministic tool-side gate on confined-irreversible rails** (`decided`, not built). Eligibility
  bar: Trinity solely fronts the rail AND the agent holds no direct credential (v1 = **Nevermined settle
  only**). Enforcement v1 = the eligibility bar (credential platform-held, not agent-injectable); defer the
  runtime credential-scanning guard until a rail whose credential could be agent-held is proposed. Effect
  classes declared as per-MCP-tool metadata. **Caveat:** tool-metadata tags only *confined* effects;
  un-confineable effects have no tool to tag — the platform's only handle is the agent's credential surface +
  optional `template.yaml` author declaration.
- **#1084 — `effect_guard` re-scoped** (`n/a`, buildable): reversible/backend-sink slice; hand
  confined-irreversible to #1408. No longer gates pull. 4 sinks already merged (`send_message`,
  `place_outbound_call`, `create_share`, `settle_payment_once`).

**Still `questions-open`** (owned by the decision session, not started): **#927** replica groups
(post-Phase-5 + PG), **#947** GuardAgent output interception (orthogonal), **#948** workflow-scoped capability
tokens (gated on the #946 decision). Do not implement until flipped to `decided`.

## 6. Live pilot (#946) — proven

Executed end-to-end on `gtm-synthesizer` (rebuilt base image, `AGENT_AUTH_SECRET` populated):
- Worker pool up (`3 worker(s)`); happy path `queued → running` (claim_token + lease set) → run → CAS result
  → terminal `success`, with **`ZCARD(agent:slots)=0` the whole run** (pure SQL lease, no ZSET slot — the core
  pull invariant). `execution_id` preserved.
- Reaper: requeue ×3 (`redelivery_count` 0→3, same id, `claim_token` kept) → park at cap (row `failed` +
  `poison-{eid}` operator alert). **Late SUCCESS after park with the kept token → `applied`** (failed→success).
- Canary S-01 lease-exclusion holds; **E-05 fires** (known open gate — §7).

Full walkthrough, gaps, and the two ops fixes surfaced (G1 compose-forwarding, G2 recreate backend-URL) are in
`docs/testing/PULL_MIGRATION_TESTING.md`.

## 7. Remaining work & gates

- **Canary E-05 / E-01 lease-awareness** — E-05 *will* false-fire on a pull lease (running >60s,
  `claude_session_id` NULL). Apply the same `lease_expires_at IS NULL` exclusion, or retire with the ZSET at
  Phase 5 — **decide before opting a pilot in** for a clean canary.
- **Canary collector on PG (G3) — ✅ closed by #1540** — the SQL-tier collector reads now route through the
  `get_engine()`/`DATABASE_URL` seam, so on a PG instance the SQL-tier checks read the live PG database
  instead of a frozen/empty `/data/trinity.db`. The canary is a trustworthy signal for a default-ON decision
  on PG. (`db/connection.py` stays the sqlite-only maintenance seam; the canary is routed around it.)
- **Tier-6 side-effect safety** — `effect_guard` is fail-open without trusted `execution_id` injection; a
  **BLOCKING prerequisite** before default-ON for side-effect agents (read-only agents reach Phase 5 without it).
- **Phase 4** — sync edge adapter + async fan-out join.
- **Build #1401 / #1402 / #1408** — now `decided`; #1402's proactive fire-and-park half + the per-agent
  `MAX_REDELIVERY` override are not yet built.
- **Phase 5** — default-ON + delete ZSET/overflow/breaker-gate/canary S-01–S-03. Gated on ≥2-wk zero-orphan
  soak (#856) + the side-effect gates.

## 8. Environment & verification caveats

- **Local stack now on PostgreSQL** (`DATABASE_URL=postgresql://…@postgres:5432/trinity`; no SQLite file).
  Backend **bind-mounts `src/backend`**, so it runs the uncommitted pull code live (endpoints answer 403/422,
  not 404). The **agent base image** was rebuilt to bake in `pull_worker.py` + scoped-key auth (the original
  image predated the worker; agents bake `agent_server` with no source bind-mount).
- **Claude auth for live turns:** local Trinity otherwise has no Claude auth (agents auth-fail in ~1s). Use a
  working out-of-tree `ANTHROPIC_API_KEY` for live pilots; prefer unit/integration tests elsewhere.
- **Status-writers:** any change writing `schedule_executions.status` MUST pass
  `tests/unit/test_schedule_status_observability.py` (the #1082 `_EXPECTED_UPDATE_SITES` guard).
- **Capacity/slots:** any change near capacity/slots MUST run `tests/test_canary_invariants.py` (S-01/S-02) —
  prove you didn't "fix" a meter by writing pull rows into the ZSET.
- **PG-only races:** the double-claim race (see testing doc, C1) only manifests on PostgreSQL; SQLite
  serializes writes and hides it. Point PG tests at a **throwaway** DB, never the live `trinity` DB.

## 9. Pointers

- `docs/planning/TARGET_ARCHITECTURE.md` — v2 destination design.
- `docs/planning/MESSAGE_ENVELOPE_SCHEMA.md` — the wire contract (#945).
- `docs/planning/ACTOR_MODEL_POSTCARD.md` — the pinned status/error_code taxonomy.
- `docs/testing/PULL_MIGRATION_TESTING.md` — tiered QA plan, confirmed defects, and the live-pilot record.
- `docs/planning/PULL_MIGRATION_ROLLBACK.md` — rollback runbook (flag off-switch, code/DB tiers, detection signals).
