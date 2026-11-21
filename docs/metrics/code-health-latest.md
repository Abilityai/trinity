# Code Health Report — 2026-11-16 (aebf54286)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 24,621 | → |
| Files > 300 lines | 297 | → |
| Stale TODOs | 10 | → |
| High fan-out files (>20 imports) | 21 | → |
| Circular imports | 0 | → |

All five headline metrics are stable vs the 2026-11-11 baseline — no regression and no
meaningful improvement this week. `high_coupling_count` moved 20 → 21 (one file crossed
the >20-import line) but stays inside the 10% noise band.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 87 | 283 | 24,621 | 6,762 |
| 2 | `src/backend/services/system_service.py` | 28 | 693 | 19,404 | 5,161 |
| 3 | `src/backend/services/task_execution_service.py` | 58 | 271 | 15,718 | 3,835 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most. All three held the same top-3
ranks last run; `system_service.py`'s complexity score rose 664 → 693 (churn flat at 28),
the largest single-file complexity increase this period, while `client_portal/service.py`'s
churn eased 96 → 87 as older commits fell out of the 90-day window.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6,762 | critical (>800) | Extract service layer (already the #1 hotspot — same file, two independent signals) |
| `src/backend/models.py` | 6,654 | critical (>800) | Split per Invariant #14's router-model exception map; centralized file has grown past single-file-reviewable size |
| `src/backend/db/migrations.py` | 6,540 | critical (>800) | Long-lived append-only migration log (Invariant #3) — size growth here is expected, not a defect |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 151 | High fan-out — app wiring hub; expected given its role registering ~72 routers |
| `src/backend/database.py` | 72 | High fan-out — facade re-exports many DB operation classes (Invariant #2) |
| `src/backend/services/agent_service/crud.py` | 44 | High fan-out — also the #6 hotspot; a coupling + complexity double-signal worth a closer look |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK/XXX markers**: 10 (unchanged vs baseline)
- Top smell-dense files: `services/monitoring_service.py` (2 real TODOs — error-rate calc,
  notification-backed alerts), `routers/ops.py` (2 — dedicated alerts table), `routers/monitoring.py`
  (1, duplicate of the `monitoring_service.py` alerts TODO)
- 4 of the 10 raw matches are false positives from the `XXXX`/`XXXXXX` literal pattern
  (`mktemp` templates in `skill_gate_service.py` and `agent_service/read_only.py`, the AAuth
  challenge-code format string in `aauth/requirement.py`) — not TODO markers, just substrings

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (6,762 lines, score 24,621) — Still the single largest
   backend file and the top hotspot for at least two consecutive runs. Per Invariant #1
   (layer-per-package), split by sub-domain (asks already has its own `client_portal/asks/`
   package per the Architecture Map) the same way — e.g. extract a `client_portal/projects/`
   or `client_portal/goals/` slice for whichever concern dominates the file, re-exporting the
   public surface from `client_portal/service.py` or an `__init__.py` package the way
   `services/git_service/` and `services/agent_client/` already do. Estimated reduction:
   2,000–3,000 lines moved into focused siblings.

2. **`services/system_service.py`** (5,161 lines, score 19,404, complexity now 693 — up from
   664) — The complexity increase without a churn increase suggests recent changes added
   branching rather than new call sites. Worth a manual read to find the function(s) driving
   the C+ sum before extraction; a mixin-style split (Invariant #2's pattern) likely applies
   if the growth is settings/domain-shaped.

3. **`services/task_execution_service.py`** (3,835 lines, score 15,718) — Owned by
   `architecture/execution.md`; any extraction here must preserve the CAS-then-close
   dispatch-activity invariant that file documents (closing a terminal writer's own activity
   is easy to lose during a refactor — it was the root cause of #1804). Read that area file
   before touching this one, not after.
