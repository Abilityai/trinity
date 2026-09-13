# CSO diff audit — abilityai/trinity#2703 (`feature/2703-skill-assign-delivery`)

**Date**: 2026-09-11 · **Mode**: `--diff` (daily gate 8/10) · **Base**: merge-base `3edd0dc6` on `dev` · **Diff**: 21 tracked files +~700 / −19 plus 4 new files (working tree, pre-commit). Surface: delivery-on-assign in the skills service + router, one new fleet-wide WS event type, listener wiring in three stores and five components, MCP response typing.

## Verdict
**No findings at the gate.** The diff collapses two owner-gated calls (assign, then Sync) into one; no principal gains reach. Every new fail path fails in the safe direction on authorization (only delivery *membership* fails open, mirroring ent#236's removal guard).

## Attack surface introduced by the diff
- **Endpoints**: none added. `POST/PUT /api/agents/{name}/skills[...]` now perform the in-container restore that `POST .../skills/inject` already performed under the same gate (`get_owned_agent_by_name` → `_enforce_connector_scope(owner_op=True)` → owner/admin). Delivery runs `force=False, assigned_only=True` — strictly less work than the pre-existing manual Sync (`force=True`).
- **WebSocket**: one new type, `agent_skills_changed`, emitted only via `skill_service.broadcast_skills_changed` → `{type, agent_name}`. Six emit sites (router ×4, background completion, fleet sweep) share the one emitter. Same disclosure class as `agent_tags_changed` (ent#305) / `agent_report` (#918) / `agent_activity`. Listeners refetch via `AuthorizedAgentByName` (`/playbooks`) and the roster-gated `/briefings`; the portal store returns early for a name not on the caller's roster, so a foreign name produces no request and no oracle.
- **MCP**: `skills.ts` gains a response type and reworded descriptions; no new parameters, no new tool. The pre-existing absence of an MCP-layer `{self} ∪ permitted` gate on `assign_skill_to_agent`/`set_agent_skills` is recorded in `DEBT_INBOX` (`debt:2026-09-11-mcp-skill-assign-tools-have-no-self-or-permitted-gate`).
- **Trust-boundary effect**: none. Library content still enters only through admin+human-gated sources (ent#237); assignment stays owner-gated.

## Findings
| # | Sev | Conf | Status | Category | Finding | Phase | File:Line |
|---|-----|------|--------|----------|---------|-------|-----------|
| — | — | — | — | — | none at the gate (0 TENTATIVE) | — | — |

## Verification performed
- **Secrets (P2, diff)**: no commits on the branch yet; the working diff plus the four new files were scanned for known prefixes (`AKIA`, `sk-`, `ghp_`/`gho_`/`github_pat_`, `xox*`): none. The enterprise-docs seam file `main.py` gains one import + one manager call, no paid-module tokens; the only doc token hit (`Monetization`) is a pre-existing section header in `architecture/backend.md`.
- **Guard suites**: `test_186_enumeration_uniformity`, `test_2094_dependency_path_param_pairing`, `test_agent_auth_header_guard`, `test_credential_paths_parity`, `test_model_context_parity`, `test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_918_report_broadcast`, `test_305_tags_broadcast` — 82 passed.
- **Name guard (A03)**: `validate_skill_name` filters every new site — `_deliver_assigned_skills` (router), `deliver_assigned`, and `_inject_skills_locked`'s `valid_names` BEFORE the under-lock re-read, which only narrows. A DB-sourced name never reaches `_read_agent_skill_metas` or `_restore_skill` unvalidated.
- **Credential exposure**: audit `details` = `{trigger, status, reason, skills: names[:50]}`; WS = identifiers; the per-skill `delivery.error` can carry an agent-server response fragment (`restore failed (<status>): <text[:300]>`) — the same content `/skills/inject` already returned to the same owner-gated principals; never rendered by the frontend (fixed `REASON_TEXT` map, `{{ }}` interpolation only).
- **Frontend (P7/A03)**: no `v-html`/`innerHTML` added.
- **Fail direction**: Docker `None` → `not_delivered/docker_unavailable`; stopped → `pending_start`; busy → one 2 s retry → `not_delivered/injection_in_progress`; transport → `agent_not_ready`; background → strong-ref set + WS; `assigned_now` unreadable → caller's (already validated) list.
- **Resource amplification**: owner-only, per-agent `SingleFlightLock` (TTL 1800 s), 20 s budget, no LLM call on the path (hard exclusion 1).
- **Concurrency**: PUT-inject vs DELETE across workers — the opt-in under-lock re-read drops the deleted name (`unassigned_meanwhile`); a background delivery holding the lock defers a removal to the start-path reconcile (staleness on the owner's own agent, not an exploit).
- **Supply chain / CI / infra (P3–P5)**: no dependency, lockfile, Dockerfile, compose or workflow change.
