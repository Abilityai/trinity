// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#1357 — the dashboard opens in Grid for a person
 * who has never chosen a view.
 *
 * Someone arriving on a new instance has one agent or none; Grid shows what
 * exists, Timeline shows activity they do not have yet. The rule:
 *
 *   - nothing stored             → Grid (the first view)
 *   - a stored, known view       → that view, always (their choice wins)
 *   - a stored, unknown view     → Timeline, as before (a stale 'graph' from
 *                                  #1689 was a choice; nothing changes for it)
 *
 * Opening in Grid is not a choice: nothing is written until the person picks a
 * view. Mounted (#2918): Dashboard shallow, the store real, the socket a fake
 * fed the backend's real `agent_created` envelope.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = (q) => ({
    matches: /max-width/.test(q) && window.innerWidth <= 640,
    media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
})
const { axiosMock } = vi.hoisted(() => ({
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), patch: vi.fn() },
}))
vi.mock('axios', () => ({
  default: Object.assign(axiosMock, {
    create: () => ({ ...axiosMock, interceptors: { request: { use() {} }, response: { use() {} } }, defaults: { headers: { common: {} } } }),
    interceptors: { request: { use() {} }, response: { use() {} } },
    defaults: { headers: { common: {} } },
  }),
}))

import Dashboard from '@/views/Dashboard.vue'
import { useNetworkStore, AGENT_CREATED_COALESCE_MS } from '@/stores/network'

const MODE_KEY = 'trinity-dashboard-view'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
let socket
class FakeWebSocket {
  static OPEN = 1
  constructor() { socket = this; this.readyState = 0 }
  send() {}
  close() {}
}

let fleet
const originalWidth = window.innerWidth

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  socket = undefined
  globalThis.WebSocket = FakeWebSocket
  fleet = []
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.get.mockImplementation(async (url) => (url === '/api/agents' ? { data: fleet.map((a) => ({ ...a })) } : { data: [] }))
  axiosMock.post.mockImplementation(async (url) => (url === '/api/ws/ticket' ? { data: { ticket: 't' } } : { data: {} }))
})
afterEach(() => {
  vi.useRealTimers()
  window.innerWidth = originalWidth
})

async function mountDashboard() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div/>' } }, { path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/'); await router.isReady()
  const w = shallowMount(Dashboard, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

// Which pane is mounted — each is a v-if, so exactly one exists.
function pane(w) {
  const grid = w.find('[data-testid="grid-view"]').exists()
  const timeline = w.findComponent({ name: 'ReplayTimeline' }).exists()
  const list = w.findComponent({ name: 'AgentListPanel' }).exists()
  return [grid && 'grid', timeline && 'timeline', list && 'list'].filter(Boolean)
}

describe('ent#1357 — the first dashboard view', () => {
  it('a person who has never chosen a view opens in Grid, and nothing is written for them', async () => {
    const w = await mountDashboard()
    expect(pane(w)).toEqual(['grid'])
    expect(useNetworkStore().viewMode).toBe('grid')
    // Opening is not choosing: the next release may pick a different first view.
    expect(localStorage.getItem(MODE_KEY)).toBeNull()
  })

  it('a person who chose Timeline opens in Timeline', async () => {
    localStorage.setItem(MODE_KEY, 'timeline')
    const w = await mountDashboard()
    expect(pane(w)).toEqual(['timeline'])
  })

  it('a person who chose List opens in List', async () => {
    localStorage.setItem(MODE_KEY, 'list')
    fleet = [{ name: 'alpha', status: 'running' }] // the List panel mounts over a non-empty roster
    const w = await mountDashboard()
    expect(pane(w)).toEqual(['list'])
  })

  it('a stale stored view (the retired graph mode) still degrades to Timeline, as before', async () => {
    localStorage.setItem(MODE_KEY, 'graph')
    const w = await mountDashboard()
    expect(pane(w)).toEqual(['timeline'])
  })

  it('picking a view from Grid stores it, and it wins on the next open', async () => {
    const w = await mountDashboard()
    const timelineButton = w.findAll('[data-testid="view-mode-switcher"] button').find((b) => b.text() === 'timeline')
    await timelineButton.trigger('click')
    expect(localStorage.getItem(MODE_KEY)).toBe('timeline')
    w.unmount()
    setActivePinia(createPinia())
    const again = await mountDashboard()
    expect(pane(again)).toEqual(['timeline'])
  })

  it('the first agent a person builds appears on the Grid without a reload', async () => {
    const w = await mountDashboard()
    expect(w.find('[data-testid="grid-view"]').text()).toContain('No agents yet')
    expect(socket, 'the dashboard opened its socket').toBeTruthy()

    vi.useFakeTimers()
    fleet = [{ name: 'first-agent', status: 'running' }]
    socket.onmessage({ data: JSON.stringify({ event: 'agent_created', type: 'agent_created', data: { name: 'first-agent', status: 'running' } }) })
    vi.advanceTimersByTime(AGENT_CREATED_COALESCE_MS)
    await flushPromises()

    const grid = w.findComponent({ name: 'FleetGrid' })
    expect(grid.exists(), 'the grid replaced the empty state in place').toBe(true)
    expect(grid.props('agents').map((a) => a.name)).toEqual(['first-agent'])
    expect(pane(w)).toEqual(['grid'])
  })

  it('at phone width a person who never chose opens in Grid, with every view still reachable', async () => {
    window.innerWidth = 375
    window.dispatchEvent(new Event('resize'))
    const w = await mountDashboard()
    expect(pane(w)).toEqual(['grid'])
    const switcher = w.find('[data-testid="view-mode-switcher"]')
    expect(switcher.exists()).toBe(true)
    const labels = switcher.findAll('button').map((b) => b.text())
    expect(labels).toEqual(['timeline', 'grid', 'list'])
    // Nothing on the path from the switcher up hides it at a small breakpoint.
    for (let el = switcher.element; el && el !== w.element.parentNode; el = el.parentElement) {
      expect(el.className || '', 'switcher ancestor').not.toMatch(/(^|\s)hidden(\s|$)/)
    }
    // The empty Grid still names the next step.
    expect(w.find('[data-testid="grid-view"]').text()).toContain('Get started')
  })
})
