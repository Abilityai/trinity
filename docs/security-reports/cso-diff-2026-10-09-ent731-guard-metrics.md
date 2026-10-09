# CSO diff audit: abilityai/trinity-enterprise#731 guard metrics (2026-10-09)

Scope: `--diff` against `origin/dev`, daily mode (8/10 gate). Docker available.

**Result: 0 findings.**

## Surface changed
- Input: canon objective files gain a top-level `guard_metrics:` list. It is read from the document the existing hardened YAML loader already produced.
- Output: `GET /api/agents/{name}/objectives` and MCP `get_objectives` gain `metrics[].role`, `summary.guards` and three finding codes. The Workspace role card gains `PortalRoleMetric.role`.
- No new endpoint, auth change, MCP tool, SQL, dependency, Dockerfile or workflow.

## Checks
| Check | Result |
|---|---|
| Secrets / internal hosts in added lines | none |
| Invisible or bidi Unicode | none |
| `v-html` / `innerHTML` | none added |
| Bare YAML loads | none added |
| Enterprise-docs guard pattern | no hit |
| Input bounds | name `_safe_id`, per-list cap, `finite_number` target/tolerance, direction capped at 16 chars, non-list section is a named finding |
| Cross-agent reads | guard names go through `resolve_served_metrics` behind the same `can_view` grant check as primaries |
| Findings scoping | guard findings publish only for objectives the read returns |
| Workspace disclosure | `role` is a server-chosen constant; findings cross as codes only |
| Failure direction | malformed guards degrade to a named finding; the objective still joins |

## Coverage gaps
- Phase 4 (CI/CD) and Phase 6 (webhooks): nothing in the diff.
