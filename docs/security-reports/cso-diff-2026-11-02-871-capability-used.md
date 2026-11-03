# CSO diff audit — trinity-enterprise#871 capability use audit

- **Version / mode:** cso 1.2.3, `--diff`, daily gate (8/10)
- **Scope:** `feature/871-capability-used-audit` vs merge-base `c8f67cb85`
- **Changed:** `dependencies.py` (the capability fence), `services/agent_service/files.py`, `routers/agents.py`, `routers/skills.py`, `services/system_service.py`, `services/capability_grant_service.py`, `models.py` (a comment), docs, tests.

## Attack surface

No new endpoint, MCP tool, WebSocket channel, credential, schema or dependency. One existing read, `GET /api/agents/{name}/skill-sets`, takes the request so it can write a row. The owner bound moves inside `enforce_agent_capability`.

## Checks

| Area | Result |
|---|---|
| P0 quicklist | Starlette: no gate reads `request.url.path` in `dependencies.py` or `routers/` (one comment). Claude Code pin 2.1.281, past the listed fixes. Gemini CLI and Codex pinned. Other rows have no overlap with this diff. |
| P2 secrets | Branch history grep for known credential prefixes: none. Emails and IPs: test placeholders and documentation ranges only. No hidden Unicode in added lines. Enterprise docs guard clean. |
| P3 supply chain | No dependency change. |
| P4 CI/CD, P5 infra | No workflow, Dockerfile or compose change. |
| P6 integrations | `/schedules/{id}/webhook` routes keep their gate and now write a use row. The audit `endpoint` is the path only, and no fenced path carries a secret. |
| P7 ASI03 | `test_293_admin_gate_rejects_agent_keys.py`, `test_2996_human_only_routes.py`, `test_1310_auth_wiring.py`, `test_186_enumeration_uniformity.py`, `test_ent629_agent_reach.py` green. Findings 1 and 2 are here. |
| P8 agent-shipped | No skill, template, hook or MCP config change. |
| P9 A01 | Check order, status codes, the 404 body and the reach set match `origin/dev` in all three fences. Not-owned and nonexistent run the same query, write the same row and answer the same 404. |
| P9 A09 | This is the change's purpose. `capability_used` is written after the owner bound, so a refused fence never reads as a use. |
| P9 A10 | Failure directions stated: `_audit_capability` and `record_capability_use` are best-effort, because neither decides access. An absent `mcp_scope` still fails closed at `capability_refusal` before `_is_agent_principal` is read. |
| P10 repudiation | Improved. The agent, not its owner, is the actor on every row, and the `delete` row now joins on `request_id`. `request_id` follows the caller's header, so a join is not proof; the flow doc says so. |

## Findings

All three are fixed on this branch. Each was reproduced by execution, each has a test that goes red with the fix reverted.

| # | Sev | Conf | Status | Evidence | Category | Finding | File |
|---|---|---|---|---|---|---|---|
| 1 | HIGH | 9/10 | VERIFIED | SUPPORTED | broken-access-control | A capability holder reached the system agent | `dependencies.py` `owner_owns_agent` |
| 2 | MEDIUM | 9/10 | VERIFIED | SUPPORTED | broken-access-control | An edge skipped the owner bound on the file routes | `services/agent_service/files.py` `_require_skill_capability` |
| 3 | LOW | 9/10 | VERIFIED | SUPPORTED | security-logging | The create row named the raw body, not the agent | `routers/agents.py` `_require_create_capability` |

**1. A capability holder reached the system agent** (pre-existing; A01, ASI03). The bound compared owners, and an admin owns `trinity-system`. On an admin-owned install an agent granted one narrow capability wrote the system agent's `CLAUDE.md`, added a skill file, created an enabled schedule and changed its timeout (reproduced: 200, 200, 201, 200).
*Exploit:* an admin grants `schedules.manage` to an orchestrator; a prompt injection in that orchestrator creates a schedule on `trinity-system`; the message then runs under the system key, which bypasses every permission check.
*Fix:* `owner_owns_agent` refuses a system agent, so every fence, predicate and file route answers the uniform 404. People and the system key are unchanged.

**2. An edge skipped the owner bound on the file routes** (pre-existing; A01, ASI03). `_may_reach_for_path` returned on `agent_may_reach` before its owner check, and the capability gate had no bound.
*Exploit:* an agent holds `instructions.manage` and an edge to another user's agent that its owner can access (as an admin, or through a share). `PUT /api/agents/{target}/files?path=CLAUDE.md` wrote the target's instructions (reproduced: 200, one proxied write), though an edge means may call, never may manage. A person with only a share is refused that write, so the key exceeded its owner.
*Fix:* `_require_skill_capability` passes `owner_bound=True`.

**3. The create row named the raw body** (new on this branch; A09). `_require_create_capability` passed `config.name`, so `"Prod Bot!!"` recorded a name that never exists (the agent is `prod-bot`), and a spreadsheet formula reached the row. A NUL in the name cannot be stored on PostgreSQL, so the row would be lost while the sanitized create went on; the `create` row would remain. The NUL case is by reasoning, no PostgreSQL ran.
*Fix:* the target is `sanitize_agent_name(...)`, the slug `create_agent_internal` will use.

## Outside this diff

- **`trinity_prompt` through create-mode deploy:** tracked as #4025. Reproduced for an agent key: a `creator`-owned key holding `agents.manage` overwrote the platform prompt. It enters every agent's prompt on every turn, not at the next start as the issue says. Not counted here.
- **CSV export formula cells** (hypothesis, LOW): `routers/audit_log.py` writes fields raw, so an agent key can place a formula in `target_id` through a path name, or in `request_id` through `X-Request-ID`. An admin must export and open the file, and evaluation was not shown. Recorded, no action, by the operator's decision.

## Candidates dropped

- A lone surrogate in the create body: refuted. It fails validation, so no agent is created and no row is lost.
- The per-request memo set before the row is written, so a failed row is not retried on a second pass: confidence 6, and a failed write would fail again.
- `DELETE` on a foreign ghost answers a holder 403 rather than 404, with no row: pre-existing, confidence 7, and ghost names carry eight random hex characters.

## Verification

An independent attacker pass produced the candidates. A second fresh verifier, given only the claims, tried to refute them and ran the real gate code: finding 2 and both outside items stood, and it raised finding 1 as an adjacent gap. Findings 1, 2 and 3 were then reproduced through real routers with real keys, and the `trinity_prompt` path through `deploy_manifest`. None of those probes reached the host's Docker daemon.

## Variant analysis

- Every `enforce_agent_capability` caller was walked. Three fences and the file routes carry the bound. Agent create names an agent that does not exist yet. The remaining caller targets the agent itself.
- `assert_skill_gates_readable` kept its own owner comparison, so a holder could read the system agent's gate map (read-only). Found in review and moved onto `owner_owns_agent` in this PR.
- An edge still gives the use tier on ordinary files. That is the ruled Files tier table (trinity-enterprise#820), not a capability path.

## Coverage gaps

- Docker pass not run (no image change).
- No dependency scanner run (no dependency change).
- PostgreSQL not exercised (`TEST_POSTGRES_URL` unset).
- Variant analysis stops at the capability paths named above.
