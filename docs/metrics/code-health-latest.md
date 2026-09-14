## Code Health Report — 2026-09-14 (3cb8ea6b)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 5355 | → |
| Files > 300 lines | 200 | → |
| Stale TODOs | 6 | → |
| High fan-out files (>20 imports) | 11 | → |
| Circular imports | 0 | → |

A quiet week — six days since the last run, no metric moved more than the ±10% trend
threshold, and the smell/coupling/circular-import counts are all byte-identical to baseline.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/services/agent_service/crud.py | 35 | 153 | 5355 | 3211 |
| 2 | src/backend/client_portal/service.py | 37 | 139 | 5143 | 3916 |
| 3 | src/backend/routers/settings.py | 30 | 155 | 4650 | 3597 |

**Interpretation**: Same top 3, same order as the 2026-09-08 baseline. `agent_service/crud.py`
holds #1 with complexity unchanged (153) and churn down slightly (36→35 — one fewer commit
aged out of the 90-day window). `client_portal/service.py` (#2) and `routers/settings.py`
(#3, churn 31→30) are similarly stable. No new entrant displaced the top 3 this run —
`services/cleanup_service.py` (4575) and `services/task_execution_service.py` (4172) remain
#4 and #5, the same as baseline.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| src/backend/models.py | 4189 | critical (>800) | Split Pydantic models by router domain (Invariant #14 scope is router models — this is the one legitimate central home, but 4189 lines argues for sub-modules, e.g. `models/agents.py`, `models/schedules.py`, re-exported from `models.py`) |
| src/backend/db/migrations.py | 4141 | critical (>800) | Expected to grow (append-only migration log per Invariant #9) — not a refactor target; flagged for visibility only |
| src/backend/client_portal/service.py | 3916 | critical (>800) | Extract service layer — also the #2 hotspot; highest-ROI split candidate this run |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| src/backend/main.py | 125 | High fan-out — expected for the FastAPI app entrypoint wiring ~72 routers (Invariant #4); not a split candidate |
| src/backend/database.py | 53 | High fan-out — central DB facade; consider whether all imports are still needed after mixin composition (Invariant #2) |
| src/backend/services/agent_service/crud.py | 36 | High fan-out — consistent with its #1 hotspot ranking; a coupling+complexity double-hit |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (unchanged from baseline — identical file set)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `routers/monitoring.py` (1) — three of the six markers still concern the same missing
  alerts table (`ops.py:989`, `ops.py:1012`, echoed in `monitoring_service.py:813` /
  `routers/monitoring.py:349`) plus `db/migrations.py:1087` (Slack thread cleanup job) and
  `monitoring_service.py:510` (error-rate calculation). No new markers introduced this week.

### Suggested Refactorings (Top 3 Hotspots)

1. **`services/agent_service/crud.py`** (5355) — Per Invariant #2, agent-specific settings
   already split out via mixins under `db/agent_settings/`; this file is the service-layer
   counterpart and hasn't received the same treatment. Extract create/update/delete-specific
   concerns (e.g. provisioning validation, credential wiring) into sibling modules under
   `services/agent_service/` alongside the existing `lifecycle.py`/`deploy.py`/`helpers.py`
   split — same package, same import surface, smaller files. Estimated reduction: 800–1200
   lines if provisioning-validation and credential-wiring concerns move out.

2. **`client_portal/service.py`** (5143) — `client_portal/` already has
   `router.py`/`db.py`/`models.py`/`schema.py`/`agent_page.py` siblings; `service.py` at
   3916 lines is disproportionate. Split by portal-session vs. portal-agent-roster concerns,
   following the Three-Layer pattern (Invariant #1) more strictly at the service tier.

3. **`routers/settings.py`** (4650) — At 3597 lines this is the largest router in the
   codebase. Given the `secret_settings.py` sink-guard precedent (Invariant #12), consider
   whether settings-domain sub-routers (e.g. credential settings, feature flags, retention)
   can be split out behind the existing `/api/settings` prefix, mirroring the
   `services/settings_service.py` boundary already used on the service side.

---
*Methodology: hotspot score = git churn (90d) × cyclomatic complexity, where complexity sums
per-function scores from `radon cc -n C` (grade C/complexity≥6 and worse only), matching the
2026-09-08 baseline. Size-violation and coupling counts are full `src/backend/` scans, not
top-N windows.*
