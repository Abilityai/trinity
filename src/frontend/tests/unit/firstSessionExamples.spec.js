// @vitest-environment jsdom
/**
 * trinity-enterprise#1355 checkpoint 4: where the assistant leads the first
 * session, the agents the install came with are not on the first screen;
 * they appear once the person has built their own, or on request. The filter
 * lives in the network store's pre-query fleet, so the grid, the list, the
 * timeline and the graph all lose them together (the ent#261 seam).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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

import { useNetworkStore } from '@/stores/network'
import { examplesToHide } from '@/utils/firstSession'

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  axiosMock.get.mockReset()
  axiosMock.get.mockImplementation(async (url) => ({
    data: url === '/api/agents' ? ['cornelius', 'trinity-system', 'mine'].map((name) => ({ name, status: 'running' })) : [],
  }))
})

describe('examplesToHide', () => {
  const leads = { leads_setup: true }
  it('hides the examples in the first session, before the person has built anything', () => {
    expect(examplesToHide(leads, ['cornelius'], 0, false)).toEqual(['cornelius'])
  })
  it('shows them once the person has an agent of their own', () => {
    expect(examplesToHide(leads, ['cornelius'], 1, false)).toEqual([])
  })
  it('shows them on request', () => {
    expect(examplesToHide(leads, ['cornelius'], 0, true)).toEqual([])
  })
  it('an install where the assistant does not lead is unchanged', () => {
    expect(examplesToHide({}, ['cornelius'], 0, false)).toEqual([])
    expect(examplesToHide(null, ['cornelius'], 0, false)).toEqual([])
  })
})

describe('the dashboard fleet without the examples', () => {
  it('drops hidden names from every pane, and brings them back when cleared', async () => {
    const store = useNetworkStore()
    await store.fetchAgents()
    store.setHiddenExamples(['cornelius'])
    expect(store.visibleAgents.map((a) => a.name)).not.toContain('cornelius')
    expect(store.visibleAgents.map((a) => a.name)).toContain('mine')
    store.setHiddenExamples([])
    expect(store.visibleAgents.map((a) => a.name)).toContain('cornelius')
  })
})
