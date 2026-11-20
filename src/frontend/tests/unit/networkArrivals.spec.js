// @vitest-environment jsdom
/**
 * Which agents count as "just arrived" on the dashboard.
 *
 * The grid lands a new agent with a short animation. What it animates comes
 * from `networkStore.arrivals`, and the claim under test is what that list
 * does NOT hold: a first load, a filter being lifted and a wholesale roster
 * swap all bring rows into view without anything having been made.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { axiosMock } = vi.hoisted(() => ({
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('axios', () => ({
  default: Object.assign(axiosMock, {
    create: () => ({ ...axiosMock, interceptors: { request: { use() {} }, response: { use() {} } }, defaults: { headers: { common: {} } } }),
    interceptors: { request: { use() {} }, response: { use() {} } },
    defaults: { headers: { common: {} } },
  }),
}))

import { useNetworkStore, ARRIVAL_BATCH_MAX, ARRIVAL_NEWS_MS } from '@/stores/network'

let fleet
beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  fleet = ['alpha', 'beta']
  axiosMock.get.mockReset()
  axiosMock.get.mockImplementation(async (url, config) => {
    if (url !== '/api/agents') return { data: [] }
    const tagged = config?.params?.tags
    return { data: fleet.filter((n) => !tagged || n === 'alpha').map((name) => ({ name, status: 'running' })) }
  })
})
afterEach(() => { vi.useRealTimers() })

describe('dashboard arrivals', () => {
  it('a first load announces nobody', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    expect(store.arrivals).toEqual([])
  })

  it('an agent made while the dashboard is open is announced once, then stops being news', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    fleet = [...fleet, 'gamma', 'delta']
    await store.fetchAgents()
    expect(store.arrivals).toEqual(['gamma', 'delta'])
    await store.fetchAgents()
    expect(store.arrivals).toEqual(['gamma', 'delta'])
    vi.advanceTimersByTime(ARRIVAL_NEWS_MS)
    expect(store.arrivals).toEqual([])
    await store.fetchAgents()
    expect(store.arrivals).toEqual([])
  })

  it('lifting a tag filter brings rows back without announcing them', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    store.filterTags = ['only-alpha']
    await store.fetchAgents()
    store.filterTags = []
    await store.fetchAgents()
    expect(store.arrivals).toEqual([])
  })

  it('a roster swap is not a crowd of new agents', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    fleet = [...fleet, ...Array.from({ length: ARRIVAL_BATCH_MAX + 1 }, (_, i) => `imported-${i}`)]
    await store.fetchAgents()
    expect(store.arrivals).toEqual([])
  })

  it('an agent deleted and made again is announced again', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    store.handleAgentDeleted({ agent_name: 'beta' })
    await store.fetchAgents()
    expect(store.arrivals).toEqual(['beta'])
  })
})
