# /cso --diff: an admin's user key acts for an agent (trinity-enterprise#1024)

- **Date**: 2026-11-05
- **Mode**: diff, daily confidence gate (8/10)
- **Scope**: `dependencies.py`, `models.py`, `services/ask_service.py`, `src/mcp-server/src/access.ts`, `client.ts` and the self-acting tool modules. No dependency, CI, Docker or schema change.
- **Single-reviewer mode**: no independent verifier subagent ran.

## Attack surface

No new route. One new request header, `X-Trinity-Act-As-Agent`, read in `get_current_user`, so it reaches every route behind that door. `agent_name` on the self-acting MCP tools.

## Findings

None at the gate.

## Checked

- **Privilege**: `admit_act_as` is an allowlist of one, a `user`-scoped MCP key whose user is an admin. Agent, system, connector, portal_delegate, ops and non-admin keys and a browser session get 403 `act_as_not_permitted`, through the real door. Test: `TestThroughTheRealDoor`; removing the door call turns it red.
- **Widening**: the header widens only the ask routes, which is the ruling. On `assert_own_agent` routes it narrows. Acting as `x` on `y`'s path gets 403.
- **Credentials**: the vault and `ask_for_credential` stay out of act-as.
- **Attribution**: each admitted request writes an `acted_as_agent` row naming the user and the agent. An on-behalf ask's `raised`, `withdrawn` and `replaced` rows carry `acted_by`.
- **Input**: the header is bounded (1-128 characters, else 400) and is compared and logged, never used in a query. `agent_name` reaches no request body.
- **Client state**: `asAgent` is a clone, so one call's target can't reach another's.
- **Secrets**: none in the diff.

## Below the gate (not findings)

- The door's row is written before the route decides, so its target can differ from the path acted on. Stated in `requirements/auth.md`. `TENTATIVE`, 5/10.
