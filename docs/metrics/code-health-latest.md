# Code Health Report — 2026-10-26 (`bbd0d4095`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-10-19 (`93b57e43d`).

## Executive Summary

| Metric | Value | Trend vs 2026-10-19 |
|--------|-------|-------|
| Top hotspot score | 19513 (`client_portal/service.py`) | → |
| Files > 800 lines | 86 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 16 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a sixth consecutive run (18772 → 19513, +3.9%, inside
the 10% noise band) as churn ticked up 76 → 79 with complexity flat at 247. The reshuffle this
run is in the #2/#3 slots again: `services/task_execution_service.py` moved from #3 to #2
(11086 → 11638, +5.1%) on a complexity bump (241 → 253, churn flat at 46), while
`services/agent_service/crud.py` — #2 last run — dropped to #3 (11492 → 10387, -9.6%) as its
complexity cooled slightly (221 unchanged, churn rose 52 → 47 — net decrease driven by the
churn-window dropping older commits out of the 90-day lookback). `services/operator_queue_service.py`
held #4 (9835 → 9554, -2.9%) and keeps the single highest per-file CC score in the tree (281,
unchanged). `services/chat_execution_service.py` stayed at #5 but eased back (8296 → 7808,
-5.9%). All four macro metrics (size violations, stale TODOs, fan-out, circular imports) moved
by at most one unit and stay inside the noise band — this run's signal, like last run's, is
entirely in the top-hotspot ordering, not broad-based drift.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 79 | 247 | 19513 | 6309 |
| 2 | `src/backend/services/task_execution_service.py` | 46 | 253 | 11638 | 3636 |
| 3 | `src/backend/services/agent_service/crud.py` | 47 | 221 | 10387 | 3872 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6309 | critical (>800) | Extract per-capability service modules (mirrors the `services/agent_service/` package split, Invariant #1) |
| `src/backend/db/migrations.py` | 5865 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |
| `src/backend/models.py` | 5854 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 143 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 68 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 39 | High fan-out — also this run's #3 hotspot; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (a raw grep for the literal substring `XXX` returns
  10 hits — excluded as false positives: `\uXXXX` inside a unicode-escape comment in
  `services/skill_gate_service.py:72`, two `mktemp "...XXXXXX"` placeholder patterns
  (`services/skill_gate_service.py:1261`, `services/agent_service/read_only.py:71`), and
  the AAuth challenge-code example `code="XXXX-XXXX"` in `services/aauth/requirement.py:13`)
- Top smell-dense files: `services/monitoring_service.py` (2 — stubbed alert fetches,
  `:510` error-rate calc, `:813` recent-alerts), `routers/ops.py` (2 — alerts table not yet
  implemented, `:373`/`:396`), `db/migrations.py` (1 — thread-cleanup job), `routers/monitoring.py`
  (1 — stubbed recent-alerts). Unchanged from the prior run; all six markers point at the same
  not-yet-built alerts/notifications surface across `monitoring.py`/`monitoring_service.py`/`ops.py`.

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py` (6309 lines, CC 247, churn 79)** — Extract per-capability
   service modules (asks, delegation, billing-view, roster) the same way
   `services/agent_service/` was split into `crud.py`/`lifecycle.py`/`deploy.py`. This is the
   #1 hotspot for the sixth straight run with no sign of slowing growth; it supports
   Invariant #1 (layer-outgrows-one-file → package, never a second module beside it).
   Estimated reduction: 6309 lines → a ~500–800 line facade + 4–6 focused modules.

2. **`services/task_execution_service.py` (3636 lines, CC 253, churn 46)** — This run's #2;
   `execution.md`'s own invariant note (every terminal side effect gated on the status CAS)
   is exactly the kind of cross-cutting concern that keeps complexity high across many
   branches. Candidate split: separate the CAS-gated terminal-write paths from dispatch-admission
   orchestration into a sibling module, following the `services/agent_client/` (`circuit`/`http_pool`/`client`)
   precedent for Invariant #1. Estimated reduction: ~3600 lines → ~2 modules of ~1800 each.

3. **`services/agent_service/crud.py` (3872 lines, CC 221, churn 47)** — Already a package
   member per Invariant #1, but at 3872 lines and 39 imports it is the largest single file in
   that package. Candidate split: separate read-path (list/get) from write-path
   (create/update/delete) handlers into `crud_read.py`/`crud_write.py` within the existing
   `agent_service/` package, preserving the public re-export surface per Invariant #1(a).
