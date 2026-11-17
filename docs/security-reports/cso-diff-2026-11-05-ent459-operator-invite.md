# /cso --diff: operator invitation email (trinity-enterprise#459)

- **Date**: 2026-11-05
- **Mode**: diff, daily confidence gate (8/10)
- **Scope**: `services/agent_invite_service.py`, `routers/sharing.py`, `db_models.py`, `models.py`, `agent_route_tiers.json`, `AccessPanel.vue`, `stores/agents.js`. No dependency, CI, Docker or schema change.
- **Single-reviewer mode**: no independent verifier subagent ran; the finding was proved by running it.

## Attack surface

One new route: `POST /api/agents/{agent_name}/share/{email}/invite`. `POST /api/agents/{agent_name}/share` gains an outbound email. New capability: a person can make the instance mail an address they typed.

## Findings

| # | Sev | Conf | Status | Finding | File |
|---|---|---|---|---|---|
| 1 | HIGH | 10/10 | FIXED in this diff | The free-text share field reached the mail provider as a recipient | `services/agent_invite_service.py` |

**1. Free-text share field used as a recipient.** `AgentShareRequest.email` is a plain `str`, which was harmless while a share sent nothing. This diff made it a recipient.

- **Exploit**: an agent owner posts `{"email": "a@x.com,b@y.com,c@z.com"}` or an address with a CRLF and a header. Before the fix, the value reached `send_email` unchanged. One quota slot could target several addresses, or carry a header, depending on the provider. Provider behaviour was not tested.
- **Fix**: `is_single_address` allows one plain address. A share still stands and reports `invalid_address`; resend refuses with 422 and spends no quota; the roster reports `invite_available: false`.
- **Evidence**: rerunning the probe sends nothing for all six hostile values. `TestOneAddressOnly` covers it, and removing each check, or loosening the pattern, turns a test red.
- **Not changed**: `POST /share` still accepts a non-address and writes the row, as it did before this diff.

## Checked, nothing to report

- **Auth**: `OwnedAgentByName` with a matching `{agent_name}`, plus `require_interactive` as a decorator dependency and inline. A shared user or stranger gets the uniform 404; agent keys and the owner's user key get 403. Removing both interactive layers turns tests red.
- **Enumeration**: `not_pending` and `not_shared` reach only the owner or an admin, who can already read the roster.
- **Mail content**: the subject is a constant. Inviter name and agent name go through `send_html`'s escaping. There is no code, token or secret in the mail.
- **Abuse**: 3 an hour per (agent, address) and 30 an hour per inviter, shared by share and resend. A share/unshare loop is held to the same window.
- **Audit**: every send attempt writes an `agent_invite` row; a refusal before sending writes none.
- **Secrets**: none in the diff.

## Below the gate (not findings)

- The failure log line names the recipient's address. `TENTATIVE`, 5/10.
- The inviter's display name is quoted in the mail up to 120 characters, so text in it reaches the recipient. No user-facing route to set it was found on this branch. `TENTATIVE`, 5/10.

## Totals

1 finding (HIGH), fixed before merge. 0 open.
