# CSO diff audit: abilityai/trinity-enterprise#837 Workspace-only `user` role (2026-10-09)

Scope: `--diff` against `origin/dev`, daily mode (8/10 gate). Docker available. Two independent adversarial reviewers, then a fresh-context verifier for each claim that survived them.

**Result: 5 findings (1 high, 1 medium, 3 low), all fixed in this PR before its first push.** Seven pre-existing observations outside the diff were routed for triage: security-class ones to the private tracker, and none are described here.

## Surface changed
- **Auth.** `get_current_user` gains the operator floor (`enforce_operator_floor`).
  - It refuses role `user`, or a role outside the ladder, with 403 `workspace_only`.
  - Two exceptions: the 11 routes marked `@workspace_route` (browser session only), and an agent's own runtime routes.
  - The four Workspace doors resolve through `resolve_platform_user_unfloored`. The set of callers is pinned by a test.
- **WebSockets.** `/ws/events` and the agent terminal refuse the rung. `/ws` gives a Workspace-only socket a kind allowlist and a field projection.
- **Sharing.** Sharing and access-request approval stop writing the login whitelist. The admin whitelist form sends `default_role`; the route is admin-gated and the value is validated server-side.
- **SPA.** Role-based landing, a router-guard redirect, the 403 reaction, and Settings aids.
- **Not changed:** no new endpoint, SQL, dependency, Dockerfile, workflow, MCP tool or agent-shipped file.

## Findings (all fixed in this PR)
| # | Sev | Conf | Evidence | Finding | Fix |
|---|---|---|---|---|---|
| 1 | HIGH | 9 | SUPPORTED | Three added passages (two docs, one test docstring) described a pre-existing route in terms that disclose an unfixed weakness tracked privately | Reworded. The unpushed branch was collapsed into one commit, so the wording never reached public history |
| 2 | MEDIUM | 9 | SUPPORTED | `get_room_principal` resolved past the floor without refusing an agent principal. An agent of a Workspace-only owner could create rooms with its owner's agents and wake them: the agent-to-agent chat the floor refuses elsewhere | The door refuses an agent whose owner is Workspace-only (403 `workspace_only`). Room wakes run in-process, so agents still take part |
| 3 | LOW | 9 | SUPPORTED | The `/ws` kind allowlist narrowed kinds but not fields. `agent_activity` still carried prompt and reply previews, cost and session ids to a Workspace-only socket. Not a regression: the payload predates the branch | `allowed_fields` cuts each delivered copy to `type`, `event`, `agent_name` and `activity_state`, plus `_eid`, on fan-out and on replay |
| 4 | LOW | 9 | SUPPORTED | `_is_own_runtime_route` matched the raw path. An agent named like a static route segment (`GET /api/agents/slots`) passed the floor, and the ghost-agent fence, on that static route | The bound name must be a parameter of the route that matched |
| 5 | LOW | 8 | SUPPORTED | The SPA reacted to a 403 `workspace_only` on the body alone. An agent's own server can return that body through the files proxy and move an operator's tab | The reaction re-reads the profile and moves the tab only when the re-read role is Workspace-only |

Exploit scenarios, before the fixes:
- **(2)** Agent `atlas` belongs to an owner who was lowered to `user`. With its injected key, `POST /api/agents/sibling/chat` returns 403, but `POST /api/rooms {"agents": ["sibling"]}` followed by a message mentioning `@sibling` woke the sibling.
- **(3)** A member with one share mints a `/ws` ticket and reads the first 100 characters of every prompt run on the shared agent.
- **(4)** The creator of an agent named `slots` is lowered to `user`. The agent's key reads `GET /api/agents/slots`, which lists capacity fleet-wide.
- **(5)** A compromised agent answers a proxied file delete with that 403 body, and the operator's tab moves to `/workspace?agent=<name>`.

Each fix has a regression test, and each one was mutation-proved: reverted, the test turned red, then restored byte-identical.

## Checks
| Check | Result |
|---|---|
| Secrets or internal hosts in added lines | none |
| Invisible or bidi Unicode (every changed file) | none |
| `v-html` and other DOM sinks | none added |
| Starlette BadHost (CVE-2026-48710) | the new matcher reads `scope["path"]`; the marks read `scope["route"]` |
| Admin gate, human-only census, auth wiring, enumeration (#293, #2996, #1310, #186) | green |
| Enterprise-docs guard | no hit |
| Open redirect (`?redirect=`, `?agent=`) | held: 34 crafted values under 4 roles, and the origin never changed |
| Whitelist `default_role` | admin-only, validated against the role set, defaults to `user` |
| Keys on marked routes | refused: only an interactive JWT passes a mark |
| Floor failure direction | fails closed on an unmarked route, a missing route and an unknown role; stated in the comment above it |

## Filtered
- **Withdrawn:** "the floor does not state its failure direction". The comment above it does.
- **Below the gate (confidence 5), kept as a documented trade-off:** a Workspace-only account cannot list or revoke its own MCP keys; an admin can. The upgrade note says so.
- **Outside the diff:** seven pre-existing observations, routed for triage.

## Coverage gaps
- Phases 3 (dependencies), 4 (CI/CD) and 5 (infrastructure): nothing in the diff.
