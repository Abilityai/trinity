# CSO diff audit — #3814 system agent as a pull pilot

- Mode: `--diff`, daily (8/10 gate). Base `origin/dev` (d1c2447c4). 14 files, +186/−58, plus `tests/unit/test_3814_system_agent_pull.py`.
- Result: **0 findings** (0 SUPPORTED, 0 hypotheses).

## Change under audit

`heartbeat_service.authorize_heartbeat` now accepts a `scope == "system"` key when the key row's `agent_name` equals the path AND the path is `trinity-system`. It gates four seams:

| Seam | System key, own name | System key, other agent |
|---|---|---|
| `GET /api/internal/next-task` | 200 if allowlisted | 403 |
| `POST /api/internal/tasks/{id}/result` | CAS applies | uniform 404 |
| `POST /api/agents/{name}/heartbeat` | 200 | 403 |
| `POST /api/agents/{name}/executions/{id}/result` (#1083) | passes auth | 403 |

Env: a piloted system agent gets `TRINITY_PULL_MODE` + `TRINITY_BACKEND_URL` at create and recreate; de-pilot removes both. Boot raises a `system_agent_pull_drift` platform alert (reserved prefix `base-image-stale-pull-`).

## Checks

- **Who holds a system-scoped key:** the only writer of `scope='system'` is `system_agent_service._set_system_scope` (`system_agent_service.py:751`), applied to a key minted by `create_agent_mcp_api_key(agent_name=SYSTEM_AGENT_NAME)`. No API route accepts a scope parameter. The identity comes from the key row, never a parameter (the #2975 rule).
- **Privilege delta:** the system key already resolves to fleet-wide system access through the MCP surface. The new grant is acting on its own queue, heartbeat and executions, which is strictly narrower than that.
- **Sibling boundary (Phase 6):** a system key for any other agent is refused on all four seams; tests b, c, the result-seam 404 and the callback 403 exercise the real handlers. The pull allowlist gate still applies to the system key.
- **A10 failure direction:** the predicate fails closed (default `False`, name compared before scope). `_report_pull_mode` swallows exceptions; it is boot diagnostics, the direction is stated in its docstring, and it gates nothing.
- **Enumeration (#186):** the result seam keeps the uniform 404 for not-owned vs unknown executions.
- **Secrets / enterprise disclosure:** no credential patterns, no enterprise design in the changed docs.
- **Guards run:** `test_293_admin_gate_rejects_agent_keys`, `test_2996_human_only_routes`, `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_agent_auth_header_guard` — 137 passed.
- **Mutation evidence:** reverting each production change turns at least one test red (predicate, result pre-check, env set-or-clear, create wiring, drift URL check, boot call, alert kind).

## Coverage gaps

- Docker-pass checks (image UIDs, `.pth`, OSV over installed trees) not run; this diff changes no Dockerfile, dependency or compose file.
- No independent verifier subagent: there was no finding to refute. The sibling-boundary claim was challenged in-context and by the tests above.
