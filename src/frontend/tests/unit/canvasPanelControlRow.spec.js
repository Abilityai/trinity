// @vitest-environment jsdom
/**
 * ent#724 — the Canvas tab's selector is ONE fixed-height control row
 * (design-system principle 30), not a chip strip that grows a row per few
 * canvases and pushes the canvas down by however many there are.
 *
 * jsdom has no layout, so "the canvas body starts at the same y" is pinned the
 * way it can be here: the control row's rendered STRUCTURE (every element, tag
 * and class above the canvas) is identical for 1, 2 and 40 canvases — only the
 * option list inside the native <select> differs, and a native select does not
 * grow with its options. The in-browser check is stated in the PR.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'

// jsdom has no matchMedia; the canvas import chain reads it at module load.
vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return false },
  }))
})
import { mount, flushPromises } from '@vue/test-utils'
import CanvasPanel from '@/components/canvas/CanvasPanel.vue'
import { canvasOptionLabel, CANVAS_OPTION_MAX } from '@/components/canvas/canvasUtils'

const LONG = 'Decision 1 — Codex subscription auth (ent#602) — archived from main 2026-09-14'

function canvases(n, over = {}) {
  return Array.from({ length: n }, (_, i) => ({
    canvas_id: `c${i + 1}`,
    title: i === 0 ? LONG : `Canvas ${i + 1}`,
    updated_at: `2026-09-${String(28 - (i % 20)).padStart(2, '0')}T10:00:00Z`,
    ...(over[i] || {}),
  }))
}

let wrapper
async function mountPanel(list, props = {}) {
  const fetchDetail = async (id) => ({ canvas_id: id, title: list.find((c) => c.canvas_id === id)?.title, blocks: [] })
  wrapper = mount(CanvasPanel, {
    props: { canvases: list, fetchDetail, canManage: true, deleteCanvas: async () => {}, pinCanvas: async () => {}, ...props },
    global: { stubs: { CanvasKit: { template: '<div><slot /></div>' }, CanvasDocument: true, CanvasBlock: true } },
  })
  await flushPromises()
  return wrapper
}

/** The control row as a structure: every element's tag + classes + testid, options excluded. */
function rowShape(w) {
  const row = w.find('[data-testid="canvas-control-row"]').element
  const walk = (el) => `${el.tagName}.${el.getAttribute('class') || ''}[${el.getAttribute('data-testid') || ''}](${
    [...el.children].filter((c) => c.tagName !== 'OPTION').map(walk).join(',')})`
  return walk(row)
}

/** Everything rendered between the panel root and the canvas body, as tags + testids. */
function aboveCanvas(w) {
  const root = w.element
  const body = w.find('[data-testid="canvas-panel"]').element
  return [...root.children].slice(0, [...root.children].indexOf(body)).map((el) => `${el.tagName}[${el.getAttribute('data-testid') || el.id || ''}]`)
}

afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('the control row does not change with the number of canvases (principle 30)', () => {
  it('is structurally identical for 1, 2 and 40 canvases', async () => {
    const shapes = []
    const above = []
    for (const n of [1, 2, 40]) {
      // 40 would cross the search threshold; hold search off so the comparison
      // is about cardinality alone (search is its own fixed-width slot, below).
      const w = await mountPanel(canvases(n))
      shapes.push(rowShape(w).replace(/INPUT[^,)]*\[canvas-search\]\(\),?/, ''))
      above.push(aboveCanvas(w))
      w.unmount(); wrapper = null
    }
    expect(shapes[1]).toBe(shapes[0])
    expect(shapes[2]).toBe(shapes[0])
    expect(above[1]).toEqual(above[0])
    expect(above[2]).toEqual(above[0])
  })

  it('never renders a chip strip, whatever the count', async () => {
    const w = await mountPanel(canvases(40))
    expect(w.findAll('button[data-canvas-id]')).toHaveLength(0)
    expect(w.findAll('[data-testid="canvas-select"] option')).toHaveLength(40)
  })

  it('a single canvas shows the same row with the dropdown disabled', async () => {
    const w = await mountPanel(canvases(1))
    const select = w.find('[data-testid="canvas-select"]')
    expect(select.exists()).toBe(true)
    expect(select.attributes('disabled')).toBeDefined()
    const two = await mountPanel(canvases(2))
    expect(two.find('[data-testid="canvas-select"]').attributes('disabled')).toBeUndefined()
  })
})

describe('the options', () => {
  it('pinned canvases sort first and carry the pin mark; long titles clamp, the full title rides on `title`', async () => {
    const w = await mountPanel(canvases(3, { 2: { pinned: true } }))
    const opts = w.findAll('[data-testid="canvas-select"] option')
    expect(opts[0].attributes('value')).toBe('c3')
    expect(opts[0].text()).toMatch(/^📌 /)
    const long = opts.find((o) => o.attributes('value') === 'c1')
    expect(long.text().length).toBeLessThanOrEqual(CANVAS_OPTION_MAX)
    expect(long.text().endsWith('…')).toBe(true)
    expect(long.attributes('title')).toBe(LONG)
  })

  it('choosing an option selects that canvas exactly as before (fetch + canvas-selected)', async () => {
    const w = await mountPanel(canvases(3))
    await w.find('[data-testid="canvas-select"]').setValue('c2')
    await flushPromises()
    expect(w.find('[data-testid="canvas-panel"]').attributes('data-canvas-id')).toBe('c2')
    expect(w.emitted('canvas-selected').at(-1)).toEqual(['c2'])
    expect(w.find('[data-testid="canvas-panel"] h3').text()).toBe('Canvas 2')
  })

  it('the full title of the open canvas still renders in the canvas header', async () => {
    const w = await mountPanel(canvases(2))
    await w.find('[data-testid="canvas-select"]').setValue('c1')
    await flushPromises()
    expect(w.find('[data-testid="canvas-panel"] h3').text()).toBe(LONG)
  })
})

describe('Manage lives in the row and opens below it', () => {
  it('toggling manage leaves the row exactly as it was and opens the list below', async () => {
    const w = await mountPanel(canvases(3))
    const before = rowShape(w).replace(/\[canvas-manage-toggle\]\([^)]*\)/, '')
    await w.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    const after = rowShape(w).replace(/\[canvas-manage-toggle\]\([^)]*\)/, '')
    expect(after).toBe(before)
    expect(w.find('[data-testid="canvas-manage-toggle"]').text()).toBe('Done')
    const list = w.find('[data-testid="canvas-manage-list"]')
    expect(list.exists()).toBe(true)
    expect(w.find('#canvas-manage-region').element.contains(list.element)).toBe(true)
    // The list is BELOW the row, not in its place.
    expect(w.find('[data-testid="canvas-select"]').exists()).toBe(true)
  })

  it('Done returns to the canvas that was open', async () => {
    const w = await mountPanel(canvases(3))
    await w.find('[data-testid="canvas-select"]').setValue('c2')
    await flushPromises()
    await w.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await w.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    expect(w.find('[data-testid="canvas-manage-list"]').exists()).toBe(false)
    expect(w.find('[data-testid="canvas-panel"]').attributes('data-canvas-id')).toBe('c2')
  })

  it('without manage permission there is no Manage control, and the row is unchanged otherwise', async () => {
    const w = await mountPanel(canvases(3), { canManage: false })
    expect(w.find('[data-testid="canvas-manage-toggle"]').exists()).toBe(false)
    expect(w.find('[data-testid="canvas-select"]').exists()).toBe(true)
  })
})

describe('canvasOptionLabel', () => {
  it('leaves a short title alone', () => {
    expect(canvasOptionLabel({ title: 'Q4 pipeline' })).toBe('Q4 pipeline')
  })
  it('clamps at a word boundary and drops a dangling separator', () => {
    const out = canvasOptionLabel({ title: LONG })
    expect(out.length).toBeLessThanOrEqual(CANVAS_OPTION_MAX)
    expect(out).toMatch(/…$/)
    expect(out).not.toMatch(/[—\s]…$/)
  })
  it('clamps an unbroken title too', () => {
    expect(canvasOptionLabel({ title: 'x'.repeat(90) })).toHaveLength(CANVAS_OPTION_MAX)
  })
  it('falls back to the id and marks a pin', () => {
    expect(canvasOptionLabel({ canvas_id: 'c9', pinned: true })).toBe('📌 c9')
  })
})
