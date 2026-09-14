/**
 * Inline-only markdown for table cells (#2771) — the decidable half, pure.
 *
 * A `table` block's cells were rendered as plain text, so an agent writing
 * `**Deploy**`, `` `done` `` or `[runbook](https://example.com)` — which agents
 * routinely do in status columns and reference columns — saw the literal
 * characters. A GFM pipe table written in `markdown` prose went through `marked`
 * and formatted correctly, so the same canvas showed two tables behaving
 * differently, and the one the MCP tool guide recommends was the broken one.
 *
 * Why a separate module from `markdown.js`: that file cannot be imported without
 * a DOM (DOMPurify's DOM-less stub has no `addHook`), so anything decided inside
 * it is unreachable by `vitest`, which runs `environment: 'node'`. Everything
 * here is pure and executed by `tests/unit/inlineMarkdown.spec.js`; the one
 * impure step — sanitising — stays in `markdown.js` beside every other
 * DOMPurify call, because a second sanitizer instance is the H-005 failure one
 * level up.
 *
 * INLINE-ONLY is the design, not a limitation. `marked.parseInline` never emits
 * a block element, and the allowlist below drops the ones raw HTML in a cell
 * could smuggle in, so a cell cannot break the row/column layout the #2583
 * gallery pins — a heading or a list in a cell degrades to its own text.
 */
import { marked } from './markedConfig'

/**
 * Tags a cell may keep. Inline formatting only: no `div`/`p`/`table`/`ul`
 * (layout), no `img`/`iframe`/`svg` (a cell is not a media slot), no `style`
 * (document-global, the reason `BASE_CONFIG` forbids it app-wide).
 */
export const INLINE_TAGS = Object.freeze([
  'a', 'abbr', 'b', 'br', 'code', 'del', 'em', 'i', 'kbd', 'mark',
  's', 'span', 'strong', 'sub', 'sup', 'u',
])

/**
 * `href`/`title` are the author's; `target`/`rel` are the sanitize hook's own
 * link hardening, allowlisted here so its work survives this policy.
 */
export const INLINE_ATTR = Object.freeze(['href', 'title', 'target', 'rel'])

/** The DOMPurify policy for a cell. Consumed by `markdown.js::renderInlineMarkdown`. */
export const INLINE_SANITIZE_CONFIG = Object.freeze({
  ALLOWED_TAGS: INLINE_TAGS,
  ALLOWED_ATTR: INLINE_ATTR,
})

const ESCAPES = Object.freeze({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})

/** HTML-escape a value that must render verbatim, never as markup. */
export function escapeText(value) {
  return String(value).replace(/[&<>"']/g, (c) => ESCAPES[c])
}

/**
 * Parse one cell's markdown, inline only. Returns UNSANITISED html — the caller
 * sanitises. Split that way so this stays testable without a DOM; the two are
 * never used apart (`renderInlineMarkdown` is the only consumer).
 */
export function parseInlineMarkdown(value) {
  if (value === null || value === undefined) return ''
  return marked.parseInline(String(value))
}

/**
 * What a cell should render, and how.
 *
 * `{ kind: 'markdown' }` only for an actual string. A number, boolean or object
 * keeps exactly its pre-#2771 rendering (`JSON.stringify` for an object,
 * `String()` otherwise) and is escaped rather than parsed — running a JSON blob
 * through a markdown parser would let its own `*` and `_` characters italicise
 * a value the agent never meant as prose.
 */
export function cellSource(row, columns, col) {
  const value = Array.isArray(row) ? row[columns.indexOf(col)] : row?.[col]
  if (value === null || value === undefined) return { kind: 'empty', text: '' }
  if (typeof value === 'string') return { kind: 'markdown', text: value }
  return { kind: 'text', text: typeof value === 'object' ? JSON.stringify(value) : String(value) }
}
