import { describe, it, expect } from 'vitest'
import { computeInlineCount, FIT_EPSILON } from '../../src/utils/overflowFit.js'

/**
 * #1925 — the priority+ fit rule, extracted from `OverflowTabs.vue` so NavBar's
 * link row can share it. These drive the real function with plain numbers; the
 * DOM half (measuring the mirror row) stays in the components.
 */
describe('computeInlineCount', () => {
  const MORE = 90

  it('renders everything inline before the container has been measured', () => {
    // The first-paint case. Collapsing here is the snap the mirror row exists to
    // avoid, so an unmeasured container must answer "all", never zero.
    expect(computeInlineCount({ containerWidth: 0, itemWidths: [100, 100, 100], moreWidth: MORE })).toBe(3)
    expect(computeInlineCount({ containerWidth: -1, itemWidths: [100], moreWidth: MORE })).toBe(1)
  })

  it('renders everything inline when the whole row fits — no More button', () => {
    expect(computeInlineCount({ containerWidth: 400, itemWidths: [100, 100, 100], moreWidth: MORE })).toBe(3)
  })

  it('spends no More-width to hide a single item that would otherwise fit', () => {
    // 3x100 = 300 into 310: the last one fits outright, so reserving 90px for a
    // trigger that hides exactly one item is strictly worse.
    expect(computeInlineCount({ containerWidth: 310, itemWidths: [100, 100, 110], moreWidth: MORE })).toBe(3)
  })

  it('reserves room for the trigger once two or more items overflow', () => {
    // 5x100 = 500 > 450, so the trigger is needed: 450 - 90 = 360 available,
    // three 100px items pack and the remaining two collapse.
    expect(computeInlineCount({ containerWidth: 450, itemWidths: [100, 100, 100, 100, 100], moreWidth: MORE })).toBe(3)
  })

  it('keeps one item inline whenever the first one fits at all', () => {
    // Container narrower than item + trigger: the packing loop yields 0, and the
    // floor puts the first item back. A nav row that renders only "5 more" is
    // worse than one that renders Dashboard and "4 more".
    expect(computeInlineCount({ containerWidth: 120, itemWidths: [100, 100, 100], moreWidth: MORE })).toBe(1)
  })

  it('yields zero only when even the first item cannot fit', () => {
    expect(computeInlineCount({ containerWidth: 40, itemWidths: [100, 100], moreWidth: MORE })).toBe(0)
  })

  it('counts the gaps between items, not just the items', () => {
    const widths = [100, 100, 100, 100]
    // Sum is 400 and fits 410 with no gap...
    expect(computeInlineCount({ containerWidth: 410, itemWidths: widths, moreWidth: MORE, gap: 0 })).toBe(4)
    // ...but at gap-6 the row is really 400 + 3*24 = 472, so it must collapse.
    // A gap-blind rule keeps the last link inline and lets it clip — the exact
    // failure #1925 removes from NavBar.
    expect(computeInlineCount({ containerWidth: 410, itemWidths: widths, moreWidth: MORE, gap: 24 })).toBeLessThan(4)
  })

  it('charges a gap for the trigger as well as between packed items', () => {
    // 300 container, 24px gaps, 90px trigger → 300-90-24 = 186 available;
    // item + gap + item = 100+24+100 = 224 > 186, so only one packs.
    expect(computeInlineCount({ containerWidth: 300, itemWidths: [100, 100, 100], moreWidth: MORE, gap: 24 })).toBe(1)
  })

  it('tolerates sub-pixel rounding rather than collapsing a row that visually fits', () => {
    // Browsers report fractional widths; a strict `<=` would overflow a row that
    // is a third of a pixel too wide.
    expect(FIT_EPSILON).toBeGreaterThan(0)
    expect(computeInlineCount({
      containerWidth: 300,
      itemWidths: [100.3, 100.3, 100.3],
      moreWidth: MORE,
    })).toBe(3)
  })

  it('treats a missing width list as nothing to lay out', () => {
    expect(computeInlineCount({ containerWidth: 500, itemWidths: [], moreWidth: MORE })).toBe(0)
    expect(computeInlineCount({ containerWidth: 500, itemWidths: undefined, moreWidth: MORE })).toBe(0)
  })
})
