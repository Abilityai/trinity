# Canary Invariant Harness — Phase 1 Design

**Date:** 2026-04-27
**Status:** Proposal — implements Phase 1 of [#411](https://github.com/Abilityai/trinity/issues/411).
**Reference:** [`docs/testing/orchestration-invariant-catalog.md`](../testing/orchestration-invariant-catalog.md)

---

## Scope

Catalog's Phase 1 subset is 12 invariants. AC of #411 requires three running continuously on staging at deploy time (S-01, E-02, L-03); the remaining 9 ship as follow-up PRs against the same infrastructure.

This doc specifies the full Phase 1 design (all 12) so each follow-up is purely an additive code change.

## Invariant coverage

Each row gives the check semantics and what the snapshot collector must capture for it.

| ID | Check (one-line) | Snapshot inputs |
|---|---|---|
| **S-01** | Per agent A: `Redis ZRANGE agent:slots:A` (minus drain sentinels < 5s old) == `SQL exec_ids WHERE agent_name=A AND status='running'` | Redis ZSET, SQL running rows |
| **S-02** | Per agent A: `ZCARD agent:slots:A ≤ agent_ownership.max_parallel_tasks` | Redis ZCARD, SQL max_parallel |
| **S-03** | Per slot member: `TTL agent:slot:A:{eid} ≥ timeout_seconds + 300` | Redis TTL, per-execution timeout (schedule override or agent default) |
| **E-01** | `SQL count WHERE status='running' AND started_at < now() - (timeout + 300s)` == 0 | SQL running rows + timeouts |
| **E-02** | No `update_execution_status` log line shows `terminal_state → non_terminal_state` since last snapshot | Vector log diff (`update_execution_status` lines) |
| **E-05** | `SQL count WHERE status='running' AND started_at < now()-60s AND claude_session_id IS NULL` == 0 | SQL running rows |
| **E-06** | For every SQL row `status='running' AND started_at < now()-60s`, exec_id appears in agent's `GET /api/executions/running` | SQL running rows + agent registry |
| **B-01** | Per agent A: `backlog.get_queued_count(A) == SQL count WHERE status='queued' AND agent_name=A` | Backlog state, SQL queued rows |
| **B-02** | If queued count > 0, then `ZCARD agent:slots:A == max_parallel_tasks` (or drain pending ≤60s) | SQL queued, Redis ZCARD, SQL max_parallel |
| **L-03** | Orphan scan: no row in {agent_sharing, agent_schedules, schedule_executions (non-terminal), agent_permissions, agent_event_subscriptions, mcp_api_keys (scope='agent'), slack_channel_agents, agent_shared_folder_config, chat_sessions (active)} references an `agent_name` not in agent_ownership; no Redis `agent:slots:{name}` for missing agent | SQL multi-table joins, Redis KEYS |
| **G-01** | After backend restart: no `status='running'` SQL row without matching agent registry entry, after startup-sweep + 5min grace | SQL running rows + agent registry (post-restart only) |
| **R-01** | Per running agent container: `docker exec ps -eo stat,comm` shows zero zombie `claude` processes | Container exec output |

## Snapshot format

Single typed dataclass returned by the collector. One snapshot per check cycle.

```python
@dataclass
class Snapshot:
    timestamp: datetime
    agents: list[AgentSnapshot]
    transitions_since_last: list[StatusTransition]  # for E-02
    orphan_refs: dict[str, list[OrphanRef]]         # for L-03; keyed by table

@dataclass
class AgentSnapshot:
    name: str
    container_running: bool
    max_parallel: int
    execution_timeout_seconds: int
    # Redis
    slot_ids: set[str]
    slot_ttls: dict[str, int]              # eid -> TTL seconds
    # SQL
    running_exec_ids: set[str]
    queued_exec_ids: set[str]
    overdue_running_ids: set[str]          # started_at < now() - (timeout+300)
    no_session_running_ids: set[str]       # claude_session_id IS NULL > 60s
    # Backlog service
    backlog_queued_count: int
    # Agent registry
    registry_running_ids: set[str] | None  # None if agent unreachable this cycle
    # Container exec
    zombie_claude_count: int | None        # None if exec failed

@dataclass
class StatusTransition:
    execution_id: str
    from_status: str
    to_status: str
    timestamp: datetime
    log_source: str                        # vector log id

@dataclass
class OrphanRef:
    table: str
    column: str
    referenced_agent_name: str
    row_id: str
```

The collector lives at `src/canary/snapshot.py`. Public API:

```python
async def collect_snapshot(
    *,
    since: datetime | None = None,    # for transitions_since_last
    include_zombies: bool = True,
) -> Snapshot
```

Reusable from unit tests by passing fixtures. Phase 2's scenario runner uses the same primitive.

### Snapshot sources

| Source | Access | Used by |
|---|---|---|
| SQLite | Backend API (per catalog rec for Phase 1 — enforces real code path) | S-01, S-02, S-03, E-01, E-05, E-06, B-01, B-02, L-03, G-01 |
| Redis | `ZRANGE`/`ZCARD`/`TTL`/`KEYS` via MCP tool | S-01, S-02, S-03, B-02, L-03 |
| Agent registries | Parallel `GET /api/executions/running` via `asyncio.gather` | E-06, G-01 |
| Container exec | `docker exec {name} ps -eo stat,comm` | R-01 |
| Vector logs | Read JSON log file diff since last snapshot timestamp | E-02 |

Agent-unreachable cases set the relevant fields to `None` rather than raising; affected invariants skip the cycle for that agent (mirrors the cleanup service's "skip-and-retry-next-cycle" pattern from PR #403).

## Invariant library

Twelve pure functions: `check(snapshot) → list[ViolationReport]`. Each ~30-50 LOC.

```
src/canary/invariants/
  s01_slot_row_bijection.py
  s02_no_overbooking.py
  s03_slot_ttl.py
  e01_terminal_closure.py
  e02_no_phantom_reversal.py
  e05_dispatched_has_session.py
  e06_no_completed_unreported.py
  b01_queue_status_coherence.py
  b02_queued_implies_full.py
  l03_delete_cascades.py
  g01_no_restart_leak.py
  r01_no_zombies.py
```

`ViolationReport` matches the `canary_violations` schema below.

## Fleet

Pre-seeded agents the canary observes. All run Claude Code (no architectural bypass exists); minimize cost via trivial prompts.

Fleet is designed against the invariant set: each agent exists for specific invariants.

| Agent | Settings | Schedule | Exercises |
|---|---|---|---|
| `canary-burst` | `max_parallel=1`, default 15min timeout | every 30s, short prompt (`"reply ok"`) | S-01, S-02, B-01, B-02, E-02, E-05, R-01 |
| `canary-long` | `max_parallel=2`, custom 45min timeout | every 5min, multi-tool prompt (~10–60s) | S-03, E-01, E-06, R-01 |
| `canary-rotate-{ts}` | default | hourly create + delete by canary skill | L-03 |

**Why these and not others:**
- `canary-burst` cadence (30s) < expected task duration so backlog overflows — exercises B-01/B-02 organically.
- `canary-long` is the only agent with non-default timeout — without it, S-03 has nothing to check.
- `canary-rotate-{ts}` is the only active-mutation agent in Phase 1 (everything else is observation). Without it L-03 sits vacuously green.
- G-01 (restart leak) needs a backend restart, not a fleet member.

For the AC's 3-invariant initial deploy: only `canary-burst` + `canary-rotate-{ts}` are required (S-01 and E-02 fire on `canary-burst` traffic, L-03 on rotation). `canary-long` is added when S-03/E-01/E-06 invariants are wired up.

Naming follows catalog §Open Questions: `canary-*` prefix, dedicated synthetic operator user.

## Database migration

```sql
CREATE TABLE canary_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT NOT NULL,            -- 'S-01', 'E-02', 'L-03', ...
    tier TEXT NOT NULL,                    -- 'A' or 'B'
    severity TEXT NOT NULL,                -- 'critical', 'major', 'minor'
    snapshot_time TEXT NOT NULL,
    observed_state TEXT NOT NULL,          -- JSON payload, invariant-specific
    signal_query TEXT,                     -- the check that fired (debugging aid)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_canary_violations_invariant ON canary_violations(invariant_id, snapshot_time DESC);
CREATE INDEX idx_canary_violations_severity ON canary_violations(severity, snapshot_time DESC);
```

Versioned migration in `src/backend/db/migrations.py`. Read endpoint: `GET /api/canary/violations` (admin-only, supports filters by invariant_id / severity / time range).

## Canary agent template

New template at `config/agent-templates/canary-invariant/`:

- `template.yaml` — deletion-protected, owned by synthetic operator user
- `CLAUDE.md` — operating instructions
- `dashboard.yaml` — green/red widget per invariant + 24h violation trend per invariant
- Scheduled skill `/check-invariants` every 5 min: `collect_snapshot()` → run all enabled invariants → write violations + push alerts

Deletion-protected mirrors the `trinity-system` pattern.

## Alert channel

Three layers:

1. **Persistent** — every violation written to `canary_violations`. Source of truth for trend queries and forensic replay.
2. **Dashboard** — green/red per invariant + 24h sparklines via the agent's `dashboard.yaml`.
3. **Push** — alert *only* on state transitions (green→red) and severity thresholds. Never on every check; otherwise operators learn to ignore them.

**Push channel: TBD.** Slack / Telegram / email. Needs to be decided before staging deploy. Existing Slack/Telegram channel adapters can be reused (no new transport needed).

## Rollout

1. Migration + read endpoint
2. Snapshot collector (full schema)
3. S-01, E-02, L-03 invariants + tests + canary agent template + `canary-burst` and `canary-rotate` fleet
4. Push alert wiring (channel decided)
5. Deploy to staging; observe for 30 days → close AC
6. (Follow-up PRs) S-02, S-03, E-01, E-05, E-06, B-01, B-02, G-01, R-01 invariants + add `canary-long` to fleet

Each step is a separate PR.

## Acceptance criteria mapping

- ✅ Catalog reviewed → S-03 and E-05 added to Phase 1 subset (same PR)
- ✅ Phase 1 design doc reviewed → this doc covers all 12 invariants, snapshot format, alert channel
- 🔲 `canary_violations` table + snapshot-collector → steps 1-2
- 🔲 First 3 invariants running on staging → steps 3-5
- 🔲 One real violation caught and alerted (or 30 days clean) → step 5

## Open questions

1. **Staging deploy access** — does it exist; how to provision canary + fleet
2. **Push alert channel** — Slack / Telegram / email
3. **Snapshot retention** — keep raw snapshots for forensic replay or just violations
4. **Container exec for R-01** — `docker exec` from the canary agent requires Docker socket access; alternative is exposing a `GET /api/zombies` endpoint on the agent server. Open for review.
