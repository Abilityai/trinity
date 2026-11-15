## Code Health Report — 2026-11-11 (b99c23f6e)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 27072 | ↑ (was 22968) |
| Files > 300 lines | 293 | ↑ (was 89) |
| Stale TODOs | 10 | → |
| High fan-out files (>20 imports) | 20 | ↑ (was 17) |
| Circular imports | 0 | → |

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/client_portal/service.py | 96 | 282 | 27072 | 6689 |
| 2 | src/backend/services/system_service.py | 28 | 664 | 18592 | 4971 |
| 3 | src/backend/services/task_execution_service.py | 57 | 271 | 15447 | 3787 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most. `system_service.py` is new
to the top 3 this run (previously #3 was `chat_execution_service.py` at 10840; it now
ranks #4 at 12240). `system_service.py` carries by far the highest single-file complexity
score (664) in the backend, with much lower churn (28) than the other two — a dense,
rarely-touched file is a different risk profile than a frequently-touched one and may
warrant a dedicated read before refactoring.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| src/backend/client_portal/service.py | 6689 | critical (>800) | Extract service layer |
| src/backend/models.py | 6612 | critical (>800) | Split by domain (Invariant #14 scope is routers; this is the Pydantic model file itself growing unreviewable) |
| src/backend/db/migrations.py | 6520 | critical (>800) | Expected growth — append-only versioned migration log (Invariant #3); not a refactor target |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| src/backend/main.py | 147 | High fan-out — expected for the app entrypoint wiring every router |
| src/backend/database.py | 72 | High fan-out — the DB facade importing every `XOperations` mixin class |
| src/backend/services/agent_service/crud.py | 43 | High fan-out — consider narrowing dependencies |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 10
- Top smell-dense files: `services/skill_gate_service.py` (2), `services/monitoring_service.py` (2), `routers/ops.py` (2)

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (score 27072, churn 96, cc 282, 6689 lines) — highest
   churn AND highest complexity in the backend; top hotspot for three consecutive runs.
   Candidate for splitting by sub-domain (it already has sibling modules under
   `client_portal/asks/`) into smaller service files behind the existing router/service/db
   layering (Invariant #1).

2. **`services/system_service.py`** (score 18592, churn 28, cc 664, 4971 lines) — lower
   churn than the other two but by far the densest single file measured (cc 664 vs. next
   highest 282). A complexity-first read (not churn-first) — extract cohesive sub-flows
   into smaller functions/services before this file's edit frequency rises to match its
   size.

3. **`services/task_execution_service.py`** (score 15447, churn 57, cc 271, 3787 lines) —
   this is the file `architecture/execution.md` already flags for the CAS/dispatch-closing
   invariant; any extraction here must preserve that every terminal writer still closes its
   own dispatch activity (#1804).

### Methodology Notes

- `radon` was not on `PATH` in this run's worktree (installed under
  `~/.local/lib/python3.13/site-packages` via `--user`); invoked as `python3 -m radon`
  instead of the bare `radon` command — same tool, no fallback-proxy used.
- `size_violations_count` jumped from 89 → 293 between this run and the 2026-11-03
  baseline. Both runs count the same way (files >300 lines, same exclusions), so this
  reflects either a prior under-count or real backend growth during an unusually high
  commit-velocity window (1505 commits / 90d measured on `dev` this cycle) — flagged for
  a human read rather than resolved here.
