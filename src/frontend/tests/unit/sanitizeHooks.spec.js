/**
 * The media-hardening rule every DOMPurify pass applies (ent#536).
 *
 * The CSP `img-src` admits `https:` so a canvas image block can load; this
 * hook is what keeps that from turning every sanitised `<img>` and inline
 * `style` into a tracking pixel that reports the viewer's context. Pure, so it
 * is tested with a fake node here and pinned as registered in `markdown.js`.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import path from 'path'

import { hardenMediaAttributes } from '@/utils/sanitizeHooks'

const here = path.dirname(fileURLToPath(import.meta.url))

function fakeNode(tagName, attrs = {}) {
  const a = { ...attrs }
  return {
    tagName,
    attrs: a,
    getAttribute: (n) => (n in a ? a[n] : null),
    setAttribute: (n, v) => { a[n] = v },
    removeAttribute: (n) => { delete a[n] },
  }
}

describe('hardenMediaAttributes', () => {
  it('every <img> gets no-referrer and lazy loading', () => {
    const n = fakeNode('IMG', { src: 'https://example.com/a.png' })
    expect(hardenMediaAttributes(n)).toEqual(['referrerpolicy', 'loading'])
    expect(n.attrs.referrerpolicy).toBe('no-referrer')
    expect(n.attrs.loading).toBe('lazy')
  })
  it('an <img> that already set loading keeps it', () => {
    const n = fakeNode('img', { loading: 'eager' })
    hardenMediaAttributes(n)
    expect(n.attrs.loading).toBe('eager')
    expect(n.attrs.referrerpolicy).toBe('no-referrer')
  })
  it.each([
    'background: url(https://evil.example/p.gif)',
    'background-image:URL( "https://evil.example/p.gif" )',
    'content: image-set("https://evil.example/p.gif" 1x)',
    '@import url(x)',
  ])('an inline style that loads a url is removed: %s', (style) => {
    const n = fakeNode('DIV', { style })
    expect(hardenMediaAttributes(n)).toEqual(['style'])
    expect('style' in n.attrs).toBe(false)
  })
  it('an inline style without a url is left alone', () => {
    const n = fakeNode('DIV', { style: 'color: red; font-weight: bold' })
    expect(hardenMediaAttributes(n)).toEqual([])
    expect(n.attrs.style).toBe('color: red; font-weight: bold')
  })
  it('a node without attributes or tag is a no-op', () => {
    expect(hardenMediaAttributes({ getAttribute: () => null })).toEqual([])
    expect(hardenMediaAttributes({})).toEqual([])
  })
})

describe('registration', () => {
  it('markdown.js applies the rule inside its one DOMPurify hook', () => {
    const src = readFileSync(path.resolve(here, '../../src/utils/markdown.js'), 'utf8')
    expect(src).toContain("import { hardenMediaAttributes, restrictToCanvasKit } from './sanitizeHooks'")
    const hook = src.slice(src.indexOf("DOMPurify.addHook('afterSanitizeAttributes'"))
    expect(hook.slice(0, hook.indexOf('})'))).toContain('hardenMediaAttributes(node)')
  })
  it('the canvas image and the CSP mirrors agree on https', () => {
    const img = readFileSync(path.resolve(here, '../../src/components/canvas/CanvasImage.vue'), 'utf8')
    expect(img).toContain('referrerpolicy="no-referrer"')
    for (const f of ['../../vite.config.js', '../../security-headers.conf']) {
      const csp = readFileSync(path.resolve(here, f), 'utf8')
      expect(csp).toMatch(/img-src 'self' data: blob: https:;/)
    }
  })
})

// ---------------------------------------------------------------------------
// Canvas mode (trinity-enterprise#537)
// ---------------------------------------------------------------------------
import { restrictToCanvasKit } from '@/utils/sanitizeHooks'

describe('restrictToCanvasKit', () => {
  it('keeps only kit classes and removes the attribute when none survive', () => {
    const n = fakeNode('DIV', { class: 'ck-card fixed inset-0 ck-info' })
    expect(restrictToCanvasKit(n)).toEqual(['class'])
    expect(n.attrs.class).toBe('ck-card ck-info')
    const m = fakeNode('DIV', { class: 'text-red-500 z-50' })
    expect(restrictToCanvasKit(m)).toEqual(['class'])
    expect('class' in m.attrs).toBe(false)
  })
  it('leaves an already-clean class untouched and reports no change', () => {
    const n = fakeNode('SPAN', { class: 'ck-chip ck-warning' })
    expect(restrictToCanvasKit(n)).toEqual([])
    expect(n.attrs.class).toBe('ck-chip ck-warning')
  })
  it('keeps only a bounded width / max-width in style and removes the attribute otherwise', () => {
    const n = fakeNode('DIV', { style: 'width: 40%; position: fixed; inset: 0' })
    expect(restrictToCanvasKit(n)).toEqual(['style'])
    expect(n.attrs.style).toBe('width: 40%')
    const m = fakeNode('DIV', { style: 'position: fixed; z-index: 9999' })
    restrictToCanvasKit(m)
    expect('style' in m.attrs).toBe(false)
  })
  it('composes with the media hardening: a url() style is gone before the kit filter runs', () => {
    const n = fakeNode('DIV', { style: 'width: 40%; background: url(https://t/p.png)' })
    hardenMediaAttributes(n)
    expect('style' in n.attrs).toBe(false)
    expect(restrictToCanvasKit(n)).toEqual([])
  })
  it('ignores a node with no class and no style, and a non-node', () => {
    expect(restrictToCanvasKit(fakeNode('P'))).toEqual([])
    expect(restrictToCanvasKit(null)).toEqual([])
    expect(restrictToCanvasKit({})).toEqual([])
  })
})

describe('the canvas-mode wiring in markdown.js', () => {
  const src = readFileSync(path.resolve(here, '../../src/utils/markdown.js'), 'utf8')
  it('reads the per-call config flag from the hook\'s third argument — no module state', () => {
    const hook = src.slice(src.indexOf("DOMPurify.addHook('afterSanitizeAttributes'"))
    expect(hook).toMatch(/\(node, _data, config\) =>/)
    expect(hook).toMatch(/if \(config && config\.canvasKit\) restrictToCanvasKit\(node\)/)
    expect(src).not.toMatch(/let canvasMode/)
  })
  it('forbids the <style> element on every html/markdown path and only the SVG path keeps it', () => {
    expect(src).toContain("FORBID_TAGS: ['style']")
    const calls = [...src.matchAll(/DOMPurify\.sanitize\((.*)\)/g)].map((m) => m[1]) // one call per line
    const bare = calls.filter((c) => !c.includes('CONFIG'))
    expect(bare).toHaveLength(1)
    expect(bare[0]).toContain('svg')
  })
})
