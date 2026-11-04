/**
 * Terms acceptance (trinity-enterprise#1054).
 *
 * The server is the boundary: while a Workspace-only person has not accepted
 * the current terms version, every route they call answers 403 with one of
 * `TERMS_REFUSAL_CODES`, and the terms routes themselves are exempt. These
 * helpers only decide where the SPA goes around that wall.
 */
import { safeRedirect } from './safeRedirect'
import { isAuthRoute } from './platformSession'
import { isTermsPath, landingAfterSignIn, TERMS_PATH } from './workspaceOnly'

export { isTermsPath, TERMS_PATH }

/** `terms_required`: accept to continue. The other two: the invitation that
 *  opened the account is no longer live, so there is nothing to accept. */
export const TERMS_REFUSAL_CODES = Object.freeze(['terms_required', 'invite_expired', 'invite_revoked'])

const CLOSED_MESSAGES = Object.freeze({
  invite_expired: 'This invitation has expired.',
  invite_revoked: 'This invitation was withdrawn.',
})

/** What the terms page says instead of the text, or null while the door is open. */
export function closedInviteMessage(code) {
  return CLOSED_MESSAGES[code] || null
}

/**
 * Where an accepted (or never-gated) person goes from the terms page: the page
 * they were sent from, role-aware — never the terms page itself.
 */
export function afterTermsLocation(rawRedirect, role) {
  const target = safeRedirect(rawRedirect, null)
  return landingAfterSignIn(target && !isTermsPath(target) ? target : undefined, role)
}

/** A 403 the terms gate answered. */
export function isTermsRefusal(error) {
  return error?.response?.status === 403 && TERMS_REFUSAL_CODES.includes(error.response.data?.detail?.code)
}

/**
 * What the SPA does with that refusal: open /terms, carrying the page to come
 * back to — unless the page already is /terms (its own routes are exempt, so
 * a refusal there is not the gate's, and following it would loop) or a sign-in
 * page (the person must always be able to sign out and back in).
 * Collaborators are arguments so a test can watch what is called.
 */
export function reactToTermsRefusal(error, { currentPath, push }) {
  const path = currentPath() || ''
  const bare = path.split(/[?#]/)[0]
  if (isTermsPath(bare) || isAuthRoute(bare)) return undefined
  return push(termsLocation(path, error?.response?.data?.detail?.code))
}

/**
 * The terms page, carrying the page to come back to and — for a closed
 * invitation — the reason. One builder, so a link to /terms goes exactly
 * where the refusal handler would have sent the person.
 */
export function termsLocation(fullPath, code) {
  const query = {}
  const back = safeRedirect(fullPath, null)
  if (back) query.redirect = back
  if (closedInviteMessage(code)) query.reason = code
  return { path: TERMS_PATH, query }
}

// One handler, set by main.js (which owns the router), fed by every HTTP
// client's response interceptor; a page's burst of refusals reports once.
// A refusal starts the window only when it moved the person: one that was
// ignored (a sign-in page, /terms) or whose navigation failed costs nothing,
// so the next refusal on a settled page still reacts (trinity-enterprise#1082).
// While the push to /terms is still navigating, it absorbs the burst.
const NOTIFY_WINDOW_MS = 2000
let _handler = null
let _lastNavigated = -Infinity
let _inFlight = null

export function setTermsRefusalHandler(fn) {
  _handler = fn
  _lastNavigated = -Infinity
  _inFlight = null
}

export function notifyTermsRefusal(error, now = Date.now()) {
  if (!_handler || !isTermsRefusal(error)) return
  if (_inFlight || now - _lastNavigated < NOTIFY_WINDOW_MS) return
  try {
    const result = _handler(error)
    if (!result || typeof result.then !== 'function') return
    // vue-router resolves a failed navigation with a NavigationFailure; only a
    // completed one starts the window. A rejection must not surface unhandled.
    const flight = result.then(
      (failure) => {
        if (_inFlight !== flight) return // the handler was reset meanwhile
        _inFlight = null
        if (!failure) _lastNavigated = now
      },
      () => {
        if (_inFlight === flight) _inFlight = null
      },
    )
    _inFlight = flight
  } catch {
    /* a failure to react must never replace the error being rejected */
  }
}
