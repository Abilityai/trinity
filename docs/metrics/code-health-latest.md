# Code Health Report — 2026-10-05 (`482ad0742`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-09-29 (`4114d3d4`).

## Executive Summary

| Metric | Value | Trend vs 2026-09-29 |
|--------|-------|-------|
| Top hotspot score | 13390 (`client_portal/service.py`) | ↑ |
| Files > 800 lines | 75 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 15 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a third consecutive run and widened its lead:
hotspot score jumped 9918 → 13390 (+35%) as both churn (58 → 65) and complexity
(171 → 206) rose together — this is the file to prioritize next, not just repeat the
same note. The bigger shift is in the #2/#3 slots: `services/agent_service/crud.py`
overtook `services/cleanup_service.py` for #2 (6160 → 9464, churn 40 → 52, complexity
154 → 182 — both the biggest churn and biggest complexity jump of any top-5 file this
run), and `services/task_execution_service.py` entered the top 3 for the first time
(5425 → 7371, previously ranked #5). `cleanup_service.py` itself did not regress — its
churn (26) and complexity (239, still the single highest CC score in the tree) are
byte-identical to the prior run — it simply fell to #4 as its neighbors grew faster.
The four summary counts (size violations, stale TODOs, fan-out, circular imports) all
land inside the ±10% noise band or are flat, consistent with a quieter week on broad
surface area even as churn and complexity concentrated hard on three files.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 65 | 206 | 13390 | 5877 |
| 2 | `src/backend/services/agent_service/crud.py` | 52 | 182 | 9464 | 3563 |
| 3 | `src/backend/services/task_execution_service.py` | 39 | 189 | 7371 | 3392 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 5877 | critical (>800) | Extract per-capability service modules (mirrors `services/agent_service/` package split) |
| `src/backend/db/migrations.py` | 5291 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |
| `src/backend/models.py` | 5183 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 135 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 64 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 38 | High fan-out — also the #2 hotspot this run; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (a raw grep for the literal substring `XXX` also
  matches `\uXXXX` inside a unicode-escape comment in `services/skill_gate_service.py:64` —
  excluded here as a false positive, not a marker)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `db/migrations.py` (1), `routers/monitoring.py` (1) — identical marker set to the
  prior run, no new debt added or removed

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (5877 lines, churn 65, CC 206) — the gap between this
   file and everything else keeps widening (+35% hotspot score in one week); it already has
   sibling `client_portal/router.py` and `client_portal/db.py`, so the service layer itself
   has outgrown a single module. Extract by portal capability (delegation, roster,
   feature-flag resolution) into a `client_portal/service/` package following the
   router/service-package precedent in Invariant #1 (`routers/settings/`,
   `services/git_service/`), keeping `client_portal/service.py` as a re-export facade.
   Estimated reduction: 5877 → ~800 lines in the facade, remainder split across 4-5
   focused modules.

2. **`services/agent_service/crud.py`** (3563 lines, churn 52 — highest churn of the
   top 3 this run, up from 40 — also the #3 coupling offender at 38 imports) — already
   inside the `agent_service/` package (Invariant #1 precedent), so the split is one
   level deeper: separate the `_create_agent_container` hard-coded network wiring
   (architecture.md Network Topology) from general CRUD, since that function is
   independently cited as a stable reference point elsewhere in the docs and churns for
   unrelated reasons. Both churn and complexity grew together here, which usually means
   active feature work is accreting onto an already-large file rather than one isolated
   change — worth splitting before the next feature lands on top of it.

3. **`services/task_execution_service.py`** (3392 lines, churn 39, CC 189) — new to the
   top 3 this run (was #5 at 5425, now #3 at 7371). `execution.md` already names this file
   as the one where every terminal side effect must be gated on winning the status CAS
   with the dispatch-activity close paired to the CAS winner (#1804 class) — that coupling
   between correctness-critical control flow and ordinary feature churn is exactly the
   kind of file where a size-driven split needs to preserve invariant behavior, not just
   move lines. A complexity-reduction pass (extract the CAS-win/dispatch-close pairing into
   a narrow helper used by every terminal writer) would reduce both the line count and the
   chance of a fourth occurrence of the #1804 bug class, without touching unrelated code.
