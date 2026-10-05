# CSO diff audit — execution search (abilityai/trinity-enterprise#653) — 2026-09-19

Mode: daily (8/10 gate), scope: branch diff. Branches: public `feature/653-execution-search` → `dev`; private submodule `feature/653-execution-search` → `main`.

## Attack surface introduced
- `GET /api/enterprise/execution-search` — entitlement-gated (`execution_search`). Callable by JWT humans, user-scoped and system-scoped MCP keys. Agent-scoped keys are refused by `reject_agent_principal` and, since this audit, by an allow-list over `mcp_scope` (`ADMIN_GATE_SCOPES`); connector, ops, portal-delegate and ephemeral keys are refused earlier at the auth entry. Read-only over the OSS executions table; only bounded excerpts leave the db layer.
- MCP tool `search_executions` — per-tool `canAccess` allow-list `{system, user}`, in-tool refusal with the audited denial envelope, honest `available:false` when the module is absent or unentitled.
- OSS backend: one comment line (`# mcp:` header). No dependency, workflow, Docker or schema changes.

## Findings
None at or above the gate.

## Verification
- Adversarial fresh-context verifier over six angles: every principal `get_current_user` can mint dies at a cited line; `q`/`agents`/`status`/`triggered_by` are bound parameters, `fields` is allow-listed in the db layer before interpolation, LIKE wildcards escaped with `ESCAPE '\'`; non-admin humans can only shrink the IN list; raw bodies are popped in the db layer and absent from the response model; the tool is neither listed nor callable for agent/connector/ops/portal-delegate/anonymous sessions in prod; the entitlement gate is router-wide and rolls back on failed registration.
- Post-audit hardening applied: the route fence became an allow-list (Invariant #8 shape) with fail-closed attribute access; `agent` stripped. 39/39 private tests, 438/438 MCP, tsc clean.
- Secrets scan and the enterprise-disclosure guard pattern over the diff: clean.

## Appendix (below gate / accepted / pre-existing)
- A1 LOW 6/10 TENTATIVE — the query string travels in the URL and reaches access logs (same as the OSS `?search=`); recommend dropping the query text from the tool's console log.
- A2 INFO ACCEPTED — humans sharing an agent can grep each other's chat responses; amplification of the per-id route; separate issue per operator ruling.
- A3 INFO PRE-EXISTING — unauthenticated probe fingerprints the edition (404/403/401); shared by every enterprise router.

Trend vs 2026-09-17 diff audit: flat (0 → 0).
