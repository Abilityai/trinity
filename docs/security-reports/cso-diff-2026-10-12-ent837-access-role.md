# CSO diff audit: abilityai/trinity-enterprise#837 PR 2, the platform role on the Access tab (2026-10-12)

Scope: `--diff` against `origin/dev` (`411e1d01a`), daily mode (8/10 gate), cso v1.2.3. The public half of the
change; the private-module half (the endpoint that creates a platform account before a first sign-in) was
reviewed with the private change and is not described here. Two independent reviewers (a pre-landing review
and an adversarial security pass), both fresh-context.

**Result: 2 findings (1 high, 1 medium), both pre-existing and fixed in this PR. No finding in the new code
reaches the gate.** Pre-existing
observations outside the diff were routed for triage; none are described here.

## Surface changed
- **Auth.** `PUT /api/users/{username}/role` now refuses every key with `reject_non_interactive_principal` before
  anything else, and writes one `role_change` audit row (from → to) on a change. Refusals and no-ops write none.
- **SPA.** Agent Detail › Access gains a platform-role select on each other Active person's row (every change
  confirmed; an admin's own row stays read-only, as the route refuses a self-change) and a "Give a platform account"
  control on a Pending row (confirmed; it calls the private endpoint above). "People this agent serves" shows each
  person's role and, after writing an assignment, shares the agent with that person through the existing share route
  (never the owner, the admin themselves, or an account with no email).
- **Not changed:** no new OSS endpoint, SQL, migration, dependency, Dockerfile, workflow, MCP tool or agent-shipped file.

## Findings (fixed in this PR)
| # | Sev | Conf | Evidence | Finding | Fix |
|---|---|---|---|---|---|
| 1 | HIGH | 9 | SUPPORTED | The role route's only gate was `require_admin`, which admits `trinity-system`'s key and an admin's user-scoped key (`ADMIN_GATE_SCOPES` includes `system` and `user`). Either could promote any account to `admin` over REST, though the router's `# mcp:` header calls the surface human-only. Pre-existing; the Access tab puts a control over this route, so it is closed here | `reject_non_interactive_principal(current_user)` is the route's first statement; `test_837_role_change_audit.py::TestOnlyAPersonChangesARole` (system, user-scoped and agent keys → 403, role unchanged, no audit row; an interactive admin → 200). Mutation-proved: removing the line turns two cases red |

| 2 | MEDIUM | 8 | SUPPORTED | A role change updated only `users.role`. The person's whitelist row kept the old role, and that row is what an account created for the address gets — again after the account re-binds to another address. A demoted person could re-bind away (`PUT /api/users/me/email`, mailbox-proven for the new address) and sign in with the old address as a NEW account at the old role. Pre-existing; the Access tab makes lowering a role routine | The route caps the row at the new role before writing it (`db.cap_whitelist_default_role` — lowered, never raised, so a promotion does not arm the old address either; no-ops heal a stale row; a failed write leaves the role unchanged); `test_837_role_change_audit.py::TestTheWhitelistRowFollowsTheRole` replays the re-bind in both directions. Mutation-proved |

Exploit scenarios, before the fixes:
- **(1)** A prompt-injected `trinity-system` (or a leaked admin user-scoped key) calls
`PUT /api/users/<any>/role {"role": "admin"}` and the account it names becomes an admin.
- **(2)** Bob was whitelisted as `admin`; an admin lowers him to `operator`. Bob binds his account to a second
  mailbox, signs in again with the first address, and gets a new account as `admin`.

No CLI command or MCP tool calls the route (checked); a script that changed roles with a key now gets 403, which the
upgrade note says.

## Checks
| Check | Result |
|---|---|
| Secrets, internal hosts, invisible or bidi Unicode in added lines (both repos, 2,574 lines) | none |
| `v-html` / `innerHTML` / `:href` added | none; the new dialogs and notes render text interpolation only (`ConfirmDialog` `{{ title }}` / `{{ message }}`) |
| Starlette BadHost (CVE-2026-48710) | the audit's `endpoint` reads `request.scope["path"]`; no new path-based fence |
| Event-loopback JWT | fenced to `POST /api/agents/{x}/task` in `get_current_user`, so it never reaches either changed route |
| Admin gate, human-only census, auth wiring, enumeration, Workspace floor and route census, ent#720 email binding (#293, #2996, #1310, #186, #837, ent#720) | 245 passed |
| Share-on-assign | agent pinned before the first await; the share runs only after the assignment is written; a 409 is "already shared"; the system agent has no Access tab and is excluded from every Workspace roster |
| Public-docs disclosure (enterprise-docs guard pattern replayed over every added doc line) | 0 hits; no module, route or gating words |
| CSRF | not exposed: bearer-header auth only |

## Coverage gaps
- **Dependencies / images / CI:** the diff changes none, so no scanner or Docker pass ran (diff mode). The stack CVE
  quicklist rows were not re-assessed.
- **Private module:** the OSS route census does not walk private-module routes; the private endpoint's gate is pinned
  by its own suite (the gate matrix through the real router, mutation-proved).

## Trend
Previous diff report on this issue: `cso-diff-2026-10-09-ent837-workspace-only-role` — 4 findings, all fixed in PR 1;
none recur. New: 2 (fixed). Persistent: 0.
