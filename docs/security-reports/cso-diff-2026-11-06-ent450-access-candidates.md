# /cso --diff: suggesting accounts in the Access tab's add field (trinity-enterprise#450)

- **Date**: 2026-11-06
- **Mode**: diff, daily confidence gate (8/10)
- **Scope**: `services/access_candidate_service.py`, `routers/sharing.py`, `db/users.py`, `database.py`, `models.py`, `agent_route_tiers.json`, `AccessPanel.vue`, `PortalTypeahead.vue`, `stores/agents.js`, `utils/accessCandidates.js`
- **Single-reviewer mode**: no independent verifier subagent ran; the finding was proved by running it.

## Attack surface

One new route: `GET /api/agents/{agent_name}/access/candidates?q=`. It reads `users`, which no non-admin could search before.

## Findings

| # | Sev | Conf | Status | Finding | File |
|---|---|---|---|---|---|
| 1 | MEDIUM | 10/10 | FIXED in this diff | A user-scoped key could search the instance's accounts | `routers/sharing.py` |

**1. A user-scoped key could search accounts.** The first draft gated the route with `require_person`, which refuses agent keys and passes user keys.

- **Exploit**: the owner of any agent uses their own MCP key to walk prefixes in a script, with no browser session.
- **Fix**: `require_interactive`, the gate on share and resend. Every key is refused.
- **Evidence**: `test_a_key_is_refused_even_the_owners_own[user]` failed against the draft and passes now. Swapping the gate back to `require_person` turns it red.

## Accepted risk

**An agent owner can find accounts by trying prefixes.** The people offered aren't on the agent yet, so the query can't be limited to people the agent already reaches. The bounds slow this down and don't prevent it: 2-character minimum, 8 rows, 60 lookups a minute per person, session only. Role is withheld from non-admins, so the list can't be used to find the admins. Accepted by the issue's owner when the plan was approved on 2026-11-06. It's recorded in the requirement and the feature flow.

The rate limit falls back to a per-worker count if Redis is unavailable, as every use of `rate_limiter` does.

## Checked, nothing to report

- **Auth**: `OwnedAgentByName` with a matching `{agent_name}`. A shared user, a stranger and an unknown agent all get the uniform 404. `test_186_enumeration_uniformity`, the dependency pairing guard and the route census pass.
- **Injection**: bound parameters, and `%`, `_` and `\` are escaped in the `LIKE`. The query is cut to 64 characters.
- **Disclosure**: the response model has only `email`, `label` and an admin-only `role`, and tests assert the exact rows. No password hash, id or last login.
- **Population**: suspended accounts and accounts with no email are excluded, on an agent with an empty roster too.
- **XSS**: rows render by interpolation. The diff adds no `v-html`.
- **Secrets and disclosure**: no secret patterns or real addresses in the diff, and the enterprise-docs guard pattern has no hits.
- **Audit**: reads aren't audited, consistent with the roster read beside it. No write path changes.

## Not run

Phases outside the diff: dependencies, CI, infrastructure, webhooks and LLM. The diff adds no dependency, workflow, container or prompt.
