/**
 * Canvas sharing — the decidable rules (ent#554).
 *
 * Pure, and outside the SFCs on purpose: vitest runs `environment: 'node'`
 * with no mount harness, so a rule that lives inside a component is one no
 * test can reach. Every string a person reads about a share link is decided
 * here.
 */

/** The two reaches a link can have. Narrow first — it is the default. */
export const SHARE_SCOPES = ['authorized', 'public']

/**
 * What the share dialog says each reach means.
 *
 * The wider one names its reach in the option itself (AC #2), rather than
 * hiding it behind a warning someone has to go looking for: the moment of
 * choosing is the only moment this is read.
 */
export function scopeCopy(scope) {
  if (scope === 'public') {
    return {
      label: 'Anyone with the link',
      detail: 'No sign-in. Anyone you send the link to can open this canvas, and so can anyone they forward it to.',
      wide: true,
    }
  }
  return {
    label: 'People who already have access',
    detail: 'Opening the link requires signing in, and only people who can already see this agent will see the canvas.',
    wide: false,
  }
}

/**
 * Turn a refusal from the share endpoint into words and, where there is one,
 * an action.
 *
 * The server names the state; this decides how it reads. `revoked` and
 * `expired` are deliberately distinct from "not found" — AC #2 asks that a
 * revoked link not 404 blankly, and whoever holds it was already told the
 * canvas exists.
 */
export function shareProblem(status, detail) {
  const named = detail && typeof detail === 'object' ? detail.status : null

  if (named === 'sign_in_required' || status === 401) {
    return {
      title: 'Sign in to view this canvas',
      body: 'It was shared with the people who already have access to its agent.',
      action: 'sign-in',
    }
  }
  if (named === 'not_authorized' || status === 403) {
    return {
      title: 'This canvas is not shared with you',
      body: 'It was shared with the people who already have access to its agent. Ask whoever sent it to share it more widely, or to add you to the agent.',
      action: null,
    }
  }
  if (named === 'revoked') {
    return {
      title: 'This link was turned off',
      body: 'Whoever shared this canvas has revoked the link. Ask them for a new one.',
      action: null,
    }
  }
  if (named === 'expired') {
    return {
      title: 'This link has expired',
      body: 'The share link had an end date and it has passed. Ask whoever shared it for a new one.',
      action: null,
    }
  }
  // Everything else — an unknown token, a canvas since deleted — reads the
  // same, so a stranger guessing tokens learns nothing from the difference.
  return {
    title: 'This link does not work',
    body: 'It may be mistyped, or the canvas it pointed at is gone.',
    action: null,
  }
}

/**
 * The absolute URL to hand someone, built from a relative path the server
 * returned.
 *
 * The server deliberately returns a RELATIVE path so a link is not bound to
 * whichever hostname the instance happened to be reached by when it was
 * minted; the origin is added here, at the moment of copying, which is the
 * only point where the right answer is known.
 */
export function shareUrl(path, origin) {
  const base = String(origin || '').replace(/\/+$/, '')
  const rel = String(path || '')
  if (/^https?:\/\//i.test(rel)) return rel
  if (!rel) return ''
  return `${base}${rel.startsWith('/') ? '' : '/'}${rel}`
}

/**
 * A one-line summary of a link for the manage list.
 *
 * Says the reach in words rather than the stored value: "authorized" is a
 * column name, not something to show a person.
 */
export function shareSummary(share, now = Date.now()) {
  if (!share) return null
  const copy = scopeCopy(share.scope)
  const views = Number(share.view_count) || 0
  const parts = [copy.label]
  if (share.revoked_at) parts.push('revoked')
  else if (share.expires_at && Date.parse(share.expires_at) <= now) parts.push('expired')
  parts.push(views === 1 ? '1 view' : `${views} views`)
  return parts.join(' · ')
}
