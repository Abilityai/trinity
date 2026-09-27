# Code Health Report — 2026-09-21 (`0c470c76`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-09-07 (`c9074cd9`).

**Data-integrity note:** the local checkout this scan started from was 156 commits
behind `origin/dev` (last synced around `c9074cd9`, missing two weeks of history). In
that gap, three local-only dashboard commits (dated 2026-09-08, 2026-09-14, 2026-09-21)
had been produced against the increasingly stale history and never reached `origin` —
`dev` is a GitHub-protected branch, so a plain `git commit` there cannot be pushed
directly, and those runs stopped short of the branch+PR step (see
`chore/code-health-dashboard-2026-09-07`, #2560, for the last run that did land). A
stray uncommitted `workspace_delivery` schedule feature (abilityai/trinity-enterprise#498,
unrelated to this scan, not yet upstream) plus a platform-managed `CLAUDE.md` skills
section were stashed rather than discarded, a fresh branch was cut from the
`origin/dev` tip, and the full scan was re-run from scratch before writing anything —
the same recovery the 2026-09-07 report used. The numbers below are a real 14-day
comparison against the actual `origin/dev` tree; the three orphaned local-only commits
were left untouched on the stale local `dev` branch (out of scope for this run).

## Executive Summary

| Metric | Value | Trend vs 2026-09-07 |
|--------|-------|-------|
| Top hotspot score | 9464 (`client_portal/service.py`) | ↑ |
| Files > 800 lines | 64 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 12 | → |
| Circular imports | 0 | → |

The headline move is the hotspot ranking flipping at #1: `client_portal/service.py`
overtook `agent_service/crud.py`, which had held #1 for at least two consecutive runs,
with a 72% higher score (9464 vs 5508 two weeks ago) — churn nearly doubled (37→56
touches in the trailing 90-day window) against a smaller complexity increase
(139→169). Every other tracked metric held inside the ±10% noise band.
`routers/settings.py` — the #3 hotspot two weeks ago at 4805, and flagged in the last
two reports as a split candidate — dropped out of the ranking entirely: it was split
into a `routers/settings/` package (11 files, largest 901 lines) sometime in the last
two weeks, exactly the refactor suggested; no successor file scores high enough
individually to re-enter the top ranks.

## Top 5 Hotspots (churn 90d × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 56 | 169 | 9464 | 5622 |
| 2 | `src/backend/services/agent_service/crud.py` | 38 | 154 | 5852 | 3277 |
| 3 | `src/backend/services/task_execution_service.py` | 33 | 154 | 5082 | 3043 |
| 4 | `src/backend/services/cleanup_service.py` | 25 | 183 | 4575 | 3008 |
| 5 | `src/backend/services/agent_service/lifecycle.py` | 30 | 137 | 4110 | 1713 |

**Interpretation**: `client_portal/service.py` is now both the largest file in
`src/backend/` (5622 lines) and the top hotspot by a wide margin, continuing the trend
the 2026-09-07 report already called out as "the most actionable finding" — it has
grown another ~1700 lines and gained 19 more touches in two weeks and has still not
been evaluated for a package-style split. `agent_service/crud.py` and
`task_execution_service.py` hold materially unchanged churn/complexity from last run
(rank #1→#2 and #5→#3 respectively, reordered only because of `service.py`'s jump and
`settings.py`'s exit). `cleanup_service.py` and `agent_service/lifecycle.py` round out
the top 5, both essentially flat vs. 2026-09-07.

## Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 5622 | critical (>800) | Extract service layer — also the #1 hotspot this run, the single highest-ROI split candidate; `client_portal/` already has `router.py`/`db.py`/`models.py`/`schema.py`/`agent_page.py` siblings, so split by portal-session vs. portal-agent-roster concerns (Invariant #1, Three-Layer pattern, more strictly at the service tier) |
| `src/backend/models.py` | 4559 | critical (>800) | Centralized Pydantic models (Invariant #14) — size is the accepted cost of "one place for the API contract," not a split candidate |
| `src/backend/db/migrations.py` | 4429 | critical (>800) | Append-only versioned migration log by design (Invariant #3) — grows monotonically; expected growth, not a refactor candidate |

## Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 126 | Router-mounting hub (Invariant #4) — mounts ~72 routers plus lifespan phase helpers; expected for its role, not a split candidate |
| `src/backend/database.py` | 55 | Facade importing its composed domain-operation mixins (Invariant #1/#2) — expected shape, not a coupling defect |
| `src/backend/services/agent_service/crud.py` | 37 | Also the #2 hotspot — this one *is* a genuine coupling concern layered on top of size and complexity |

## Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (unchanged since at least 2026-09-07 — same six lines, same files)
- All six are informational stubs, not urgent debt:
  - `db/migrations.py:1087` — Slack thread cleanup job (references an external design doc)
  - `services/monitoring_service.py:510` — error-rate calculation not yet implemented
  - `services/monitoring_service.py:813` + `routers/monitoring.py:349` — `recent_alerts` placeholder (paired stub, same shape, two files)
  - `routers/ops.py:373` + `routers/ops.py:396` — dedicated alerts table not yet built (paired stub in one file)
- These have now been stable across at least three consecutive weekly runs with no
  movement — the underlying alerts-table gap (2 markers in `ops.py`, 1 in
  `monitoring.py`, 1 in `monitoring_service.py`) is worth a `/groom` look as a real
  ticket rather than four scattered TODOs.

## Suggested Refactorings (Top 3 Hotspots)

**1. `client_portal/service.py` (9464, ↑ from rank #2 → #1)**
No architectural note yet marks this file as an accepted-monolith-by-design the way
`models.py`/`migrations.py` are. `client_portal/` already has `router.py`/`db.py`/
`models.py`/`schema.py`/`agent_page.py` siblings; `service.py` growing to 5622 lines
while backing the entire Workspace surface (rosters, sessions, chat dispatch,
briefings, rooms glue, deliverables) is the same pattern flagged last run, now more
pronounced. A natural split mirrors sub-feature boundaries: session/thread management,
roster + briefing resolution, and ratings/deliverables could become sibling modules
under a `client_portal/service/` package, following either the `db/schedules/`
mixin-package precedent or the `services/agent_service/` sub-package precedent.
Estimated reduction: 1500-2000 lines. Supports Invariant #1/#2.

**2. `services/agent_service/crud.py` (5852, ↓ from rank #1 → #2, score essentially flat)**
Per `architecture.md`, `create_agent_internal` is already a thin orchestrator over
private `_*` phase-helpers (#1484); the deferred #1028 follow-up — extracting the
phase helpers into `creation_phases.py` — remains unactioned. Estimated reduction:
1500-2000 lines with the orchestrator itself staying in `crud.py`. Supports
Invariant #1/#2.

**3. `services/task_execution_service.py` (5082, ↑ from rank #5 → #3, unchanged
churn/complexity — reordering only)**
Per the `execution.md` architecture invariant, every terminal side effect here is
gated on winning a status CAS, and each CAS winner must close its own dispatch
activity — the exact discipline behind the #1804/#767/#45 recurring bug class. That
invariant is easiest to keep correct when the CAS-winning branches are small and
few. Consider extracting the dispatch-activity close/duration-recording logic into a
focused helper module, leaving this file as orchestration only.

---
*Methodology: hotspot score = git churn (90d) × cyclomatic complexity, where complexity
sums per-function scores from `radon cc -n C` (grade C/complexity≥6 and worse only),
matching the 2026-09-07 baseline. Size-violation and coupling counts are full
`src/backend/` scans (>800 lines / >20 imports), not top-N windows. The baseline's
`hotspot_scores` map is truncated to the top 30 files by score (472 files scored > 0
overall this run) — treat entries outside that set as unranked, not zero.*
