# CSO diff audit — tool-access profiles (trinity-enterprise#341)

- **Date**: 2026-11-03 · **Mode**: `--diff`, daily (8/10 gate) · **cso**: v1.2.3
- **Scope**: branch `feature/341-tool-access-profiles` against base `13cce98b8` (working tree, new files included)
- **Result**: **0 findings** at the gate · 1 hypothesis (not counted) · 2 hardening notes · 5 coverage gaps

## What changed (attack-surface delta)

| Surface | Change | Gate |
|---|---|---|
| `GET /api/agents/{name}/tool-access` | new read | `OwnedAgentByName` (owner tier, uniform 404) |
| `PUT /api/agents/{name}/tool-access` | new write | `require_person_or_capability("agents.manage", self_person_only=True)` + `assert_agent_owner`; system agent 403 |
| `PUT /api/agents/{name}/read-only` | now the Strict read-only shortcut; writes through the same compare-and-set | unchanged gate |
| `PUT /api/agents/{name}/guardrails` | keeps the stored profile; 409 on a list edit under a stored profile | unchanged gate |
| `GET /api/agents/{name}/guardrails` | `tool_access` reduced to `{profile}` (less disclosure at access tier) | unchanged gate |
| 9 send routes (messages, Telegram/Slack group posts, notifications, voice reply, VoIP call, room post, shared files, reports) | new first-action check `assert_tool_allowed` (agent principals only; caller's list, and the path agent's on the four routes that send AS it) | added check, no gate relaxed |

No MCP tool added (Invariant #13 boundary stated in the `agent_config.py` header). No DB migration, no Dockerfile/compose/workflow/dependency change.

## Stack CVE quicklist

Not re-evaluated: the diff changes no pin, image, compose file or lockfile. Every row is **not reachable by this diff**; the full-run verdicts stand.

## Checks run

- **Secrets (P2)**: added lines grepped for `sk-`/`sk-ant-`/`ghp_`/`github_pat_`/`xox[bp]-`/`AKIA`/`re_` shapes — 0 hits. No `system_settings` writer added.
- **Enterprise disclosure (P2)**: `enterprise-docs-guard.yml` pattern replayed (Python `re`) over every added line — 0 hits.
- **Concealed Unicode (P8)**: tag-block / zero-width / bidi grep over the diff — 0 hits.
- **Auth guards (ASI03, A01)**: `test_293_admin_gate_rejects_agent_keys`, `test_2996_human_only_routes` (both new routes classified), `test_1310_auth_wiring`, `test_186_enumeration_uniformity`, `test_2094_dependency_path_param_pairing`, `test_ent164_self_change_grants`, `test_2996_owner_config_person_only` (tool-access added to the grantable/self-person-only matrix) — **379 passed** (challenged controls).
- **Injection (A05)**: the Custom list (≤50 × 1–256 chars, validated) reaches the CLI as list argv and `AGENT_GUARDRAILS` JSON, the same path `disallowed_tools` already used; read-only patterns still ride `root_config_command`'s positional argument, never shell text. All SQL is SQLAlchemy Core with bound values (the compare-and-set uses `is_not_distinct_from`).
- **Concurrency (A01/A10)**: the three writers share one compare-and-set over `guardrails_config` + `read_only_mode` (409 on a lost race, mutation-proved in `test_ent341_tool_access_routes.py`); racing live applies converge to the row.
- **Output handling (LLM05/ASI09)**: the new Vue section renders statements, coverage and error details as text; no `v-html`.
- **ASI02/ASI10**: an agent cannot change its own profile on any of the three writers (`self_person_only`), and cannot widen its send surface through `.mcp.json` aliases or its shell, because the backend sinks check the caller's enforced list.

## Hypothesis (not counted)

**H1 — a blocked agent can ask an unblocked agent to send.** By the operator's ruling on the issue, agent-to-agent tools (`chat_with_agent`, `fan_out`, `call_a2a_agent`, `emit_event`) are not sends and no profile removes them. A Strict read-only agent can therefore ask a Full-access sibling to message a person; the sibling sends under its own identity and its own profile, and the audit trail attributes the send to the sibling. Not a boundary break (the sibling's owner chose its profile), but the "can't send" copy is per agent. Recorded in the requirement (§28.10) as a stated limit.

## Hardening notes (below the gate)

- **N1 — `assert_tool_allowed` does not state its failure direction (A10).** A malformed `guardrails_config` reads as `{}` and the send is allowed (fail-open), the same direction as every other guardrails reader (a malformed row is baseline). Not attacker-reachable: only validated owner writes reach the column. Recommendation: say so in the docstring. *Applied in the same PR.*
- **N2 — `getattr(principal, "agent_name", None)` (A10 trap shape).** Absent reads as "not an agent", which is the unchecked direction. Every agent identity on these routes is `models.User`, which always declares `agent_name`; the only principal without it is the Workspace visitor (`WorkspacePrincipal`, rooms), which is a person by construction. Recommendation: name that in the docstring. *Applied in the same PR.*

## Coverage gaps

1. **Docker pass** — not run (diff mode; no image, compose or base-image file changed, so nothing image-internal is in scope).
2. **Supply chain (P3)** — no dependency, lockfile or install change in the diff; no scanner run.
3. **CI/CD (P4)** — no workflow change; not audited.
4. **Agent-shipped supply chain (P8)** — no skill, template, plugin or `.mcp.json` template change; only the concealed-Unicode grep ran.
5. **ent#926 agent-route tier census** — not on this branch's base; the two new routes get their tier rows when `dev` is merged before push.

## Trend

Diff run; no prior report covers these files at this root cause. Nothing resolved or persistent to carry.
