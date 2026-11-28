// @vitest-environment jsdom
/**
 * The real FleetGrid on a fresh install with no saved layout
 * (trinity-enterprise#1416): it hands the store its first-load arrangement,
 * reads the core agents from the first-run store, and keeps the board fitted
 * to the window (as agents arrive and as the assistant panel opens and closes)
 * until the person pans or zooms.
 *
 * Mounted (#2918): FleetGrid with the real fleetGrid / firstRun / prefs stores,
 * the network mocked. jsdom has no layout, so the canvas size is given through
 * clientWidth/clientHeight and the fit is read back from the world transform.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.hoisted(() => {
  const store = new Map()
  // jsdom on Node 20 (CI) exposes `window.localStorage` as a getter-only
  // accessor, so a plain assignment throws at load. Define it instead
  // (autonomyDialPanel.spec.js).
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    },
  })
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})
const { axiosMock, resizeCallbacks } = vi.hoisted(() => ({
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
  resizeCallbacks: [],
}))
vi.mock('axios', () => ({
  default: Object.assign(axiosMock, {
    create: () => ({ ...axiosMock, interceptors: { request: { use() {} }, response: { use() {} } }, defaults: { headers: { common: {} } } }),
    interceptors: { request: { use() {} }, response: { use() {} } },
    defaults: { headers: { common: {} } },
  }),
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ authHeader: { Authorization: 'Bearer t' }, user: { role: 'admin' }, principalId: 'ada', isAuthenticated: true }),
}))

globalThis.ResizeObserver = class {
  constructor(cb) { resizeCallbacks.push(cb) }
  observe() {}
  unobserve() {}
  disconnect() {}
}

import FleetGrid from '@/components/FleetGrid.vue'
import { useFleetGridStore } from '@/stores/fleetGrid'
import { useFirstRunStore } from '@/stores/firstRun'
import { cellXY, defaultLayout, CELL_W, CELL_H } from '@/utils/gridLayout'

const SYSTEM = { name: 'trinity-system', status: 'running', is_system: true, tags: [] }
const CHIEF = { name: 'corbin', status: 'running', tags: ['dept-headquarters'] }
const BRAIN = { name: 'cornelius', status: 'running', tags: ['dept-headquarters', 'reports-to-corbin'] }

let canvas = { w: 1440, h: 800 }
// The first-run read FleetGrid makes for itself: a fresh install by default.
let firstRunRead
Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return this.classList?.contains('fleet-canvas') ? canvas.w : 0 } })
Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get() { return this.classList?.contains('fleet-canvas') ? canvas.h : 0 } })

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  resizeCallbacks.length = 0
  canvas = { w: 1440, h: 800 }
  axiosMock.get.mockReset()
  axiosMock.put.mockReset()
  firstRunRead = () => Promise.resolve({ data: { fresh_install: true, core_agents: ['corbin', 'cornelius'] }, headers: {} })
  axiosMock.get.mockImplementation((url) => (String(url).includes('/onboarding/first-run')
    ? firstRunRead()
    : Promise.resolve({ data: {}, headers: {} })))
  axiosMock.put.mockImplementation(() => Promise.resolve({ data: { updated_at: 's1' } }))
})

async function mountGrid(agents, core = ['corbin', 'cornelius']) {
  const fresh = firstRunRead
  firstRunRead = () => fresh().then((r) => ({ ...r, data: { ...r.data, core_agents: core } }))
  const wrapper = mount(FleetGrid, {
    props: { agents, orgAgents: agents, arrivals: [] },
    global: { stubs: { AgentTile: true, RouterLink: true, 'router-link': true } },
    attachTo: document.body,
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

function worldTransform(wrapper) {
  const style = wrapper.find('.gv-world').attributes('style') || ''
  expect(style).toMatch(/scale|translate/)   // never a vacuous comparison of two blanks
  return style
}

describe('FleetGrid first load (ent#1416)', () => {
  it('stacks the Brain below the Chief of Staff and sets the system agent apart', async () => {
    await mountGrid([SYSTEM, BRAIN, CHIEF])
    const store = useFleetGridStore()

    expect(store.layoutIsAuto).toBe(true)
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
    expect(store.layout['trinity-system']).toEqual({ c: 2, r: 0 })
    // the info tiles sit in the row directly above
    const tiles = Object.entries(store.layout).filter(([k]) => k.startsWith('widget:'))
    expect(tiles.length).toBeGreaterThan(0)
    expect(tiles.every(([, p]) => p.r === -1)).toBe(true)
  })

  it('a core agent the first-run store adds joins the pair, below it', async () => {
    const extra = { name: 'archivist', status: 'running', tags: ['dept-headquarters'] }
    await mountGrid([SYSTEM, BRAIN, CHIEF, extra], ['cornelius', 'archivist', 'corbin'])
    const store = useFleetGridStore()
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
    expect(store.layout.archivist).toEqual({ c: 0, r: 2 })
  })

  // Live walk on 14bf38d (2026-11-28): the first-run read lands while the seed
  // pass is still running, so it lists only the core agents present THEN. The
  // Chief of Staff created afterwards must still go above the Brain — the
  // first paint read it as an ordinary agent and put it beside it.
  it('stacks the Chief of Staff above the Brain when it lands after the first-run read', async () => {
    const early = [SYSTEM, { ...BRAIN, tags: [] }]
    const wrapper = await mountGrid(early, ['cornelius'])
    const store = useFleetGridStore()
    expect(store.layout.cornelius).toEqual({ c: 0, r: 0 })

    const landed = [SYSTEM, { ...BRAIN, tags: [] }, { ...CHIEF, tags: [] }]
    await wrapper.setProps({ agents: landed, orgAgents: landed })
    await flushPromises()
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })

    const tagged = [SYSTEM, BRAIN, CHIEF]   // the org tags land a poll later
    await wrapper.setProps({ agents: tagged, orgAgents: tagged })
    await flushPromises()
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
    expect(store.layoutIsAuto).toBe(true)
  })

  it('stacks the pair when the first-run read came before either core agent existed', async () => {
    const wrapper = await mountGrid([SYSTEM], [])
    const roster = [SYSTEM, BRAIN, CHIEF]
    await wrapper.setProps({ agents: roster, orgAgents: roster })
    await flushPromises()
    const store = useFleetGridStore()
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
  })

  it('writes no layout while nobody has arranged it', async () => {
    await mountGrid([SYSTEM, BRAIN, CHIEF])
    await new Promise((r) => setTimeout(r, 50))
    const layoutPuts = axiosMock.put.mock.calls.filter(([url]) => String(url).endsWith('/grid_layout'))
    expect(layoutPuts).toEqual([])
  })

  it('places an agent built in the first session next to the pair', async () => {
    const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
    const roster = [SYSTEM, BRAIN, CHIEF, { name: 'lead-finder', status: 'running', tags: [] }]
    await wrapper.setProps({ agents: roster, orgAgents: roster })
    await flushPromises()
    const store = useFleetGridStore()
    expect(store.layout['lead-finder']).toEqual({ c: 1, r: 0 })
  })

  it('re-fits when the canvas changes size (assistant opened or closed)', async () => {
    const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
    const wide = worldTransform(wrapper)

    canvas = { w: 1000, h: 800 }   // the assistant panel took ~440px
    resizeCallbacks.forEach((cb) => cb([]))
    await nextTick()

    expect(worldTransform(wrapper)).not.toBe(wide)
  })

  // Live walk on 14bf38d: with the assistant closed (98%) the fourth fleet
  // tile ran off the right edge and the zoom controls sat on the Brain's card;
  // with it open, the org toolbar covered a tile. The first view frames the
  // whole board (every tile, the Headquarters group, the system agent) inside
  // the canvas and clear of the canvas overlays. Second ruling (trinity-pm,
  // 2026-11-28): with the assistant open that cost too much zoom (57% at
  // 1440x900, 47% at 1280x800) for a new person to read the cards, so on a
  // canvas too narrow for four tiles in a row at 70% the tiles go two to a row.
  // Canvas sizes measured in a real run of the page: 1440x900 and 1280x800,
  // assistant panel closed and open.
  describe('the first view fits inside the canvas, clear of its controls', () => {
    const CANVASES = {
      '1440x900 assistant closed': { w: 1377, h: 789, tileRows: 1, zoom: 0.7 },
      '1440x900 assistant open': { w: 1041, h: 789, tileRows: 2, zoom: 0.7 },
      '1280x800 assistant closed': { w: 1217, h: 689, tileRows: 1, zoom: 0.65 },
      '1280x800 assistant open': { w: 881, h: 689, tileRows: 2, zoom: 0.6 },
    }
    // The overlays as measured in that real run (canvas pixels): the org
    // toolbar and the Tiles button top-right, the zoom controls and readout
    // bottom-left, the legend bottom-right.
    const overlays = (w, h) => [
      { l: w - 285, t: 14, r: w - 16, b: 49 },
      { l: w - 74, t: 58, r: w - 16, b: 88 },
      { l: 14, t: h - 100, r: 44, b: h - 14 },
      { l: 50, t: h - 38, r: 80, b: h - 18 },
      { l: w - 404, t: h - 44, r: w - 84, b: h - 14 },
    ]
    const OVERHANG = 28   // the avatar hangs off a card's left edge
    const readView = (wrapper) => {
      const m = worldTransform(wrapper).match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)\s*scale\(([-\d.]+)\)/)
      const [tx, ty, z] = m.slice(1).map(Number)
      return { tx, ty, z }
    }
    const tileRows = (store) => new Set(Object.entries(store.layout).filter(([k]) => k.startsWith('widget:')).map(([, p]) => p.r)).size
    for (const [label, size] of Object.entries(CANVASES)) {
      it(`${label}: tiles ${size.tileRows === 1 ? 'in one row' : 'two to a row'}, readable, nothing under a control`, async () => {
        canvas = size
        const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
        const store = useFleetGridStore()
        const { tx, ty, z } = readView(wrapper)
        const keys = Object.keys(store.layout)
        expect(keys.filter((k) => k.startsWith('widget:')).length).toBe(4)
        expect(tileRows(store)).toBe(size.tileRows)
        expect(z).toBeGreaterThanOrEqual(size.zoom)
        for (const k of keys) {
          const [x, y] = cellXY(store.layout[k].c, store.layout[k].r)
          const box = { l: x * z + tx - OVERHANG * z, t: y * z + ty, r: (x + CELL_W) * z + tx, b: (y + CELL_H) * z + ty }
          expect(box.l, `${k} left`).toBeGreaterThanOrEqual(0)
          expect(box.t, `${k} top`).toBeGreaterThanOrEqual(0)
          expect(box.r, `${k} right`).toBeLessThanOrEqual(size.w)
          expect(box.b, `${k} bottom`).toBeLessThanOrEqual(size.h)
          for (const o of overlays(size.w, size.h)) {
            const apart = box.r <= o.l || box.l >= o.r || box.b <= o.t || box.t >= o.b
            expect(apart, `${k} under the control at ${JSON.stringify(o)}`).toBe(true)
          }
        }
      })
    }

    it('opening the assistant puts the tiles two to a row; closing it puts them back in one', async () => {
      canvas = { w: 1377, h: 789 }
      const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
      const store = useFleetGridStore()
      expect(tileRows(store)).toBe(1)

      canvas = { w: 1041, h: 789 }
      resizeCallbacks.forEach((cb) => cb([]))
      await nextTick()
      expect(tileRows(store)).toBe(2)
      expect(readView(wrapper).z).toBeGreaterThanOrEqual(0.7)
      expect(store.layoutIsAuto).toBe(true)

      canvas = { w: 1377, h: 789 }
      resizeCallbacks.forEach((cb) => cb([]))
      await nextTick()
      expect(tileRows(store)).toBe(1)
    })

    it('once the person zooms, opening the assistant no longer reshapes the board', async () => {
      canvas = { w: 1377, h: 789 }
      const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
      const store = useFleetGridStore()
      wrapper.find('.fleet-canvas').element.dispatchEvent(new WheelEvent('wheel', { deltaY: -100, clientX: 10, clientY: 10, cancelable: true }))
      await nextTick()
      canvas = { w: 1041, h: 789 }
      resizeCallbacks.forEach((cb) => cb([]))
      await nextTick()
      expect(tileRows(store)).toBe(1)
    })
  })

  it('stops re-fitting once the person zooms', async () => {
    const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
    wrapper.find('.fleet-canvas').element.dispatchEvent(new WheelEvent('wheel', { deltaY: -100, clientX: 10, clientY: 10, cancelable: true }))
    await nextTick()
    const zoomed = worldTransform(wrapper)

    canvas = { w: 1000, h: 800 }
    resizeCallbacks.forEach((cb) => cb([]))
    await nextTick()

    expect(worldTransform(wrapper)).toBe(zoomed)
  })

  it('an existing install with nothing saved keeps the old default placement', async () => {
    firstRunRead = () => Promise.resolve({ data: { fresh_install: false, core_agents: ['cornelius'] }, headers: {} })
    const roster = [SYSTEM, BRAIN, CHIEF]
    await mountGrid(roster, ['cornelius'])
    const store = useFleetGridStore()

    expect(store.layoutIsAuto).toBe(false)
    const old = defaultLayout(roster.map((a) => a.name), new Set(['trinity-system']))
    for (const a of roster) expect(store.layout[a.name]).toEqual(old[a.name])
  })

  it('arranges nothing, and saves nothing, until it knows the install is fresh', async () => {
    let answer
    firstRunRead = () => new Promise((r) => { answer = r })
    await mountGrid([SYSTEM, BRAIN, CHIEF])
    const store = useFleetGridStore()
    const layoutPuts = () => axiosMock.put.mock.calls.filter(([url]) => String(url).endsWith('/grid_layout'))

    expect(store.layoutIsAuto).toBe(false)
    expect(store.layout.corbin).toBeUndefined()
    expect(layoutPuts()).toEqual([])

    answer({ data: { fresh_install: true, core_agents: ['corbin', 'cornelius'] }, headers: {} })
    await flushPromises()
    await nextTick()

    expect(store.layoutIsAuto).toBe(true)
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(layoutPuts()).toEqual([])
  })

  // Validation vrtwKN9y0mZ42UNpVdCSHQ: "not known" is never "existing install".
  // A failed read, or the backend answering unknown (asked before the setup
  // pass recorded its verdict), shows the old placement WITHOUT saving it and
  // asks again; only a known answer or a person's move saves a layout.
  describe('when the install kind is not known', () => {
    const layoutPuts = () => axiosMock.put.mock.calls.filter(([url]) => String(url).endsWith('/grid_layout'))
    const roster = [SYSTEM, BRAIN, CHIEF]
    const old = () => defaultLayout(roster.map((a) => a.name), new Set(['trinity-system']))
    const firstRunReads = () => axiosMock.get.mock.calls.filter(([url]) => String(url).includes('/onboarding/first-run')).length

    beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) })
    afterEach(() => { vi.useRealTimers() })

    it('a failed first-run read shows the old placement and saves nothing to /grid_layout', async () => {
      firstRunRead = () => Promise.reject(new Error('Network Error'))
      await mountGrid(roster, ['cornelius'])
      const store = useFleetGridStore()

      for (const a of roster) expect(store.layout[a.name]).toEqual(old()[a.name])
      expect(store.layoutIsAuto).toBe(true)   // shown, not saved

      await vi.advanceTimersByTimeAsync(120000)   // every retry, and any debounced save
      expect(firstRunReads()).toBeGreaterThan(1)  // it asked again
      expect(layoutPuts()).toEqual([])
      expect(store.layoutIsAuto).toBe(true)
    })

    it('retries the read and opens on the first-load arrangement once it is known', async () => {
      let calls = 0
      firstRunRead = () => (++calls === 1
        ? Promise.reject(new Error('timeout of 30000ms exceeded'))
        : Promise.resolve({ data: { fresh_install: true }, headers: {} }))
      await mountGrid(roster)
      const store = useFleetGridStore()
      expect(store.layout.corbin).toEqual(old().corbin)

      await vi.advanceTimersByTimeAsync(120000)
      expect(store.layoutIsAuto).toBe(true)
      expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
      expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
      expect(layoutPuts()).toEqual([])
    })

    it('the backend answering unknown saves nothing; a later "existing install" answer saves the old placement', async () => {
      let calls = 0
      firstRunRead = () => Promise.resolve({ data: { fresh_install: ++calls === 1 ? null : false }, headers: {} })
      await mountGrid(roster, ['cornelius'])
      const store = useFleetGridStore()
      expect(store.layoutIsAuto).toBe(true)
      expect(layoutPuts()).toEqual([])

      await vi.advanceTimersByTimeAsync(120000)
      expect(store.layoutIsAuto).toBe(false)
      for (const a of roster) expect(store.layout[a.name]).toEqual(old()[a.name])
      expect(layoutPuts().length).toBe(1)
    })

    it('a tile the person moves is saved even while the install kind is unknown', async () => {
      firstRunRead = () => Promise.reject(new Error('Network Error'))
      await mountGrid(roster, ['cornelius'])
      const store = useFleetGridStore()
      store.moveTile('corbin', 6, 3)
      await vi.advanceTimersByTimeAsync(120000)
      expect(store.layoutIsAuto).toBe(false)
      expect(layoutPuts().length).toBe(1)
      expect(store.layout.corbin).toEqual({ c: 6, r: 3 })
    })
  })
})

// PR #4826 e2e (run 46152561376): switching Timeline → Grid remounts FleetGrid
// with the first-run store ALREADY loaded, so setup's own sync arranges the
// fresh-install board synchronously — before the rest of setup has run. A
// helper it reached that was declared further down setup threw there, setup
// aborted and the Grid pane never rendered (`.fleet-canvas` not found).
describe('FleetGrid remount after a mode switch (ent#1416)', () => {
  it('renders the canvas and the arranged board when the first-run read is already in', async () => {
    const agents = [SYSTEM, BRAIN, CHIEF]
    const first = await mountGrid(agents)
    expect(useFirstRunStore().loaded).toBe(true)
    first.unmount()

    const again = mount(FleetGrid, {
      props: { agents, orgAgents: agents, arrivals: [] },
      global: { stubs: { AgentTile: true, RouterLink: true, 'router-link': true } },
      attachTo: document.body,
    })
    await flushPromises()
    await nextTick()
    expect(again.find('.fleet-canvas').exists()).toBe(true)
    const store = useFleetGridStore()
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 0, r: 1 })
    again.unmount()
  })
})
