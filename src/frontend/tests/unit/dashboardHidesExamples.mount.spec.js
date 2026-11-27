// @vitest-environment jsdom
/**
 * trinity-enterprise#1355 checkpoint 4 — how the Dashboard decides which
 * example agents to keep off the first screen.
 *
 * `examplesToHide` (the rule) and the network store's filter (the seam) are
 * covered in firstSessionExamples.spec.js. This mounts the Dashboard itself,
 * which joins them: it counts the person's own agents (neither a system agent
 * nor one the install came with), feeds that and the assistant's status into
 * the rule, hands the answer to the store, offers the examples back on
 * request (remembered in this browser), and clears the filter when it goes.
 *
 * Mounted (#2918): Dashboard shallow, the stores real, the network mocked.
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
import { useNetworkStore } from '@/stores/network'
import { useAssistantStore } from '@/stores/assistant'
import { useFirstRunStore } from '@/stores/firstRun'
import { SHOW_EXAMPLES_KEY } from '@/utils/firstSession'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
globalThis.WebSocket = globalThis.WebSocket || class { static OPEN = 1; close() {} send() {} }

const SYSTEM = { name: 'trinity-system', status: 'running', is_system: true }
const EXAMPLE = { name: 'cornelius', status: 'running' }
const MINE = { name: 'mine', status: 'running' }
let fleet = []

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  fleet = [SYSTEM, EXAMPLE]
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.get.mockImplementation(async (url) => ({ data: url === '/api/agents' ? fleet.map((a) => ({ ...a })) : [] }))
  axiosMock.post.mockImplementation(async () => ({ data: {} }))
})

// The Dashboard as a person reaches it: the assistant's status and which agents
// the install came with are the stores' own answers, set as the reads leave them.
async function mountDashboard({ leads = true } = {}) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div/>' } }, { path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/'); await router.isReady()
  useFirstRunStore().seededAgents = [EXAMPLE.name]
  useAssistantStore().status = leads ? { available: true, leads_setup: true } : { available: true }
  const w = shallowMount(Dashboard, { global: { plugins: [router] } })
  await useNetworkStore().fetchAgents()
  await flushPromises()
  return w
}
const shown = () => useNetworkStore().visibleAgents.map((a) => a.name)
const offer = (w) => w.find('[data-testid="dashboard-examples-hidden"]')

describe('ent#1355 — the Dashboard keeps the example agents off the first screen', () => {
  it('hides what the install came with while the person has no agent of their own; a system agent is not theirs', async () => {
    const w = await mountDashboard()
    expect(useNetworkStore().hiddenExamples).toEqual([EXAMPLE.name])
    expect(shown()).not.toContain(EXAMPLE.name)
    expect(shown()).toContain(SYSTEM.name)
    expect(offer(w).exists()).toBe(true)
  })

  it('brings them back once the person has an agent of their own', async () => {
    const w = await mountDashboard()
    fleet = [SYSTEM, EXAMPLE, MINE]
    await useNetworkStore().fetchAgents()
    await flushPromises()
    expect(useNetworkStore().hiddenExamples).toEqual([])
    expect(shown()).toEqual(expect.arrayContaining([EXAMPLE.name, MINE.name]))
    expect(offer(w).exists()).toBe(false)
  })

  it('hides nothing where the assistant does not lead', async () => {
    const w = await mountDashboard({ leads: false })
    expect(useNetworkStore().hiddenExamples).toEqual([])
    expect(shown()).toContain(EXAMPLE.name)
    expect(offer(w).exists()).toBe(false)
  })

  it('shows them on request, and remembers that in this browser', async () => {
    const w = await mountDashboard()
    await offer(w).findComponent({ name: 'BaseButton' }).vm.$emit('click')
    await flushPromises()
    expect(useNetworkStore().hiddenExamples).toEqual([])
    expect(shown()).toContain(EXAMPLE.name)
    expect(localStorage.getItem(SHOW_EXAMPLES_KEY)).toBe('true')

    w.unmount()
    setActivePinia(createPinia())
    await mountDashboard()                     // a later visit: still shown
    expect(useNetworkStore().hiddenExamples).toEqual([])
    expect(shown()).toContain(EXAMPLE.name)
  })

  it('leaving the Dashboard clears the filter for every other view of the fleet', async () => {
    const w = await mountDashboard()
    expect(useNetworkStore().hiddenExamples).toEqual([EXAMPLE.name])
    w.unmount()
    expect(useNetworkStore().hiddenExamples).toEqual([])
    expect(shown()).toContain(EXAMPLE.name)
  })
})
