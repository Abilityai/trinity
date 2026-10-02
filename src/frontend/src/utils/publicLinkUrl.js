/**
 * The full URL of a public link, as shown and copied in the Sharing panel.
 *
 * The backend builds `url` as `${FRONTEND_URL}/chat/<token>`, and
 * FRONTEND_URL defaults to empty, so `url` is a bare path on any install that
 * never set it (#3170). Resolve it against the page origin; an absolute
 * `url` or `external_url` passes through unchanged.
 */
export function publicLinkUrl(link, origin = window.location.origin) {
  const raw = link.external_url || link.url
  return raw ? new URL(raw, origin).href : ''
}
