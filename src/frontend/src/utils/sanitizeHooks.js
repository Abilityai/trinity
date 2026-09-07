/**
 * Media hardening applied to EVERY element DOMPurify lets through (ent#536).
 *
 * Pure, importing nothing, so a unit test can drive it with a fake node —
 * `utils/markdown.js` (which registers it as an `afterSanitizeAttributes`
 * hook) cannot be imported without a DOM.
 *
 * Why it exists: the frontend CSP `img-src` admits `https:` so that a canvas
 * `image` block can show a web image at all. That directive is page-global,
 * and DOMPurify's default profile keeps `<img src="https://…">` and inline
 * `style` attributes — so an agent-authored `html` or `markdown` block (and
 * every other sanitised markdown surface) could otherwise carry a tracking
 * pixel: the host the agent chose learns each viewer's IP, user agent and
 * view time, plus the deployment origin from the Referer. Two rules close it:
 *
 *   1. every `<img>` gets `referrerpolicy="no-referrer"` (and lazy loading),
 *      so the deployment hostname never travels with the request;
 *   2. an inline `style` that references `url(` is removed — a CSS background
 *      image is the same tracking pixel through a different attribute, and it
 *      is not something agent prose ever legitimately needs.
 *
 * A web image itself still loads (that is the feature); what it can no longer
 * carry is the viewer's context.
 */

const URL_IN_STYLE_RE = /url\s*\(|image-set\s*\(|@import/i

/**
 * Apply the two rules to one DOM-like node. Returns what changed so a test
 * can assert without a DOM.
 *
 * @param {{tagName?: string, getAttribute(n:string): string|null,
 *          setAttribute(n:string, v:string): void, removeAttribute(n:string): void}} node
 */
export function hardenMediaAttributes(node) {
  const changes = []
  const tag = String(node?.tagName || '').toUpperCase()
  if (tag === 'IMG') {
    node.setAttribute('referrerpolicy', 'no-referrer')
    changes.push('referrerpolicy')
    if (!node.getAttribute('loading')) {
      node.setAttribute('loading', 'lazy')
      changes.push('loading')
    }
  }
  const style = node?.getAttribute ? node.getAttribute('style') : null
  if (typeof style === 'string' && URL_IN_STYLE_RE.test(style)) {
    node.removeAttribute('style')
    changes.push('style')
  }
  return changes
}
