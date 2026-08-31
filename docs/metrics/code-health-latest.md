# Code Health Report — 2026-08-31 (`135248e9`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-08-18 (`5517b7a3`).

**Data-integrity note:** the local checkout this scan started from had silently
diverged from `origin/dev` by 137 commits (68 touching `src/backend/`) — the two prior
"weekly" dashboard commits (`aa1f7df2`/`08-24`, and an in-progress `08-31` pass) were
both computed against that stale fork and never reached `origin/dev`. Per the standing
lesson from that gap (recorded before this run), this run reset the local branch to
`origin/dev` tip and re-ran the full scan from scratch before writing anything. The two
stale local-only commits were discarded (dashboard-only content, fully superseded by
this corrected pass); no other repo state was touched. **This is the first report in
several weeks measuring the actual `origin/dev` tree**, so the trend column below is a
real 13-day comparison, not noise from a frozen fork.

## Executive Summary

| Metric | Value | Trend vs 2026-08-18 |
|--------|-------|-------|
| Top hotspot score | 5508 (`crud.py`) | ↑ |
| Files > 800 lines | 56 | ↑ |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 10 | ↑ |
| Circular imports | 0 | → |

## Top 5 Hotspots (churn 90d × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/services/agent_service/crud.py` | 36 | 153 | 5508 | 3211 |
| 2 | `src/backend/services/task_execution_service.py` | 22 | 214 | 4708 | 2246 |
| 3 | `src/backend/client_portal/service.py` | 34 | 126 | 4284 | 3460 |
| 4 | `src/backend/routers/settings.py` | 27 | 154 | 4158 | 3468 |
| 5 | `src/backend/services/agent_service/lifecycle.py` | 29 | 139 | 4031 | 1702 |

**Interpretation**: `crud.py`, `task_execution_service.py`, and `lifecycle.py` — the
standing #1028 decomposition targets — all rose in both churn and score over the 13
days this comparison actually spans, consistent with continued heavy feature work
(#1081 pull-mode, #1083 fire-and-forget, ephemeral agents, Brain Orb) landing through
these three files.

**New entrant: `client_portal/service.py` (rank 3, was outside the top-3 on 08-18).**
This is the most actionable finding this run — the Workspace/client-portal surface
(epic ent#78) has been under sustained, rapid development (ent#356 OSS-core move,
ent#357 platform-session entry, ent#358 session absorption, ent#392 composer
typeahead, #2128/#2196/#2198/#2258 roster+availability+batch-sessions work, all dated
within the last month per `architecture.md`), and the file has grown to 3460 lines
(now itself a top-5 size violation) while carrying real complexity (CC 126) and the
highest raw churn of the top 5 (34 commits/90d). It has not yet been evaluated for
#1028-style decomposition. Recommend `/groom` add it to the Debt Health coverage check
alongside `crud.py`/`task_execution_service.py`/`lifecycle.py`.

## Top 5 Size Violations

| File | Lines | Threshold | Suggested Action |
|------|-------|-----------|-------------------|
| `src/backend/models.py` | 4053 | critical (>800) | Centralized Pydantic models (Invariant #14) — size is the accepted cost of "one place for the API contract," not a split candidate |
| `src/backend/db/migrations.py` | 3971 | critical (>800) | Append-only versioned migration log by design (Invariant #3) — grows monotonically; expected growth, not a refactor candidate |
| `src/backend/routers/settings.py` | 3468 | critical (>800) | Also hotspot #4 (CC 154, churn 27) — many settings sub-resources (template-registry, retention, brain-orb, elevenlabs, a2a-endpoints, skills-library, room budgets). Candidate for a `routers/settings/` package split, mirroring the `db/schedules/` mixin-package precedent (Invariant #2) |
| `src/backend/client_portal/service.py` | 3460 | critical (>800) | Also hotspot #3 (new entrant, see above) — highest-churn file in the fleet; grew from outside the top-5 size list. Worth a fresh look for a router/service split before it compounds further |
| `src/backend/database.py` | 3435 | critical (>800) | Facade over 27+ domain operation classes (Invariant #2) — high size is the documented shape of the pattern, not a refactor candidate |

56 files exceed the 800-line critical threshold (↑ from 50 on 08-18 — +6 over 13 days).
Two of the top five (`models.py`, `db/migrations.py`) and `database.py` are
architecturally-sanctioned by design (Invariants #14, #3, #2 respectively).
`routers/settings.py` and the new `client_portal/service.py` are the standing
actionable candidates.

## Top 3 Coupling Issues

| File | Import Count | Issue |
|------|---------------|-------|
| `src/backend/main.py` | 122 | Router mounting — structurally expected per Invariant #4, not a split candidate |
| `src/backend/database.py` | 52 | Facade over 27+ domain operation classes (Invariant #2) — high fan-out is the documented shape of the pattern |
| `src/backend/services/agent_service/crud.py` | 36 | Same file as hotspot #1 — genuine fan-out across docker/template/git/credentials/capacity/breaker services, worth revisiting alongside any `crud.py` split |

10 files above 20 imports (↑ from 9 on 08-18 — `routers/public.py` newly crossed the
threshold at 21). No circular import pairs detected.

## Stale Smell Inventory

- **Total TODO/FIXME/HACK/XXX markers**: 6 (→ flat vs 2026-08-18)
- Smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `routers/monitoring.py` (1), `db/migrations.py` (1)

Five of the six markers describe one coherent, still-unaddressed gap — a not-yet-built
alerts/notifications table (`routers/ops.py:988`, `:1011`) and its would-be consumers
(`services/monitoring_service.py:510`, `:813`, `routers/monitoring.py:349`) — rather
than five independent smells. Flat across every run this signal has been tracked.

## Notes for /groom Debt Health

- Hotspot coverage candidates: `crud.py`/`task_execution_service.py`/`lifecycle.py`
  remain under open issue #1028 (split oversized backend routers & services).
  **`client_portal/service.py` is a new top-5 hotspot and top-5 size violation with no
  tracked decomposition issue** — recommend filing or folding into #1028's scope.
- Size violations grew 50 → 56 and high-fan-out grew 9 → 10 over the real 13-day
  window; both are worth a glance but neither crossed into critical-growth territory
  on its own.
- **Process note, not a code finding:** confirm this run's dashboard commit actually
  reaches `origin/dev` (`git log --oneline origin/dev -1` should show today's commit)
  before relying on next week's trend column — the exact failure mode that produced
  the stale-fork gap this run had to correct.
