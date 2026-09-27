# Archived Documentation

> **Purpose**: Contains completed design documents, superseded specs, and old reports.
> These documents are preserved for historical reference but are no longer actively maintained.

## Archive Date: 2026-01-05

## Contents

### drafts/ (15 files)
Completed feature design documents - features now implemented:

| File | Feature | Status |
|------|---------|--------|
| CONTENT_FOLDER_CONVENTION.md | Content folder gitignore | Req 12.1 ✅ |
| FILE_MANAGER.md | File Manager page | Req 12.2 ✅ |
| FIRST_TIME_SETUP.md | Admin password wizard | Req 11.4 ✅ |
| INTERNAL_SYSTEM_AGENT.md | trinity-system agent | Req 11.1 ✅ |
| LOCAL_AGENT_DEPLOY.md | MCP deploy tool | Feature-flow ✅ |
| OTEL_INTEGRATION.md | OpenTelemetry metrics | Req 10.8 ✅ |
| PARALLEL_HEADLESS_EXECUTION.md | Parallel task mode | Req 12.1 ✅ |
| PUBLIC_AGENT_LINKS.md | Public chat links | Req 11.3 ✅ |
| REMOVE_CHROMA_VECTOR_STORE.md | Chroma removal | Done 2025-12-24 |
| SYSTEM_AGENT_OPS_REQUIREMENTS.md | System agent ops scope | Req 11.2 ✅ |
| SYSTEM_MANIFEST_PHASE1.md | Multi-agent deploy Phase 1 | Req 10.7 ✅ |
| SYSTEM_MANIFEST_PHASE2.md | Multi-agent deploy Phase 2 | Req 10.7 ✅ |
| SYSTEM_MANIFEST_PHASE3.md | Multi-agent deploy Phase 3 | Req 10.7 ✅ |
| SYSTEM_MANIFEST_SIMPLIFIED.md | Superseded by PHASE1-3 | N/A |
| SYSTEM_YAML_SPEC.md | Superseded by SYSTEM_MANIFEST | N/A |

### refactoring/ (4 files)
Completed refactoring plans:

| File | Completed |
|------|-----------|
| AGENTDETAIL_REFACTORING_PLAN.md | Superseded |
| AGENT_DETAIL_VUE_REFACTORING_PLAN.md | 2025-12-27 |
| AGENTS_ROUTER_REFACTORING_PLAN.md | 2025-12-27 |
| DATABASE_REFACTORING_PLAN.md | 2025-12 |

### memory/ (2 files)
Completed audits and merged requirements:

| File | Reason |
|------|--------|
| FEATURE-FLOWS-AUDIT-2025-12-26.md | Audit complete |
| requirements-agent-permissions.md | Merged into requirements.md |

### testing/ (17 files)
Old test reports (archived 2026-01-05):

| File | Date |
|------|------|
| TEST_REPORT_2025-12-08.md | 2025-12-08 |
| UI_TEST_REPORT_2025-12-08.md | 2025-12-08 |

Testing guidance consolidated into `docs/testing/STRATEGY.md` (archived 2026-09-13, #2339 — Rail R5).
The live click-through scenarios stayed in `docs/testing/phases/` (run by `/ui-sweep`, which
writes its dated reports to `docs/testing/ui-sweep/`); the pull-migration test record moved
beside its entry point at `docs/planning/PULL_MIGRATION_TESTING.md`.

| File | What it was | Superseded by |
|------|-------------|---------------|
| 302-settings-tabbed-layout-manual-test-plan.md | Manual test plan for PR #700 (2026-06) | Shipped; `docs/planning/302-settings-test-list.md` |
| API_TEST_REQUIREMENTS.md | 2025-12 requirements draft for the API suite | `tests/README.md` + `tests/registry.json` |
| GEMINI_TESTING_PLAN.md | Pre-merge manual plan for the Gemini runtime (2025-12) | `docs/GEMINI_SUPPORT.md`; runtime tests under `tests/` |
| MODULAR_TESTING_STRUCTURE.md | 2025-12 announcement of the phase split | `docs/testing/phases/README.md` |
| QUICK_START.txt | 2025-12 quick start for the phases | `docs/testing/phases/INDEX.md` |
| TESTING_AGENTS_DESIGN.md | 2025-12 design for the test-agent suite | `docs/memory/feature-flows/testing-agents.md` |
| TESTING_FRAMEWORK_COMPLETE.md | 2025-12 completion note for the phases | `docs/testing/phases/` |
| UI_INTEGRATION_TEST.md | The monolithic checklist the phases were split from | `docs/testing/phases/` |
| UI_TESTING_GAP_ANALYSIS.md | 2026-01 gap analysis of user stories against the phases | `tests/journeys/catalog.yaml` (the promise list) |
| UI_TEST_RESULTS_2026-01-14.md | Dated phase run report | Point-in-time |
| audit-trail-manual-test-plan.md | SEC-001 manual acceptance plan (#20, 2026-04) | Shipped; cited from `docs/memory/requirements/security.md` |
| exploratory-e2e-report-2026-08-14.md | Dated exploratory report | `docs/testing/ui-sweep/` reports |
| run_integration_test.py | 2025-12 API smoke script | `tests/run-*.sh` |
| test-report.md | 2026-03 full-suite run report | CI artifacts; `tests/reports/` |
| TESTING_GUIDE.md | Was `docs/TESTING_GUIDE.md` (2025-12): "manual > automated", flow Testing template, dated coverage tables | `docs/testing/STRATEGY.md`; the template is the add-testing skill |

### root/ (2 files)
Superseded architecture documents:

| File | Superseded By |
|------|---------------|
| ACTIVITY_TRACKING_ARCHITECTURE.md | feature-flows/activity-stream.md |
| AGENT_CUSTOM_METRICS_SPEC.md | feature-flows/agent-custom-metrics.md |

### plans/ (12 files)
Completed / superseded implementation & sprint plans:

| File | Status |
|------|--------|
| ORCHESTRATION_RELIABILITY_2026-04.md | Sprints A–D′ shipped; destination superseded 2026-06-05 by `docs/planning/TARGET_ARCHITECTURE.md` (pull / work-stealing coordination) |
| _(11 earlier plans)_ | Completed implementation plans — settings-service, access-control, http-client, github-api extraction, docker-stats, execution-termination, vector-logging, ssh-access, system-agent consolidation, dashboard-permissions, credential-assignment |

## Finding Current Documentation

- **Requirements**: `docs/memory/requirements.md`
- **Architecture**: `docs/memory/architecture.md`
- **Feature Flows**: `docs/memory/feature-flows/`
- **Active Drafts**: `docs/drafts/` (remaining files are future ideas)
- **Testing**: `docs/testing/STRATEGY.md` (method, lanes, harness bar) → catalog, `JOURNEYS.md`, `phases/`, `ui-sweep/`
