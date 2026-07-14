# Ephemeral "Ghost" Agents (trinity-enterprise#69)

Disposable agents with a hard budget — created for N executions and/or a TTL,
then **hard-discarded**: container removed, DB rows purged via the cascade
primitive, Redis runtime state cleared. Ghosts never enter soft-delete/retention
(no 180-day name reservation) and are **volume-less** (container writable layer
only — they never recreate, so nothing needs to survive a recreate). Creation
with an ephemeral budget is **entitlement-gated** (`ephemeral_agents`); every
lifecycle mechanic below is an edition-agnostic OSS primitive (the
`suspended_at` core-primitive pattern).

**Positioning**: heterogeneous-workspace jobs — a different repo/config per
ghost. Burst parallelism on ONE agent belongs to `fan_out` today and replica
groups post-pull (`TARGET_ARCHITECTURE.md` names ghost-clones the anti-pattern).

## Creation

`POST /api/agents` / MCP `create_agent` with `ephemeral {max_executions?,
ttl_seconds?}` (≥1 required; Pydantic `EphemeralConfig`, models.py). In
`create_agent_internal` (crud.py), BEFORE any side effect:

1. Entitlement gate — `entitlement_service.is_entitled("ephemeral_agents")` →
   403 `ephemeral_not_entitled`.
2. `fork_to_own` conflict → 400; **ephemeral caller refusal** (an ephemeral
   agent cannot spawn ephemeral agents — chain-spawn depth-1 kill) → 403
   `ephemeral_spawn_recursion`.
3. Per-parent spawn rate limit (agent-scoped callers):
   `rate_limiter.enforce("agent_spawn:{parent}")`,
   `EPHEMERAL_SPAWN_RATE_LIMIT`/`_WINDOW_S` env (default 10/h).
4. TTL ceiling: `ephemeral_expires_at` is **ALWAYS stamped** (defaults to
   `ephemeral_ttl_ceiling_seconds`, default 24h) — no immortal ghost; over
   ceiling → 400 `ephemeral_ttl_exceeds_ceiling`.
5. **Server-suffixed name** `{base}-{hex8}` — unique-by-construction (no
   KEEP-row history inheritance, no fan-out collisions, no successor
   inheriting `spawned_by` control).
6. **Atomic quota** just before the docker block:
   `ephemeral.try_reserve_ephemeral_slot(owner_id, cap)` — Redis
   INCR-with-cap on `ephemeral:quota:{owner_id}` (fresh-counter reseed from
   the DB count; DB-count fallback when Redis is down), released on creation
   failure and at discard. Cap: `max_ephemeral_agents_per_owner` (default 5,
   0=unlimited, NO admin exemption — the quota bounds runaway spawning).

Ghost specifics in the docker block: **no workspace volume** (writable layer),
labels `trinity.ephemeral=true` + `trinity.ephemeral-expires-at`, avatar seed
skipped, git auto-sync stays off, `max_parallel_tasks=1` (bounds check-then-act
budget overshoot to one in-flight turn), ownership row written with the
ephemeral columns. Credential auto-injection at start is skipped
(`lifecycle.py` — no-credentials-by-default; explicit injection stays possible
and is human-only under Part 2).

## Spawn provenance + parent control (Part 2)

For ANY agent-spawned creation (durable or ephemeral; parent =
`current_user.agent_name`, set only for scope="agent" keys):

- `spawned_by_agent` + `spawned_by_key_id` persisted on `agent_ownership`
  (+ `trinity.spawned-by` label), and
- the `agent_permissions` edge parent→child is auto-granted with
  `created_by="spawn:{parent}"` — the parent can immediately
  `chat_with_agent`/`list_agents`/`get_agent_info` its child (the MCP layer
  gates on `agent_permissions`; `grant_default_permissions` stays a no-op).

Backend guards (`dependencies.py`):

- `enforce_agent_spawn_scope` — agent-scoped callers may start/stop/delete
  ONLY agents whose `spawned_by_agent` AND `spawned_by_key_id` match the
  calling key (name-only is forgeable via name reuse). **Interim until #948**
  capability tokens; `spawned_by_*` stays as provenance either way.
- `reject_agent_principal` — human-only 403 for agent-scoped callers on
  share/unshare, permissions set/add/remove, rename, credential
  inject/export/import.
- `_enforce_ephemeral_key_fence` — a GHOST's own key is confined at the single
  auth entry point (`get_current_user`, the connector-fence pattern) to:
  heartbeat, execution result callback, reports, notifications, own info.
  Everything else 403 (closes the "agent key resolves to owner" skeleton-key
  breadth for untrusted workspaces; also blocks REST chain-spawn). Fail-open
  on DB read error. Keyed off the agent row's `is_ephemeral` — no key-schema
  change, and heartbeat/report/callback auth keeps working (scope stays
  "agent").

Fleet-wide narrowing of agent-key breadth on other mutating routes is a
recorded accepted-risk follow-up.

## Budget enforcement

- **Admission gate** at the TOP of `CapacityManager.acquire` (beside the
  dispatch-breaker gate; the no-enqueue precedent): expired TTL or
  `terminal+running+queued ≥ max_executions` raises
  `EphemeralBudgetExhausted` — nothing admitted or enqueued. Covers every
  admission surface through the one facade (`/chat` + `/task` + scheduler +
  a2a + loop iterations). The count **excludes the row being admitted**
  (#1601: `/task` + scheduler pre-create a RUNNING row before `acquire` —
  counting it denied a `max_executions=1` ghost its first run; racing
  sibling admissions still see each other, so the overshoot bound holds).
  Fail-open on DB error. Routers map it to **410 Gone**
  (`ephemeral_budget_exhausted`); `execute_task` maps it to a FAILED
  row with `TaskExecutionErrorCode.EPHEMERAL_EXHAUSTED`.
- **Terminal hook**: `_maybe_discard_exhausted_ephemeral` — spawned via
  `_spawn_bg` after a CAS-won terminal in `apply_result` (both branches,
  AFTER slot release; fail-open). Counts ALL budget terminals
  (success/failed/cancelled; SKIPPED excluded) and triggers discard at
  budget.
- **Known gap (documented)**: `/chat` finalizes terminals outside
  `apply_result` — chat exhaustion is admission-gated immediately, discard
  lags to the GC sweep (≤5 min).
- **Pull-mode note (#1081)**: this gate lives at today's admission chokepoint,
  which pull-mode replaces — the claim endpoint must re-check the same
  predicate.

## Hard discard (`services/agent_service/ephemeral.py`)

`discard_ephemeral_agent(name, reason)` under a Redis SETNX+TTL lock
`ephemeral:discard:{name}` (hook vs GC vs second worker; fail-open unlocked
when Redis is down — steps are idempotent). Crash-convergent ordering:

0. **Durable intent marker**: `ephemeral_expires_at = now`
   (`mark_ephemeral_discard_intent`) — any crash below re-qualifies via GC
   pass 1 next cycle.
1. Cancel queued/overflow + **CAS-fail all non-terminal rows**
   (`fail_all_nonterminal_for_agent`: queued/running/pending_retry →
   FAILED `ghost_discarded`) — keeps canary L-03/E-01 green through the purge
   AND makes a late in-flight `apply_result` lose its CAS (no breaker-key
   resurrection). Open activities aren't individually closed — cascade
   deletes the rows in step 4.
2. Remove container — `force=True`, NotFound tolerated (half-discarded state
   must be resumable). The writable layer (the whole workspace) dies here.
3. `clear_agent_runtime_state` — **BEFORE the purge** frees the name
   (mirrors the delete-endpoint ordering; #1560).
4. `purge_ephemeral_agent_ownership` — cascade over ~40 child tables; refuses
   non-ephemeral rows; `schedule_executions` KEEP (ages out via 90d
   retention; post-purge the rows are admin-only visible — owner visibility
   derives from the purged ownership row, documented deviation). Quota slot
   released on success.
5. Audit `agent_lifecycle:ephemeral_discard`.

`DELETE /api/agents/{name}` routes ephemeral agents here — the branch runs
BEFORE the container lookup (a half-discarded ghost is force-discardable,
never 404). MCP `delete_agent` is the discard surface (no separate tool).

## GC (`cleanup_service._sweep_ephemeral_agents`, 5-min)

1. **DB pass**: `find_discardable_ephemeral_agents` (expired OR over budget) →
   discard; capped `EPHEMERAL_DISCARDS_PER_CYCLE=10` + 60s per-discard timeout
   (serial Docker I/O must not stall the other sweeps).
2. **Docker-as-truth orphan pass**: containers labeled `trinity.ephemeral=true`
   with NO ownership row (restart mid-create/mid-discard) → removed, with a
   **15-min newborn grace** on the `trinity.created` label (creation writes
   the ownership row LAST).

Interim-until-#429: folds into the consolidated lease reaper.

## Fleet hygiene

- Heartbeat watch loop + fleet health polling **exclude** ghosts (no
  spurious alive→stale alert per discard; less per-ghost RPC); operator-queue
  polling keeps them (a ghost may escalate).
- Execution/cost stats stay **inclusive** (billing truth).
- Schedule creation on a ghost → 400 `schedule_on_ephemeral_agent`.
- `AgentStatus.ephemeral` (from the label) flows through `GET /api/agents` +
  MCP `list_agents`; GHOST badge in Agents list + AgentHeader.

## Schema

`agent_ownership` + `is_ephemeral INTEGER DEFAULT 0`,
`ephemeral_max_executions INTEGER`, `ephemeral_expires_at TEXT`,
`spawned_by_agent TEXT`, `spawned_by_key_id TEXT`. Dual-track:
SQLite `agent_ownership_ephemeral` (db/migrations.py) + Alembic
`0016_agent_ownership_ephemeral`; DDL in db/schema.py + MetaData in
db/tables.py. Accessors: `db/agent_settings/ephemeral.py` (EphemeralMixin) +
`fail_all_nonterminal_for_agent` (db/schedules.py); facade delegations in
database.py.

## Testing

`tests/unit/test_69_ephemeral_agents.py` (42 tests, db_harness — real engine,
never a wholesale-mocked `database`): column live-select; mixin accessors
round-trips incl. purge-refuses-durable + cascade read-back; facade
delegations; acquire-gate deny matrix (expired/exhausted/active-counted,
fail-open) proven to fire BEFORE slot work, incl. own-row exclusion at both
the DB and gate layers (#1601); key-fence allow/deny matrix;
Part 2 guard matrix (name+key-id); budget hook fail-open + trigger; discard
full-path + idempotent re-run + half-discarded resume + lock contention +
audit-row read-back; atomic quota INCR-with-cap + Redis-down fallback.

## Known residuals (recorded)

- Shared kernel (no gVisor/microVM lane) and no per-ghost egress control —
  mitigations: key fence, no-credentials default, resource caps, quotas.
- Ephemeral ≠ credential-free: the execution credential (platform key /
  subscription token) is still in-container env.
- Durable-agent volume leak (`volume_remove` had zero callers) is a separate
  public bug — ghosts sidestep it by being volume-less.
