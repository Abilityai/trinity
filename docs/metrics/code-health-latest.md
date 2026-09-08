# Code Health Report — 2026-09-07 (`c9074cd9`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-08-31 (`135248e9`).

**Data-integrity note:** the local checkout this scan started from was 15 commits
behind `origin/dev` (last synced at `b4112055`, missing everything through `c9074cd9`
— including the ent#438/#474/#475/#525/#451 Workspace consolidation and the #2526
pull-mode loop work). A stray uncommitted `workspace_delivery` migration/feature
(unrelated to this scan, not yet upstream) was stashed rather than discarded, the
branch was fast-forwarded to `origin/dev` tip, and the full scan was re-run from
scratch before writing anything — the same recovery the 2026-08-31 report used. The
numbers below are a real 7-day comparison against the actual `origin/dev` tree.

## Executive Summary

| Metric | Value | Trend vs 2026-08-31 |
|--------|-------|-------|
| Top hotspot score | 5508 (`crud.py`) | → |
| Files > 800 lines | 63 | ↑ |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 11 | → |
| Circular imports | 0 | → |

**The flat top-line hides real movement underneath.** `crud.py` held the #1 spot at an
*identical* score (5508 — same churn, same complexity, untouched this week), but the
#2 through #4 hotspots reshuffled hard: `client_portal/service.py` jumped from rank 3
to rank 2 (4284 → 5143, +20%), `routers/settings.py` jumped from rank 4 to rank 3
(4158 → 4805, +16%), and `cleanup_service.py` jumped from rank 6 to rank 4 (3456 →
4758, +38%, driven almost entirely by a complexity spike: 138 → 183). Meanwhile
`task_execution_service.py` fell from rank 2 to rank 5 (4708 → 4321) as its churn
cooled. None of these individually cross the aggregate trend threshold, but three of
the top five hotspots moved by double digits in one direction.

## Top 5 Hotspots (churn 90d × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/services/agent_service/crud.py` | 36 | 153 | 5508 | 3211 |
| 2 | `src/backend/client_portal/service.py` | 37 | 139 | 5143 | 3916 |
| 3 | `src/backend/routers/settings.py` | 31 | 155 | 4805 | 3597 |
| 4 | `src/backend/services/cleanup_service.py` | 26 | 183 | 4758 | 3008 |
| 5 | `src/backend/services/task_execution_service.py` | 29 | 149 | 4321 | 2619 |

**Interpretation**: `crud.py` remains untouched at #1 (still the standing #1028
decomposition target — a phase-helper orchestrator per `architecture.md`, apparently
stable this week). The real story is `client_portal/service.py`, which the 2026-08-31
report flagged as "the most actionable finding" on its debut into the top 3 — one week
later it has grown another 456 lines (3460 → 3916, +13%), gained a commit (34 → 37),
and is now both the #2 hotspot *and* the #3 largest file in the entire backend. The
Workspace/client-portal surface (epic ent#78) shipped five more feature commits this
week alone (ent#438 agent canvas, ent#475 loops/canvas/files rail, ent#525 live
execution card, ent#451 chat tabs — all visible in the churn log), all landing through
this one file. It has still not been evaluated for a package-style split.

`cleanup_service.py`'s jump is worth separate attention: its churn barely moved
(25 → 26 commits) but its complexity rose 32% (138 → 183) — this is a file getting
*harder*, not just *busier*. Given `cleanup_service.py`'s central role (watchdog
reconciliation, retention sweeps, the #1560 name-keyed Redis cleanup, ephemeral-agent
GC, lease-reaper — all in one module per `architecture.md`'s Background Services
table), a complexity spike here is a reliability-relevant signal, not just a size one.

## Top 5 Size Violations

| File | Lines | Threshold | Suggested Action |
|------|-------|-----------|-------------------|
| `src/backend/models.py` | 4177 | critical (>800) | Centralized Pydantic models (Invariant #14) — size is the accepted cost of "one place for the API contract," not a split candidate |
| `src/backend/db/migrations.py` | 4109 | critical (>800) | Append-only versioned migration log by design (Invariant #3) — grows monotonically; expected growth, not a refactor candidate |
| `src/backend/client_portal/service.py` | 3916 | critical (>800) | Also hotspot #2 (CC 139, churn 37) — the strongest concrete refactor candidate this run; see hotspot interpretation above |
| `src/backend/database.py` | 3806 | critical (>800) | Documented facade orchestrating 27 domain operation classes from `db/` (Invariant #1/#2) — expected shape for the pattern, not itself a split candidate; genuine complexity lives in the classes it composes |
| `src/backend/routers/settings.py` | 3597 | critical (>800) | Also hotspot #3 (CC 155, churn 31, +16% this week) — many settings sub-resources (template-registry, retention, brain-orb, elevenlabs, a2a-endpoints, skills-library, room budgets). Candidate for a `routers/settings/` package split, mirroring the `db/schedules/` mixin-package precedent (Invariant #2). Flagged last run too; growth has not slowed |

## Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 125 | Router-mounting hub (Invariant #4) — mounts ~72 routers plus lifespan phase helpers (#1028); expected for its role, not a split candidate |
| `src/backend/database.py` | 53 | Facade importing its 27 composed domain-operation mixins (Invariant #1/#2) — expected shape, not a coupling defect |
| `src/backend/services/agent_service/crud.py` | 36 | Also the #1 hotspot — this one *is* a genuine coupling concern layered on top of size and complexity |

## Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (unchanged since at least 2026-08-31 — same six lines, same files)
- All six are informational stubs, not urgent debt:
  - `db/migrations.py:1087` — Slack thread cleanup job (references an external design doc)
  - `services/monitoring_service.py:510` — error-rate calculation not yet implemented
  - `services/monitoring_service.py:813` + `routers/monitoring.py:349` — `recent_alerts` placeholder (paired stub, same shape, two files)
  - `routers/ops.py:989` + `routers/ops.py:1012` — dedicated alerts table not yet built (paired stub in one file)
- These have now been stable across at least two consecutive weekly runs with no
  movement — worth a `/groom` look at whether the underlying alerts-table gap (ops.py
  ×2, monitoring.py, monitoring_service.py — 4 of the 6 markers trace to the same
  missing "alerts table") is worth a real ticket rather than four scattered TODOs.

## Suggested Refactorings (Top 3 Hotspots)

**1. `services/agent_service/crud.py` (5508, unchanged rank #1)**
Per `architecture.md`, `create_agent_internal` is already a thin orchestrator over
private `_*` phase-helpers (#1484) — the file's size (3211 lines) is the sum of those
helpers living in one module, not one monolithic function. The next incremental step
recorded in the architecture doc itself: "Module split to `creation_phases.py` is the
deferred #1028 follow-up." Extracting the phase helpers (ephemeral pre-gate, template
resolution, config staging, env build, volume mounts, container create, register,
materialize) into `creation_phases.py` would cut this file by an estimated 1500-2000
lines with the orchestrator itself staying in `crud.py`. Supports Invariant #1
(Router → Service → DB, no bloated service files) and #2 (composition over monoliths).

**2. `client_portal/service.py` (5143, ↑ from rank #3 → #2)**
No architectural note yet marks this file as an accepted-monolith-by-design the way
`models.py`/`migrations.py`/`database.py` are. It backs the entire Workspace surface
(rosters, sessions, chat dispatch, briefings, rooms glue, deliverables) which
`architecture.md`'s Workspace section describes as many distinct sub-features
(ent#356/357/358/364/365/366/380/392/440/451/456 etc.) that have each landed as prose
additions to one service file. A natural split mirrors the sub-feature boundaries
already named in the docs: session/thread management, roster + briefing resolution,
and ratings/deliverables could plausibly become sibling modules under a
`client_portal/service/` package (the `db/schedules/` mixin-package precedent, or the
`services/agent_service/` sub-package precedent). Estimated reduction: 1200-1800 lines
if session/roster/briefing logic moves to siblings. Supports Invariant #1/#2.

**3. `routers/settings.py` (4805, ↑ from rank #4 → #3)**
The file mixes many independently-versioned settings sub-resources — template
registry, retention windows, brain-orb flags, ElevenLabs config, A2A endpoints,
skills-library automation, room budget defaults — each already documented as its own
concern in `architecture.md`'s API Endpoints → Platform Settings section. Splitting
into a `routers/settings/` package (one module per sub-resource family, composed via a
shared `APIRouter` the way `db/schedules/__init__.py` composes ten mixins) would let
each concern's tests and changes stay isolated, and is a close structural parallel to
the already-adopted `db/schedules/` split (Invariant #2). Estimated reduction:
1000-1500 lines off the main file once sub-resources move to siblings.
