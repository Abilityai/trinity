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

### testing/ (2 files)
Old test reports:

| File | Date |
|------|------|
| TEST_REPORT_2025-12-08.md | 2025-12-08 |
| UI_TEST_REPORT_2025-12-08.md | 2025-12-08 |

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
