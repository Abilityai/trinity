// @vitest-environment jsdom
/**
 * #3109 — closing the first-run overlay refreshes the dashboard's agents.
 *
 * Setup seeds agents server-side while the overlay is open, and no live event
 * reaches the dashboard store for them, so the Timeline sat empty until the
 * 30 s poll. The create modal already refetches on close (`onCreateModalClose`);
 * the overlay now does the same, on every way it closes.
 *
 * Mounted (#2918): Dashboard shallow, the overlay a stub that emits its model.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
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

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
globalThis.WebSocket = globalThis.WebSocket || class { static OPEN = 1; close() {} send() {} }

const agentListReads = () => axiosMock.get.mock.calls.filter(([url]) => url === '/api/agents').length

beforeEach(() => {
  setActivePinia(createPinia())
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.get.mockImplementation(async () => ({ data: [] }))
  axiosMock.post.mockImplementation(async () => ({ data: {} }))
})

async function mountDashboard() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div/>' } }, { path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/'); await router.isReady()
  const w = shallowMount(Dashboard, { global: { plugins: [router] } })
  await flushPromises()
  return w
}

describe('#3109 — Dashboard and the first-run overlay', () => {
  it('refetches the agents when the overlay closes', async () => {
    const w = await mountDashboard()
    const overlay = w.findComponent({ name: 'FirstRunOverlay' })
    expect(overlay.exists()).toBe(true)
    overlay.vm.$emit('update:open', true)
    await flushPromises()
    const before = agentListReads()
    overlay.vm.$emit('update:open', false)
    await flushPromises()
    expect(agentListReads()).toBe(before + 1)
  })

  it('opening it does not refetch', async () => {
    const w = await mountDashboard()
    const before = agentListReads()
    w.findComponent({ name: 'FirstRunOverlay' }).vm.$emit('update:open', true)
    await flushPromises()
    expect(agentListReads()).toBe(before)
  })
})
