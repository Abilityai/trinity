# CSO diff audit — #3989 chat slot keyed on the execution row id

- Date: 2026-10-27 · Mode: `--diff`, daily (8/10 gate) · Skill: cso v1.2.3
- Scope: staged diff of `feature/3989-chat-slot-row-id` vs `origin/dev` — 7 files: `services/chat_execution_service.py` (one argument + two comments), `canary/invariants/s01_slot_row_bijection.py` (docstring), two unit-test files, three feature-flow docs.

## Change under audit

`prepare_chat_execution` now passes `execution_id=chat_execution_id` to `db.create_task_execution`, so a `/chat` row's `schedule_executions.id` is the admission's `uuid4` — the same value used as the Redis capacity-slot member, `capacity.release` id and audit-log id.

## Phases (diff-scoped)

| Phase | Result |
|---|---|
| 0 Architecture / quicklist | No dependency, image, workflow or pin touched; every quicklist row is out of diff scope (not re-verified here). |
| 1 Attack surface | No new endpoint, WebSocket channel, MCP tool, upload point, ingress into an agent prompt, or background job. |
| 2 Secrets | Staged diff grep for `sk-`, `ghp_`, `xox`, `AKIA`, password/token/secret assignments: no hits. |
| 3 Supply chain | No dependency or lockfile change. |
| 4 CI/CD | No workflow change. |
| 5 Infrastructure | No Docker/compose/network/Redis ACL change. |
| 6 Webhooks / integrations | Not touched. |
| 7 Agentic (ASI01–10) | ASI03/ASI10: the row id an agent receives (`payload["execution_id"]`) is unchanged in role; agent-key self-boundaries (result callback, heartbeat) authorize on `current_user.agent_name`, never on id secrecy. ASI08: the turn now holds its capacity slot for its whole life (the cleanup sweep no longer reclaims it as orphaned), which tightens `max_parallel_tasks` on chat — a reduction in amplification headroom, not an increase. |
| 8 Agent-shipped supply chain | Not touched. |
| 9 OWASP 2025 | A01: `resolve_and_validate_execution` still requires id AND `agent_name` match, so a caller-supplied `X-Trinity-Execution-Id` cannot claim another agent's row; id-format validators (`models.py`, `execution_integrity.py`, MCP `execution_id.ts`) accept UUIDs. A04: `uuid4` draws from `os.urandom` (122 random bits) — execution ids are not used as bearer capabilities anywhere in the diff path. A05: id is a bound parameter in the INSERT. A10: no guard added or changed. |
| 10 STRIDE | Scheduler/backend: Repudiation improves — slot logs and `/slots` now name a resolvable execution row. No other component changes. |
| 11 Data classification | Execution ids remain INTERNAL. |

## Findings

None (SUPPORTED: 0).

## Hypotheses

None (daily mode).

## Coverage gaps

- Docker pass not run (diff mode; no image-affecting change).
- No dependency scanner run (no dependency change in diff).

## Trend

Prior related diff run `cso-diff-2026-10-26-3952-s01-sql-grace`: no findings; none carried.
