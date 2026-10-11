# Phase 10: Error Handling

> ⛔ **Retired — 2026-10-09.** Do not run this phase. The scenario runner skips it.

## Why it was retired

The phase drove a `test-error` agent and a `/api/agents/{name}/errors` endpoint. Neither exists: there is no such template in `config/agent-templates/` and no such route under `src/backend/routers/`. Every step after the first depended on that fixture's canned output.

## What covers this now

Honest failure states are checked where they appear, not in a phase of their own:

- unknown agent and unknown execution — **Phase 33** and **Phase 20**;
- invalid or disabled public link — **Phase 27**; invalid shared-canvas link — **Phase 34**;
- wrong password and logged-out deep links — **Phase 1**;
- the `edges` lane of `/ui-sweep` probes hostile and boundary input on every area.

The January text of this phase is in git history (`git log -- docs/testing/phases/PHASE_10_ERROR_HANDLING.md`).
