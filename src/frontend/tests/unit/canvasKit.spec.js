/**
 * The canvas design kit's allowlists (trinity-enterprise#537).
 *
 * In canvas mode the sanitiser admits the kit's classes and nothing else, and
 * an inline style keeps only a bounded `width` / `max-width`. Pure, so it is
 * driven here without a DOM; `restrictToCanvasKit` (the hook body) is covered
 * in `sanitizeHooks.spec.js` with a fake node.
 */
import { describe, it, expect } from 'vitest'

import {
  ALLOWED_INLINE_PROPERTIES,
  KIT_CLASSES,
  KIT_CLASS_PREFIX,
  filterInlineStyle,
  filterKitClasses,
} from '@/utils/canvasKit'

describe('KIT_CLASSES', () => {
  it('is a frozen set of ck-* names only', () => {
    expect(KIT_CLASSES.size).toBeGreaterThan(20)
    for (const c of KIT_CLASSES) expect(c.startsWith(KIT_CLASS_PREFIX)).toBe(true)
    expect(Object.isFrozen(KIT_CLASSES)).toBe(true)
  })
  it('does not contain the app-emitted layout families', () => {
    for (const c of KIT_CLASSES) {
      expect(c.startsWith('ck-layout')).toBe(false)
      expect(c.startsWith('ck-slot')).toBe(false)
    }
  })
})

describe('filterKitClasses', () => {
  it('keeps kit classes and drops everything else, in order, deduplicated', () => {
    expect(filterKitClasses('ck-card fixed inset-0 z-50 ck-card ck-info')).toBe('ck-card ck-info')
  })
  it('is exact membership, never a prefix match', () => {
    expect(filterKitClasses('ck-anything ck-cardx ck-')).toBeNull()
  })
  it('returns null when nothing survives, so the attribute is removed', () => {
    expect(filterKitClasses('text-red-500 hidden')).toBeNull()
    expect(filterKitClasses('')).toBeNull()
    expect(filterKitClasses(null)).toBeNull()
    expect(filterKitClasses(42)).toBeNull()
  })
  it('tolerates any whitespace between classes', () => {
    expect(filterKitClasses('  ck-chip\t\nck-success  ')).toBe('ck-chip ck-success')
  })
})

describe('filterInlineStyle', () => {
  it('admits only width and max-width', () => {
    expect(ALLOWED_INLINE_PROPERTIES).toEqual(['width', 'max-width'])
  })
  it('keeps a bounded width and drops the rest of the declaration list', () => {
    expect(filterInlineStyle('width: 40%; position: fixed; inset: 0; z-index: 9999')).toBe('width: 40%')
    expect(filterInlineStyle('MAX-WIDTH : 480PX ; color: red')).toBe('max-width: min(480px, 100%)')
  })
  it('clamps magnitudes: percent ≤ 100, pixels ≤ 9999', () => {
    expect(filterInlineStyle('width: 100%')).toBe('width: 100%')
    expect(filterInlineStyle('width: 101%')).toBeNull()
    expect(filterInlineStyle('width: 9999px')).toBe('width: min(9999px, 100%)')
    expect(filterInlineStyle('width: 10000px')).toBeNull()
    expect(filterInlineStyle('width: 0%')).toBe('width: 0%')
    expect(filterInlineStyle('width: -5px')).toBeNull()
  })
  it('makes url(), calc(), var(), expression() and !important unreachable by shape', () => {
    for (const v of [
      'width: url(https://x/y.png)',
      'width: calc(100% - 1px)',
      'width: var(--x)',
      'width: expression(alert(1))',
      'width: 40% !important',
      'width: 40%; height: 99999px',
      'width: "40%"',
      'width: 40%\\',
      'width:40%:50%',
    ]) {
      const out = filterInlineStyle(v)
      expect(out === null || out === 'width: 40%').toBe(true)
      if (out) expect(out).toBe('width: 40%')
    }
    expect(filterInlineStyle('width: url(https://x/y.png)')).toBeNull()
    expect(filterInlineStyle('width: calc(100% - 1px)')).toBeNull()
    expect(filterInlineStyle('width: 40% !important')).toBeNull()
  })
  it('never admits a property outside the set even with an allowed-looking value', () => {
    for (const p of ['height', 'position', 'margin', 'margin-left', 'z-index', 'transform', 'opacity', 'pointer-events', 'grid-column', 'flex', 'text-align', 'background']) {
      expect(filterInlineStyle(`${p}: 40%`)).toBeNull()
      expect(filterInlineStyle(`${p}: 12px`)).toBeNull()
    }
  })
  it('returns null for empty, malformed or non-string input', () => {
    expect(filterInlineStyle('')).toBeNull()
    expect(filterInlineStyle('width')).toBeNull()
    expect(filterInlineStyle(';;;')).toBeNull()
    expect(filterInlineStyle(undefined)).toBeNull()
  })
})
