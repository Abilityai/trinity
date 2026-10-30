# CSO diff audit — #4105 pull trigger census

- **Version / mode:** cso 1.2.3, `--diff`, daily gate (8/10)
- **Scope:** `feature/4105-pull-self-task-skill-gate` vs merge-base `26f506a23`
- **Changed:** `services/pull_pilot.py`, `services/task_execution_service.py` (trigger-set constants), `services/capacity_manager.py` (one diagnostic call), `routers/executions.py` (filter allow-list), docs, tests.

## Attack surface

No new endpoints, routes, MCP tools, WebSocket channels, ingress paths, credentials, schema or dependencies.

## Checks

| Area | Result |
|---|---|
| P2 secrets | Diff grep for known credential prefixes, emails, private IPs: only the `u@example.com` test placeholder. |
| P3 supply chain | No dependency change. |
| P4 CI/CD, P5 infra | No workflow, Dockerfile or compose change. |
| P6 integrations | `note_unreachable_pull_trigger` now also runs from `capacity_manager.acquire`. Its `triggered_by` values come from server-side derivation (`/task`) or fixed literals. `routers/internal.py` passes `request.triggered_by` to `execute_task`, but the router is gated by `verify_internal_secret` (`routers/internal.py:75`). |
| P7 ASI03/ASI10 | `self_task` / `skill_gate` move from push to the pilot's own worker pool. No authorization decision changes. The agent-key path to `/task` is unchanged. Capacity tightens: a pilot no longer runs above `max_parallel_tasks`. |
| P7 ASI08 | The chain-depth cap is unchanged. `skill_gate` keeps `chain_depth` on the row it creates. |
| P9 A10 | The new diagnostic is wrapped in the existing `except Exception: return False`. It logs only, it is not a guard, and failing open is correct for it. |

## Findings

None.

Candidate considered and dropped: `_UNREACHABLE_NOTED` grows by one entry per (pilot agent, unclassified trigger). An arbitrary trigger string reaches it only through the internal-secret route. This is also excluded as resource exhaustion (hard exclusion 1).

## Coverage gaps

- Docker pass not run (no image change).
- No dependency scanner run (no dependency change).
