/**
 * #2211 — an empty composer must not show a vertical scrollbar.
 *
 * The old `autoGrow()` did:
 *
 *     el.style.height = 'auto'
 *     el.style.height = Math.min(el.scrollHeight, 160) + 'px'
 *
 * Under `box-sizing: border-box` — which the textarea has, with a 1px border per
 * side — `scrollHeight` EXCLUDES the border. So the assigned height was 2px shorter
 * than the single line the field must hold, and the browser drew a scrollbar at zero
 * characters. `overflow-y` was never managed either, so the scrollbar stayed
 * available for every height below the ceiling.
 *
 * The arithmetic is a pure function here for two reasons: it can be asserted without
 * a DOM, and both composers (`PortalConversation`, `PortalRoom`) share it — two
 * copies is how one of them silently keeps the bug. `PortalRoom` in fact had NO
 * auto-grow at all, so it gained the corrected version rather than a patched copy.
 */
import { describe, it, expect } from 'vitest'
import { resolveComposerGrowth } from '../../src/components/portal/portalUtils'

const MAX = 160
/** A textarea's three measurements. `border` is split across top and bottom. */
const el = (scrollHeight, border = 2) => ({
  scrollHeight,
  clientHeight: scrollHeight,
  offsetHeight: scrollHeight + border,
})

describe('#2211 empty composer', () => {
  it('is tall enough for its own single line, borders included', () => {
    // The reported bug: one line of content (40px) in a 2px-bordered box needs 42px.
    const { height } = resolveComposerGrowth(el(40), MAX)
    expect(height).toBe(42)
  })

  it('hides overflow-y when there is nothing to scroll', () => {
    expect(resolveComposerGrowth(el(40), MAX).overflowY).toBe('hidden')
    expect(resolveComposerGrowth(el(0), MAX).overflowY).toBe('hidden')
  })

  it('reproduces the old shortfall, so the fix is not a coincidence', () => {
    // What the old code assigned, for the same field: 2px short of 42.
    expect(Math.min(el(40).scrollHeight, MAX)).toBe(40)
    expect(resolveComposerGrowth(el(40), MAX).height).toBeGreaterThan(40)
  })
})

describe('#2211 growth up to the ceiling', () => {
  it('grows with content while below the ceiling', () => {
    expect(resolveComposerGrowth(el(60), MAX).height).toBe(62)
    expect(resolveComposerGrowth(el(100), MAX).height).toBe(102)
  })

  it('clamps at the ceiling and only THEN allows scrolling', () => {
    const at = resolveComposerGrowth(el(158), MAX)     // 158 + 2 == exactly 160
    expect(at.height).toBe(160)
    expect(at.overflowY).toBe('hidden')                // exactly full is not overflowing

    const over = resolveComposerGrowth(el(400), MAX)
    expect(over.height).toBe(160)
    expect(over.overflowY).toBe('auto')
  })

  it('measures the border instead of assuming 2px', () => {
    // A future `border-2` (4px total) must not reintroduce the shortfall.
    expect(resolveComposerGrowth(el(40, 4), MAX).height).toBe(44)
    // ...and a borderless field must not gain phantom pixels.
    expect(resolveComposerGrowth(el(40, 0), MAX).height).toBe(40)
  })
})

describe('#2211 degenerate input', () => {
  it('never returns a negative height or a NaN', () => {
    for (const metrics of [undefined, null, {}, { scrollHeight: -5, offsetHeight: 0, clientHeight: 9 }]) {
      const out = resolveComposerGrowth(metrics, MAX)
      expect(Number.isFinite(out.height)).toBe(true)
      expect(out.height).toBeGreaterThanOrEqual(0)
      expect(['auto', 'hidden']).toContain(out.overflowY)
    }
  })

  it('falls back to the documented ceiling when given a junk max', () => {
    expect(resolveComposerGrowth(el(400), 0).height).toBe(160)
    expect(resolveComposerGrowth(el(400), undefined).height).toBe(160)
    expect(resolveComposerGrowth(el(400), -20).height).toBe(160)
  })
})
