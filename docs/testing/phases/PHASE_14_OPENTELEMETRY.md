# Phase 14: OpenTelemetry

> ⛔ **Retired — 2026-10-09.** Do not run this phase. The scenario runner skips it.

## Why it was retired

The phase tested an Observability panel and cost/token figures in the Dashboard header. Both were removed from the UI ("refactor(ui): remove OTel stats from Dashboard"); nothing in `src/frontend/src/views/Dashboard.vue` references them. What remains is backend-only (`GET /api/ops/costs`, admin or `ops`-scope key — the `/api/observability/*` routes were removed in #3434) and needs `OTEL_ENABLED=1`, a running collector and a billable chat turn — not a click-through scenario.

## What covers this now

- **Phase 22** (Telemetry & Logs) covers the telemetry the UI does show: per-agent CPU and memory, and host telemetry on the Dashboard.
- The cost endpoint (`GET /api/ops/costs`) belongs to the API suite under `tests/`.

The January text of this phase is in git history (`git log -- docs/testing/phases/PHASE_14_OPENTELEMETRY.md`).
