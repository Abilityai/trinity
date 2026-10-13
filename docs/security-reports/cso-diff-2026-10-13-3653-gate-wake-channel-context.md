# /cso --diff: trinity#3653 gate wake-up channel context

**Date:** 2026-10-13 · **Mode:** daily (8/10 gate), diff · **Branch:** `feature/3653-gate-wake-channel-context` vs `dev` · **PR:** #3657

## Scope

Eight files: `services/skill_gate_service.py`, `services/task_execution_service.py`, comments in `services/turn_audience.py` and `routers/agents.py`, three test files, one flow doc. No endpoint, dependency, workflow, Dockerfile, compose, migration or MCP tool changes.

## What the change does to the trust model

An agent that requests a gated skill during a turn that came from a conversation has that turn's channel context recorded on the gate record. Wake-up runs that report an ending then carry the context, and `channel_completion_report` posts the agent's reply into the conversation.

New data flow: outcome text (gate outcome, a rejecting approver's note, up to 1000 characters of the gated run's reply) → requesting agent's prompt (existing, framed as "not an instruction") → requesting agent's reply → the person's conversation (new destination for this run).

## Findings

None at or above the confidence gate.

## Checked

| Area | Check | Result |
|---|---|---|
| A01 / ASI03, destination provenance | `_requester_chat` takes the context only from an execution that is the requesting agent's own and still `running` at request time, the rule `_inherited_channel_context` applies. Mutation: removing the `running` check or the own-agent check each turns one test red. | holds |
| A01, recipient check at delivery | `_resolve_portal` re-reads the session row and requires `source_channel_client` to equal the session's client; Slack and Telegram resolvers apply their consent checks. Unchanged by the diff. | holds |
| ASI03, grants on the wake-up row | The row always carries `source_channel_agent`, so `turn_audience._is_delegated` reads it as delegated: file shares are owner-only, the voice route answers `delegated_turn`. | no new grant |
| ASI01 / ASI07, third-party text | The wake-up text keeps its existing framing and bounds. The `skill_gate` trigger is exempt from re-gating as before. | unchanged |
| A10, failure direction | `_requester_chat` returns no context on any error or missing field; stated in its docstring. Direction: a missing report, never a misaddressed one. | closed, stated |
| A05 injection | No SQL, shell, YAML or template construction added. | n/a |
| Secrets | Known-prefix and URL-userinfo scan over `git log -p` for the branch. | no hits |
| Concealed Unicode | Tag-block, zero-width and bidi scan over changed files. | no hits |
| Enterprise disclosure | Guard pattern over added doc lines. | no hits |
| Guard tests | `test_293_admin_gate_rejects_agent_keys`, `test_1310_auth_wiring`, `test_ent614_source_agent_attribution`, `test_186_enumeration_uniformity`, `test_ent457_inheritance_liveness`, `test_ent457_portal_completion_report`, `test_292_cmdline_credential_leak`: 213 passed. `DISPATCH_FIELDS` parity passes with the new keyword. | green |

## Hypotheses (not counted)

- **Concurrent live turn of another client.** An agent serving client Y that cites the id of client X's turn while X's turn is running would address Y's outcome reply to X's thread. Requires the agent to present another turn's id as its own; the platform-injected id is read first. Same residual `_inherited_channel_context` documents for delegation; closing it needs a runtime-injected turn identity the agent cannot choose.
- **Relay of approver's note.** A rejecting approver's free-text reason reaches the requesting agent's prompt (existing) and the agent may now repeat it in an external client's conversation. Product decision, stated in the PR.

## Coverage gaps

- Phases 3, 4, 5, 8: no changed files in scope; not run.
- No Docker pass, no live exploit attempt; analysis is code reading plus unit-level mutation.
- Independent verifier: none spawned, as no finding passed the gate. A separate fresh-context review of the diff covered auth and disclosure and reported no critical or high finding.
