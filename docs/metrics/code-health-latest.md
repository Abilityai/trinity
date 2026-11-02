# Code Health Report — 2026-11-02 (`c8f67cb85`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-10-26 (`bbd0d4095`).

## Executive Summary

| Metric | Value | Trend vs 2026-10-26 |
|--------|-------|-------|
| Top hotspot score | 22618 (`client_portal/service.py`) | ↑ |
| Files > 800 lines | 89 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 17 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a seventh consecutive run and this time breaks the
10% noise band on both sides (18772 → 19513 → 22618, +15.9% this run) as churn rose 79 → 86
and complexity rose 247 → 263 — the fastest single-run jump this file has shown since
tracking began; it is the clearest standing case for the Invariant #1 package split. The
bigger story this run is **`services/operator_queue_service.py` became a package**
(`refactor(operator-queue): operator_queue_service becomes a package...` #3785 "PR 1 of 4",
#4036, landed the same morning as this scan) — exactly the shape of split this dashboard has
recommended for seven straight runs. The split so far only pulled a 41-line `deps.py` (DI
wiring) out of the file; the bulk (now `operator_queue_service/__init__.py`, 2949 lines)
hasn't shrunk, and this run's churn+complexity both landed on that file (complexity 281 → 299,
the highest single-file CC score in the tree, churn tracked across the rename via
`--follow`: 40 in the last 90 days), pushing its hotspot score from 9554 (#4 last run) to
11960 — now **#3**, displacing `services/agent_service/crud.py` (10387 → 7956, -23.4%, its
complexity held flat at 221 while churn cooled 47 → 36, falling to #5). `services/task_execution_service.py`
holds #2 and also broke its noise band (11638 → 13780, +18.4%, churn 46 → 52 and complexity
253 → 265). `services/chat_execution_service.py` moved up one slot to #4 (7808 → 10530,
+34.9%, churn 39 unchanged, complexity 233 → 270). Three of this run's top four files moved
by more than 10% — the busiest single-run reshuffle since this dashboard started tracking;
read as a leading indicator, not noise, given the operator-queue PR series (1 of 4) explicitly
targets this exact file set next.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 86 | 263 | 22618 | 6453 |
| 2 | `src/backend/services/task_execution_service.py` | 52 | 265 | 13780 | 3688 |
| 3 | `src/backend/services/operator_queue_service/__init__.py` | 40 | 299 | 11960 | 2949 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6453 | critical (>800) | Extract per-capability service modules (mirrors the `services/agent_service/` package split, Invariant #1) |
| `src/backend/models.py` | 6242 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |
| `src/backend/db/migrations.py` | 6197 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 145 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 69 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 39 | High fan-out — also this run's #5 hotspot; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (a raw grep for the literal substring `XXX` returns
  10 hits — excluded as false positives: `\uXXXX` inside a unicode-escape comment in
  `services/skill_gate_service.py:72`, two `mktemp "...XXXXXX"` placeholder patterns
  (`services/skill_gate_service.py:1249`, `services/agent_service/read_only.py:71`), and
  the AAuth challenge-code example `code="XXXX-XXXX"` in `services/aauth/requirement.py:13`)
- Top smell-dense files: `services/monitoring_service.py` (2 — stubbed alert fetches,
  `:510` error-rate calc, `:813` recent-alerts), `routers/ops.py` (2 — alerts table not yet
  implemented, `:373`/`:396`), `db/migrations.py` (1 — thread-cleanup job), `routers/monitoring.py`
  (1 — stubbed recent-alerts). Unchanged from the prior run; all six markers point at the same
  not-yet-built alerts/notifications surface across `monitoring.py`/`monitoring_service.py`/`ops.py`.

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py` (6453 lines, CC 263, churn 86)** — Extract per-capability
   service modules (asks, delegation, billing-view, roster) the same way
   `services/agent_service/` was split into `crud.py`/`lifecycle.py`/`deploy.py`. This is the
   #1 hotspot for the seventh straight run and just posted its largest single-run jump
   (+15.9%); it supports Invariant #1 (layer-outgrows-one-file → package, never a second
   module beside it). Estimated reduction: 6453 lines → a ~500–800 line facade + 4–6 focused
   modules.

2. **`services/task_execution_service.py` (3688 lines, CC 265, churn 52)** — This run's #2,
   up from #2 last run with both metrics rising together; `execution.md`'s own invariant note
   (every terminal side effect gated on the status CAS) is exactly the kind of cross-cutting
   concern that keeps complexity high across many branches. Candidate split: separate the
   CAS-gated terminal-write paths from dispatch-admission orchestration into a sibling module,
   following the `services/agent_client/` (`circuit`/`http_pool`/`client`) precedent for
   Invariant #1. Estimated reduction: ~3700 lines → ~2 modules of ~1850 each.

3. **`services/operator_queue_service/__init__.py` (2949 lines, CC 299, churn 40)** — New to
   the top 3 this run, but not a new file: it's the `operator_queue_service.py` that held #4
   for the last several runs, now mid-split into a package (#3785, "PR 1 of 4", landed the
   same morning as this scan). Only `deps.py` (41 lines, DI wiring) has been extracted so far
   — the bulk is still in one file, and its complexity score (299) is now the single highest
   in the entire tree, surpassing even `client_portal/service.py`'s 263. Candidate split for
   the remaining 3 PRs in the series: break out the alert-budget/platform-alert seam work
   visible in the #4036 commit (`create_bounded_alert*`, `observe`) into its own module, then
   separate CRUD/queue-read paths from the broadcast/notification paths, matching the
   `agent_service/` (`crud.py`/`lifecycle.py`/`deploy.py`) shape. Estimated reduction: ~2950
   lines → ~4-5 modules of ~600-700 each once the series completes. Track via #3785's
   remaining PRs rather than filing a new issue.
