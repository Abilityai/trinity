# Canary Invariant Harness — Phase 1 Design

**Date:** 2026-04-27
**Status:** Proposal — implements Phase 1 of [#411](https://github.com/Abilityai/trinity/issues/411).
**Reference:** [`docs/testing/orchestration-invariant-catalog.md`](../testing/orchestration-invariant-catalog.md)

---

## Scope

Catalog's Phase 1 subset is 12 invariants. AC of #411 requires three running continuously on staging:

- **S-01** — Slot↔row bijection
- **E-02** — No phantom reversal
- **L-03** — Delete cascades

This doc covers the infrastructure to support those three plus the fleet they observe. The remaining 9 ship as follow-up PRs against the same snapshot collector — each is ~50 LOC.

## Fleet

Pre-seeded set of agents the canary observes. All run Claude Code (no architectural bypass exists); minimize cost via trivial prompts (e.g. `"reply ok"`).

**Strawman** — template choices and counts not in catalog, open for review:

| Agent | Schedules |
|---|---|
| `canary-1` | every 2min |
| `canary-2` | every 5min |
| `canary-3` | every 10min |
| `canary-rotate-{ts}` | created+deleted hourly by canary skill |

The rotation slot exists so L-03 fires on real deletes, not just vacuous orphan scans. Rotation is the only active behavior in Phase 1; everything else is observation.

Naming follows catalog §Open Questions: `canary-*` prefix, dedicated synthetic operator user.

## Snapshot collector

Single async function gathering four sources roughly simultaneously:

| Source | Access |
|---|---|
| SQLite | Backend API (per catalog rec for Phase 1 — enforces real code path) |
| Redis | `ZRANGE`/`ZCARD`/`TTL`/`KEYS agent:slot:*` via MCP tool |
| Docker | Container labels via backend API |
| Agent registries | Parallel `GET /api/executions/running` via `asyncio.gather` |

Returns a typed `Snapshot` dataclass. Reusable from unit tests. Phase 2's scenario runner uses the same primitive.

Lives in `src/canary/snapshot.py`.

## Invariant library

Three pure functions: `check(snapshot) → list[ViolationReport]`. Each ~30-50 LOC.

```
src/canary/invariants/
  s01_slot_row_bijection.py
  e02_no_phantom_reversal.py
  l03_delete_cascades.py
```

E-02 is about transitions, not state — implementation reads `update_execution_status` log lines from Vector since the last snapshot and asserts no terminal→non-terminal transitions.

## Database migration

```sql
CREATE TABLE canary_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    severity TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    observed_state TEXT NOT NULL,      -- JSON
    signal_query TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_canary_violations_invariant ON canary_violations(invariant_id, snapshot_time DESC);
CREATE INDEX idx_canary_violations_severity ON canary_violations(severity, snapshot_time DESC);
```

Versioned migration in `src/backend/db/migrations.py`. Read endpoint: `GET /api/canary/violations` (admin-only).

## Canary agent template

New template at `config/agent-templates/canary-invariant/`:

- `template.yaml` — deletion-protected, owned by synthetic operator user
- `CLAUDE.md` — operating instructions
- `dashboard.yaml` — green/red widget per invariant + 24h violation trend
- Scheduled skill `/check-invariants` every 5 min

Deletion-protected mirrors the `trinity-system` pattern.

## Alerts

Three layers:

1. **Persistent** — every violation written to `canary_violations`. Source of truth for trend queries.
2. **Dashboard** — green/red per invariant + 24h sparklines via the agent's `dashboard.yaml`.
3. **Push** — alert *only* on state transitions (green→red) and severity thresholds. Never on every check; otherwise operators learn to ignore them.

Channel for push alerts: **TBD** (Slack / Telegram / email).

## Rollout

1. Migration + read endpoint
2. Snapshot collector
3. S-01, E-02, L-03 invariants + tests
4. Canary agent template + scheduled skill
5. Fleet seed script
6. Push alert wiring (once channel decided)
7. Deploy to staging; observe for 30 days

Each step is a separate PR.

## Acceptance criteria mapping

- ✅ Catalog reviewed → S-03 and E-05 added to Phase 1 subset (same PR)
- ✅ Phase 1 design doc reviewed → this doc
- 🔲 `canary_violations` table + snapshot-collector → steps 1-2
- 🔲 First 3 invariants running on staging → steps 3-5
- 🔲 One real violation caught and alerted (or 30 days clean) → step 7

## Open questions

1. **Staging deploy access** — does it exist; how to provision canary + fleet
2. **Alert channel** — Slack / Telegram / email
3. **Fleet templates** — strawman above; better minimal options?
4. **Snapshot retention** — keep raw snapshots for forensic replay or just violations
