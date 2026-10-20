import { describe, it, expect } from 'vitest'
import {
  INLINE_TAGS,
  INLINE_ATTR,
  INLINE_SANITIZE_CONFIG,
  cellSource,
  escapeText,
  parseInlineMarkdown,
} from '../../src/utils/inlineMarkdown.js'

/**
 * #2771 — table cells render inline markdown.
 *
 * These drive the REAL parser (`utils/markedConfig.js`, the app's one
 * configured `marked`), so a future marked upgrade or renderer registration
 * that changes cell output turns this red instead of silently un-formatting
 * every table. The sanitizing half lives in `markdown.js` and cannot be
 * imported here — vitest runs `environment: 'node'` and DOMPurify's DOM-less
 * stub has no `addHook` — so what is asserted here is the POLICY it applies,
 * plus an e2e (`canvas-table-markdown.spec.js`) that proves the rendered DOM.
 */
describe('parseInlineMarkdown', () => {
  it('formats the three things agents actually put in cells', () => {
    expect(parseInlineMarkdown('**Deploy**')).toContain('<strong>Deploy</strong>')
    expect(parseInlineMarkdown('`done`')).toContain('<code>done</code>')
    const link = parseInlineMarkdown('see [runbook](https://example.com)')
    expect(link).toContain('href="https://example.com"')
    expect(link).toContain('runbook')
  })

  it('handles emphasis and strikethrough', () => {
    expect(parseInlineMarkdown('*soon*')).toContain('<em>soon</em>')
    expect(parseInlineMarkdown('~~dropped~~')).toContain('<del>dropped</del>')
  })

  it('hardens links at the parser, not only at the sanitizer', () => {
    // `markedConfig` registers the link renderer; asserting it here means a cell
    // link is hardened even before DOMPurify's hook runs.
    const html = parseInlineMarkdown('[x](https://example.com)')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('emits NO block element, whatever the cell contains', () => {
    // This is what keeps a cell from breaking the row it sits in (#2583).
    for (const src of ['# Heading', '- one\n- two', '> quote', '```\ncode\n```', '| a | b |\n|---|---|']) {
      const html = parseInlineMarkdown(src)
      expect(html, `block markup leaked for: ${src}`).not.toMatch(/<(h[1-6]|ul|ol|li|blockquote|pre|table|div|p)\b/)
    }
  })

  it('renders an empty string for null and undefined', () => {
    expect(parseInlineMarkdown(null)).toBe('')
    expect(parseInlineMarkdown(undefined)).toBe('')
  })
})

describe('cellSource — what gets parsed and what does not', () => {
  const cols = ['Item', 'Status', 'Count']

  it('reads array rows positionally and object rows by key', () => {
    expect(cellSource(['a', 'b', 1], cols, 'Status')).toEqual({ kind: 'markdown', text: 'b' })
    expect(cellSource({ Item: 'a', Status: 'b' }, cols, 'Status')).toEqual({ kind: 'markdown', text: 'b' })
  })

  it('parses strings as markdown', () => {
    expect(cellSource(['**x**'], cols, 'Item')).toEqual({ kind: 'markdown', text: '**x**' })
  })

  it('leaves non-strings exactly as they rendered before', () => {
    // AC: numbers, booleans and objects keep their current rendering. They are
    // `text`, never `markdown` — a JSON blob's own `*` and `_` must not
    // italicise a value nobody wrote as prose.
    expect(cellSource([42], cols, 'Item')).toEqual({ kind: 'text', text: '42' })
    expect(cellSource([0], cols, 'Item')).toEqual({ kind: 'text', text: '0' })
    expect(cellSource([false], cols, 'Item')).toEqual({ kind: 'text', text: 'false' })
    expect(cellSource([{ a: 1 }], cols, 'Item')).toEqual({ kind: 'text', text: '{"a":1}' })
    expect(cellSource([['x', 'y']], cols, 'Item')).toEqual({ kind: 'text', text: '["x","y"]' })
  })

  it('renders null, undefined and a missing column as empty', () => {
    expect(cellSource([null], cols, 'Item').kind).toBe('empty')
    expect(cellSource([undefined], cols, 'Item').kind).toBe('empty')
    expect(cellSource({}, cols, 'Status').kind).toBe('empty')
    expect(cellSource(null, cols, 'Status').kind).toBe('empty')
  })

  it('keeps an empty string as an empty cell, not the string "undefined"', () => {
    expect(cellSource([''], cols, 'Item')).toEqual({ kind: 'markdown', text: '' })
  })
})

describe('escapeText', () => {
  it('escapes every character that could open markup', () => {
    expect(escapeText('<script>alert(1)</script>'))
      .toBe('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(escapeText(`a & b "c" 'd'`)).toBe('a &amp; b &quot;c&quot; &#39;d&#39;')
  })

  it('escapes the ampersand first, so an escape is not double-escaped', () => {
    expect(escapeText('&lt;')).toBe('&amp;lt;')
  })
})

describe('the cell sanitize policy', () => {
  it('allows inline formatting only', () => {
    for (const tag of ['strong', 'em', 'code', 'a', 'del', 'br']) {
      expect(INLINE_TAGS, `${tag} must survive`).toContain(tag)
    }
  })

  it('admits nothing that could break the row or reach the page', () => {
    // A cell is not a layout slot, not a media slot, and `<style>` is
    // document-global — the reason the app-wide policy forbids it.
    for (const tag of ['div', 'p', 'table', 'ul', 'li', 'h1', 'pre',
                       'img', 'svg', 'iframe', 'script', 'style', 'form', 'input']) {
      expect(INLINE_TAGS, `${tag} must NOT be allowed in a cell`).not.toContain(tag)
    }
  })

  it('allows only link attributes, so no event handler can ride a cell', () => {
    expect(INLINE_ATTR).toEqual(expect.arrayContaining(['href', 'title', 'target', 'rel']))
    for (const attr of INLINE_ATTR) {
      expect(attr.startsWith('on'), `${attr} is an event handler`).toBe(false)
    }
    expect(INLINE_ATTR).not.toContain('style')
    expect(INLINE_ATTR).not.toContain('class')
    expect(INLINE_ATTR).not.toContain('src')
  })

  it('is the object handed to DOMPurify, so the lists above are the live ones', () => {
    expect(INLINE_SANITIZE_CONFIG.ALLOWED_TAGS).toBe(INLINE_TAGS)
    expect(INLINE_SANITIZE_CONFIG.ALLOWED_ATTR).toBe(INLINE_ATTR)
  })

  it('is frozen — a caller cannot widen the policy for everyone', () => {
    expect(Object.isFrozen(INLINE_SANITIZE_CONFIG)).toBe(true)
    expect(Object.isFrozen(INLINE_TAGS)).toBe(true)
  })
})
