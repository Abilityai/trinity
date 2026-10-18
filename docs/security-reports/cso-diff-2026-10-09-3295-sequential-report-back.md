# CSO diff audit: abilityai/trinity#3295 sequential chat report-back (2026-10-09)

Scope: `--diff` against `origin/dev`, daily mode (8/10 gate).

**Result: 0 findings.**

## Surface changed
- New endpoint: `POST /api/agents/{name}/executions/{execution_id}/report-back`, body `{parent_execution_id}`. It attaches an outbound destination (a Slack/Telegram/Workspace conversation) to a `/chat` execution row, so the row's terminal is reported there.
- MCP server: a backend 504 on `/chat` now runs the existing #914 execution lookup before answering with a receipt; a `queued_timeout` receipt triggers one arm call with the caller's turn.
- No auth-dependency change, no new MCP tool, no SQL beyond ent#498's existing add-only stamp, no dependency, Dockerfile, network or workflow change.

## Checks
| Check | Result |
|---|---|
| Secrets / internal hosts in added lines | none (test fixtures are deliberately fake) |
| Enterprise-docs guard pattern | no hit |
| Dependencies | none changed; `npm audit` advisories are pre-existing on `dev` |
| Route gate | `get_authorized_agent` (uniform 404) + `get_current_user`; listed in `_route_census.AGENT_CALLABLE`; `test_2996` green |
| Who may arm | only the row's dispatcher: agent key = `source_agent_name` (from the key, never the header), person = `source_user_id`; connector 403; anything else one uniform 404 — all pinned |
| Where it may point | `_inherited_channel_context` unchanged: the agent must BE the parent's agent, a person must own it, the parent must be running — pinned |
| Repointing | add-only stamp (`WHERE source_channel IS NULL`) + inbound channel turns excluded, so no reply destination can be redirected — pinned |
| Sibling-agent key | cannot arm a row it did not dispatch, cannot name a turn it is not serving |
| Replay / recovery | 409 replay implies the same dispatcher; the 504 lookup has the #914/#2661 bounds (exact message, trigger, key id, ambiguity → none) |
| Report content | unchanged `report_completion`: per-channel consent, sanitize-then-truncate, `effect_guard` |
| Logs | ids and principal names only |
| Failure direction | refusals read as `off`; nothing fails toward a second post |

## Hypothesis examined and dropped
- **Execution-id existence differential** (404 for missing/foreign row, 403 for not-dispatcher) inside an agent the caller already accesses. Not a finding: those ids are already readable through the accessor-scoped executions list, and the #186 rule concerns agent enumeration, which the dependency answers uniformly before this code. Applied before merge: the dispatcher refusal now answers the same uniform 404.

## Coverage gaps
- Phases 4, 5, 8: nothing in the diff.
