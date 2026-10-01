# /cso --diff — ent#727 narrow cross-agent metrics read (2026-09-30)

**Scope**: branch `feature/727-cross-agent-metrics-read` vs `dev` · daily mode (8/10 gate) · cso v1.1
**Result**: **0 findings** (0 critical / 0 high / 0 medium).

## Surface changed
- `GET /api/agents/{agent_name}/metrics` and `/metrics/definitions` — an agent key may read another agent while holding an `agent_permissions` grant on it (definitions previously had no self-gate at all).
- `GET /api/agents/{agent_name}/objectives` — may resolve an `actual` from a granted serving agent.
- MCP `get_metrics(agent?)` — policy row `enforce` on `agent` (`checkAgentEdge`).

## Verified clean
| Check | Evidence |
|---|---|
| Gate order (Invariant #8) | uniform-404 dependency first; grant gate before limiter and store |
| No oracle over metric existence | identical 403 body for existing / absent metric (pinned by test) |
| Agent cannot self-grant | grant routes reject agent principals; spawn auto-grant is parent→new child; system deploy grants only among agents it created |
| Ephemeral agents | none of the three routes is on `_EPHEMERAL_ALLOWED_ROUTES` |
| Objectives cross-viewer disclosure | `can_view` required (fail-closed); router uses `can_user_access_agent`; ambiguity finding names only visible agents |
| MCP description (#846 class) | static text, no per-agent metadata |
| Audit | all three cross-read paths audited hourly per (reader, target, route); marker after write |
| Secrets / PII / enterprise tokens | none in diff |

## Accepted (pre-existing / by design)
- Agent-key 403-vs-404 differential on sibling names — exists since ent#479; ent#629 territory.
- N granted readers × 240/min on one target — store-only indexed read (requirements §51.3).
