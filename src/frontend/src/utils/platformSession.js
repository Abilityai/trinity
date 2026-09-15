/**
 * The platform session, in one place (#2791).
 *
 * One browser used to hold the platform JWT in two places that could disagree —
 * the in-memory `axios.defaults.headers.common['Authorization']` copy written by
 * `auth.js::setupAxiosAuth`, and `localStorage['token']` re-read per request by
 * `api.js` — with no cross-tab sync and three separate 401 handlers. The visible
 * cost was a stale Workspace tab logging a freshly re-established session out:
 * its poll went out on the OLD token, 401'd, and the handler called
 * `authStore.logout()`, which removed the NEW session's token from localStorage.
 * The handler never asked whether the token that failed was still the current one.
 *
 * This module owns three things, and nothing else in the app may re-derive them:
 *
 *   1. **where the credential lives** — `readStoredToken()` / `clearStoredSession()`;
 *   2. **what a 401 means** — `sessionLostVerdict()`, the one predicate;
 *   3. **how other tabs find out** — `installCrossTabSync()`.
 *
 * Everything decidable is a pure function of its arguments, because
 * `vitest.config.js` pins `environment: 'node'` with no mount harness: a rule
 * that lives inside an interceptor closure is a rule no unit test can reach, and
 * this file exists precisely because three copies of one rule drifted.
 */

export const TOKEN_KEY = 'token'
export const USER_KEY = 'auth0_user'

/** Routes that are already the way out — bouncing from them is a loop. */
const AUTH_ROUTES = ['/login', '/setup', '/m']

/** Surfaces whose session may be a CLIENT's rather than the operator's. */
const WORKSPACE_PREFIXES = ['/workspace', '/portal']

export function isAuthRoute(path) {
  return AUTH_ROUTES.includes(path || '')
}

export function isWorkspacePath(path) {
  const p = path || ''
  return WORKSPACE_PREFIXES.some((prefix) => p.startsWith(prefix))
}

/**
 * The stored platform credential, or null.
 *
 * `localStorage` is the durable source and the one other tabs mutate, so it is
 * the source of truth; the Pinia store mirrors it. Reads are wrapped because a
 * private window with site data blocked throws on access rather than returning
 * null — and a throw here would take down an interceptor on every request.
 */
export function readStoredToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || null
  } catch {
    return null
  }
}

export function readStoredUser() {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/**
 * Forget the platform session locally. ONE implementation (AC #6).
 *
 * `api.js`'s 401 path used to remove only `token` and hard-reload, leaving
 * `auth0_user` behind — so the next load restored a user object for a session
 * that no longer existed, and `initializeAuth` skipped its own cleanup branch
 * because the pair was no longer both-present.
 */
export function clearStoredSession() {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  } catch {
    /* storage unavailable — there is nothing to clear */
  }
}

/**
 * The bearer token an axios request actually went out with, or null.
 *
 * Read from the request config rather than from anywhere current: the question
 * `sessionLostVerdict` asks is whether the credential that FAILED is still the
 * one we hold, and only the config knows what was sent.
 */
export function tokenOfRequest(config) {
  const headers = config?.headers || {}
  const raw = headers.Authorization || headers.authorization || ''
  const value = typeof raw === 'string' ? raw : ''
  return value.startsWith('Bearer ') ? value.slice(7) : null
}

/**
 * What to do about a 401. The ONE predicate, replacing three copies.
 *
 * @returns {'ignore'|'stale'|'logout'}
 *
 *   * `ignore` — this 401 is not the platform session's to act on;
 *   * `stale`  — the credential that failed has since been REPLACED, so the
 *                session it belonged to is already gone and the current one is
 *                innocent. Re-adopt what is stored; never destroy it;
 *   * `logout` — the stored platform credential is the one that failed.
 *
 * The `stale` arm is finding 1 of the issue, and it is the whole reason this
 * function takes `failedToken`. A Workspace tab left open across a logout and a
 * re-login holds the previous JWT in a closure; its next poll 401s; the old code
 * ran `logout()` and deleted the NEW session's token from under the tab that had
 * just created it. Comparing the two answers that in one line.
 *
 * Order is load-bearing:
 *   - auth routes first, so nothing can bounce off the page that fixes it;
 *   - `stale` before every session question, because a superseded credential
 *     says nothing about the session that replaced it;
 *   - the Workspace/portal veto (AC #5) before the final logout, so a CLIENT
 *     whose browser happens to hold a dead operator JWT is not thrown onto the
 *     operator login on page load. `initializeAuth` calls `fetchUserProfile`
 *     through bare axios on EVERY load, which is exactly how that fired.
 *
 * Note the veto is scoped by path as well as by portal token. Off the Workspace
 * the surface itself is an operator one, so an expired operator JWT bounces
 * there even if a portal token is lying around — that is today's behaviour and
 * this change does not widen it.
 */
export function sessionLostVerdict({
  failedToken = null,
  storedToken = null,
  portalTokenPresent = false,
  path = '',
} = {}) {
  if (isAuthRoute(path)) return 'ignore'

  // Superseded: someone replaced the credential between the request and its
  // answer. Whatever went wrong belonged to a session that is already over.
  if (failedToken && storedToken && failedToken !== storedToken) return 'stale'

  const onWorkspace = isWorkspacePath(path)

  // No platform session to end. On the Workspace that is the ordinary state of
  // an external client; anywhere else it still means "go and sign in".
  if (!storedToken) return onWorkspace ? 'ignore' : 'logout'

  // AC #5 — a live client session owns this tab, and the platform credential
  // beside it is not what the person is using.
  if (onWorkspace && portalTokenPresent) return 'ignore'

  return 'logout'
}


// ---------------------------------------------------------------------------
// The one handler, reached from three transports (AC #3)
// ---------------------------------------------------------------------------
//
// Acting on the verdict needs the router and the auth store, and both of those
// import (transitively) the modules that need to CALL this — so the reaction is
// registered from `main.js`, where they already live, and the transports reach
// it through here. A direct import would be a cycle; this is the same shape
// `clientPortal.js::setPlatformSessionLostHandler` already uses, generalised so
// there is one of it instead of one per transport.

let _onPlatformUnauthorized = null

export function setPlatformUnauthorizedHandler(fn) {
  _onPlatformUnauthorized = fn
}

/**
 * Report a 401 to the single handler. Never throws and never returns a promise
 * the caller must await: an interceptor's job is to reject the original error,
 * not to wait on the logout it may have triggered.
 */
export function notifyPlatformUnauthorized(error) {
  try {
    const result = _onPlatformUnauthorized?.(error)
    // The reaction pushes a route, and Vue Router REJECTS a redundant or
    // aborted navigation. A sync try/catch cannot see that, so a second 401
    // arriving while /login is already loading would surface as an unhandled
    // rejection in every user's console — noise that looks like a real fault.
    if (result && typeof result.catch === 'function') result.catch(() => {})
  } catch {
    /* a failure to react must never replace the error being rejected */
  }
}
