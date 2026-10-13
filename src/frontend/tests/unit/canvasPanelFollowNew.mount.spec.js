// @vitest-environment jsdom
/**
 * #3218 — a canvas the agent creates while another one is open is SHOWN, not
 * left in the selector for the reader to find.
 *
 * Mounted: the real CanvasPanel, its `canvases` prop swapped the way a list
 * refresh swaps it. The rule itself (`canvasesAppeared`) is pinned in
 * canvasFollowNew.spec.js; this file pins that the panel acts on it — and the
 * neighbouring behaviours it must leave alone.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return false },
  }))
})
import { mount, flushPromises } from '@vue/test-utils'
import CanvasPanel from '@/components/canvas/CanvasPanel.vue'

const row = (id, updated, extra = {}) => ({ canvas_id: id, title: `Canvas ${id}`, updated_at: updated, ...extra })
const BASE = [row('a', '2026-10-01T10:00:00Z'), row('b', '2026-09-30T10:00:00Z')]

let wrapper
let fetched
async function mountPanel(list, props = {}) {
  fetched = []
  const fetchDetail = vi.fn(async (id) => {
    fetched.push(id)
    const c = wrapper?.props('canvases')?.find((x) => x.canvas_id === id) || list.find((x) => x.canvas_id === id)
    return { canvas_id: id, title: c?.title, updated_at: c?.updated_at, blocks: [] }
  })
  wrapper = mount(CanvasPanel, {
    props: {
      canvases: list, fetchDetail, canManage: true,
      deleteCanvas: async () => {}, pinCanvas: async () => {},
      shareCanvas: async () => ({ id: 's1', token: 't' }), listShares: async () => [],
      ...props,
    },
    global: { stubs: { CanvasKit: { template: '<div><slot /></div>' }, CanvasDocument: true, CanvasBlock: true } },
  })
  await flushPromises()
  return wrapper
}

const open = () => wrapper.find('[data-testid="canvas-panel"]').attributes('data-canvas-id')
const selected = () => wrapper.emitted('canvas-selected')?.map((e) => e[0]) || []
async function refresh(list) {
  await wrapper.setProps({ canvases: list })
  await flushPromises()
}

afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('a newly created canvas is followed (#3218)', () => {
  it('first load selects the first row, as before — not "everything is new"', async () => {
    await mountPanel(BASE)
    expect(open()).toBe('a')
    expect(selected()).toEqual(['a'])
  })

  it('a refresh with a new canvas switches to it, fetches its blocks and announces it', async () => {
    await mountPanel(BASE)
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    expect(open()).toBe('new')
    expect(fetched.at(-1)).toBe('new')
    expect(selected().at(-1)).toBe('new')
    expect(wrapper.find('[data-testid="canvas-select"]').element.value).toBe('new')
  })

  it('follows the new id even when a pinned canvas sorts ahead of it', async () => {
    const pinned = [row('p', '2026-09-01T10:00:00Z', { pinned: true }), ...BASE]
    await mountPanel(pinned)
    await refresh([...pinned, row('new', '2026-09-02T10:00:00Z')])
    expect(open()).toBe('new')
  })

  it('several new canvases in one refresh: the most recently updated wins', async () => {
    await mountPanel(BASE)
    await refresh([...BASE, row('n1', '2026-10-03T10:00:00Z'), row('n2', '2026-10-04T10:00:00Z')])
    expect(open()).toBe('n2')
  })

  it('a refresh with nothing new leaves the reader where they are', async () => {
    await mountPanel(BASE)
    wrapper.find('[data-testid="canvas-select"]').setValue('b')
    await flushPromises()
    await refresh(BASE.map((c) => ({ ...c })))
    expect(open()).toBe('b')
  })

  it('a rewrite of the open canvas still re-reads it in place', async () => {
    await mountPanel(BASE)
    const before = fetched.length
    await refresh([row('a', '2026-10-06T10:00:00Z'), BASE[1]])
    expect(open()).toBe('a')
    expect(fetched.length).toBe(before + 1)
    expect(fetched.at(-1)).toBe('a')
  })

  it('a rewrite of a different, existing canvas does not pull focus', async () => {
    await mountPanel(BASE)
    await refresh([BASE[0], row('b', '2026-10-07T10:00:00Z')])
    expect(open()).toBe('a')
  })

  it('a different agent\'s list under the same panel selects its first row, as a first load', async () => {
    await mountPanel(BASE)
    await refresh([row('x1', '2026-09-01T10:00:00Z', { pinned: true }), row('x2', '2026-10-09T10:00:00Z')])
    expect(open()).toBe('x1')
  })

  it('deleting the open canvas still falls back to the first row', async () => {
    await mountPanel(BASE)
    await refresh([BASE[1]])
    expect(open()).toBe('b')
  })
})

describe('a panel that mounts BEFORE its list loads (#3218 review C1)', () => {
  // Agent Detail's Canvas tab mounts the panel with `canvases: []` and loads
  // the real list afterwards. An empty list is not a baseline: the first real
  // list is a first load, so the pinned-first order's first row opens.
  it('empty mount, then a list with a pinned older row ahead of a newer one: the pinned row opens', async () => {
    await mountPanel([])
    await refresh([row('p', '2026-09-01T10:00:00Z', { pinned: true }), row('x', '2026-10-05T10:00:00Z')])
    expect(open()).toBe('p')
    expect(selected()).toEqual(['p'])
  })

  it('empty mount, then the list, then the list plus a new canvas: the new one is still followed', async () => {
    await mountPanel([])
    await refresh(BASE)
    expect(open()).toBe('a')
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    expect(open()).toBe('new')
  })
})

describe('the reader is never yanked mid-interaction (#3218)', () => {
  it('in manage mode the switch waits, and happens when manage ends', async () => {
    await mountPanel(BASE)
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    expect(open()).toBe('a')
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await flushPromises()
    expect(open()).toBe('new')
  })

  it('with the share dialog open the switch waits until it closes', async () => {
    await mountPanel(BASE)
    await wrapper.find('[data-testid="canvas-share-open"]').trigger('click')
    await flushPromises()
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    expect(open()).toBe('a')
    expect(wrapper.find('[data-testid="canvas-share-panel"]').exists()).toBe(true)
    const close = wrapper.findAll('[data-testid="canvas-share-panel"] button').find((b) => b.text() === 'Close')
    await close.trigger('click')
    await flushPromises()
    expect(open()).toBe('new')
  })

  it('a canvas the reader picks while a switch is waiting cancels it', async () => {
    await mountPanel(BASE)
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    wrapper.find('[data-testid="canvas-select"]').setValue('b')
    await flushPromises()
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await flushPromises()
    expect(open()).toBe('b')
  })

  it('a waiting canvas that is gone by the time the interaction ends is not followed', async () => {
    await mountPanel(BASE)
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await refresh([...BASE, row('new', '2026-10-05T10:00:00Z')])
    await refresh(BASE)
    await wrapper.find('[data-testid="canvas-manage-toggle"]').trigger('click')
    await flushPromises()
    expect(open()).toBe('a')
  })

  it('a match the search selects while a switch is waiting cancels it (#3218 review I2)', async () => {
    // More than six canvases, so the search box renders.
    const many = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((id, i) => row(id, `2026-09-2${i}T10:00:00Z`))
    await mountPanel(many)
    const search = wrapper.find('[data-testid="canvas-search"]')
    await search.setValue('zz')
    await flushPromises()
    await refresh([...many, row('new', '2026-10-05T10:00:00Z')])
    // Narrowing to one hit selects it (ent#553) — the reader found it by searching.
    await search.setValue('canvas c')
    await flushPromises()
    expect(open()).toBe('c')
    await search.setValue('')
    await flushPromises()
    expect(open()).toBe('c')
  })
})
