# Code Health Report — 2026-08-18 (`5517b7a3`)

> Dashboard committed by `/code-health` (dashboard-only since the 2026-08-18 rework — no
> GitHub issues; `/groom`'s Debt Health section converts findings). Previous baseline:
> 2026-05-08 — a 3-month gap (the dev agent's 2026-07-15 recreate wiped the schedule;
> see abilityai/trinity#1184).

## Executive Summary

| Metric | Value | Trend vs 2026-05-08 |
|--------|-------|-------|
| Top hotspot score | 4743 (`crud.py`) | ↓ (was 6105 `chat.py`) |
| Files > 800 lines | 50 | ↑ (was 14) |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 9 | ↑ (was 4) |
| Circular imports | 0 | → |

## Top 3 Hotspots (churn 90d × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/services/agent_service/crud.py` | 31 | 153 | 4743 | 3206 |
| 2 | `src/backend/services/task_execution_service.py` | 19 | 214 | 4066 | 2233 |
| 3 | `src/backend/services/agent_service/lifecycle.py` | 28 | 139 | 3892 | 1702 |

**Interpretation**: the #1028 decomposition targets remain the right ones — `crud.py`,
`task_execution_service.py` and `lifecycle.py` are all frequently changed AND cognitively
expensive. The former top hotspot (`routers/chat.py`, 6105) fell off the top-3 after the
#1483 chat split — evidence the loop pays.

## Top 3 Size Violations

| File | Lines | Threshold |
|------|-------|-----------|
| `src/backend/models.py` | 3855 | critical (>800) |
| `src/backend/db/migrations.py` | 3559 | critical (>800) |
| `src/backend/routers/settings.py` | 3269 | critical (>800) |

## Coupling

Top fan-out: `main.py` (119 — router mounting, structurally expected), `database.py` (51 —
facade, #1482 tracks narrowing), `agent_service/crud.py` (36). 9 files above 20.
No circular import pairs.

## Stale Smell Inventory

6 TODO/FIXME/HACK/XXX markers in `src/backend/` (flat vs 2026-05-08).

## Notes for /groom Debt Health

- Hotspot coverage candidates: top-3 all fall under open issue #1028 (split oversized
  backend routers & services) — verify it names `lifecycle.py`, else widen or file.
- Size violations went 14 → 50 since May; the >800 class is growing and #1028/#1482
  are the standing paydown vehicles.
