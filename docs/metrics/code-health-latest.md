## Code Health Report — 2026-11-03 (93658be60)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 22968 | ↑ (was 19513) |
| Files > 800 lines (critical size) | 89 | → (was 86) |
| Stale TODOs | 10 | ↑ (was 6) |
| High fan-out files (>20 imports) | 17 | → (was 16) |
| Circular imports | 0 | → (was 0) |

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/client_portal/service.py | 87 | 264 | 22968 | 6468 |
| 2 | src/backend/services/task_execution_service.py | 53 | 266 | 14098 | 3748 |
| 3 | src/backend/services/chat_execution_service.py | 40 | 271 | 10840 | 3549 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
`client_portal/service.py` stayed the #1 hotspot and its score rose ~18% this run — churn
jumped from 79→87 commits in the 90-day window while complexity held roughly flat (247→264).
Refactoring it reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| src/backend/client_portal/service.py | 6468 | critical (>800) | Extract service layer (per client_portal area owner) |
| src/backend/models.py | 6242 | critical (>800) | Audit for split-by-domain; centralization invariant (#654) still applies to scope only |
| src/backend/db/migrations.py | 6197 | critical (>800) | Expected growth pattern (append-only versioned migrations); not a refactor candidate |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| src/backend/main.py | 145 | High fan-out — expected for the app-wiring entrypoint (72 routers) |
| src/backend/database.py | 69 | High fan-out — consider splitting responsibilities |
| src/backend/services/chat_execution_service.py | 39 | High fan-out — also a top-3 hotspot; prime split candidate |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 10 (↑ from 6 last run)
- Top smell-dense files: `services/skill_gate_service.py` (2), `services/monitoring_service.py` (2), `routers/ops.py` (2)

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** — extract the asks/approval sub-flow (already partially
   split into `client_portal/asks/service.py`, itself a top-7 hotspot at score 4564) into its
   own service module; estimated 1500–2000 line reduction. Supports Invariant #1
   (Router → Service → DB, one file per concern) and Invariant #2 (mixin/package
   composition once a service outgrows one file).
2. **`services/task_execution_service.py`** — the CAS-gated terminal-write paths documented
   in `architecture/execution.md` are dense branching logic; splitting status-transition
   helpers into a sibling module (mirroring the `agent_service/` package split) would lower
   both churn-surface and cyclomatic load without touching the CAS invariant itself.
3. **`services/chat_execution_service.py`** — highest per-line complexity density of the
   top 3 (271 CC over 3549 lines); a mixin-style split along session-turn vs dispatch
   concerns (same pattern as `db/schedules/__init__.py`, Invariant #2) is the lowest-risk cut.

*(Findings are dashboard input only — conversion into `type-refactor` issues is a human call
in `/groom`'s Debt Health section, per the Dashboard-not-registry policy.)*
