## Code Health Report — 2026-11-23 (a40b0670a)

### Executive Summary

| Metric | Value | Trend |
|--------|-------|-------|
| Top hotspot score | 24682 | → |
| Files > 300 lines | 301 | → |
| Stale TODOs | 10 | → |
| High fan-out files (>20 imports) | 23 | → |
| Circular imports | 0 | → |

Previous run: 2026-11-16 @ `aebf54286`. No metric moved by more than the 10% trend threshold
this week — the backend's debt profile is stable, not improving or regressing.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 86 | 287 | 24682 | 6827 |
| 2 | `src/backend/services/system_service.py` | 28 | 693 | 19404 | 5161 |
| 3 | `src/backend/services/task_execution_service.py` | 58 | 271 | 15718 | 3840 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most. All three held the same top-3
ranking in the 2026-11-16 baseline — no new hotspot has displaced them, and no existing one
has been paid down.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6827 | critical (>800) | Split into a `client_portal/service/` package by sub-domain (also the #1 hotspot — see below) |
| `src/backend/models.py` | 6666 | critical (>800) | Out of scope for splitting — Invariant #14 centralizes all router request/response models here by design; size reflects API surface breadth, not a layering defect |
| `src/backend/db/migrations.py` | 6540 | critical (>800) | Out of scope for splitting — Invariant #3 requires every SQLite schema change to append a new versioned entry here; length is append-only history, not a structural smell |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 151 | Expected for the FastAPI app entrypoint (72 router registrations, Invariant #4 ordering) — not a split candidate |
| `src/backend/database.py` | 72 | Facade over every `db/X.py` module by design (Invariant #1/#2) — fan-out reflects the number of domains, not entanglement |
| `src/backend/services/agent_service/crud.py` | 45 | Already split from the `agent_service/` package (Invariant #1a); watch if fan-out keeps climbing — a further split (e.g. isolating container-create wiring) may be warranted if it crosses ~55 |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 10 (unchanged from 2026-11-16)
- Top smell-dense files: `src/backend/services/skill_gate_service.py` (2 — both `mktemp` template literals containing `XXXXXX`, not real debt markers), `src/backend/services/monitoring_service.py` (2 — genuine: unimplemented error-rate calc, stubbed `recent_alerts`), `src/backend/routers/ops.py` (2 — genuine: missing alerts table)
- Genuine outstanding debt markers: `monitoring_service.py:555`, `monitoring_service.py:858`, `routers/monitoring.py:509`, `routers/ops.py:393`, `routers/ops.py:416`, `db/migrations.py:1088` (thread-cleanup job) — 6 real markers; the other 4 matches are `XXXXXX`/`XXXX-XXXX` placeholder literals in `mktemp` templates and an example header, not debt

### Suggested Refactorings (Top 3 Hotspots)

**1. `client_portal/service.py` (6827 lines, hotspot score 24682, highest churn at 86 commits/90d)**
Extract into a `client_portal/service/` package following the Invariant #1a pattern already
used for `routers/settings/` and `services/git_service/` — one module per portal sub-domain
(e.g. messaging, task delegation, asset access), composing back onto the current public
import path so `from client_portal.service import ...` call sites are untouched. This file
owns the workspace area (`docs/memory/architecture/workspace.md`); its "roster payload is
the only capability channel" invariant and the per-agent-field asymmetry warning must survive
the split unchanged — read that file before touching this one.

**2. `services/system_service.py` (5161 lines, hotspot score 19404, by far the highest raw
complexity at 693 — 2.4x the next-densest file)**
Complexity this concentrated in one module, with comparatively low churn (28), suggests a
god-object accreting unrelated system-level concerns rather than one actively-iterated
feature. A one-time audit to identify 2–3 cohesive sub-responsibilities and extract them as
sibling services (not a package, since churn doesn't yet justify the Invariant #1a
ceremony) would cut the complexity score disproportionately to the effort.

**3. `services/task_execution_service.py` (3840 lines, hotspot score 15718, churn 58)**
Owned by `docs/memory/architecture/execution.md` — this is the file where "every terminal
side effect is gated on winning the status CAS" and where a terminal writer that skips
closing its dispatch activity has caused the #1804/#767/#45 class of 120-minute-fabricated-
duration bugs three times. High churn + high complexity here is the costliest combination to
touch carelessly. Before any extraction, read the execution.md invariant in full; a safe
first step is isolating the CAS-win/dispatch-close bookkeeping into a small helper so every
call site is structurally forced through the same close path, rather than restructuring the
service's broader responsibilities.
