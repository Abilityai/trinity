# CSO Diff Audit — trinity#2258 Workspace "Sign out" signs out

- **Date**: 2026-08-17 · **Mode**: `--diff` (daily, ≥8/10 confidence gate) · **Branch**: `fix/2258-workspace-signout` (vs merge-base `541db23e`)
- **Scope**: 7 files, frontend-only — `stores/auth.js`, `stores/clientPortal.js`, `views/Portal.vue`, `components/portal/PortalSidebar.vue`, `components/portal/portalUtils.js`; 2 test files (`workspaceSession.spec.js`, new `workspaceSignOut.spec.js`)
- **Verification**: two independent fresh-context reviewers over the plan (strategy + engineering) plus a third-model adversarial pass on the revised plan; the shipped code was walked in a real browser against a live backend for **both** reported variants (platform user; client on a browser also holding a platform JWT — real minted portal token, revoked afterwards); every load-bearing property below is pinned by a unit test in the diff

## Findings

**None at the ≥8 confidence gate.**

## What the diff changes, security-wise

The bug was an **identity/UX defect with a shared-machine hazard**, not a credential leak: the Workspace's "Sign out" cleared only the client portal token, and because a platform JWT is an *implicit* Workspace session (ent#357), removing the portal token was precisely what activated the platform fallback — a refresh re-entered as the operator. The fix **removes** the platform credential on user-initiated Workspace sign-out (in addition to the portal token) and routes by principal kind. Net effect: strictly less credential lives in the browser after the click than before this change.

The issue's own suggested primitive — a persisted "suppress the platform fallback" flag — was **rejected on evidence and is recorded here as a security decision**: `auth.js` installs the platform JWT as an axios *default* header, and per-request headers merge over defaults, so a UI flag would have shown the OTP form while every portal request still carried the operator's JWT and `get_portal_principal` still answered as the operator (a hidden disclosure); one path (`streamPortalExecution`, bare `fetch`) would have lost auth while every axios path kept it; and a wrong OTP digit (`/auth/verify` → 401) would have tripped the global bounce onto the operator `/login`.

## Clean categories (what was checked)

| Axis | Verdict | Basis |
|---|---|---|
| Attack surface delta | CLEAN | no new endpoint, no auth-dependency change, no new input path; the only wire change is *fewer* credentials sent post-sign-out |
| A01 access control — post-sign-out reach | CLEAN | `signOutEverywhere()` ends the platform credential (`authStore.logout()`) BEFORE clearing portal state, so there is no window in which `portalToken` is gone while `isAuthenticated` is true (the state where `authHeader` would hand an in-flight poll the operator's identity); pinned by the ordering test (`portalTokenDuringLogout === 'portal-token'`); post-sign-out `authHeader === {}` and `axios.defaults.headers.common['Authorization']` is undefined (`workspaceSession.spec.js:178,199`, `workspaceSignOut.spec.js:155-158`); browser-verified: revisiting `/workspace` renders the OTP form with `localStorage['token'] === null` |
| A02 crypto / JWT revocation | CLEAN | the #187 server-side revoke still fires and still carries the token: `logout()` now clears local state FIRST but the `POST /api/auth/logout` rides the axios default header, deleted only after the call — pinned (`header: 'Bearer jwt'` at POST time). Reordering makes revocation *more* reliable: an already-expired token's 401 no longer re-enters `logout()` via the global interceptor |
| A07 session invalidation | CLEAN | both principal kinds reach a state with no live browser credential; `endSession({expired})` deliberately does NOT end a platform session (expiry is not a user act — an operator working in another tab must not be logged out by a client's idle timeout); pinned |
| Open redirect | CLEAN | the only new navigation is `router.push(PLATFORM_LOGIN_ROUTE)` — a module constant; the client path uses the existing constant-target `escapeStage()` |
| XSS / template | CLEAN | no `v-html`; new template text is a static string and two attribute bindings (`:title`/`:aria-label`) of a module constant; the footer caption is a static ternary |
| 401-bounce predicate | CLEAN | `api.js`/`main.js` key on `localStorage['token']`, which the fix genuinely removes — correct by construction, no predicate change needed (a flag design would have required editing both copies plus the spec restatement) |
| Secrets in diff | CLEAN | branch-range scan for key-shaped strings (AWS/OpenAI/GitHub/Slack prefixes, JWT shape): 0 hits; test literals are `platform-jwt` / `portal-token` / `jwt` |
| Enterprise-docs-guard (ent#45) | N/A | no docs or seam files in the diff |
| Vendored parity / channels / CI / Docker / deps / MCP / skills | N/A | untouched |

## Below the gate (recorded, not findings)

| Item | Confidence | Disposition |
|---|---|---|
| **A client's portal token stays server-side valid after "Sign out"** — up to the idle window (7d default) / absolute cap (30d): there is no self-service `POST /client-portal/auth/logout`, and the sign-out is client-side token disposal only. Pre-existing and unchanged by this diff. The revocation primitive exists (`dependencies.revoke_portal_sessions_for_email`, wired only to the operator route via `service.logout_client`, ent#281) but is per-**email**, so a self-service route would sign that client out on every device. | n/a (pre-existing) | **Deferred by plan decision** — reviewed and agreed by all three review voices; would turn a frontend-only P1 into a backend change with its own auth/rate-limit surface. Stated in the PR; candidate follow-up. |
| A **client** session that merely *expires* on a browser which later gained a platform login still falls back to that platform identity through `endSession → signOut`. Same class as #2258 by an ordering the UI does not produce (the OTP form never renders while a platform JWT exists, so it needs the platform login to arrive AFTER the client signed in). Documented in code (`clientPortal.js` above `signOutEverywhere`). | 6/10 | **Follow-up issue #2261** (filed incubating). Not fixable with credential destruction (expiry is not a user act) and the flag alternative is unsound (above). |
| A client signing out on a browser that also holds an operator's login ends the operator's platform session too. | n/a | **Accepted, by design** — "someone at this browser asked to be signed out"; the reverse (leaving a live operator credential behind) is the reported hazard. Label reflects it for the platform kind ("Sign out of Trinity"). |

## Verdict

A frontend-only change whose sole effect on the wire is that fewer credentials survive a sign-out. The rejected alternative would have shipped a fix that reports success while the operator's JWT stayed on every portal request. Ship-clean.
