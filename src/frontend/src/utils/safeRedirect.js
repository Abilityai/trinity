/**
 * Where to go after signing in (#3406).
 *
 * A page that offers its own sign-in — a shared canvas behind an `authorized`
 * link — links to `/login?redirect=<where the person was>`. The value arrives
 * in the URL, so anyone can craft it: only an in-app path is honoured, and
 * everything else falls back to the dashboard, which is where every sign-in
 * went before this.
 *
 * @param {unknown} raw   `route.query.redirect` (a string, an array when the
 *                        parameter repeats, or undefined)
 * @returns {string}
 */
export function safeRedirect(raw, fallback = '/') {
  if (typeof raw !== 'string' || !raw.startsWith('/')) return fallback
  // `//host` is protocol-relative, and browsers read `/\host` the same way;
  // no in-app path needs a backslash or a control character.
  if (raw.startsWith('//') || /[\\\u0000-\u001f\u007f]/.test(raw)) return fallback
  // Back to the login page is a loop, not a destination.
  if (/^\/login(?:[/?#]|$)/.test(raw)) return fallback
  return raw
}
