# Feature: The Workspace-only `user` rung (trinity-enterprise#837)

## Overview

The platform role `user` means *a member who works with agents through the Workspace*.
The operator UI starts at `operator`. A Workspace-only principal — role `user`, or any role
outside `ROLE_HIERARCHY` — is refused on every operator route with one named 403; the
Workspace keeps working. Sharing an agent no longer creates a platform login.

**Implementation status**: PR 1 (OSS core: the floor, sign-in routing, sharing). PR 2,
after trinity-enterprise#810, adds the role control on the Access-tab row and the
companion-assignment check. Requirement: `requirements/auth.md` §2.10 (ROLE-002).

## Revision History

| Date | Changes |
|------|---------|
| 2026-10-09 | Initial flow (trinity-enterprise#837 PR 1) |

## User Story

As an admin, I add the people who answer an agent's asks as `user` members, and they work
in the Workspace without ever seeing the Dashboard, agent pages or Operations. As a person an
agent is shared with, I sign in to the Workspace with an emailed code; sharing does not make
me a platform account.

## Entry Points

- **Gate**: `src/backend/dependencies.py::get_current_user` → `resolve_platform_user_unfloored`
  + `enforce_operator_floor`
- **Workspace doors**: `client_portal/portal_auth.py::get_portal_principal`,
  `shared_sessions/router.py::get_room_principal`, `client_portal/router.py::portal_voice_start`
- **SPA**: `src/frontend/src/utils/workspaceOnly.js`, `router/index.js` (`beforeEach`:
  `confirmedRole` + `operatorRouteRedirect`), `views/Login.vue` (`afterLogin`)
- **Sharing**: `routers/sharing.py` — `share_agent_endpoint`, `decide_access_request_endpoint`

## Backend

### The operator floor

```
get_current_user(request, token)                    dependencies.py
  └─ resolve_platform_user_unfloored(request, token)   every scope fence, unchanged
  └─ enforce_operator_floor(request, user)
        is_workspace_only_role(user.role)?  no  → return     (operator, creator, admin)
        user.connector_agent set → return     (the ent#46 fence already confined it)
        user.agent_name set:
            _is_own_runtime_route(request, agent, _AGENT_SELF_ROUTES) → return
        else, an interactive session (JWT; is_interactive_principal):
            getattr(request.scope["route"].endpoint, WORKSPACE_ROUTE_ATTR) → return
        raise 403 detail=WORKSPACE_ONLY_DETAIL
```

- `role_level` / `is_workspace_only_role` (`dependencies.py`) sit beside
  `ROLE_HIERARCHY`; `require_role` shares `role_level`. A role outside the ladder ranks −1.
- `WORKSPACE_ONLY_DETAIL` = `{"code": "workspace_only", "message": "This account works in the Workspace. Open /workspace."}`.
- Every dependency built on `get_current_user` inherits the floor: `CurrentUser`,
  `AuthorizedAgent*`, `OwnedAgent*`, `require_role`, `require_admin`, `get_optional_user`
  (a refused `user` reads as anonymous on an unmarked optional-auth route),
  `get_user_or_anonymous` (re-raises the 403). Enterprise routes are covered the same way.
- The refusal reads only the principal and the matched route, so it fires before any agent
  lookup and discloses nothing (Invariant #8).

### Routes a Workspace-only person may reach — `@workspace_route` (browser session only)

`workspace_route(reason)` (`dependencies.py`) sets `WORKSPACE_ROUTE_ATTR` on the handler
and returns it unchanged; FastAPI 0.115 puts the matched `APIRoute` in `scope["route"]`.
Marked today (frozen in `tests/unit/test_837_workspace_route_census.py`):

| Route | Why |
|---|---|
| `GET /api/users/me` | the SPA routes on the role |
| `POST /api/auth/logout` | sign-out ends whichever credential is live (#2258) |
| `POST /api/ws/ticket` | `/ws` live updates for the Workspace stores (events scoped by agent, ent#467, and by kind, #837) |
| `GET /api/users/me/preferences`, `PUT …/{key}` | the conversation's UI preferences (the `DELETE` is the Dashboard grid's reset, so it stays unmarked) |
| `GET/POST /api/agents/{name}/loops`, `POST /api/loops/{loop_id}/stop` | the rail's loops |
| `GET /api/agents/{name}/voice/{session_id}/panel` | the voice canvas |
| `GET /api/public/canvas/{token}` | an `authorized` canvas link recognises the member |
| `GET /api/public/sessions/{token}`, `…/{session_id}` | the public chat page lists and reopens the person's own past chats on that link |

Not marked: `GET /api/agents/{agent_name}/files/preview`, so a canvas path image falls back
for a `user`, as for an external client.

Only an interactive session passes a mark: a key — even the person's own `user`-scoped one,
minted before the upgrade and no longer listable or revocable by them — is refused there and
scripts the Workspace through its own doors instead.

**`/ws` carries only the Workspace's kinds.** `resolve_ws_identity` gives a Workspace-only
account `allowed_types = WORKSPACE_WS_EVENT_TYPES` (`agent_activity`, `loop_run_completed`,
`loop_completed`, `agent_skills_changed`, `resync_required`), and the dispatcher withholds every
other kind — the operator queue, notifications, reports, room triggers, the agent lifecycle and
the sharing events — on live fan-out and on replay. Each event it does get is cut to
`WORKSPACE_WS_EVENT_FIELDS` (`type`, `event`, `agent_name`, `activity_state`) plus the `_eid`
cursor, so an activity's prompt and reply previews, cost and session ids never reach it. A
Workspace consumer for a new kind or field adds it there (`services/ws_identity_service.py`).

**Inside the Workspace a `user` keeps the team view.** Signed in with the platform login,
`get_portal_principal` resolves the account as a platform principal (`is_platform=True`), so the Workspace gives it what it gives every team
member: every canvas audience (`agent_page.canvas_audience_for`), the agent's whole activity, loop
runs included (`agent_page._client_scope`), voice and model choice. The narrower client view
(`roster` canvases, their own work) is for people without a platform account. Decided at #837;
nothing new is exposed, since a `user` saw all of it on Agent Detail before.

**Known limit (follow-up):** the loop routes keep their pre-#837 reach — a member lists every
loop on a reachable agent and may stop any of them.

### Review round 1 (PR #3488)

- **Connector keys** of a Workspace-only owner pass the floor: the ent#46 fence has already
  confined them to the bound agent's chat and playbook list, and like a public link they
  serve that agent's external consumers.
- **The self-route name is the matched route's parameter.** `_is_own_runtime_route` binds
  `{name}` only when `request.path_params` carries it, so a static route registered ahead of
  `/{agent_name}` that spells it (`GET /api/agents/slots`) is never an agent's own. This
  covers the ghost-agent fence too.
- **A shared canvas recognises a Workspace session.** With no platform credential,
  `GET /api/public/canvas/{token}` reads a Workspace session from the bearer token
  (`portal_auth.workspace_email_from_request`). It shows an `authorized` canvas only when the
  agent is on that person's roster and the canvas's audience is `roster`
  (`canvas_share_service.WorkspaceViewer`). The page sends the Workspace token when it has no
  platform one. "Sign in" goes to the Workspace with `?redirect=`, and the code sign-in returns
  there (`canvasReturnPath`, shared-canvas paths only). A Trinity account still has `/login`.
- **A whitelist grant replaces a row sharing wrote.** `add_to_whitelist` overwrites a row whose
  source is `agent_sharing` or `access_request` when the new grant comes from anywhere else. The
  form names an existing account's role, since a whitelist role applies only at a first sign-in.
  The SSO provisioning path still reads an existing row as-is, a private-repo follow-up.
- **The 403 reaction is role-checked.** `reactToWorkspaceOnly` awaits the profile re-read and
  moves the tab only when the re-read role is `user`. An agent's own server can return that
  body through the file proxy. It also reconnects `/ws` (`reconnectWebSocket`), because a
  socket keeps the scope it connected with.
- **`/m`** (the mobile admin signs in by itself) carries `meta.operatorSurface`, and the guard
  sends a signed-in member to the Workspace.
- **MCP and CLI name the refusal.** `client.ts` rethrows `workspace_only` from
  `getPermittedAgents` / `getAgentAccessInfo` instead of reading it as "none". `trinity init`
  exits non-zero with the reason (`trinity_cli/refusals.py`).

### An agent never exceeds its owner

`_AGENT_SELF_ROUTES` (`:988`) = `_EPHEMERAL_ALLOWED_ROUTES` (heartbeat, result delivery,
reports, notifications, self-info) + `POST /api/agents/{name}/operator-queue`,
`GET /api/agents/{name}/operator-queue/{id}` and `POST /api/skill-gate/check` (the hook fails
closed on a refused check). `{name}` must equal the key's agent. Heartbeat and result delivery
authenticate outside `get_current_user` anyway. Everything else an agent of a `user` owner
did is refused until an admin raises the owner's role — there is no ownership transfer, and the
agent's key stays bound to the account that minted it.

### Workspace doors

The Workspace is where the rung belongs, so its doors resolve the credential through
`resolve_platform_user_unfloored`: `get_portal_principal` (every `/api/enterprise/client-portal/*`
route, Workspace asks/work/suggestions, Workspace projects), `get_room_principal` (rooms — a
`user` stays a `User`, not a portal fallback; an agent whose owner is Workspace-only is refused
there, since rooming with its owner's agents is agent-to-agent chat), and `portal_voice_start`
(its user id). The caller
set is pinned by `test_837_workspace_only_floor.py::test_the_unfloored_resolver_has_exactly_the_pinned_callers`.

### WebSocket doors

| Route | Disposition |
|---|---|
| `/ws` | ticket from the marked `POST /api/ws/ticket`; events scoped to accessible agents and, for a Workspace-only account, to `WORKSPACE_WS_EVENT_TYPES` |
| `/ws/events` | `main.py::websocket_events_endpoint` refuses a Workspace-only owner at the handshake, before `accept()` (the client sees HTTP 403) |
| `/api/agents/{agent_name}/terminal` | `services/agent_service/terminal.py` closes 4003 for a Workspace-only role, after the JWT decode and before any agent lookup |
| system-agent terminal | admin only |
| `/ws/voice/{id}` | bound to the voice session's owner (#600) |
| VoIP media stream | provider-authenticated |

### Sharing

`share_agent_endpoint` and `decide_access_request_endpoint` no longer call `add_to_whitelist`.
The share row is the Workspace access (`client_portal/service.py::email_has_access`). The
manual whitelist (`routers/settings/whitelist.py`, `default_role` from the new picker), CLI
self-signup and SSO first sign-in keep writing.

## Frontend

| File | Change |
|---|---|
| `utils/workspaceOnly.js` | `isWorkspaceOnlyRole` (exactly `'user'`; an unknown role is not), `landingAfterSignIn`, `operatorRouteRedirect`, `isWorkspaceOnlyRefusal`, `reactToWorkspaceOnly`, `setWorkspaceOnlyHandler` / `notifyWorkspaceOnly` (one reaction per 2 s burst), `WORKSPACE_ONLY_ROLE_LABEL` |
| `router/index.js` | on a `requiresAuth` route a `user` goes to `/workspace` (`/agents/:name/*` → `/workspace?agent=:name`), before the entitlement branch; a cached `user` role is confirmed with `GET /api/users/me` first, so an account raised since its last sign-in is not bounced; authenticated `/login` → role landing |
| `views/Login.vue` | `afterLogin` and the SSO callback use `landingAfterSignIn`; a pointer "Was an agent shared with you? Sign in to the Workspace" |
| `main.js`, `api.js` | response interceptors report a 403 `workspace_only` to the handler: re-read the profile, `router.replace('/workspace')` unless already on a Workspace / canvas / chat / sign-in page (path via `pathForVerdict`, #3406) |
| `utils/websocket.js` | for a `user` the operator stores (agents, notifications, operator queue, executions) do not follow `/ws`; the Workspace stores still do |
| `views/Portal.vue`, `components/portal/PortalSidebar.vue` | an empty roster shows a `user` the ask-for-a-share copy, not "Go to your agents" / "Create one →" |
| `views/Settings.vue` | Email Whitelist: role picker (default `user`) + Role column; tip rewritten. User Management: legend and pickers read "user — Workspace only"; a one-line count of Workspace-only accounts with a **Show only these** filter |
| `components/settings/SsoPanel.vue` | the default-role picker's `user` option relabelled |
| `components/AccessPanel.vue` | copy: adding a person grants Workspace access; the operator UI is the platform role |

## Sequence — a `user` signs in

```
/login → POST /api/auth/email/verify → _finalizeLogin → GET /api/users/me (marked) → role "user"
      → landingAfterSignIn(redirect, "user") → /workspace
/workspace → App.vue connect() → POST /api/ws/ticket (marked) → /ws
           → GET /api/enterprise/client-portal/my-agents (portal door) → roster
operator URL → router guard → /workspace          any operator route → 403 workspace_only
```

## Error Handling

| Case | Behaviour |
|---|---|
| `user` on an operator route | 403 `workspace_only`, before any agent lookup |
| route not marked, or no matched route | refused (fail closed) |
| agent of a `user` owner off its runtime surface | 403 `workspace_only` |
| stale cached role in the SPA | the interceptor re-reads the role and moves the tab to `/workspace` |
| role not yet loaded | the guard does not redirect; the API refuses what it must |

## Security Considerations

- The floor is the boundary; the SPA only spares a member the wall.
- `resolve_platform_user_unfloored` is the bypass by design; its callers are pinned.
- A `user`'s MCP keys are refused on operator routes and `/ws/events`; the account cannot
  manage keys (an admin revokes them).
- Upgrade runbook: `docs/migrations/WORKSPACE_ONLY_USER_ROLE_2026-10.md`.

## Testing

- `tests/unit/test_837_workspace_only_floor.py`, `tests/unit/test_837_workspace_route_census.py`,
  `tests/unit/test_837_ws_workspace_event_kinds.py`
- `tests/test_access_control.py::TestWorkspaceOnlyRung` (live)
- Frontend: `workspaceOnly.spec.js`, `workspaceOnlyRouterGuard.spec.js`,
  `websocketWorkspaceOnly.spec.js`, `apiWorkspaceOnlyInterceptor.spec.js`,
  `loginRedirect.spec.js`, `portalSidebarWorkspaceOnly.mount.spec.js`

Manual: whitelist an email with role `user`, sign in at `/login` → `/workspace`; open
`/agents/<name>` → `/workspace?agent=<name>`; `curl -H "Authorization: Bearer <jwt>"
/api/agents` → 403 `workspace_only`; share an agent with a new email → no whitelist row.

## Related Flows

- [role-model.md](role-model.md) — the ladder and `require_role`
- [agent-sharing.md](agent-sharing.md) — the Access tab and shares
- [email-authentication.md](email-authentication.md) — the whitelist and `/login`
- [workspace-session-signout.md](workspace-session-signout.md) — sign-out by principal
