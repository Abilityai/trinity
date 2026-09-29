# Code Health Report — 2026-09-29 (`4114d3d4`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-09-21 (`0c470c76`).

## Executive Summary

| Metric | Value | Trend vs 2026-09-21 |
|--------|-------|-------|
| Top hotspot score | 9918 (`client_portal/service.py`) | → |
| Files > 800 lines | 71 | ↑ |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 14 | ↑ |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a second consecutive run (9464 → 9918, +4.8%,
inside the noise band) — churn held flat at 58 touches while complexity ticked up
171 (was 169). The rest of the ranking is stable: `cleanup_service.py` (#2, 6214) and
`agent_service/crud.py` (#3, 6160) both held their positions from the prior run.
Files > 800 lines rose 64 → 71 (+11%) and high fan-out files rose 12 → 14 (+17%) —
both outside the ±10% noise band, consistent with eight days of active feature landing
(skill sets, telegram group context, workspace suggestions, chain-depth limiting, and
the ask-endings operator-queue work all merged in this window per recent commit
history) rather than a single runaway file.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 58 | 171 | 9918 | 5663 |
| 2 | `src/backend/services/cleanup_service.py` | 26 | 239 | 6214 | 3459 |
| 3 | `src/backend/services/agent_service/crud.py` | 40 | 154 | 6160 | 3389 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 5663 | critical (>800) | Extract per-capability service modules (mirrors `services/agent_service/` package split) |
| `src/backend/models.py` | 5018 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |
| `src/backend/db/migrations.py` | 5014 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 130 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 62 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 38 | High fan-out — also a top-3 hotspot; coupling adds to the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2), `routers/monitoring.py` (1)

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (5663 lines, churn 58, CC 171) — the file already has a
   sibling `client_portal/router.py` and `client_portal/db.py`; the service layer itself has
   outgrown a single module. Extract by portal capability (delegation, roster, feature-flag
   resolution) into a `client_portal/service/` package following the router/service-package
   precedent in Invariant #1 (`routers/settings/`, `services/git_service/`), keeping
   `client_portal/service.py` as a re-export facade. Estimated reduction: 5663 → ~800 lines
   in the facade, remainder split across 4-5 focused modules.

2. **`services/cleanup_service.py`** (3459 lines, churn 26, CC 239 — highest complexity of
   any file in the tree) — the complexity concentration (239 across only 3459 lines, denser
   than either other top-3 hotspot) suggests deeply nested conditional logic rather than
   sheer size. A targeted complexity pass (extract guard-clause helpers, flatten nested
   branches) before any size-driven split would pay off faster than restructuring — this is
   the file `reliability.md` names for retention-guard interaction (Invariant safety-floor
   note), so any extraction must keep `OPS_SETTINGS_DEFAULTS` read-at-prune-time semantics
   intact.

3. **`services/agent_service/crud.py`** (3389 lines, churn 40 — highest churn of the top 3,
   also the #3 coupling offender at 38 imports) — already inside the `agent_service/`
   package (Invariant #1 precedent), so the split is one level deeper: separate the
   `_create_agent_container` hard-coded network wiring (architecture.md Network Topology)
   from general CRUD, since that function is independently cited as a stable reference point
   elsewhere in the docs and churns for unrelated reasons.
