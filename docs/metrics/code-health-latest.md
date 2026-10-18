# Code Health Report — 2026-10-13 (`3f60683a0`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-10-05 (`482ad0742`).

## Executive Summary

| Metric | Value | Trend vs 2026-10-05 |
|--------|-------|-------|
| Top hotspot score | 17464 (`client_portal/service.py`) | ↑ |
| Files > 800 lines | 83 | ↑ |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 15 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a fourth consecutive run and widened its lead
again: hotspot score jumped 13390 → 17464 (+30%) as both churn (65 → 74) and complexity
(206 → 236) rose together — same trajectory as last run, still the clearest refactor
priority in the tree. The bigger shift is in the #2/#3 slots: `services/operator_queue_service.py`
jumped into #2 (6160 two runs ago → 10678 now, churn 38, complexity 281 — the single
highest CC score of any file measured this run), and `services/task_execution_service.py`
held the #3 slot it entered last run (7371 → 10277, churn 39 → 43, complexity 189 → 239).
`services/agent_service/crud.py`, #2 last run at 9464, fell to #4 (9932) — its own numbers
grew too (churn 52, complexity 191), it was simply overtaken by faster-growing neighbors.
Files > 800 lines rose 75 → 83 (+10.7%, just over the noise band) — broad-based growth
rather than concentrated in the top hotspots, consistent with ordinary feature churn
across many files this cycle. Stale TODOs, fan-out, and circular imports are all flat.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 74 | 236 | 17464 | 6210 |
| 2 | `src/backend/services/operator_queue_service.py` | 38 | 281 | 10678 | 2847 |
| 3 | `src/backend/services/task_execution_service.py` | 43 | 239 | 10277 | 3550 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6210 | critical (>800) | Extract per-capability service modules (mirrors `services/agent_service/` package split) |
| `src/backend/db/migrations.py` | 5711 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |
| `src/backend/models.py` | 5523 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 139 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 66 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 38 | High fan-out — also a top-5 hotspot this run; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (raw grep for the literal substring `XXX` returns
  9 hits — excluded as false positives: `\uXXXX` inside a unicode-escape comment in
  `services/skill_gate_service.py:72`, and two `mktemp "...XXXXXX"` placeholder patterns
  in `services/skill_gate_service.py:1052` and `services/agent_service/read_only.py:70`,
  the latter newly introduced since the last run by #3273 — none are actual markers)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `db/migrations.py` (1), `routers/monitoring.py` (1) — identical marker set to the
  prior run, no new debt added or removed

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (6210 lines, churn 74, CC 236) — fourth consecutive
   run at #1, and the gap keeps widening (+30% hotspot score this week alone, +80% over
   the last two runs combined). It already has sibling `client_portal/router.py` and
   `client_portal/db.py`, so the service layer itself has outgrown a single module.
   Extract by portal capability (delegation, roster, feature-flag resolution) into a
   `client_portal/service/` package following the router/service-package precedent in
   Invariant #1 (`routers/settings/`, `services/git_service/`), keeping
   `client_portal/service.py` as a re-export facade. Estimated reduction: 6210 → ~800
   lines in the facade, remainder split across 4-5 focused modules.

2. **`services/operator_queue_service.py`** (2847 lines, churn 38, CC 281 — now the
   single highest complexity score of any file in the tree, overtaking
   `cleanup_service.py`) — new to the top 3 this run (was outside it entirely two runs
   ago). The operator-queue surface (`ask_operator`/`respond_to_operator_queue` plumbing)
   has been growing steadily; a size-driven split should separate request lifecycle
   (create/expire/supersede) from response/notification fan-out, mirroring the
   `services/schedules/` mixin-composition precedent (Invariant #2) rather than a package
   split, since the domain is cohesive but the file has outgrown one module.

3. **`services/task_execution_service.py`** (3550 lines, churn 43, CC 239) — held the
   #3 slot for a second consecutive run, growing further (7371 → 10277). `execution.md`
   already names this file as the one where every terminal side effect must be gated on
   winning the status CAS with the dispatch-activity close paired to the CAS winner
   (#1804 class) — that coupling between correctness-critical control flow and ordinary
   feature churn is exactly the kind of file where a size-driven split needs to preserve
   invariant behavior, not just move lines. A complexity-reduction pass (extract the
   CAS-win/dispatch-close pairing into a narrow helper used by every terminal writer)
   would reduce both the line count and the chance of a fourth occurrence of the #1804
   bug class, without touching unrelated code.
