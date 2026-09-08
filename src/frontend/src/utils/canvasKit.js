/**
 * The canvas design kit's decidable half (trinity-enterprise#537).
 *
 * An agent's canvas should look designed without the agent touching CSS: it
 * composes `html` / `markdown` blocks against a small, platform-owned class
 * vocabulary (`ck-*`), and ONE stylesheet — the unscoped `<style>` in
 * `components/canvas/CanvasKit.vue`, every selector under `.canvas-kit` —
 * renders it in both themes from the design tokens.
 *
 * This module is the allowlist the sanitiser enforces in canvas mode
 * (`utils/markdown.js::sanitizeCanvasHtml` / `renderCanvasMarkdown`, through
 * `sanitizeHooks.js::restrictToCanvasKit`): a class outside `KIT_CLASSES` is
 * DROPPED, never passed through, and an inline style keeps only the two
 * properties below with a bounded value. Pure and importing nothing, so a
 * vitest spec (node environment, no DOM) can drive it, and so a backend test
 * can read the set and pin the platform prompt's class list against it.
 *
 * Membership is exact — never `startsWith('ck-')`: a prefix match would let an
 * agent-invented `ck-anything` through to a rule that does not exist (harmless)
 * or, worse, to a future app class that happens to share the prefix.
 */

export const KIT_CLASS_PREFIX = 'ck-'

export const KIT_CLASSES = Object.freeze(new Set([
  // layout
  'ck-grid', 'ck-grid-2', 'ck-grid-3', 'ck-grid-4', 'ck-span-2', 'ck-span-full', 'ck-stack', 'ck-row',
  // card
  'ck-card', 'ck-card-title', 'ck-card-meta', 'ck-card-body',
  // section header
  'ck-section', 'ck-section-title', 'ck-section-sub',
  // KPI tile — the v-html twin of ReportKpiTiles.vue (same tokens)
  'ck-kpi', 'ck-kpi-label', 'ck-kpi-value', 'ck-kpi-unit', 'ck-kpi-delta', 'ck-up', 'ck-down', 'ck-flat',
  // table — the v-html twin of ReportTable.vue
  'ck-table', 'ck-table-wrap', 'ck-num',
  // callout + chip, with the tone modifiers they share
  'ck-callout', 'ck-chip', 'ck-info', 'ck-success', 'ck-warning', 'ck-danger', 'ck-neutral',
  // figure
  'ck-figure', 'ck-caption',
  // text
  'ck-muted', 'ck-mono', 'ck-small', 'ck-right', 'ck-center',
]))

/**
 * The stated small set of inline-style properties an agent may use, and the
 * only values they may take. `width: 40%` on a figure or `max-width: 480px` on
 * a card is legitimate page composition; `height`, `position`, margins,
 * `z-index`, `transform` and everything else are not admitted because each is
 * a way to reach outside the block (overlay the panel chrome, stretch a
 * customer's page). Percent is clamped to 100 and pixels to 9999 so a value
 * cannot be a layout bomb either — and a pixel value is emitted as
 * `min(<px>, 100%)`, because 9999px IS a layout bomb in a 24rem rail column:
 * the gallery (#2583) measured a `width: 9999px` card scrolling the whole
 * canvas 10,000px sideways. The wrap is applied to OUR string after the value
 * matched the bounded shape, so nothing the agent wrote reaches CSS unparsed.
 */
export const ALLOWED_INLINE_PROPERTIES = Object.freeze(['width', 'max-width'])
const INLINE_VALUE_RE = /^(?:(?:100|[1-9]?\d)%|\d{1,4}px)$/

/**
 * Keep only the kit's classes from a `class` attribute value.
 * @returns {string|null} the surviving classes, or null when none survive
 */
export function filterKitClasses(value) {
  if (typeof value !== 'string') return null
  const kept = []
  for (const cls of value.split(/\s+/)) {
    if (cls && KIT_CLASSES.has(cls) && !kept.includes(cls)) kept.push(cls)
  }
  return kept.length ? kept.join(' ') : null
}

/**
 * Keep only the allowed declarations from a `style` attribute value.
 *
 * Each declaration is split at its first `:`; the property is trimmed and
 * lower-cased before matching, and the value must match the bounded shape
 * above exactly — so `url(`, `calc(`, `var(`, `expression(`, `!important`,
 * quotes, backslashes and a second `:` are all unreachable by construction
 * rather than by a denylist.
 * @returns {string|null} the surviving declarations, or null when none survive
 */
export function filterInlineStyle(value) {
  if (typeof value !== 'string') return null
  const kept = []
  for (const decl of value.split(';')) {
    const at = decl.indexOf(':')
    if (at < 0) continue
    const prop = decl.slice(0, at).trim().toLowerCase()
    const val = decl.slice(at + 1).trim().toLowerCase()
    if (!ALLOWED_INLINE_PROPERTIES.includes(prop)) continue
    if (!INLINE_VALUE_RE.test(val)) continue
    kept.push(`${prop}: ${val.endsWith('px') ? `min(${val}, 100%)` : val}`)
  }
  return kept.length ? kept.join('; ') : null
}
