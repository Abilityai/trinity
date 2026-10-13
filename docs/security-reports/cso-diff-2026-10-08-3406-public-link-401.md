# CSO diff audit — 2026-10-08 — #3406 (a public link's 401 is never the operator's /login)

**Mode**: `--diff`, daily (8/10 gate) · **Skill**: cso v1.2.3 · **Base**: `dev` @ `82b99c302` · **Branch**: `feature/3406-public-link-401` (working tree, pre-commit) · **Counts as full run**: no

## Scope

| Area | Change |
|---|---|
| 401 verdict | `utils/platformSession.js`. New arms for `/chat/:token`, `/canvas/s/:token` and `/m`; a new verdict `logout-in-place` (end the platform session, never navigate); `isBearerChallenge` reads `WWW-Authenticate`; `pathForVerdict` covers the first-navigation window |
| Wiring | `main.js` passes `pathForVerdict(router, START_LOCATION, window.location.pathname)` |
| Public chat | `views/PublicChat.vue`. One reaction (`endPublicSession`) for a link-session 401. Stop sends `session_token` in the query on email links |
| Login | `views/Login.vue` plus `utils/safeRedirect.js`: `?redirect=` is honoured for in-app paths only, on the non-SSO success paths |
| Tests | 4 new frontend specs. A backend unit test pins which 401s carry the Bearer challenge (no product code) |

No endpoint, schema, dependency, CI, Docker, compose, base-image or agent-shipped file changed.

## Phase 0 — model and quicklist

The verdict decides what a 401 does to the platform session in the browser. Server-side authorization is untouched; every change is client-side UX over existing server decisions. The new trust input is a response header, `WWW-Authenticate: Bearer`, emitted only by `get_current_user` (`credentials_exception`), by `OAuth2PasswordBearer`'s missing-token path, and by the auth routes. The API is same-origin, so the header is readable.

Quicklist: no pinned component is touched.

## Phase 1 — attack surface delta

- **New endpoints, MCP tools and WebSocket channels:** 0.
- **New client input:** `route.query.redirect` on `/login`.
- **Changed request:** Stop on an email link carries the visitor's session as a query parameter. That is the route's declared transport, and the same as history, intro and session DELETE.
- **Agent-context ingress:** none added.

## Phases 2–11

- **Secrets (diff only):** no secret shapes, private IPs or real emails; `example.com` placeholders only. The enterprise-docs-guard pattern, replayed over the changed docs and the new learnings fragments, finds nothing.
- **Agentic (P7):** no ingress, tool, scope or rendering change. No `v-html`, `innerHTML` or `eval` was added.
- **A01:**
  - `safeRedirect` cannot produce an off-origin navigation. vue-router's web history builds `protocol + '//' + host + base + path`. `//host`, backslashes, control characters, absolute URLs and repeated parameters fall back to `/`.
  - No frontend route performs a write from its query string. `Settings ?slack=` only shows a message, and a direct link can reach it already.
- **A07:**
  - The verdict cannot be driven to sign out a valid session. Only an invalid credential draws a Bearer challenge.
  - `/login` is deliberately excluded from the in-place arm. A wrong password is challenged too, and a sibling tab's fresh token rides that request.
- **A10:** every new guard now states its failure direction:
  - `safeRedirect` falls back to `/`;
  - `pathForVerdict` falls back to the location;
  - the storage `try/catch` in `endPublicSession` still resets in memory;
  - `isBearerChallenge` fails toward `false` (finding 1, fixed).
- **STRIDE (browser session handler):**
  - Spoofing a challenge needs a same-origin response.
  - Tampering is limited to the user's own storage.
  - Info disclosure: see the informational note.
  - DoS: no forced sign-out path.
  - EoP: none (no server change).
- **Data classification:**
  - platform JWT, RESTRICTED (`localStorage.token`);
  - link session, CONFIDENTIAL (`localStorage.public_session_<token>`, query strings of four routes and the chat body; it grants one visitor's chat on one link for 24 h).

## Findings

| # | Sev | Conf | Status | Evidence | Category | Finding | File |
|---|---|---|---|---|---|---|---|
| 1 | MEDIUM | 9/10 | VERIFIED | SUPPORTED | unstated-failure-direction (A10) | The new 401 discriminator did not state its failure direction | `src/frontend/src/utils/platformSession.js` `isBearerChallenge` |

**1.** `isBearerChallenge` returns `false` on any header it cannot read, which is the safe direction: the verdict then ignores the 401. But nothing said so. The next editor could not tell whether "false on unknown" was deliberate, and flipping it would sign operators out on public pages.
- **Exploit:** none directly.
- **Fixed in-branch:** the docstring now states it fails toward `false`, and that the worst case is a lingering dead token, never a signed-out operator.
- **Verifier:** a sequential in-context challenge. It is a wording check, and the pre-fix docstring is the whole evidence.

### Informational (not counted)

- **CWE-598 class, pre-existing.** The visitor's link session travels in query strings, which uvicorn access logs record. Stop now uses the route's declared query parameter, like history, intro and session DELETE. This is not new as a class: the token is scoped to one visitor's chat on one link and expires in 24 h.

### Hypotheses

None (daily mode).

### Coverage gaps

- No Docker pass: diff mode, and no image change.
- Header readability through a deployed proxy chain (nginx, cloudflared) was not exercised live. If a proxy strips `WWW-Authenticate`, the code fails safe to `ignore`.
- The live-stack reproduction of the issue is pending; it is the operator's eyeball step.

**Totals**: CRITICAL 0 · HIGH 0 · MEDIUM 1 (fixed in-branch) · LOW 0. **Trend**: diff run, with no fingerprint overlap with prior reports.
