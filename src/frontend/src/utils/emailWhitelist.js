/**
 * The Settings email whitelist: what counts as an entry, and how a stored
 * entry is addressed (#3455, #3456).
 *
 * The whitelist is matched by exact, lower-cased address — there is no domain
 * or wildcard entry form — so an entry is ONE email address. The server is
 * authoritative (`db_models.EmailWhitelistAdd`); this is the same rule run
 * before the request, so the user is told what is wrong, how to fix it and
 * what a valid entry looks like (design-system principle 17) without a round
 * trip. The field is `type="email"` but sits outside a `<form>`, so the
 * browser's own check never ran.
 *
 * Deliberately permissive about WHICH characters an address may use: a
 * self-hosted install whitelists intranet (`user@localhost`) and
 * internationalised addresses, and a stricter grammar would refuse them.
 */

// RFC 5321 §4.5.3.1.3 — mirrors `WHITELIST_EMAIL_MAX_LENGTH` in db_models.py.
export const WHITELIST_EMAIL_MAX_LENGTH = 254

const EXAMPLE = 'user@example.com'

/**
 * @param {unknown} raw  what the user typed
 * @returns {{email: string, error: string}}  the normalised address and '',
 *   or '' and the reason. Never throws, and never echoes the input — an
 *   oversized entry would overflow the message it is reported in.
 */
export function validateWhitelistEmail(raw) {
  const email = typeof raw === 'string' ? raw.trim().toLowerCase() : ''
  const refuse = (error) => ({ email: '', error })

  if (!email) return refuse(`Enter an email address, for example ${EXAMPLE}.`)
  if (email.length > WHITELIST_EMAIL_MAX_LENGTH) {
    return refuse(
      `That entry is too long (${email.length} characters; an email address has at most ` +
      `${WHITELIST_EMAIL_MAX_LENGTH}). Enter one address, for example ${EXAMPLE}.`
    )
  }
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f]/.test(email)) {
    return refuse(`An email address cannot contain spaces. Remove them, for example ${EXAMPLE}.`)
  }
  const at = email.indexOf('@')
  if (at <= 0 || at !== email.lastIndexOf('@') || at === email.length - 1) {
    return refuse(`That is not an email address. Use the form name@domain, for example ${EXAMPLE}.`)
  }
  return { email, error: '' }
}

/**
 * The URL of one stored entry. The value is sent as a single encoded path
 * segment: rows that predate validation hold arbitrary text, and `/` is a
 * legal local-part character besides (#3456). Never validated — every row the
 * list shows must be removable.
 */
export function whitelistEntryUrl(email) {
  return `/api/settings/email-whitelist/${encodeURIComponent(email)}`
}
