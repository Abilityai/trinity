# Terms of Use — per instance, shown once to every person on any door

> **Issue**: abilityai/trinity-enterprise#1107 (extends the ent#1054 invite door)
> **Requirements**: `docs/memory/requirements/auth.md` §2.13
> **Architecture**: `architecture/security.md` (the gate), `architecture/backend.md` (`routers/terms.py`, `services/terms_service.py`), `architecture/frontend.md` (the SPA), `architecture/database.md` (`terms_acceptances`)

## Overview

The terms of use are an instance setting with a shipped default. Every signed-in
person on a declared interactive session — an admin arriving through the panel, a
member in the Workspace, someone who came through public sign-in, an external client
on a Workspace portal session (ruling A(1)) — sees the current
version once on their first visit and cannot continue until they accept. A new
version is shown again at the next visit. An admin replaces the text in Settings ›
Access; one action restores the default.

## User Story

As an operator, I want every person who signs in to this Trinity to accept its terms
once, recorded with the version and time, so that nobody uses the instance without
having agreed to them — and as an admin, I want to replace the shipped text and see
who accepted what.

## Entry Points

- **UI**: `src/frontend/src/views/TermsAcceptance.vue` — the `/terms` screen the SPA opens on a `terms_required` refusal (platform session); `src/frontend/src/components/portal/PortalTerms.vue` — the same screen inside the Workspace for a portal session; `src/frontend/src/components/settings/TermsPanel.vue` — Settings › Access editor
- **API**: `GET /api/terms/current`, `POST /api/terms/accept`, `GET /api/terms/acceptances`, `PUT /api/terms`, `POST /api/terms/restore-default`
- **Gate**: every authenticated call, inside `dependencies.resolve_platform_user_unfloored` (`src/backend/dependencies.py:785`); a Workspace portal session inside `client_portal/portal_auth.py::get_portal_principal` (`terms_gate.enforce_portal_session`)

## Frontend Layer

### Components
- `views/TermsAcceptance.vue:158` — `onAccept()` posts the version read; `:185` `signOut()` — declining signs out, the copy says how to come back
- `components/portal/PortalTerms.vue` — the portal session's screen: `load()` (`store.loadTerms`), `onAccept()` (`store.acceptTerms`, emits `accepted` → `Portal.vue::bootstrap()`), `decline` → `Portal.vue::onSignOut()`; mounted by `views/Portal.vue` while `store.termsRefusal && store.portalToken`
- `components/settings/TermsPanel.vue:252` — `save()` (text + title, stamps a version); `:258` `restore()` (confirmed first); `:262` `saveSwitch()` (the switch alone; a failed save re-reads the toggle); `:216` the watcher keeps an unsaved draft across a store update
- `utils/termsGate.js:50` — `reactToTermsRefusal()` opens `/terms?redirect=<page>`; `:87` `notifyTermsRefusal()` — one reaction per burst, fed by every HTTP client's interceptor (`api.js`, `main.js`, `stores/clientPortal.js` for a platform session)

### State Management
- `stores/terms.js:31` — `load()` (`GET /current`); `:46` `accept(version)`; `save(patch)`, `restoreDefault()`, `loadAcceptances()` for the admin panel
- `stores/clientPortal.js` — `loadTerms()` / `acceptTerms(version)` on `portalHttp` with the portal bearer; `fetchRoster()` records the refusal as `termsRefusal` (#1082); `verifyCode()` sends `client: 'web'`, which is what makes the portal session a visit the gate asks
- `stores/auth.js` — `loginWithCredentials` / `verifyEmailCode` / the hand-off send `client: 'web'` (`SPA_CLIENT`), which is what makes the session a visit the gate asks

## Backend Layer

### Endpoints
- `src/backend/routers/terms.py` — `terms_caller()`: a platform principal through `get_optional_user`, or the Workspace portal session the bearer carries (`decode_portal_session` + the client-block check) as a `terms_service.PortalPerson`, else the resolver's own refusal
- `src/backend/routers/terms.py` — `current_terms()` (`assert_person` first, `@workspace_route` + `@terms_route`)
- `src/backend/routers/terms.py` — `accept_terms()` (`reject_non_interactive_principal` first, both marks; `terms_gate.assert_door_open`, then `terms_service.accept`, hooks after the response)
- `src/backend/client_portal/portal_auth.py::get_portal_principal` — the portal-token branch calls `terms_gate.enforce_portal_session(request, email, interactive=portal_session_is_interactive(token))` after `_reject_if_blocked` and before the slide
- `src/backend/client_portal/router.py::portal_auth_verify` → `service.portal_signin_verify(..., interactive=body.client == SPA_CLIENT)` → `dependencies.create_portal_session_token(..., interactive=True)` mints the claim; `renew_portal_session` carries it
- `src/backend/routers/terms.py:83` — `list_acceptances()` (`require_admin`)
- `src/backend/routers/terms.py:90` — `update_terms()`; `:110` `restore_default_terms()` (admin + interactive, audited by version)
- `src/backend/routers/auth.py:361` — `_session_claims()` mints `interactive: true` for `client=web` (`:374` `login`, `:734` `_complete_email_login`)
- `src/backend/routers/settings/generic.py:230` and `:741` — the four `terms_*` keys refused on the generic PUT and DELETE

### Business Logic
1. The resolver resolves the credential, then calls `terms_gate.enforce` (`services/terms_gate.py:244`) once.
2. `_is_person` (`:143`) and `_is_visit` (`:172`) decide whether this principal is asked at all; a marked route (`dependencies.terms_route`, `dependencies.py:1202`) is skipped. A portal session enters the same `enforce` through `enforce_portal_session` as a `PortalPerson` whose `interactive_session` is the token's own claim.
3. A registered provider answers through `_provider_decision` (`:187`, the fault policy — a `PortalPerson` is role `user`, so a provider fault is fail-closed for it); otherwise `terms_service.decision` (`services/terms_service.py`): the switch, the version, one acceptance lookup — `has_accepted` by account, or by email for a `PortalPerson`.
4. Outstanding → `refusal()` (`:133`), 403 with a code from `REFUSAL_CODES`.
5. Accepting: `assert_door_open` (`terms_gate.py:228`), then `terms_service.accept` (`terms_service.py`) — version check, INSERT (`user_id` NULL + email for a portal person), audit, hooks.
6. Editing: `set_terms` (`terms_service.py:140`) validates both fields, writes only on a change, stamps a version, records the author; `restore_default` (`:179`) deletes the three rows.

## The flow

```
Person signs in (any door)
  └─ first authenticated call → resolve_platform_user_unfloored        dependencies.py
     (a Workspace portal session: get_portal_principal                 client_portal/portal_auth.py
        → terms_gate.enforce_portal_session(email, interactive=claim))
       └─ terms_gate.enforce(request, user)                            services/terms_gate.py
            not a person (agent / connector / delegate / loopback) → pass
            not a visit → pass: a JWT without the `interactive` claim (CLI, curl, a
              harness, a pre-change token), a machine credential of operator-and-above
            route marked @terms_route (sign-out, /users/me, /terms/*) → pass
            provider registered? its decision (the invite door, then the OSS one)
            else terms_service.decision(user)                          services/terms_service.py
                 switch off → not required
                 acceptance row for (user, current version)? → not required
                 → 403 {"code": "terms_required", "version": v}
  └─ SPA interceptor (api.js / axios / portalHttp) → notifyTermsRefusal   utils/termsGate.js
       └─ router.push('/terms?redirect=<page>')                         views/TermsAcceptance.vue
     (a portal session: fetchRoster → store.termsRefusal → Portal.vue    components/portal/PortalTerms.vue
        mounts the screen in place of the roster, on the portal bearer)
            GET /api/terms/current  → title, text, version (the shipped default until replaced)
            tick + Accept → POST /api/terms/accept {version}
                 └─ terms_service.accept → INSERT terms_acceptances (UNIQUE user, version;
                                           a portal person: user_id NULL, UNIQUE email, version)
                                           audit AUTHORIZATION/terms_accepted {version, door}
                                           BackgroundTasks → run_acceptance_hooks (an optional
                                           module binds the invitation, boots a member's agent)
            → back to the page they came from (role-aware); a portal session re-bootstraps the Workspace
            Decline → sign out; the copy says how to come back (sign in again and accept)
```

## Data Layer

### Database Operations
- `db/terms.py` — `insert_terms_acceptance()`: INSERT, `None` on the UNIQUE (user, version) or, for a session row (`user_id` NULL), the partial unique (email, version) — the second accept is a no-op; emails stored lower-cased
- `db/terms.py` — `get_terms_acceptance()` (by account) / `get_terms_acceptance_by_email()` (a session row or an account's — the same person): the one lookup the decision makes; `terms_acceptances_for_email`, `list_terms_acceptances` for the record
- `system_settings` rows `terms_required` / `terms_title` / `terms_text` / `terms_version` through `db.get_setting_value` / `set_setting` / `delete_setting`
- Table `terms_acceptances`: SQLite `terms_acceptances_table` (`db/migrations.py`; rebuilds a development DB on the earlier NOT NULL shape, rows kept) + Alembic `0119_terms_acceptances` (`user_id` nullable, `uq_terms_acceptances_email_version ... WHERE user_id IS NULL`)

## The setting

| Key | Resolution | Written by |
|---|---|---|
| `terms_required` | absent ⇒ **on** | `PUT /api/terms {required}` |
| `terms_title` | absent ⇒ `terms_defaults.DEFAULT_TERMS_TITLE` | `PUT /api/terms {title}` |
| `terms_text` | absent ⇒ `terms_defaults.DEFAULT_TERMS_TEXT` (Markdown) | `PUT /api/terms {text}` |
| `terms_version` | absent ⇒ `terms_defaults.DEFAULT_TERMS_VERSION` (`default-1`) | stamped (ISO-Z) by every text/title save; deleted by restore-default |

The generic `PUT` and `DELETE /api/settings/{key}` both refuse the four keys (422), so the validated
route is the only writer: empty text `terms_text_empty`, over 200 KB
`terms_text_too_large`, a title over 200 chars `terms_title_too_long` — both fields validated
before either is written, so a refused title publishes nothing. A save of the text and
title already in force writes nothing and stamps no version. The admin who
saves a version is recorded as having accepted it (`door = "author"`), so they are not
bounced out of Settings by their own save; everyone else is asked at their next visit.
Restoring the default brings the constant version back, so anyone who accepted the
default before is not asked again (the text is byte-identical).

## Who is and is not gated

| Principal | Gated? | Why |
|---|---|---|
| JWT carrying `interactive: true` — the SPA's own sign-in (`client=web` on `POST /api/token`, `"client": "web"` on the email verify and the Workspace hand-off), any role (admin included) | yes | every person's first visit; setup resolves no principal, so the admin claim comes first |
| JWT without the claim — the CLI, curl, a CI harness, a test fixture, a session minted before the claim existed | no | the gate is a UX/record contract the client declares, not a security boundary (`terms_gate.py` module doc); a headless caller never accepts |
| Workspace portal session (the emailed-code sign-in, no `users` row) minted with `interactive: true` — the Workspace sends `"client": "web"` on `/auth/verify`; the claim is carried across rotation | yes — ruling A(1) | a Workspace person like any other; the acceptance is recorded by email (`user_id` NULL) through the terms routes on that same token; the screen is `PortalTerms.vue` inside the Workspace |
| portal session token without the claim — a harness, a delegate-minted session (an embedding, the person did not sign in through the Workspace) | no | the same ruling-A line: not a visit the client declared |
| Workspace-only person's own `user` key | yes | the key is the person (ent#1054) |
| operator-and-above `user` key, the system key | no | automation, not a visit — an ops key must not fail because the admin has not signed in since an upgrade |
| agent, connector, portal-delegate, loopback | no | the owner's agents and automations |
| channel bindings, public links, the shared-canvas viewer read (`workspace_email_from_request`) | not consulted | no platform principal / a route anonymous visitors may open |

Failure: the OSS decision fails closed for everyone; a registered provider's fault
fails closed for a Workspace-only person and is ignored for operator-and-above
(they face the OSS decision alone), so a broken optional module cannot lock an admin
out of the instance that could fix it.

## Side Effects
- Audit: `AUTHORIZATION/terms_accepted` `{version, door}`; `CONFIGURATION/terms_updated` / `terms_restored` `{version}` — never the text
- Acceptance hooks (`terms_service.register_acceptance_hook`) run after the response, once per new row; a hook that raises is audited `terms_hook_failed`
- No WebSocket broadcast

## Error Handling
- Outstanding terms → `403 {"code": "terms_required", "version", "message"}`; a closed invitation (provider) → `403 invite_expired` / `invite_revoked`, on every route and on the accept
- Stale version on accept → `409 version_stale` (the page reloads and asks for a fresh tick)
- Empty text → `422 terms_text_empty`; over 200 KB → `422 terms_text_too_large`; title over 200 chars → `422 terms_title_too_long` — nothing written on any of them
- The four keys on `PUT`/`DELETE /api/settings/{key}` → `422`
- The OSS decision unreadable → fail closed (`403 terms_required`); a provider fault → closed for a Workspace-only person, ignored for operator-and-above

## The admin surface

`components/settings/TermsPanel.vue` (Settings › Access): title, Markdown body,
"Save as a new version", "Restore the default" (confirmed, consequence restated),
the switch, the current version, and the acceptance record (account, version,
relative time with the absolute on hover; bounded, total stated) from
`GET /api/terms/acceptances`. Client-side named refusals in `utils/termsEditor.js`
mirror the server's.

## The seam for an optional module

`terms_gate.register_provider(provider)` — `provider.decision(user)` answers instead
of the OSS decision (it decides its door and delegates to `terms_service.decision`);
`provider.door_for(user)` names the invitation the OSS accept route stamps on the row;
`terms_gate.assert_door_open(user)` runs first on the accept, so a decision naming a closed
door (`invite_expired` / `invite_revoked`) refuses the acceptance with that code and the OSS
route is not a way around the module's door (W2).
`terms_service.register_acceptance_hook(fn)` — `fn(user_id, email, terms_version,
accepted_at)` after a new row, scheduled after the response; a hook that raises is
logged and audited (`terms_hook_failed`).

## Testing

### Prerequisites
- Backend running; an admin signed in through the SPA (the session carries the `interactive` claim)

### Test Steps
1. **Action**: sign in as a new person through the SPA
   **Expected**: the first authenticated call answers 403 `terms_required`; the SPA opens `/terms`
   **Verify**: `GET /api/terms/current` answers 200 with the shipped default; accept → the page returns to where the person was
2. **Action**: `curl -H "Authorization: Bearer <token minted without client=web>" /api/agents`
   **Expected**: 200 — a bare token is never gated
3. **Action**: Settings › Access, change the text, Save
   **Expected**: a new ISO-Z version; the saving admin is not asked; every other person is asked at their next visit
4. **Action**: Save the same text again
   **Expected**: no new version
5. **Action**: toggle the switch while a draft is in the editor
   **Expected**: the draft stays; only `required` is sent

### Unit and component tests

- `tests/unit/test_ent1107_terms_per_instance.py` — the default, the setting and its named refusals, the gate on an admin JWT (refused on a plain route, admitted on a marked one), accept → lifted, a new version asks everyone else again, the admin record, setup resolves no principal, `main.py` registers the router; ruling A(1): the session row (`user_id` NULL, one per email+version, the four DDL homes agree, the rebuild), the portal door refused until accepted / admitted on the switch off, a marked route or a bare token / delegate-minted session, the terms routes on the portal token record the email, by address not by token, the provider fail-closed and its door passed through, the verify route mints the claim only for `client: "web"`.
- `tests/unit/test_ent1054_terms_gate.py` — the seam: who is gated, a provider's own reasons, fail-closed vs the operator fallback, exempt routes, one consult in the resolver.
- `tests/unit/test_837_workspace_route_census.py` — the two terms routes are member pages.
- Frontend: `termsPanel.mount.spec.js` (editor, named refusals, restore confirmed, the switch, the draft across a switch save, a failed switch save, the record, decline copy), `termsAcceptance.mount.spec.js`, `termsRoute.spec.js`, `apiTermsRequiredInterceptor.spec.js`; `portalTerms.mount.spec.js` (the portal screen on the portal bearer), `portalTermsRefusal.spec.js` (`loadTerms` / `acceptTerms`, the refusal never reports to the /terms handler on a portal session), `portalTermsRefusal.mount.spec.js` (the shell: screen for a portal session, link for a platform one); `e2e/auth.setup.js` accepts through the API after the UI sign-in.

## Related Flows
- [workspace-only-role.md](workspace-only-role.md) — the operator floor the gate sits beside
- [admin-login.md](admin-login.md), [email-authentication.md](email-authentication.md) — the sign-ins that mint the `interactive` claim
- [role-model.md](role-model.md)
