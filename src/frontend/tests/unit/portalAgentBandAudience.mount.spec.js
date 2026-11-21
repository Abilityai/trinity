// @vitest-environment jsdom
/**
 * #4346 — the stats band explains itself, and a client does not get numbers
 * it has no way to read. A client session sees the activity chart only (no
 * counters, no counter skeleton); a platform session reads "succeeded" and
 * "succeeded without a retry" as visible labels, and a band with runs but a
 * 0% success rate says what that means.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign({ get: vi.fn(), post: vi.fn(), create: mk },
    { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } }, defaults: { headers: { common: {} } } }) }
})
const page = { stats: ref({}), loaded: ref(true), ratings: ref({ total: 2, up: 1, down: 1, unavailable: false }) }
vi.mock('@/composables/usePortalAgentPage', () => ({
  usePortalAgentPage: () => ({
    stats: page.stats, ratings: page.ratings, loaded: page.loaded, error: ref(null), denied: ref(false), reload: vi.fn(),
  }),
}))

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentBand from '@/components/portal/PortalAgentBand.vue'

let store
beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  page.loaded.value = true
  page.stats.value = { total_executions: 20, success_rate: 0.9, first_try: { rate: 0.75 }, timeline: [] }
})
const asClient = () => { store.portalToken = 'portal-token' }
const asPlatform = () => { store.portalToken = null }
const band = () => mount(PortalAgentBand, { props: { agentName: 'scout' }, global: { stubs: { ActivityWave: { template: '<div data-testid="wave" />' } } } })

describe('PortalAgentBand — by audience (#4346)', () => {
  it('a client sees the chart and no counters', () => {
    asClient()
    const w = band()
    expect(w.find('[data-testid="wave"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-band-counters"]').exists()).toBe(false)
    expect(w.text()).not.toMatch(/tasks|succeeded|completed|first try|helpful/)
  })

  it('a client sees no counter skeleton while loading', () => {
    asClient()
    page.loaded.value = false
    const w = band()
    expect(w.find('[aria-busy="true"]').exists()).toBe(false)
    expect(w.find('[data-testid="wave"]').exists()).toBe(true)
  })

  it('a platform session reads labels that explain themselves', () => {
    asPlatform()
    const text = band().text()
    expect(text).toContain('90%succeeded')
    expect(text).toContain('75%succeeded without a retry')
    expect(text).not.toMatch(/completed|first try/)
  })

  it('runs with a 0% success rate carry a line saying what that means', () => {
    asPlatform()
    page.stats.value = { total_executions: 20, success_rate: 0, first_try: { rate: 0 }, timeline: [] }
    const w = band()
    expect(w.find('[data-testid="portal-band-zero-note"]').text()).toMatch(/none of the 20 runs succeeded/i)
  })

  it('no zero line when there are no runs or some succeeded', () => {
    asPlatform()
    page.stats.value = { total_executions: 0, success_rate: null, first_try: { rate: null }, timeline: [] }
    expect(band().find('[data-testid="portal-band-zero-note"]').exists()).toBe(false)
    page.stats.value = { total_executions: 20, success_rate: 0.1, first_try: { rate: 0 }, timeline: [] }
    expect(band().find('[data-testid="portal-band-zero-note"]').exists()).toBe(false)
  })
})
