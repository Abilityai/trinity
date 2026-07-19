# Pull / Work-Stealing Migration — Rollback Runbook

**Scope:** how to safely disable or roll back the pull-coordination change (umbrella #1081, Phases 0–3)
on a live instance. Companion to `PULL_MIGRATION_STATUS.md` (what shipped) and `PULL_MIGRATION_TESTING.md`
(risk surface + findings).

> **TL;DR** — This change is flag-gated and additive by construction. On a normal instance it is **inert**
> until `PULL_MODE_PILOT_AGENTS` is populated. If a *pilot* misbehaves → **Tier 0** (empty the flag, seconds).
> If the *push fleet* regresses → **Tier 1** (redeploy the previous image). The DB rarely needs touching.

---

## 1. Why rollback is low-risk by design

- **Flag-gated, default OFF.** Everything routes through `PULL_MODE_PILOT_AGENTS` (backend env, default
  empty). Empty ⇒ no worker pool starts, no leases are created, the capacity meter is a no-op, the
  lease-reaper finds nothing. Populating the list is the *only* switch that activates pull for an agent.
- **Additive, backward-compatible schema.** The migrations add **nullable** columns
  (`claim_token`, `lease_expires_at`, `claimed_by_worker`, `redelivery_count`) via `ADD COLUMN IF NOT
  EXISTS`. Old code neither reads nor writes them, so you can roll back **code without touching the DB**
  (expand/contract-safe).
- **The reaper is self-gating.** `_sweep_expired_leases` only acts on rows with `lease_expires_at IS NOT
  NULL`, and *only* the pull claim path ever sets that. Flag off ⇒ zero leased rows ⇒ reaper is inert even
  though it runs every cleanup cycle.
- **Additive until Phase 5.** The push path, Redis slot ZSET, overflow LIST, dispatch-breaker-gate, and
  canary S-01–S-03 are untouched. Nothing is deleted or rewired, so the pre-change behavior is fully intact
  underneath.

**Two risk tiers (from `PULL_MIGRATION_TESTING.md` §1):**

| Surface | Reaches | Rollback |
|---------|---------|----------|
| Pull worker pool, claim/CAS endpoints, redelivery/poison-park | **only piloted agents** (staged) | Tier 0 (flag) |
| New nullable columns; `lease_expires_at IS NULL` grafted onto 6 existing sweeps; the reaper loop; the CAS `claim_token` param; canary S-01; the capacity **shadow** meter | **every instance, flag or not** | Tier 1 (code) |

---

## 2. Pre-deploy checklist (capture a restore point)

1. **Back up the DB.** `pg_dump` (Postgres) or `scripts/deploy/backup-database.sh` (SQLite). Keep the file
   off the instance.
2. **Record the rollback target** — the currently-running backend image tag / commit SHA.
3. **Confirm the deploy is inert:** `PULL_MODE_PILOT_AGENTS` empty in the backend env (default). A fresh
   deploy should change *no runtime behavior* until you opt an agent in.
4. **Post-deploy, confirm migrations applied:** Alembic head reaches `0021_schedule_executions_redelivery_count`
   (Postgres) / the SQLite `schedule_executions_redelivery_count` migration is recorded, and the backend
   `/health` is 200.

---

## 3. Tier 0 — instant off-switch (no redeploy, seconds)

Use when **a piloted agent misbehaves** (duplicate work, over-subscription, stuck leases). No data loss.

1. Empty the allowlist in the backend env: `PULL_MODE_PILOT_AGENTS=` (remove the agent, or all of them).
2. **Restart the backend.** The claim seam (`_pull_authorized`) is allowlist-gated, so de-piloted agents'
   workers now get **403** on `GET /api/internal/next-task` and idle. The **result-report** path stays open
   so any **in-flight** result still lands (no orphaned running rows).
3. **Recreate the piloted agent(s)** to fully stop the worker *process* (env injection is at create/recreate;
   the B1 fix pops the baked `TRINITY_PULL_MODE` on recreate). Until recreate, the worker keeps polling but
   is rejected — harmless.

After this, dispatch is 100% back on the untouched push path. Leftover leased rows (if any) are recovered
by the reaper (re-queued to the push fleet) or age out.

> Backend restart invalidates JWTs (users re-login) and MCP clients reconnect — the normal restart cost.

---

## 4. Tier 1 — code rollback (redeploy previous image)

Use when a regression hits the **push fleet with the flag already empty** — i.e., a bug in the
"reaches every instance" surface (the 6 amended sweeps, the reaper loop, the shadow meter, the CAS
`claim_token` param, canary S-01).

1. Redeploy the **previous** backend image / commit (the target recorded in §2).
2. **Leave the new DB columns in place.** They are nullable and unread by the old code — no downgrade
   needed, no data loss. This is the expand/contract guarantee.
3. Rebuild/redeploy the agent base image only if you had piloted agents on the pull worker; a stale worker
   in an old agent container simply gets 403s from the reverted backend and idles.

No DB step is required for a code rollback.

---

## 5. Tier 2 — DB rollback (rarely / never needed)

The migrations are purely additive. **Recommended posture: leave the columns.** They cost nothing and keep
a future re-deploy a no-op (`ADD COLUMN IF NOT EXISTS`).

If you are *required* to remove them (e.g., a strict schema audit):
- Postgres: `alembic downgrade 0015_enterprise_connectors` (drops `0017`→`0016`, the two pull revisions).
  Do **not** target `0014_agent_schedules_webhook_auth` — that also drops `0015_enterprise_connectors`
  (the OSS-core MCP connector table, #118), which has nothing to do with this feature. Verify no
  application is mid-write to `schedule_executions` first.
- SQLite: the bespoke runner has no down-migrations; restore from the §2 backup instead.

Only do this after a full code rollback (Tier 1) — never drop columns the running code still references.

---

## 6. Detection signals (know when to pull the cord)

- **Operator queue:** poison-park items (`poison-{execution_id}`, type `alert`) — a task hit
  `MAX_REDELIVERY` and parked for a human. A rising count signals a pilot agent failing repeatedly.
- **DB:** `redelivery_count` climbing on `schedule_executions` rows; `running` rows with a stale
  `lease_expires_at`.
- **Fleet health / execution failure rate** via existing monitoring.
- **Canary invariants** — ✅ **closed by #1540:** all SQL-tier collector reads now route through the
  `get_engine()`/`DATABASE_URL` seam, so on a **Postgres** instance the SQL-tier checks
  (S-01/E-01/E-05/E-02/B-01 and the fleet-wide E-03/G-03/E-06/L-03) read the live PG database, not a stale
  `/data/trinity.db`. The canary is a trustworthy signal on PG. (Historical G3 caveat: the collector was
  SQLite-only and went vacuously green on PG.)

---

## 7. Scenario → response

| Symptom | Likely cause | Response |
|---------|--------------|----------|
| A piloted agent duplicates work / exceeds `max_parallel_tasks` | **B4** (pull admission bypasses the ZSET budget → up to 2N; shadow meter observes, doesn't gate — deferred to Phase 5) | **Tier 0** — de-pilot that agent |
| Piloted agent's tasks pile into poison-park | agent failing every attempt (auth, crash, hang) | Resolve/inspect via operator queue; **Tier 0** de-pilot if systemic; fix root cause |
| Push-fleet executions regress with the flag **empty** | a bug in the dark surface (sweep exclusion / reaper / shadow meter / CAS param) | **Tier 1** — redeploy previous image |
| Backend won't boot after deploy (migration error) | migration failure | Migrations are idempotent (`IF NOT EXISTS`) and the version-column width is handled by #1420; if truly stuck, **Tier 1** (old code runs fine with the columns present) and inspect `alembic_version` |
| Leased rows never recovered | reaper not running / paused | Confirm cleanup service is up; check the #1085 re-delivery governor isn't paused; leases age out via the stale-execution window as a backstop |

---

## 8. Point of no easy return — Phase 5

This PR covers **Phases 0–3 only** and flips **nothing** on by default. The irreversible step is **Phase 5**
(default-ON + *deleting* the legacy push machinery: slot ZSET, overflow LIST, dispatch-breaker-gate, canary
S-01–S-03). That is explicitly gated behind a **≥2-week zero-orphan soak (#856)** plus the side-effect gates
(#1401/#1402/#1408), and is **out of scope here**. Until Phase 5, the push path remains the always-available
fallback and every tier above applies.
