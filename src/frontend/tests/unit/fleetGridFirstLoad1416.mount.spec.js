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
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
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

const SYSTEM = { name: 'trinity-system', status: 'running', is_system: true, tags: [] }
const CHIEF = { name: 'corbin', status: 'running', tags: ['dept-headquarters'] }
const BRAIN = { name: 'cornelius', status: 'running', tags: ['dept-headquarters', 'reports-to-corbin'] }

let canvas = { w: 1440, h: 800 }
Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get() { return this.classList?.contains('fleet-canvas') ? canvas.w : 0 } })
Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get() { return this.classList?.contains('fleet-canvas') ? canvas.h : 0 } })

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  resizeCallbacks.length = 0
  canvas = { w: 1440, h: 800 }
  axiosMock.get.mockReset()
  axiosMock.put.mockReset()
  axiosMock.get.mockImplementation(() => Promise.resolve({ data: {}, headers: {} }))
  axiosMock.put.mockImplementation(() => Promise.resolve({ data: { updated_at: 's1' } }))
})

async function mountGrid(agents, core = ['corbin', 'cornelius']) {
  const firstRun = useFirstRunStore()
  firstRun.coreAgents = core
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
  it('arranges the core pair side by side and the system agent apart', async () => {
    await mountGrid([SYSTEM, BRAIN, CHIEF])
    const store = useFleetGridStore()

    expect(store.layoutIsAuto).toBe(true)
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 1, r: 0 })
    expect(store.layout['trinity-system']).toEqual({ c: 3, r: 0 })
    // the info tiles sit in the row directly above
    const tiles = Object.entries(store.layout).filter(([k]) => k.startsWith('widget:'))
    expect(tiles.length).toBeGreaterThan(0)
    expect(tiles.every(([, p]) => p.r === -1)).toBe(true)
  })

  it('reads the core order from the first-run store', async () => {
    await mountGrid([SYSTEM, BRAIN, CHIEF], ['cornelius', 'corbin'])
    const store = useFleetGridStore()
    expect(store.layout.cornelius).toEqual({ c: 0, r: 0 })
    expect(store.layout.corbin).toEqual({ c: 1, r: 0 })
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
    expect(store.layout['lead-finder']).toEqual({ c: 2, r: 0 })
  })

  it('re-fits when the canvas changes size (assistant opened or closed)', async () => {
    const wrapper = await mountGrid([SYSTEM, BRAIN, CHIEF])
    const wide = worldTransform(wrapper)

    canvas = { w: 1000, h: 800 }   // the assistant panel took ~440px
    resizeCallbacks.forEach((cb) => cb([]))
    await nextTick()

    expect(worldTransform(wrapper)).not.toBe(wide)
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
})
