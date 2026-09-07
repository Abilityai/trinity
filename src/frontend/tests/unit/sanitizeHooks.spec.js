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
    expect(src).toContain("import { hardenMediaAttributes } from './sanitizeHooks'")
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
