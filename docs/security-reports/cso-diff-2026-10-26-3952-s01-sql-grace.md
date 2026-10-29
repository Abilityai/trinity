# /cso --diff — abilityai/trinity#3952 (S-01 SQL-side grace)

- Date: 2026-10-26 · Mode: daily, diff · Branch: `feature/3952-s01-pull-pre-enqueue` → `dev`
- Changed: `src/backend/canary/invariants/s01_slot_row_bijection.py`, `tests/unit/test_canary_invariants.py`, `docs/memory/architecture/reliability.md`, one learnings fragment.

## Result

No findings. No coverage gaps.

## What was checked

- Attack surface: no new endpoint, WebSocket channel, MCP tool or agent-context ingress. S-01 is a read-only predicate over the canary snapshot.
- Secrets (Phase 2, diff range): known-prefix grep over added lines, no hits.
- Agent-shipped content (Phase 8 grep): invisible/bidi Unicode over added lines, no hits.
- A10 failure direction: `_older_than` treats a missing or unparseable `started_at` as old, so the row is reported instead of raising out of `check()` for every agent. Direction stated in its docstring; catch limited to `TypeError`/`ValueError`.
- Phases 3-6, 8, 10, 11: no diff overlap (no dependency, workflow, Docker, compose, integration, template or skill change).
