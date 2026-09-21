/**
 * #1923 — the modal keyboard contract's decidable rules.
 *
 * Six bespoke modals shipped with no Esc-to-close and no focus trap: keyboard
 * users could not dismiss them, and Tab walked out behind the overlay.
 *
 * These test the RULES. The wiring — that the listener is attached, that
 * focus() is actually called, that the lock is held and released — is proven
 * by MOUNTING the shell in `baseModal.spec.js` (per-file jsdom opt-in, #2918).
 */
import { describe, it, expect } from 'vitest'
import {
  TABBABLE_SELECTOR, tabbable, nextFocusIndex, isDismissKey, isTabKey,
  initialFocusIndex, isDestructive, isBackdropClick, createScrollLock,
} from '../../src/utils/focusTrap.js'

const el = (over = {}) => ({
  disabled: false, hidden: false, dataset: {},
  getAttribute: (n) => (n === 'tabindex' ? (over.tabindex ?? null)
    : n === 'data-destructive' ? (over.destructive ? '' : null) : null),
  ...over,
})

describe('tabbable', () => {
  it('keeps ordinary focusable elements', () => {
    expect(tabbable([el(), el()])).toHaveLength(2)
  })

  it('drops disabled controls — they match the tag selector but cannot take focus', () => {
    expect(tabbable([el({ disabled: true }), el()])).toHaveLength(1)
  })

  it('drops tabindex="-1"', () => {
    expect(tabbable([el({ tabindex: '-1' }), el()])).toHaveLength(1)
  })

  it('keeps a positive or zero tabindex', () => {
    expect(tabbable([el({ tabindex: '0' })])).toHaveLength(1)
  })

  it('drops hidden elements — cycling onto one strands the user on nothing', () => {
    expect(tabbable([el({ hidden: true }), el()])).toHaveLength(1)
  })

  it('is total on junk input', () => {
    expect(tabbable(null)).toEqual([])
    expect(tabbable([null, undefined])).toEqual([])
  })

  it('selector covers the custom-focusable case', () => {
    expect(TABBABLE_SELECTOR).toContain('[tabindex]:not([tabindex="-1"])')
    expect(TABBABLE_SELECTOR).toContain('button:not([disabled])')
  })
})

describe('nextFocusIndex — the wrap IS the trap', () => {
  it('advances on Tab', () => {
    expect(nextFocusIndex(3, 0, false)).toBe(1)
  })

  it('wraps last -> first, which is what stops focus escaping the overlay', () => {
    expect(nextFocusIndex(3, 2, false)).toBe(0)
  })

  it('wraps first -> last on Shift+Tab', () => {
    expect(nextFocusIndex(3, 0, true)).toBe(2)
  })

  it('retreats on Shift+Tab', () => {
    expect(nextFocusIndex(3, 2, true)).toBe(1)
  })

  it('recovers focus that is outside the modal entirely', () => {
    expect(nextFocusIndex(3, -1, false)).toBe(0)
    expect(nextFocusIndex(3, -1, true)).toBe(2)
  })

  it('pins a single focusable in place rather than dividing by zero', () => {
    expect(nextFocusIndex(1, 0, false)).toBe(0)
    expect(nextFocusIndex(1, 0, true)).toBe(0)
  })

  it('defers to the browser when there is nothing to cycle', () => {
    expect(nextFocusIndex(0, -1, false)).toBeNull()
  })
})

describe('key predicates', () => {
  it('Escape dismisses', () => {
    expect(isDismissKey({ key: 'Escape' })).toBe(true)
    expect(isDismissKey({ key: 'Esc' })).toBe(true)   // older browsers
  })

  it('a modified Escape does not — that is a browser/OS gesture, not a dismiss', () => {
    for (const mod of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey']) {
      expect(isDismissKey({ key: 'Escape', [mod]: true })).toBe(false)
    }
  })

  it('other keys do not dismiss', () => {
    expect(isDismissKey({ key: 'Enter' })).toBe(false)
    expect(isDismissKey(null)).toBe(false)
  })

  it('recognises Tab', () => {
    expect(isTabKey({ key: 'Tab' })).toBe(true)
    expect(isTabKey({ key: 'a' })).toBe(false)
  })
})

describe('initial focus lands on the SAFE action', () => {
  it('prefers an explicitly requested index', () => {
    expect(initialFocusIndex([el(), el()], { explicitIndex: 1 })).toBe(1)
  })

  it('ignores an out-of-range explicit index rather than focusing nothing', () => {
    expect(initialFocusIndex([el()], { explicitIndex: 9 })).toBe(0)
  })

  it('skips the destructive control — a reflexive Enter must not delete', () => {
    const items = [el({ destructive: true, dataset: { destructive: '' } }), el()]
    expect(initialFocusIndex(items)).toBe(1)
  })

  it('falls back to the first when everything is destructive', () => {
    const items = [el({ destructive: true, dataset: { destructive: '' } })]
    expect(initialFocusIndex(items)).toBe(0)
  })

  it('returns null with nothing to focus', () => {
    expect(initialFocusIndex([])).toBeNull()
  })

  it('reads destructiveness from a declared marker, never from label text', () => {
    // Guessing from words would be wrong in every language but English.
    expect(isDestructive(el({ dataset: { destructive: '' } }))).toBe(true)
    expect(isDestructive(el())).toBe(false)
  })
})

describe('backdrop click', () => {
  it('is identity against the overlay, not a rectangle test', () => {
    // A rectangle test mis-fires for a select popup or date picker that
    // renders at the document root, closing the modal under the user.
    const overlay = { id: 'overlay' }
    expect(isBackdropClick({ target: overlay }, overlay)).toBe(true)
    expect(isBackdropClick({ target: { id: 'panel' } }, overlay)).toBe(false)
    expect(isBackdropClick(null, overlay)).toBe(false)
  })
})

describe('createScrollLock — a ref count over one global value', () => {
  it('the first holder hides overflow, the last release restores what was there', () => {
    const style = { overflow: 'auto' }
    const lock = createScrollLock(() => style)
    lock.acquire()
    expect(style.overflow).toBe('hidden')
    lock.acquire()                       // a nested modal
    lock.release()                       // the nested one closes
    expect(style.overflow).toBe('hidden')   // the outer still holds it
    expect(lock.holders).toBe(1)
    lock.release()
    expect(style.overflow).toBe('auto')  // restored, not cleared
    expect(lock.holders).toBe(0)
  })

  it('a release with no holder is a no-op, never a clear', () => {
    const style = { overflow: 'hidden' }   // some other component's lock
    const lock = createScrollLock(() => style)
    lock.release()
    expect(style.overflow).toBe('hidden')
  })
})
