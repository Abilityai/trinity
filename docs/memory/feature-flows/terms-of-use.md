# Terms of Use — per instance, shown once to every person on any door

> **Issue**: abilityai/trinity-enterprise#1107 (extends the ent#1054 invite door)
> **Requirements**: `docs/memory/requirements/auth.md` §2.13
> **Architecture**: `architecture/security.md` (the gate), `architecture/backend.md` (`routers/terms.py`, `services/terms_service.py`), `architecture/frontend.md` (the SPA), `architecture/database.md` (`terms_acceptances`)

## What it is

The terms of use are an instance setting with a shipped default. Every signed-in
person — an admin arriving through the panel, a member in the Workspace, someone
who came through public sign-in — sees the current version once on their first
visit and cannot continue until they accept. A new version is shown again at the
next visit. An admin replaces the text in Settings › Access; one action restores
the default.

## The flow

```
Person signs in (any door)
  └─ first authenticated call → resolve_platform_user_unfloored        dependencies.py
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
            GET /api/terms/current  → title, text, version (the shipped default until replaced)
            tick + Accept → POST /api/terms/accept {version}
                 └─ terms_service.accept → INSERT terms_acceptances (UNIQUE user, version)
                                           audit AUTHORIZATION/terms_accepted {version, door}
                                           BackgroundTasks → run_acceptance_hooks (an optional
                                           module binds the invitation, boots a member's agent)
            → back to the page they came from (role-aware)
            Decline → sign out; the copy says how to come back (sign in again and accept)
```

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
| Workspace-only person's own `user` key | yes | the key is the person (ent#1054) |
| operator-and-above `user` key, the system key | no | automation, not a visit — an ops key must not fail because the admin has not signed in since an upgrade |
| agent, connector, portal-delegate, loopback | no | the owner's agents and automations |
| channel bindings, public links | not consulted | no platform principal |

Failure: the OSS decision fails closed for everyone; a registered provider's fault
fails closed for a Workspace-only person and is ignored for operator-and-above
(they face the OSS decision alone), so a broken optional module cannot lock an admin
out of the instance that could fix it.

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

## Tests

- `tests/unit/test_ent1107_terms_per_instance.py` — the default, the setting and its named refusals, the gate on an admin JWT (refused on a plain route, admitted on a marked one), accept → lifted, a new version asks everyone else again, the admin record, setup resolves no principal, `main.py` registers the router.
- `tests/unit/test_ent1054_terms_gate.py` — the seam: who is gated, a provider's own reasons, fail-closed vs the operator fallback, exempt routes, one consult in the resolver.
- `tests/unit/test_837_workspace_route_census.py` — the two terms routes are member pages.
- Frontend: `termsPanel.mount.spec.js` (editor, named refusals, restore confirmed, the switch, the record, decline copy), `termsAcceptance.mount.spec.js`, `termsRoute.spec.js`, `apiTermsRequiredInterceptor.spec.js`.
