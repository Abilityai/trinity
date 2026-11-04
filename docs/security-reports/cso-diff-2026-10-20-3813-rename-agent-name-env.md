# CSO Diff Audit — #3813 renamed agent's AGENT_NAME and label re-baked on recreate

- **Date**: 2026-10-20 · **Mode**: `--diff` (daily gate 8/10) · **Branch**: `feature/3813-rename-agent-env` vs `origin/dev` (merge-base `d1c2447c4`)
- **Scope**: `src/backend/services/agent_service/{helpers,lifecycle}.py`, comment-only edits in `src/backend/dependencies.py` and `src/backend/routers/agent_rename.py`, two feature-flow docs, `tests/unit/test_3813_rename_agent_name_env.py`, two test stub lists, `tests/registry.json`

## Verdict: 0 findings.

The recreate now writes `AGENT_NAME` and the container label `trinity.agent-name` from the agent's live name, beside the existing `TRINITY_AGENT_AUTH_TOKEN` re-derive. A new start-path predicate recreates a stopped container whose name values differ from the live name.

## Checks run (diff mode; phases 0, 1, 12–14 + diff-relevant rows)

| Check | Result |
|---|---|
| Attack-surface delta | None. No new routes, inputs, dependencies, CI or Docker changes |
| Source of the written name | `agent_name` passed into `recreate_container_with_updated_config` and `start_agent_internal` by their existing callers; no request field reaches it. The agent cannot choose the value |
| Agent self-boundary (heartbeat, result callback, pull poll, git) | Unchanged. The backend still authorizes by the agent-scoped key's bound `agent_name` (`heartbeat_service.authorize_heartbeat`: `scope == "agent"` and `agent_name == path`). Correcting `AGENT_NAME` makes an agent present its own name; it cannot present a sibling's |
| Backend→agent auth (#1159) | Token derivation unchanged; `test_agent_auth_header_guard.py` green |
| Container label readers | Stats-cache invalidation and the ephemeral orphan reclaim read the container label; both now see the live name. Volume labels (the #1664 ownership evidence) are separate objects and are not written |
| Recreate trigger | Cold start only, absent values match, unreadable attrs match: cannot turn an ensure-running call into a container kill, cannot recreate the fleet |
| A10 failure direction | Predicate fails open (match) on unreadable attrs, stated in its docstring; it is a repair trigger, not an auth gate |
| Secrets in diff | None |
| Hidden Unicode in new files | None |
| Enterprise-docs guard | The diff's docs add no paid-module tokens |
| Auth guards | `test_186_enumeration_uniformity`, `test_1310_auth_wiring`, `test_293_admin_gate_rejects_agent_keys`, `test_agent_auth_header_guard`: 71/71 green |
| Tests | `tests/unit/test_3813_rename_agent_name_env.py` 11/11 green (red-first: 8 red before the fix) |

## Hypotheses

None (daily mode).

## Coverage gaps

Full-repo phases not run in diff mode: history secrets, dependency scan, CI/CD, image internals.
