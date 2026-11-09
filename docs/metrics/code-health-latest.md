## Code Health Report — 2026-11-09 (7b0e29ed4)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 27645 | ↑ (was 22968) |
| Files > 800 lines (critical size) | 92 | → (was 89) |
| Stale TODOs | 10 | → (was 10) |
| High fan-out files (>20 imports) | 20 | ↑ (was 17) |
| Circular imports | 0 | → (was 0) |

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/client_portal/service.py | 97 | 285 | 27645 | 6681 |
| 2 | src/backend/services/system_service.py | 28 | 676 | 18928 | 4932 |
| 3 | src/backend/services/task_execution_service.py | 56 | 268 | 15008 | 3774 |

**Interpretation**: `client_portal/service.py` remains #1 and its score rose ~20% this run —
churn jumped from 87→97 commits in the 90-day window while complexity ticked up (264→285).
`services/system_service.py` is a new entrant to the top 3, displacing
`chat_execution_service.py` (now #4 at 12240, up from 10840): it carries by far the highest
per-file complexity measured this run (676, driven by six grade-D/E/F functions —
`deploy_manifest` F(83), `read_apply_snapshot` F(83), `export_manifest` F(68),
`resolve_apply_plan` F(59) — against comparatively modest churn (28). It did not appear in
the prior baseline's hotspot list at all, which is itself worth a look next run (possible
gap in the prior radon pass rather than a genuine one-week complexity jump, since the file's
git history goes back well past 90 days). `task_execution_service.py` holds steady at #3
(was #2) as churn and complexity both held roughly flat.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| src/backend/client_portal/service.py | 6681 | critical (>800) | Extract service layer (per client_portal area owner) |
| src/backend/models.py | 6612 | critical (>800) | Audit for split-by-domain; centralization invariant (#654) still applies to scope only |
| src/backend/db/migrations.py | 6498 | critical (>800) | Expected growth pattern (append-only versioned migrations); not a refactor candidate |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| src/backend/main.py | 147 | High fan-out — expected for the app-wiring entrypoint (72+ routers) |
| src/backend/database.py | 72 | High fan-out — consider splitting responsibilities |
| src/backend/services/agent_service/crud.py | 43 | High fan-out — also a top-5 hotspot; prime split candidate |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 10 (→ unchanged from last run)
- Top smell-dense files: `services/skill_gate_service.py` (2 — one is a false-positive match
  on a `\uXXXX` comment, not an actual marker), `services/monitoring_service.py` (2),
  `routers/ops.py` (2)

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** — extract the asks/approval sub-flow (already partially
   split into `client_portal/asks/service.py`, itself a top-9 hotspot at score 5952) into its
   own service module; estimated 1500–2000 line reduction. Supports Invariant #1
   (Router → Service → DB, one file per concern) and Invariant #2 (mixin/package
   composition once a service outgrows one file).
2. **`services/system_service.py`** — the manifest apply/deploy path (`deploy_manifest`,
   `read_apply_snapshot`, `export_manifest`, `resolve_apply_plan`) accounts for most of the
   676 CC score in four functions; splitting manifest I/O (parse/validate/export) from the
   apply-plan execution engine into sibling modules (mirroring the `agent_service/` package
   split, Invariant #2) would cut per-function complexity without touching the deploy
   contract documented in `architecture/agent-lifecycle.md`.
3. **`services/task_execution_service.py`** — the CAS-gated terminal-write paths documented
   in `architecture/execution.md` are dense branching logic; splitting status-transition
   helpers into a sibling module (mirroring the `agent_service/` package split, Invariant #2)
   would lower both churn-surface and cyclomatic load without touching the CAS invariant
   itself.
