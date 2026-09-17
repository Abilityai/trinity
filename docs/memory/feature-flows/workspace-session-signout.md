# Feature: Workspace session identity — two ways in, one way out

> **Issues**: ent#357 (implicit platform entry), trinity#2258 (sign-out) · **Status**: shipped
> **Related**: [workspace-absorbs-session.md](workspace-absorbs-session.md) (the turn engine), [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the sidebar the button lives in), [architecture.md → Workspace / Client Portal](../architecture/workspace.md#workspace--client-portal-epic-ent78-oss-core-since-ent356)

## The model

The Workspace (`/workspace`, `views/Portal.vue`) is one surface with **two kinds of
principal**, resolved on the backend by `client_portal/portal_auth.py::get_portal_principal`
from whichever credential the request carries:

| Principal | Credential | Where it lives | How it got there |
|---|---|---|---|
| **external client** | portal session token | `localStorage['trinity.portalToken']` | 6-digit email code (`POST /auth/verify`) |
| **platform user** | the platform JWT | `localStorage['token']` + `axios.defaults.headers.common['Authorization']` | the ordinary Trinity login |

ent#357 made the second kind **implicit**: a signed-in platform user reaches the
Workspace in one click, no OTP, because their platform session *is* a workspace
session. In `stores/clientPortal.js`:

```
authHeader        = portalToken ? Bearer portalToken : authStore.authHeader   // portal wins
isPlatformSession = !portalToken && authStore.isAuthenticated
isClientSignedIn  = !!portalToken || isPlatformSession
```

The store comment reasons about one direction and is right: *signing out of the
platform ends the Workspace session by construction* — there is no second
credential to revoke.

## The bug (#2258): the same derivation runs the other way

`signOut()` cleared only the portal token. Read the predicate again: removing the
portal token is **exactly what activates the platform fallback**. Two variants:

1. **Platform user clicks Sign out.** No portal token to clear → `isClientSignedIn`
   stays true → the shell stays mounted with an emptied roster, blank email, no
   threads. Refresh restores everything.
2. **External client signs out on a browser that also holds a platform login.**
   Their portal token is dropped, then the store falls back to the platform header
   — re-authenticated as the operator, with the operator's email and roster.

Not a credential leak (the JWT is the browser owner's own credential and the backend
re-scopes to whichever it is handed) — an identity/UX defect and a shared-machine
hazard: "Sign out" was a button that did not sign the user out.

## The fix: destroy the credential, never a derivation of it

```
Portal.vue::onSignOut  (signingOut ref guards re-entry + holds the frame)
   └─ store.signOutEverywhere()                          ← the ONE decision, unit-testable
        ├─ wasPlatform = isPlatformSession                (read BEFORE either clear)
        ├─ if authStore.isAuthenticated: await authStore.logout()   ← platform credential FIRST
        ├─ signOut()                                      (portal state; unchanged primitive)
        └─ return wasPlatform ? '/login' : '/workspace'
   ├─ '/login'    → router.push  (an operator signed out of Trinity)
   └─ '/workspace'→ escapeStage() (a client lands on the OTP form; ?agent= stripped, #2158)
```

Three properties are load-bearing, each pinned in `tests/unit/workspaceSession.spec.js`
/ `workspaceSignOut.spec.js`:

- **Order.** The platform credential ends *first*, so there is no window where
  `portalToken` is gone while `isAuthenticated` is still true — that is the exact
  state in which `authHeader` would hand an in-flight portal poll the operator's
  identity.
- **`signOut()` stays the plain state-clearing primitive.** `endSession({expired})`
  calls it from the 401 path, and an *expired* portal session must never end a
  platform session — expiry is not a user act, and it would log out an operator
  working in another tab. Only the explicit click reaches `signOutEverywhere`.
- **`auth.logout()` clears local state before the network revoke.** Two readers key
  on `localStorage['token']`: the global 401 interceptors (`api.js`/`main.js`) decide
  whether to bounce to `/login`, and the router guard sends `/login → /` while
  `isAuthenticated`. Revoke-first meant an already-expired JWT's 401 re-entered
  `logout()` and pushed the *operator* login at a client, and an un-awaited
  `logout(); push('/login')` (NavBar, since #187) lost the race to the dashboard.
  The revoke still carries the token — it rides the axios default header, deleted
  only after the call.

The sidebar footer is **one button** (no v-if swap — the #2159 focus lesson) whose
accessible name comes from `portalUtils.signOutLabelFor(isPlatformSession)`:
"Sign out of Trinity" for a platform user (it ends the platform session and says
so), "Sign out" for a client. Never "Leave workspace" — that promises navigation
while leaving a live credential in a browser the person just asked to leave.

## Why not the suppression flag the issue suggested

The issue's Technical Notes prescribed a persisted "suppress the platform fallback"
flag that `isPlatformSession` honours until the next explicit sign-in. Three
independent reviews converged on it before the fourth read `auth.js`:

- The platform JWT is installed as an **axios default header**, and per-request
  headers merge over defaults — omitting `Authorization` deletes nothing. Under a
  flag the OTP form renders while every portal request still carries the
  operator's JWT and `get_portal_principal` still answers as the operator. **The
  flag hides the disclosure instead of removing it.**
- `streamPortalExecution` uses bare `fetch` with `authHeader` only, so that one
  path would genuinely lose auth and 401 mid-stream while every axios path stayed
  authorized.
- `POST /auth/verify` 401s on a wrong code; `main.js` bounces to `/login` when
  `onWorkspace && localStorage['token']`. One mistyped digit would throw the
  signed-out client onto the operator login and destroy the operator's token.

A *correct* flag carried eight further obligations (reactive Pinia hydration, a
cross-store clear with an import cycle, three copies of the bounce predicate to move
in lockstep, `authHeader` divergence, an escape hatch for an operator dead-ended at
the OTP form, storage-write failure, cross-tab staleness). Credential destruction
carries none, and leaves the 401 predicate correct **by construction**.


## The expiry path (#2261)

Expiry still cannot end the platform credential — an operator working in another
tab must not be logged out because a client's idle window lapsed — so the fix
breaks the *derivation* instead, for the tab where the client's session died.

**Two halves, and neither works alone.**

1. **The wire.** Every workspace call moved onto a dedicated axios instance
   (`portalHttp`), whose request interceptor **discards any `Authorization` on the
   config and rebuilds it from the store** — so the store is the only source, and
   `authHeader` returning `{}` while suppressed means nothing goes out.

   Two things worth stating precisely, because the first version of this got the
   second one wrong. *(a)* Verified against axios 1.19.0: an instance created
   before `auth.js` mutates `axios.defaults.headers.common` does **not** inherit
   that mutation, so moving onto the instance is what actually closes the
   disclosure (pinned by `tests/unit/axiosInstanceIsolation.spec.js` against real
   axios, so a version bump that changes it is visible). *(b)* The interceptor
   originally *preserved* whatever `Authorization` it found, which cannot tell
   "the store decided this" from "axios inherited this" — it would have kept an
   inherited JWT rather than stripping it. It is now unconditional, so the
   property does not rest on merge behaviour we neither control nor test. This is the half that answers the objection above: with
   it, "the workspace is signed out" is a statement about the wire, not only the
   screen. (`streamPortalExecution` uses bare `fetch` with `authHeader` only, so it
   was already honest and stays so.)
2. **The screen.** `platformFallbackSuppressed` — set when a client session
   EXPIRES here, cleared by `signOut()`, by a client signing in again, and by the
   operator's explicit "Continue as <email>". `isPlatformSession` returns false
   while it is set. Stored in **sessionStorage**: it must survive a refresh of this
   tab and nothing wider, which is also what keeps an operator's other tab
   untouched.

**The escape hatch is mandatory, not a nicety.** The suppression fails closed, so
it necessarily catches the operator when the browser is genuinely theirs. One
explicit click (`continueAsPlatform`) is the way back; re-deriving automatically is
the bug.

**The 401 bounce moved with the transport, and onto a better question.** The global
`axios` interceptor in `main.js` decided with `onWorkspace && localStorage['token']`.
Workspace calls no longer pass through it, so the operator bounce is re-registered
via `setPlatformSessionLostHandler` and fires only when `isPlatformSession` — which
is false in exactly the case that predicate got wrong: a client at the OTP form on a
browser holding an operator's JWT, where one mistyped digit would have 401'd, bounced
them to `/login` and destroyed the operator's session (objection 3 above).

**`resumePath` is finally read.** `endSession` has recorded where the client was
since ent#375 and nothing consumed it, so the expired notice's "pick up where you
left off" was a promise the app did not keep; the sign-in that consumes the expiry
now navigates there. The expired notice itself was also unrendered until #2261 — the
form simply reappeared, indistinguishable from "you were never signed in".

**Degradation:** if `sessionStorage` is unavailable (private mode), the marker reads
as absent — pre-#2261 behaviour, rather than a workspace nobody can enter.

## One credential, one verdict, one handler (#2791)

Everything above is about *which session a tab is in*. #2791 is the layer under
it: **where the credential lives, and who is allowed to end it.**

One browser used to hold the platform JWT in two places that could disagree —
the in-memory `axios.defaults.headers.common['Authorization']` written once at
login by `auth.js::setupAxiosAuth`, and `localStorage['token']` re-read per
request by `api.js` — with no cross-tab listener anywhere under
`src/frontend/src`, and three separate 401 handlers. The Workspace made it bite
hardest because it opens in its own tab (ent#456) and polls every 20s.

**The reported symptom.** Log out and log back in on the main app with a
Workspace tab open from the previous session. That tab still holds the OLD JWT;
its next poll 401s; the handler calls `authStore.logout()`, which removes
`localStorage['token']` — *the token the re-login had just written*. The main
tab's next request finds nothing and hard-redirects to `/login`. A stale tab
killed a fresh session, and the handler never asked whether the credential that
failed was still the current one.

### The three things that are now singular

**One source.** `utils/platformSession.js::readStoredToken()` is the only reader
— every former `localStorage.getItem('token')` site (22 of them, incl. the
WebSocket and EventSource ones that cannot use an interceptor) now goes through
it, and a tree-wide guard keeps it that way. The `axios.defaults` copy is gone:
`setupAxiosAuth` is a documented no-op, `App.vue`'s boot-time write — the one
that survived the first cut and made everything below inert, since axios merges
that default into the request BEFORE the interceptor chain runs — is removed,
and the guard for "nobody writes it" walks `src/frontend/src/**`, not one file.
`main.js` installs a global axios **request** interceptor
(`applyRequestCredential`) that rebuilds the header from storage on every
request — so all ~368 bare-`axios` call sites get the current credential without
being rewritten, and one added tomorrow cannot forget to opt in. An **explicit** header on the config still wins, and exactly
one caller needs that: the logout revoke, which must carry a token storage has
already dropped (the #2258 ordering above is unchanged, so the token is captured
*before* the clear and passed *after* it — otherwise #187 silently stopped
revoking anything).

**One verdict.** `sessionLostVerdict()` is a pure function returning
`ignore | stale | logout`, and it replaced a predicate that had been hand-copied
into `api.js`, `main.js` and `portalHttp` and drifted three ways:

| situation | verdict |
|---|---|
| already on `/login`, `/setup`, `/m` | `ignore` |
| the failed token is **not** the stored one | `stale` — adopt the current session, never destroy it |
| no stored token, on the Workspace | `ignore` (an ordinary external client) |
| no stored token, anywhere else | `logout` |
| on the Workspace **and** a portal token is live | `ignore` — **AC #5**: a client whose browser holds a dead operator JWT is no longer thrown onto the operator login by `initializeAuth`'s `fetchUserProfile` |
| otherwise | `logout` |

The `stale` arm is the fix for the reported symptom. The Workspace veto is scoped
by path *as well as* by portal token deliberately: off the Workspace the surface
is an operator one, so an expired operator JWT still bounces there even with a
stray portal token — this change does not widen that.

**One handler.** `setPlatformUnauthorizedHandler` / `notifyPlatformUnauthorized`
in `utils/platformSession.js`. The reaction is `reactToPlatformUnauthorized(error,
deps)` in the same file — it takes the store's two sync actions, `logout` and the
router push as arguments, so the unit suite executes it with fakes (the first cut
kept it inline in `main.js` and pinned it by regex; a mutation restoring the
reported bug stayed green). `main.js` registers a thin adapter that supplies the
real collaborators and **returns** the navigation promise, so a rejected
redundant navigation is absorbed by `notifyPlatformUnauthorized` rather than
escaping as an unhandled rejection. `api.js`, the global interceptor and
`portalHttp` all report to it. The verdict's Workspace veto
(`portalTokenPresent`) is read from the **per-tab** `clientPortal` store — the
same gate `portalHttp` uses — never from shared `localStorage`, or a client's
login in another tab would tell an operator's Workspace tab to `ignore` its own
expired JWT.
`clientPortal.js` keeps `isPlatformSession` as its local gate — not redundant,
because it is the only thing that knows this tab's client session was
*suppressed* (#2261's `platformFallbackSuppressed`), which no amount of reading
localStorage reconstructs.

### Cross-tab sync

`main.js` listens for `storage` and hands the event to `reactToStorageEvent`
(executed by the unit suite), which acts on the token key, the user key — a
sibling login writes `token` first and `auth0_user` a tick later — and a
whole-storage clear. A sibling tab logging in → `adoptStoredSession()` (converge, re-fetch the profile, reset
`profileVerified` so role-gated UI stays closed until *this* token's profile
lands). A sibling logging out → `applySessionEndedElsewhere()`, which drops the
in-memory mirror only: it fires no second server revoke for an already-revoked
token, and writes nothing to storage, because N background tabs reacting to one
event would otherwise each clear it again.

Neither branch navigates. A background tab pushing `/login` is the noise this
issue reports; the visible tab converges through the router guard and its next
request, both of which read the state these set.

## Stated residuals (not hidden)

- ~~**Client-session expiry with a later platform login** still falls back to the
  platform identity.~~ **Fixed in #2261** — see below. The flag alternative was
  unsound *because of the axios-default leak*; removing that leak is what made a
  suppression honest, so the fix is both halves together, never the flag alone.
- **A client's portal token stays server-side valid** after "Sign out" (idle 7d /
  absolute 30d by default): the sign-out is client-side disposal; there is no
  self-service `POST /auth/logout`. The primitive exists
  (`dependencies.revoke_portal_sessions_for_email`, wired only to the operator route
  via `service.logout_client`, ent#281) but is per-**email** — a self-service route
  would sign that client out on every device. Deferred by decision.
- **A client signing out on a browser that also holds an operator's login ends the
  operator's session too.** By design: someone at this browser asked to be signed
  out; the reverse is the reported hazard.

## Files

- `src/frontend/src/stores/clientPortal.js` — `signOutEverywhere()`, `PLATFORM_LOGIN_ROUTE`
- `src/frontend/src/utils/platformSession.js` — #2791: the one reader, the one verdict, the one handler registry
- `src/frontend/src/stores/auth.js` — `logout()` local-clear-before-revoke ordering; `adoptStoredSession` / `applySessionEndedElsewhere`
- `src/frontend/src/main.js` — global request interceptor, the registered reaction, the `storage` listener
- `src/frontend/src/api.js` — reports to the shared handler (no private predicate, no hard reload)
- `src/frontend/src/views/Portal.vue` — `onSignOut`, `signingOut` frame
- `src/frontend/src/components/portal/PortalSidebar.vue` — footer button + caption
- `src/frontend/src/components/portal/portalUtils.js` — `signOutLabelFor`
- `src/frontend/tests/unit/workspaceSession.spec.js`, `workspaceSignOut.spec.js`,
  `platformSessionVerdict.spec.js`, `platformSessionSync.spec.js`
