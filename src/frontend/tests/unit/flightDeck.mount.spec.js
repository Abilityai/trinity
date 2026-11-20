// @vitest-environment jsdom
/**
 * Flight Deck (abilityai/trinity-enterprise#1307) — the page, mounted.
 *
 * The rules are covered in flightDeck.spec.js. This file proves the wiring a
 * rule test cannot: the route really carries the entitlement, the privacy
 * switch really changes what is rendered and what is stored, "Open in window"
 * really asks for a tabless window at the navigation-free address, and a
 * hidden page really stops asking for data.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api', () => ({
  default: {
    get: vi.fn((url, opts) => {
      if (url === '/api/telemetry/host') {
        return Promise.resolve({ data: {
          cpu: { percent: 42, count: 8 },
          memory: { percent: 50, used_gb: 8, total_gb: 16 },
          disk: { percent: 95, used_gb: 95, total_gb: 100 },
        } })
      }
      const n = opts?.params?.group_by === 'day' ? 30 : 24
      return Promise.resolve({ data: { buckets: Array.from({ length: n }, (_, i) => ({ bucket: `b${i}`, total: i, failed: 0 })) } })
    }),
  },
}))

import api from '@/api'
import FlightDeckPanel from '../../src/components/flight-deck/FlightDeckPanel.vue'
import FlightDeck from '../../src/views/FlightDeck.vue'
import { routes } from '../../src/router/index.js'
import { useNetworkStore } from '../../src/stores/network'
import { useFlightDeckStore } from '../../src/stores/flightDeck'

const NET_ACTIONS = [
  'fetchAgents', 'fetchHistoricalCommunications', 'connectWebSocket', 'disconnectWebSocket',
  'startContextPolling', 'stopContextPolling', 'startAgentRefresh', 'stopAgentRefresh',
  'startActivityRefresh', 'stopActivityRefresh', 'followToken', 'fetchSchedules',
]

function setVisibility(state) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

async function mountDeck({ agents, query = {}, loadError = false, windowMode = false, view = false } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const net = useNetworkStore()
  for (const name of NET_ACTIONS) net[name] = vi.fn().mockResolvedValue(undefined)
  net.agents = agents ?? [
    { name: 'alpha', status: 'running', display_label: 'Alpha' },
    { name: 'bravo', status: 'running' },
    { name: 'charlie', status: 'stopped' },
  ]
  net.loading = false
  net.loadError = loadError
  net.workingState = { alpha: { since: new Date(Date.now() - 120000).toISOString() } }
  net.executionStats = { alpha: { taskCount: 10, successCount: 8, failedCount: 2, successRate: 80, totalCost: 1.5 } }
  net.contextStats = { alpha: { contextPercent: 41, activityState: 'active' } }
  net.schedules = [{ id: 1, agent_name: 'bravo', name: 'Acme sweep', enabled: true, next_run_at: new Date(Date.now() + 600000).toISOString() }]
  net.historicalCollaborations = [{
    source_agent: 'alpha', activity_id: 'a1', status: 'started', activity_type: 'schedule_start',
    triggered_by: 'schedule', schedule_name: 'Acme quarterly numbers', timestamp: new Date(Date.now() - 120000).toISOString(),
  }]
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/flight-deck', component: { template: '<div />' } },
    ],
  })
  router.push({ path: '/flight-deck', query })
  await router.isReady()
  const stubs = {}
  const wrapper = view
    ? mount(FlightDeck, { global: { plugins: [pinia, router], stubs } })
    : mount(FlightDeckPanel, { props: { agents: net.visibleAgents, windowMode }, global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return { wrapper, net, deck: useFlightDeckStore(), router }
}

beforeEach(() => {
  localStorage.clear()
  api.get.mockClear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('the route', () => {
  it('is signed-in only and carries the entitlement the guard checks', () => {
    const r = routes.find((x) => x.path === '/flight-deck')
    expect(r).toBeTruthy()
    expect(r.meta.requiresAuth).toBe(true)
    expect(r.meta.requiresEntitlement).toBe('flight_deck')
  })
})

describe('the page', () => {
  it('counts the fleet and lists working agents first', async () => {
    const { wrapper } = await mountDeck()
    const kpi = (id) => wrapper.get(`[data-testid="flight-deck-kpi-${id}"] dd`).text()
    expect([kpi('working'), kpi('idle'), kpi('stopped')]).toEqual(['1', '1', '1'])
    expect(wrapper.get('[data-testid="flight-deck-ring"]').attributes('aria-label')).toBe('1 of 3 agents working')
    const names = wrapper.get('[data-testid="flight-deck-agents"]').findAll('tbody tr').map((tr) => tr.text())
    expect(names[0]).toContain('Alpha')
    expect(names[0]).toContain('working 2m')
    expect(names.at(-1)).toContain('stopped')
    wrapper.unmount()
  })

  it('hides task text until the viewer turns it on, and remembers the choice', async () => {
    const { wrapper, deck } = await mountDeck()
    expect(wrapper.text()).not.toContain('Acme')
    expect(wrapper.get('[data-testid="flight-deck-task"]').text()).toBe('schedule')

    await wrapper.get('[data-testid="flight-deck-privacy"]').trigger('click')
    expect(deck.showText).toBe(true)
    expect(wrapper.get('[data-testid="flight-deck-task"]').text()).toBe('schedule · Acme quarterly numbers')
    expect(localStorage.getItem('trinity-flight-deck-show-text')).toBe('1')
    wrapper.unmount()
  })

  it('opens itself in a tabless window at the navigation-free address', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({ focus: vi.fn() })
    const { wrapper } = await mountDeck()
    await wrapper.get('[data-testid="flight-deck-popout"]').trigger('click')
    expect(open).toHaveBeenCalledTimes(1)
    const [href, name, features] = open.mock.calls[0]
    expect(href).toBe('/flight-deck')
    expect(name).toBe('trinity-flight-deck')
    expect(features).toContain('popup=yes')
    expect(wrapper.find('[data-testid="inline-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says so when the browser blocks the window, and gives the address', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const { wrapper } = await mountDeck()
    await wrapper.get('[data-testid="flight-deck-popout"]').trigger('click')
    expect(wrapper.text()).toContain('blocked the new window')
    expect(wrapper.text()).toContain('/flight-deck')
    wrapper.unmount()
  })

  it('in the separate window offers no second "open" button, and the window page runs the fleet store', async () => {
    const win = await mountDeck({ windowMode: true })
    expect(win.wrapper.find('[data-testid="flight-deck-popout"]').exists()).toBe(false)
    win.wrapper.unmount()

    const page = await mountDeck({ view: true })
    expect(page.wrapper.find('[data-testid="flight-deck"]').exists()).toBe(true)
    expect(page.wrapper.find('[data-testid="flight-deck-popout"]').exists()).toBe(false)
    expect(page.net.connectWebSocket).toHaveBeenCalledTimes(1)
    expect(page.net.fetchSchedules).toHaveBeenCalledTimes(1)
    page.wrapper.unmount()
    expect(page.net.disconnectWebSocket).toHaveBeenCalledTimes(1)
    expect(page.net.stopContextPolling).toHaveBeenCalledTimes(1)
  })

  it('shows each agent the tile facts: success, runs, cost and context', async () => {
    const { wrapper } = await mountDeck()
    const cells = wrapper.get('[data-testid="flight-deck-agents"]').findAll('tbody tr').map((tr) => tr.findAll('td').map((td) => td.text()))
    // Agent · State · Doing · Success · Runs · Cost · Context
    expect(cells[0].slice(3)).toEqual(['80%', '10', '$1.50', '41%'])
    // an agent with no runs shows no reading, never 0%
    expect(cells[1].slice(3)).toEqual(['—', '—', '—', '—'])
    const kpi = (id) => wrapper.get(`[data-testid="flight-deck-kpi-${id}"] dd`).text()
    expect([kpi('runs'), kpi('success'), kpi('failed'), kpi('cost')]).toEqual(['10', '80%', '2', '$1.50'])
    wrapper.unmount()
  })

  it('lists the next scheduled runs, without their names while text is hidden', async () => {
    const { wrapper } = await mountDeck()
    const next = wrapper.get('[data-testid="flight-deck-next"]')
    expect(next.text()).toContain('bravo')
    expect(next.text()).not.toContain('Acme')
    await wrapper.get('[data-testid="flight-deck-privacy"]').trigger('click')
    expect(next.text()).toContain('Acme sweep')
    wrapper.unmount()
  })

  it('demo mode draws a simulated fleet, says so, and asks the server for nothing', async () => {
    const { wrapper } = await mountDeck({ agents: [], query: { demo: '1' } })
    expect(wrapper.get('[data-testid="flight-deck-demo"]').text()).toContain('Demo data')
    expect(wrapper.find('[data-testid="flight-deck-empty"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="flight-deck-agents"]').findAll('tbody tr').length).toBeGreaterThan(5)
    expect(Number(wrapper.get('[data-testid="flight-deck-kpi-working"] dd').text())).toBeGreaterThan(0)
    expect(api.get).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('names the next step when there are no agents, and keeps a failed load distinct', async () => {
    const empty = await mountDeck({ agents: [] })
    expect(empty.wrapper.get('[data-testid="flight-deck-empty"]').text()).toContain('Open the agent list')
    const failed = await mountDeck({ agents: [], loadError: true })
    expect(failed.wrapper.find('[data-testid="flight-deck-empty"]').exists()).toBe(false)
    expect(failed.wrapper.find('[data-testid="load-failed"]').exists()).toBe(true)
    empty.wrapper.unmount()
    failed.wrapper.unmount()
  })

  it('shows host load, flags a nearly full disk, and draws both history strips', async () => {
    const { wrapper } = await mountDeck()
    const hostCard = wrapper.get('[data-testid="flight-deck-host"]')
    expect(hostCard.text()).toMatch(/CPU\s*42%.*Memory\s*50%.*Disk\s*95%/)
    expect(hostCard.find('[title="8 of 16 GB"]').exists()).toBe(true)
    // only the reading past its limit is coloured
    expect(hostCard.findAll('.bg-status-danger-500')).toHaveLength(1)
    expect(wrapper.find('[data-testid="flight-deck-host-missing"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="flight-deck-days"]').findAll(':scope > span')).toHaveLength(30)
    expect(wrapper.get('[data-testid="flight-deck-hours"]').findAll(':scope > span')).toHaveLength(24)
    wrapper.unmount()
  })

  it('keeps rendering when the host read fails, and says which part is missing', async () => {
    api.get.mockImplementation((url) => (url === '/api/telemetry/host'
      ? Promise.reject(new Error('boom'))
      : Promise.resolve({ data: { buckets: [] } })))
    const { wrapper } = await mountDeck()
    expect(wrapper.get('[data-testid="flight-deck-host-missing"]').text()).toContain('unavailable')
    expect(wrapper.get('[data-testid="flight-deck-kpi-working"] dd').text()).toBe('1')
    wrapper.unmount()
  })

  it('stops asking for data while hidden and starts again when shown', async () => {
    const { wrapper } = await mountDeck()
    vi.useFakeTimers()
    setVisibility('hidden')
    await wrapper.vm.$nextTick()
    api.get.mockClear()
    vi.advanceTimersByTime(60000)
    expect(api.get).not.toHaveBeenCalled()

    setVisibility('visible')
    await wrapper.vm.$nextTick()
    expect(api.get).toHaveBeenCalledWith('/api/telemetry/host')
    wrapper.unmount()
  })

  it('stops its own polls when it is left', async () => {
    const { wrapper } = await mountDeck()
    wrapper.unmount()
    api.get.mockClear()
    vi.useFakeTimers()
    vi.advanceTimersByTime(60000)
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe('the ring', () => {
  it('draws one mark per agent, keeps each in place, and rings only for a new event', async () => {
    const { default: FleetRing } = await import('../../src/components/flight-deck/FleetRing.vue')
    const rows = [
      { name: 'bravo', label: 'Bravo', working: true, stopped: false },
      { name: 'alpha', label: 'Alpha', working: false, stopped: false },
      { name: 'charlie', label: 'Charlie', working: false, stopped: true },
    ]
    const wrapper = mount(FleetRing, { props: { rows, counts: { working: 1, total: 3 }, feed: [{ key: 'a1:started', agent: 'bravo', kind: 'started' }] } })
    const titles = () => wrapper.findAll('title').map((t) => t.text())
    // name order, not the list's working-first order
    expect(titles()).toEqual(['Alpha — idle', 'Bravo — working', 'Charlie — stopped'])
    // history present at mount is not replayed
    expect(wrapper.findAll('.deck-pulse')).toHaveLength(0)

    await wrapper.setProps({ feed: [{ key: 'a2:failed', agent: 'alpha', kind: 'failed' }, { key: 'a1:started', agent: 'bravo', kind: 'started' }] })
    const pulses = wrapper.findAll('.deck-pulse')
    expect(pulses).toHaveLength(1)
    expect(pulses[0].classes()).toContain('stroke-status-danger-500')
    await pulses[0].trigger('animationend')
    expect(wrapper.findAll('.deck-pulse')).toHaveLength(0)
  })
})
