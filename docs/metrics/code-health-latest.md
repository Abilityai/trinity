# Code Health Report — 2026-10-19 (`93b57e43d`)

> Dashboard committed by `/code-health` (dashboard-only — no GitHub issues; `/groom`'s
> Debt Health section converts findings). Previous baseline: 2026-10-13 (`3f60683a0`).

## Executive Summary

| Metric | Value | Trend vs 2026-10-13 |
|--------|-------|-------|
| Top hotspot score | 18772 (`client_portal/service.py`) | → |
| Files > 800 lines | 85 | → |
| Stale TODO/FIXME/HACK | 6 | → |
| High fan-out files (>20 imports) | 15 | → |
| Circular imports | 0 | → |

`client_portal/service.py` holds #1 for a fifth consecutive run, but growth has slowed:
hotspot score rose 17464 → 18772 (+7.5%, inside the 10% noise band, unlike the +30% jump
last run) as churn (74 → 76) and complexity (236 → 247) both ticked up modestly. The real
movement this week is in the #2/#3 slots: `services/agent_service/crud.py` jumped from
#4 to #2 (9932 → 11492, +15.7%) on a complexity spike (191 → 221, churn flat at 52), while
`services/operator_queue_service.py` — #2 last run — dropped to #4 (10678 → 9835, -7.9%)
as its churn cooled (38 → 35) with complexity unchanged (281, still the single highest CC
score in the tree). `services/task_execution_service.py` held #3 for a second consecutive
run (10277 → 11086). `services/chat_execution_service.py` stayed at #5 but kept climbing
(7425 → 8296, +11.7%). Every macro metric — size violations, stale TODOs, fan-out,
circular imports — is flat; this run's signal is entirely in the top-hotspot reshuffle,
not broad-based drift.

### Top 3 Hotspots (churn × complexity — highest refactoring ROI)

| Rank | File | Churn (90d) | CC Score | Hotspot Score | Lines |
|------|------|-------------|----------|---------------|-------|
| 1 | `src/backend/client_portal/service.py` | 76 | 247 | 18772 | 6258 |
| 2 | `src/backend/services/agent_service/crud.py` | 52 | 221 | 11492 | 3872 |
| 3 | `src/backend/services/task_execution_service.py` | 46 | 241 | 11086 | 3609 |

**Interpretation**: These files are both frequently changed and cognitively expensive.
Refactoring them reduces ongoing maintenance cost the most.

### Top 3 Size Violations

| File | Lines | Threshold Exceeded | Suggested Action |
|------|----|------|---------|
| `src/backend/client_portal/service.py` | 6258 | critical (>800) | Extract per-capability service modules (mirrors `services/agent_service/` package split) |
| `src/backend/models.py` | 5781 | critical (>800) | Split by domain (agent/execution/settings request-response groups) per Invariant #14 scope |
| `src/backend/db/migrations.py` | 5742 | critical (>800) | Expected to grow append-only (Invariant #3 migration log) — not a refactor candidate, flagged for awareness only |

### Top 3 Coupling Issues

| File | Import Count | Issue |
|------|-------------|-------|
| `src/backend/main.py` | 142 | High fan-out — router registration surface, expected for the app entrypoint but worth periodic review against Invariant #4 ordering |
| `src/backend/database.py` | 67 | High fan-out — facade aggregating all `XOperations` mixins (Invariant #2); consider whether call sites can import the specific `db/` module instead |
| `src/backend/services/agent_service/crud.py` | 39 | High fan-out — also this run's #2 hotspot; coupling compounds the refactor case below |

### Stale Smell Inventory

- **Total TODO/FIXME/HACK markers**: 6 (a raw grep for the literal substring `XXX` returns
  10 hits — excluded as false positives: `\uXXXX` inside a unicode-escape comment in
  `services/skill_gate_service.py:72`, two `mktemp "...XXXXXX"` placeholder patterns in
  `services/skill_gate_service.py:1216` and `services/agent_service/read_only.py:71`, and
  a new `"XXXX-XXXX"` code-format example in `services/aauth/requirement.py:13` — none are
  actual markers)
- Top smell-dense files: `services/monitoring_service.py` (2), `routers/ops.py` (2),
  `db/migrations.py` (1), `routers/monitoring.py` (1) — identical marker set to the
  prior run, no new debt added or removed

### Suggested Refactorings (Top 3 Hotspots)

1. **`client_portal/service.py`** (6258 lines, churn 76, CC 247) — fifth consecutive
   run at #1, though growth cooled to +7.5% this week after a +30% jump last run. It
   already has sibling `client_portal/router.py`, `client_portal/db.py`, and
   `client_portal/asks/service.py` (itself now a top-10 hotspot at 3504), so the service
   layer keeps spawning sub-areas faster than it sheds responsibility from the main
   module. Extract by portal capability (delegation, roster, feature-flag resolution)
   into a `client_portal/service/` package following the router/service-package
   precedent in Invariant #1 (`routers/settings/`, `services/git_service/`), keeping
   `client_portal/service.py` as a re-export facade. Estimated reduction: 6258 → ~800
   lines in the facade, remainder split across 4-5 focused modules.

2. **`services/agent_service/crud.py`** (3872 lines, churn 52, CC 221) — new to the top
   3 this run, having jumped two places (#4 → #2) on a complexity spike alone (churn held
   flat). The file already mixes several distinct concerns under one module: template
   resolution (`_resolve_template`, `_read_source_template`, `_apply_fork_to_own`), GitHub
   access validation (`_validate_github_access`, `_validate_push_access`,
   `_resolve_github_repo_and_pat`), and container env/volume construction
   (`_build_env_vars`, `_workspace_volume_mount`, `_shared_folder_mounts`) — 59 top-level
   functions in one file. `agent_service/` is already a package (Invariant #1 precedent:
   `lifecycle.py`, `deploy.py`, `read_only.py` are siblings), so this is a same-package
   split, not a new package: pull template resolution and env/volume construction into
   their own sibling modules, keeping `crud.py` as the create/update/delete orchestration
   entry point that calls into them. This also reduces `crud.py`'s own fan-out (39
   imports, itself flagged above).

3. **`services/task_execution_service.py`** (3609 lines, churn 46, CC 241) — held the
   #3 slot for a second consecutive run, growing further (10277 → 11086). `execution.md`
   already names this file as the one where every terminal side effect must be gated on
   winning the status CAS with the dispatch-activity close paired to the CAS winner
   (#1804 class) — that coupling between correctness-critical control flow and ordinary
   feature churn is exactly the kind of file where a size-driven split needs to preserve
   invariant behavior, not just move lines. A complexity-reduction pass (extract the
   CAS-win/dispatch-close pairing into a narrow helper used by every terminal writer)
   would reduce both the line count and the chance of a fourth occurrence of the #1804
   bug class, without touching unrelated code.
