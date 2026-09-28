# Code Health Report — 2026-09-28 (`ebc2bfb4`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-09-21 (`0c470c76`).

## Executive Summary

| Metric | Value | Trend vs 2026-09-21 |
|--------|-------|-------|
| Top hotspot score | 11585 (`task_execution_service.py`) | ↑ |
| Files > 800 lines | 70 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 14 | ↑ |
| Circular imports | 0 | → |

The headline move is the hotspot ranking flipping at #1 again: `task_execution_service.py`
overtook `client_portal/service.py`, which had held #1 for at least two consecutive runs,
with a 22% higher score (11585 vs 9464 last week) — churn held flat (33→35 touches) but
complexity more than doubled (154→331), driven almost entirely by one landed feature:
`f6dfb000` ("ask endings — one sink, endings ledger, person-only endings, wake on any
ending, self-readback") touched both `task_execution_service.py` and
`operator_queue_service.py` in the same PR. `operator_queue_service.py` shows the same
signature even more sharply — complexity roughly quintupled off a low prior base while
churn stayed modest (22 touches) — and vaults straight to #3 this run; it did not
appear in last week's top-30 hotspot map at all. `client_portal/service.py` itself is
not flat either: churn rose again (56→58) and complexity ticked up (169→171), but it was
simply outpaced. High fan-out crossed the 12→14 files threshold (↑) on the back of the
same feature landing new cross-cutting imports in `operator_queue_service.py`,
`agent_service/lifecycle.py`, and `a2a.py`. Files > 800 lines rose 64→70, just inside
the ±10% noise band (70.4 would have tripped it). Stale TODOs and circular imports are
unchanged.

## Top 5 Hotspots (churn 90d × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/services/task_execution_service.py` | 35 | 331 | 11585 | 3062 |
| 2 | `src/backend/client_portal/service.py` | 58 | 171 | 9918 | 5663 |
| 3 | `src/backend/services/operator_queue_service.py` | 22 | 427 | 9394 | 1992 |
| 4 | `src/backend/services/cleanup_service.py` | 26 | 239 | 6214 | 3459 |
| 5 | `src/backend/services/agent_service/crud.py` | 40 | 154 | 6160 | 3389 |

**Interpretation**: The top 3 hotspots this run share one root cause — the `ask endings`
feature (abilityai/trinity-enterprise#611, PR A of 2) landed complex new logic across the
operator-queue / execution seam in a single week. `task_execution_service.py` is now both
the top hotspot and squarely inside the CAS-discipline invariant called out in
`architecture.md` (every terminal side effect must win the status CAS and close its own
dispatch activity — the #1804/#767/#45 recurring bug class); adding complexity here is the
highest-risk place in the codebase for that class to recur. `operator_queue_service.py`
entering the top 3 from outside the top-30 in one run is the most actionable signal this
week: complexity moved from a low base to 427 (the single highest CC score of any file
scanned, ahead of even `client_portal/service.py`) while the file itself is comparatively
small (1992 lines) — density, not sprawl, is the concern. `cleanup_service.py` and
`agent_service/crud.py` are essentially unchanged from last week (both flagged in prior
reports; `crud.py`'s deferred #1028 phase-helper extraction remains unactioned), reordered
only by the two risers above them.

## Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 5663 | critical (>800) | Extract service layer — flagged in the last two reports as the top split candidate; `client_portal/` already has `router.py`/`db.py`/`models.py`/`schema.py`/`agent_page.py` siblings, so split by portal-session vs. portal-agent-roster concerns (Invariant #1, Three-Layer pattern, more strictly at the service tier). Still not evaluated. |
| `src/backend/db/migrations.py` | 4980 | critical (>800) | Append-only versioned migration log by design (Invariant #3) — grows monotonically; expected growth, not a refactor candidate |
| `src/backend/models.py` | 4969 | critical (>800) | Centralized Pydantic models (Invariant #14) — size is the accepted cost of "one place for the API contract," not a split candidate |

## Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 130 | Router-mounting hub (Invariant #4) — mounts routers plus lifespan phase helpers; expected for its role, not a split candidate |
| `src/backend/database.py` | 61 | Facade importing its composed domain-operation mixins (Invariant #1/#2) — expected shape, not a coupling defect |
| `src/backend/services/agent_service/crud.py` | 38 | Also the #5 hotspot — this one *is* a genuine coupling concern layered on top of size and complexity |

## Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (unchanged since at least 2026-09-07 — same six lines, same files, now a fourth consecutive stable reading)
- All six are informational stubs, not urgent debt:
  - `db/migrations.py:1087` — Slack thread cleanup job (references an external design doc)
  - `services/monitoring_service.py:510` — error-rate calculation not yet implemented
  - `services/monitoring_service.py:813` + `routers/monitoring.py:377` — `recent_alerts` placeholder (paired stub, same shape, two files)
  - `routers/ops.py:373` + `routers/ops.py:396` — dedicated alerts table not yet built (paired stub in one file)
- Stable across at least four consecutive weekly runs with no movement — the underlying
  alerts-table gap (2 markers in `ops.py`, 1 in `monitoring.py`, 1 in
  `monitoring_service.py`) remains worth a `/groom` look as a real ticket rather than
  four scattered TODOs.

## Suggested Refactorings (Top 3 Hotspots)

**1. `services/task_execution_service.py` (11585, ↑ from rank #3 → #1)**
Per the `execution.md` architecture invariant, every terminal side effect here is gated
on winning a status CAS, and each CAS winner must close its own dispatch activity — the
exact discipline behind the #1804/#767/#45 recurring bug class. The newly-added
`apply_result` method alone carries a complexity of 66 (radon), the single most complex
function in the file. Consider extracting the dispatch-activity close/duration-recording
logic and the endings-ledger handling introduced by #3023 into a focused helper module,
leaving `apply_result`/`execute_task` as thin orchestration. Estimated reduction:
800-1200 lines. Supports Invariant #1 (Execution area).

**2. `client_portal/service.py` (9918, ↓ from rank #1 → #2, score essentially flat)**
Unchanged assessment from the last two reports: no architectural note yet marks this file
as an accepted-monolith-by-design the way `models.py`/`migrations.py` are. `client_portal/`
already has `router.py`/`db.py`/`models.py`/`schema.py`/`agent_page.py` siblings; a natural
split mirrors sub-feature boundaries (session/thread management, roster + briefing
resolution, ratings/deliverables) under a `client_portal/service/` package, following
either the `db/schedules/` mixin-package precedent or the `services/agent_service/`
sub-package precedent. Estimated reduction: 1500-2000 lines. Supports Invariant #1/#2.
This is the third consecutive report naming this file as the top or near-top actionable
finding with no split evaluated yet.

**3. `services/operator_queue_service.py` (9394, NEW to top 3 — was outside last week's
top-30 entirely)**
The #611 "ask endings" feature added one sink, an endings ledger, person-only endings,
wake-on-any-ending, and self-readback in a single PR against a file that was previously
mid-table. At 427 CC-score across only 1992 lines, this is now the densest file scanned —
a strong signal that the new logic paths (ledger writes, wake fan-out, readback) are not
yet decomposed into helpers the way the rest of the operator-queue seam is. Given
`operator-queue.md`'s ownership of `db/operator_queue.py` alongside this service, a
first pass should extract the endings-ledger read/write path into its own module before
the next feature lands on top of it. Estimated reduction: 500-800 lines. Supports
Invariant #1.

---
*Methodology: hotspot score = git churn (90d) × cyclomatic complexity, where complexity
sums per-function scores from `radon cc -n C` (grade C/complexity≥6 and worse only),
matching the 2026-09-07 baseline. Size-violation and coupling counts are full
`src/backend/` scans (>800 lines / >20 imports), not top-N windows. The baseline's
`hotspot_scores` map is truncated to the top 30 files by score (489 files scored > 0
overall this run) — treat entries outside that set as unranked, not zero.*
