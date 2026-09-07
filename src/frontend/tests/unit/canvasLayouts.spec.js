/**
 * Starter layouts (trinity-enterprise#537): a layout never hides a block.
 *
 * Driven through `renderableBlocks` on purpose — that rebuild is a field
 * allowlist, and the engineering review's finding was that `slot` never
 * reached the layout because the allowlist did not name it. A spec on raw
 * blocks would pass while the UI stayed stacked.
 */
import { describe, it, expect } from 'vitest'

import { CANVAS_TEMPLATES, LAYOUTS, SLOT_RE, placeBlocks } from '@/components/canvas/canvasLayouts'
import { renderableBlocks } from '@/components/canvas/canvasUtils'

const raw = (slot, kind = 'json', extra = {}) => ({ kind, slot, payload: {}, ...extra })

describe('LAYOUTS', () => {
  it('names the four templates from the issue, each with its slots', () => {
    expect(CANVAS_TEMPLATES).toEqual(['dashboard', 'report', 'brief', 'status-board'])
    expect(LAYOUTS.dashboard.slots).toEqual(['header', 'kpis', 'main', 'side', 'footer'])
    expect(LAYOUTS.report.slots).toEqual(['header', 'summary', 'body', 'figures', 'appendix'])
    expect(LAYOUTS.brief.slots).toEqual(['header', 'key-points', 'body'])
    expect(LAYOUTS['status-board'].slots).toEqual(['header', 'status', 'issues', 'next', 'log'])
  })
  it('every slot satisfies the slot charset and every gridSlot is a slot', () => {
    for (const [, layout] of Object.entries(LAYOUTS)) {
      for (const s of layout.slots) expect(SLOT_RE.test(s)).toBe(true)
      for (const g of layout.gridSlots) expect(layout.slots).toContain(g)
    }
  })
})

describe('placeBlocks', () => {
  it('places slotted blocks into regions in layout order and keeps block order within a region', () => {
    const blocks = renderableBlocks([
      raw('main', 'markdown', { id: 'm1' }),
      raw('kpis', 'kpi', { id: 'k1' }),
      raw('header', 'markdown', { id: 'h' }),
      raw('kpis', 'kpi', { id: 'k2' }),
    ])
    const p = placeBlocks('dashboard', blocks)
    expect(p.template).toBe('dashboard')
    expect(p.regions.map((r) => r.slot)).toEqual(['header', 'kpis', 'main'])
    expect(p.regions[1].blocks.map((b) => b.id)).toEqual(['k1', 'k2'])
    expect(p.regions[1].grid).toBe(true)
    expect(p.regions[2].grid).toBe(false)
    expect(p.unslotted).toEqual([])
  })
  it('never hides a block: unslotted and unknown-slot blocks render after the layout', () => {
    const blocks = renderableBlocks([
      raw('header', 'markdown', { id: 'h' }),
      raw(undefined, 'json', { id: 'u1' }),
      raw('sidebar-2', 'json', { id: 'u2' }),
      raw('Kpis', 'json', { id: 'u3' }), // malformed → treated as unslotted, still rendered
    ])
    const p = placeBlocks('dashboard', blocks)
    expect(p.regions.map((r) => r.slot)).toEqual(['header'])
    expect(p.unslotted.map((b) => b.id)).toEqual(['u1', 'u2', 'u3'])
    const total = p.regions.reduce((n, r) => n + r.blocks.length, 0) + p.unslotted.length
    expect(total).toBe(blocks.length)
  })
  it('does not render empty regions', () => {
    const p = placeBlocks('brief', renderableBlocks([raw('body', 'markdown')]))
    expect(p.regions.map((r) => r.slot)).toEqual(['body'])
  })
  it('degrades to stacked (null) with no template, an unknown template, or nothing slotted', () => {
    const blocks = renderableBlocks([raw('main'), raw(null)])
    expect(placeBlocks(null, blocks)).toBeNull()
    expect(placeBlocks(undefined, blocks)).toBeNull()
    expect(placeBlocks('poster', blocks)).toBeNull()
    expect(placeBlocks('dashboard', renderableBlocks([raw(null), raw('nowhere')]))).toBeNull()
    expect(placeBlocks('dashboard', [])).toBeNull()
    expect(placeBlocks('dashboard', undefined)).toBeNull()
  })
  it('a slot the template does not know is unslotted even if another template knows it', () => {
    const p = placeBlocks('brief', renderableBlocks([raw('kpis', 'kpi'), raw('header')]))
    expect(p.regions.map((r) => r.slot)).toEqual(['header'])
    expect(p.unslotted.map((b) => b.slot)).toEqual(['kpis'])
  })
})

describe('renderableBlocks carries slot', () => {
  it('keeps a well-formed slot and nulls a malformed or missing one', () => {
    const out = renderableBlocks([raw('key-points'), raw('Bad Slot'), raw(''), { kind: 'json' }, raw(7)])
    expect(out.map((b) => b.slot)).toEqual(['key-points', null, null, null, null])
  })
})
