## Code Health Report — 2026-09-08 (c9074cd9)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 5508 | → |
| Files > 300 lines | 200 | ↑ (methodology note below) |
| Stale TODOs | 6 | → |
| High fan-out files (>20 imports) | 11 | → |
| Circular imports | 0 | → |

**Methodology note**: the previous baseline's "size violations" count (56) was computed from
the top-40-by-line-count window (per the skill's `head -40` step), and that window is now
fully saturated — all 40 files in it exceed 800 lines (the top-40 range runs 975–4189 lines).
That made the top-40-derived count meaningless as a trend signal, so this run did a full
`src/backend/` scan instead: **200 files** exceed 300 lines. Treat the ↑ as a methodology
correction surfacing the true count, not a claim that 144 files crossed the threshold in 8
days. Future runs should keep using the full-scan count for comparability.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | src/backend/services/agent_service/crud.py | 36 | 153 | 5508 | 3211 |
| 2 | src/backend/client_portal/service.py | 37 | 139 | 5143 | 3916 |
| 3 | src/backend/routers/settings.py | 31 | 155 | 4805 | 3597 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
`agent_service/crud.py` holds the top spot unchanged from the previous run (identical
churn/complexity — no commits landed in the 90-day churn window since 2026-08-31).
`client_portal/service.py` moved up from #3 to #2 (churn 34→37, complexity 126→139) and
`routers/settings.py` newly entered the top 3, displacing `task_execution_service.py`
(now #5 at 4321, down from #2 at 4708 as its measured complexity dropped 214→149 under
radon's real AST-based scoring — the previous run may have used the keyword-count fallback
proxy for this file).

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

- **Total TODO/FIXME/HACK markers**: 6 (unchanged from baseline)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `routers/monitoring.py` (1) — three of the six markers concern the same missing alerts
  table (`ops.py:989`, `ops.py:1012`, echoed in `monitoring_service.py:813` /
  `routers/monitoring.py:349`), suggesting one coherent piece of deferred work rather than
  four independent TODOs.

### Suggested Refactorings (Top 3 Hotspots)

1. **`services/agent_service/crud.py`** (5508) — Per Invariant #2, agent-specific settings
   already split out via mixins under `db/agent_settings/`; this file is the service-layer
   counterpart and hasn't received the same treatment. Extract create/update/delete-specific
   concerns (e.g. provisioning validation, credential wiring) into sibling modules under
   `services/agent_service/` alongside the existing `lifecycle.py`/`deploy.py`/`helpers.py`
   split — same package, same import surface, smaller files. Estimated reduction: 800–1200
   lines if provisioning-validation and credential-wiring concerns move out.

2. **`client_portal/service.py`** (5143, up from #3) — Largest single-file mover this run.
   `client_portal/` already has `router.py`/`db.py`/`models.py`/`schema.py`/`agent_page.py`
   siblings; `service.py` at 3916 lines is disproportionate. Split by portal-session vs.
   portal-agent-roster concerns, following the Three-Layer pattern (Invariant #1) more
   strictly at the service tier.

3. **`routers/settings.py`** (4805, new to top 3) — At 3597 lines this is the largest router
   in the codebase. Given the `secret_settings.py` sink-guard precedent (Invariant #12,
   ent#435), settings already has a natural fault line between credential-shaped keys and
   general config — extracting a `routers/settings_secrets.py` (or moving more logic into
   `services/secret_settings.py`) would shrink the router and keep the security-sensitive
   path smaller and easier to audit in isolation.
