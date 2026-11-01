# CSO diff audit — #4121 write door serves the live turn

- **Version / mode:** cso 1.2.3, `--diff`, daily gate (8/10)
- **Scope:** `feature/4121-write-door-live-turn` vs merge-base `3ca71147a`
- **Changed:** `routers/public_memory.py`, `routers/agent_memory.py`, `services/memory_service.py` (a moved helper), `src/mcp-server/src/tools/memory.ts`, docs, tests.

## Attack surface

No new endpoint, MCP tool, WebSocket channel, credential, schema or dependency. One existing route narrows: it now refuses humans (`agent_only`), finished runs (`memory_turn_not_live`) and a run beside a live turn of another scope (`memory_turn_ambiguous`).

## Checks

| Area | Result |
|---|---|
| P2 secrets | Diff grep for known credential prefixes, emails, private IPs: only `example.com` test placeholders and a `10.0.0.1` anonymous-row fixture. No hidden Unicode. |
| P3 supply chain | No dependency change. |
| P4 CI/CD, P5 infra | No workflow, Dockerfile or compose change. |
| P6 integrations | `write_user_memory` keeps the self-gate first (`assert_own_agent`, `test_ent840_agent_self_gate.py` green). A sibling agent's key is refused before anything is read. |
| P7 ASI03 | The route stays classified agent-callable; it only loses principals. `test_2996_human_only_routes.py`, `test_293_admin_gate_rejects_agent_keys.py`, `test_1310_auth_wiring.py` green. |
| P7 ASI06 | Closes the persistent-memory write the issue reports: a finished run can no longer be named, so a later turn cannot replace another person's injected notes. The screen and envelope are unchanged. |
| P9 A01 | The named row is still checked for existence and agent before anything else. `test_186_enumeration_uniformity.py` green; the two turn refusals come after the agent match, so they disclose nothing about another agent's rows. |
| P9 A09 | Both turn refusals log the code, agent and execution, never the text. |
| P9 A10 | Failure directions: a row with no status is refused; `another_live_scope` states and tests fail-closed on an unreadable ledger; `owner_email` failing yields the person's own address, so the owner's turn beside a headless run refuses rather than passes. No `getattr` default makes an absent value the privileged one. |
| MCP | `resolveExecutionId` puts the platform header over a typed id on both tools, the shape every other effect tool has. A forged header is no worse than a typed id, since both still meet the ledger rule. `access.ts` fence unchanged. |

## Findings

None in the diff.

Candidates considered and dropped:
- A headless run naming the owner's live turn writes the owner's notes. The filed rule compares scopes, and `scope_for` folds the owner in. Decided behaviour, pinned by a test and stated in the feature flow.
- A process outside any turn that holds the agent's key can name the one live run by calling the API directly. The ent#926 ledger rule never claimed to prove the caller is in the turn; the header is a claim. Stated in the feature flow.

## Variant analysis

Outside `--diff` scope.

## Coverage gaps

- Docker pass not run (no image change).
- No dependency scanner run (no dependency change).
- Variant analysis outside this diff.
