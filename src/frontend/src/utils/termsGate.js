/**
 * Terms acceptance (trinity-enterprise#1054).
 *
 * The server is the boundary: while a Workspace-only person has not accepted
 * the current terms version, every route they call answers 403 with one of
 * `TERMS_REFUSAL_CODES`, and the terms routes themselves are exempt. These
 * helpers only decide where the SPA goes around that wall.
 */
import { safeRedirect } from './safeRedirect'
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
