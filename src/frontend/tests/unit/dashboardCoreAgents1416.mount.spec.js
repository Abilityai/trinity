// @vitest-environment jsdom
/**
 * trinity-enterprise#1416 — the Company Brain and the Chief of Staff are never
 * examples. A fresh install's first-run state names them as `core_agents`;
 * they still count as seeded (they do not end the first session), but the
 * Dashboard never keeps them off the screen and the assistant is told about
 * them. Any other seeded agent is still an example.
 *
 * Mounted (#2918): Dashboard shallow, the stores real, the network mocked —
 * the harness of dashboardHidesExamples.mount.spec.js.
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

import { useFirstRunStore as firstRunStore } from '@/stores/firstRun'
import { examplesToHide, examplesOutOfSight, coreTeamSentence } from '@/utils/firstSession'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
globalThis.WebSocket = globalThis.WebSocket || class { static OPEN = 1; close() {} send() {} }

const SYSTEM = { name: 'trinity-system', status: 'running', is_system: true }
const CHIEF = { name: 'corbin', status: 'running' }
const BRAIN = { name: 'cornelius', status: 'running' }
const ACME = { name: 'acme-scout', status: 'running' }
let fleet = []

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('token', 'tok')
  fleet = [SYSTEM, CHIEF, BRAIN, ACME]
  axiosMock.get.mockReset(); axiosMock.post.mockReset()
  axiosMock.get.mockImplementation(async (url) => {
    if (url === '/api/agents') return { data: fleet.map((a) => ({ ...a })) }
    if (url === '/api/onboarding/first-run') {
      return { data: { first_run: true, seeded_agents: ['acme-scout', 'corbin', 'cornelius'], core_agents: ['corbin', 'cornelius'] } }
    }
    return { data: [] }
  })
  axiosMock.post.mockImplementation(async () => ({ data: {} }))
})

async function mountDashboard() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div/>' } }, { path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/'); await router.isReady()
  await firstRunStore().fetchState(true)       // the real read of /first-run
  useAssistantStore().status = { available: true, leads_setup: true }
  const w = shallowMount(Dashboard, { global: { plugins: [router] } })
  await useNetworkStore().fetchAgents()
  await flushPromises()
  return w
}
const shown = () => useNetworkStore().visibleAgents.map((a) => a.name)

describe('ent#1416 — the core agents are on the Grid from the first load', () => {
  it('the first-run read carries the core agents', async () => {
    await firstRunStore().fetchState(true)
    expect(firstRunStore().coreAgents).toEqual(['corbin', 'cornelius'])
  })

  it('keeps only the other seeded agents off the first screen', async () => {
    await mountDashboard()
    expect(useNetworkStore().hiddenExamples).toEqual(['acme-scout'])
    expect(shown()).toEqual(expect.arrayContaining(['corbin', 'cornelius', 'trinity-system']))
    expect(shown()).not.toContain('acme-scout')
  })

  it('the core agents do not count as the person\'s own: the first session still runs', async () => {
    await mountDashboard()
    expect(firstRunStore().firstRun).toBe(true)
    // nothing of their own yet, so the rest of the examples stay hidden
    expect(useNetworkStore().hiddenExamples).not.toEqual([])
  })

  it('the assistant is told about both, and never about the hidden examples', async () => {
    await firstRunStore().fetchState(true)
    const assistant = useAssistantStore()
    assistant.status = { available: true, leads_setup: true }
    const out = assistant.outOfSight([SYSTEM, CHIEF, BRAIN, ACME].map((a) => ({ name: a.name })))
    expect(out.has('corbin')).toBe(false)
    expect(out.has('cornelius')).toBe(false)
    expect(out.has('acme-scout')).toBe(true)
  })
})

describe('the rules, with the core list', () => {
  const leads = { leads_setup: true }
  it('examplesToHide leaves the core agents out', () => {
    expect(examplesToHide(leads, ['acme-scout', 'corbin', 'cornelius'], 0, false, ['corbin', 'cornelius'])).toEqual(['acme-scout'])
  })
  it('accepts one name too (the ent#1409 shape)', () => {
    expect(examplesToHide(leads, ['corbin', 'cornelius'], 0, false, 'cornelius')).toEqual(['corbin'])
  })
  it('examplesOutOfSight never names a core agent, and is empty when only core agents were seeded', () => {
    const fleetNow = [{ name: 'corbin' }, { name: 'cornelius' }]
    expect(examplesOutOfSight(leads, fleetNow, ['corbin', 'cornelius'], false, ['corbin', 'cornelius'])).toEqual([])
  })
  it('the greeting names the team by role', () => {
    expect(coreTeamSentence(['corbin', 'cornelius'])).toBe('Your Chief of Staff (corbin) and Company Brain (cornelius) are already here.')
    expect(coreTeamSentence(['cornelius'])).toBe('Your Company Brain (cornelius) is already here.')
    expect(coreTeamSentence([])).toBe('')
  })
})
