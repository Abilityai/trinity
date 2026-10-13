# Code Health Report — 2026-10-12 (`84ad179fd`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-10-05 (`482ad0742`).

## Executive Summary

| Metric | Value | Trend vs 2026-10-05 |
|--------|-------|-------|
| Top hotspot score | 17228 (`client_portal/service.py`) | ↑ |
| Files > 800 lines | 83 | ↑ |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 15 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a fourth consecutive run and widened its lead
again: hotspot score jumped 13390 → 17228 (+29%) as both churn (65 → 73) and complexity
(206 → 236) kept rising together — this is now four straight weeks of the same file
compounding, not a one-off spike. The bigger reshuffle is behind it: `operator_queue_service.py`
jumped from #4 (6160 last baseline) to #2 this run (10360, +68%) on complexity alone
(216 → 280 CC — the single highest CC score in the tree now, overtaking `cleanup_service.py`'s
long-held top spot) while its churn stayed flat (37). `agent_service/crud.py` held #2
last week and is effectively tied for #2/#3 this run (10123 vs 10360 — a 2% gap, within
noise) as its churn kept climbing (52 → 53). `task_execution_service.py` held its #3→#4
slot (7371 → 10038, +36%) on both churn (39 → 42) and complexity (189 → 239) rising
together — the same "active feature work accreting onto an already-large file" pattern
flagged last run has not let up. Files > 800 lines ticked up from 75 → 83 (+11%, just
over the noise band) with no single dominant driver; fan-out, stale TODOs, and circular
imports are all flat.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 73 | 236 | 17228 | 6191 |
| 2 | `src/backend/services/operator_queue_service.py` | 37 | 280 | 10360 | 2822 |
| 3 | `src/backend/services/agent_service/crud.py` | 53 | 191 | 10123 | 3592 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most. Note `task_execution_service.py`
(churn 42, CC 239, score 10038, 3549 lines) sits immediately behind #3 — effectively a
four-way cluster at the top rather than a clean top 3 this run.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6191 | critical (>800) | Extract per-capability service modules (mirrors `services/agent_service/` package split) |
| `src/backend/db/migrations.py` | 5711 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |
| `src/backend/models.py` | 5523 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 139 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 66 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 38 | High fan-out — also the #3 hotspot this run; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (a raw grep for the literal substring `XXX` also
  matches `\uXXXX` inside a unicode-escape comment in `services/skill_gate_service.py:72`
  and two `mktemp "...XXXXXX"` template strings in `skill_gate_service.py:1036` and
  `agent_service/read_only.py:70` — all three excluded here as false positives, not markers)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `db/migrations.py` (1), `routers/monitoring.py` (1), `agent_service/read_only.py` (1,
  false positive) — identical real marker set to the prior run, no new debt added or removed

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (6191 lines, churn 73, CC 236) — fourth consecutive
   run at #1, and the gap keeps widening (+29% hotspot score this week alone, +72% over
   two weeks). It already has sibling `client_portal/router.py` and `client_portal/db.py`,
   so the service layer itself has outgrown a single module. Extract by portal capability
   (delegation, roster, feature-flag resolution) into a `client_portal/service/` package
   following the router/service-package precedent in Invariant #1 (`routers/settings/`,
   `services/git_service/`), keeping `client_portal/service.py` as a re-export facade.
   This is the fourth straight report naming this exact split — it is the highest-ROI
   open item on this dashboard.

2. **`services/operator_queue_service.py`** (2822 lines, churn 37, CC 280 — now the
   single highest complexity score in `src/backend/`, overtaking `cleanup_service.py`)
   — complexity grew (216 → 280, +30%) with flat churn, meaning the file is getting
   harder to reason about without a proportional increase in change frequency; that
   combination (complexity outpacing churn) is a sign the file is accreting branching
   logic rather than straightforward feature additions. Candidate split: separate the
   flood-guard / escalation logic from CRUD operations on queue items, following the
   same service/db split pattern already used elsewhere (Invariant #1).

3. **`services/agent_service/crud.py`** (3592 lines, churn 53 — highest churn of the
   top 3 this run, up from 52 — also the #3 coupling offender at 38 imports) — already
   inside the `agent_service/` package (Invariant #1 precedent), so the split is one
   level deeper: separate the `_create_agent_container` hard-coded network wiring
   (architecture.md Network Topology) from general CRUD, since that function is
   independently cited as a stable reference point elsewhere in the docs and churns for
   unrelated reasons. This is the second straight report where both churn and complexity
   grew together here — worth splitting before the next feature lands on top of it.
