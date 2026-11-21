// @vitest-environment jsdom
/**
 * #3138 — the Workspace agent band rendered the success and first-try rates
 * as `Math.round(ratio)%`, but both are RATIOS in 0..1
 * (`db/schedules/analytics.py`: `round(success_count / terminal_total, 4)`),
 * so 12 successes out of 12 read "1%" and anything under 50% read "0%".
 * Every other surface multiplies by 100 (OverviewPanel, ScheduleAnalyticsCard).
 *
 * Mounted (#2918): the number on screen is the bug, so the test reads it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

// #4346: the counters are a platform session's — the auth store says signed in
// and no portal token is held, so `isPlatformSession` is true.
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
beforeEach(() => { setActivePinia(createPinia()) })

const state = { stats: ref({}) }
vi.mock('@/composables/usePortalAgentPage', () => ({
  usePortalAgentPage: () => ({
    stats: state.stats,
    ratings: ref({ total: 0, unavailable: false }),
    loaded: ref(true),
    error: ref(null),
    reload: vi.fn(),
  }),
}))

import PortalAgentBand from '@/components/portal/PortalAgentBand.vue'

function rates(successRate, firstTryRate) {
  state.stats.value = {
    total_executions: 12, success_rate: successRate, first_try: { rate: firstTryRate }, buckets: [],
  }
  const w = mount(PortalAgentBand, {
    props: { agentName: 'scout' },
    global: { stubs: { ActivityWave: true } }, // the chart is stubbed: the numbers are under test, not the wave (ent#968)
  })
  const text = w.text()
  w.unmount()
  return text
}

describe('#3138 — the band renders 0..1 ratios as percentages', () => {
  it.each([
    [1, '100%'],            // 12 of 12
    [1 / 3, '33%'],         // 1 of 3
    [0.5, '50%'],
    [0.994, '99%'],         // rounds, never claims 100% early
    [0, '0%'],              // measured and zero is a real 0%
  ])('success rate %s reads %s', (ratio, shown) => {
    expect(rates(ratio, null)).toContain(`${shown}succeeded`)
  })

  it('first try uses the same scale', () => {
    expect(rates(null, 1)).toContain('100%succeeded without a retry')
    expect(rates(null, 0.25)).toContain('25%succeeded without a retry')
  })

  it('a rate never measured reads — , never 0%', () => {
    const text = rates(null, undefined)
    expect(text).toContain('—succeeded')
    expect(text).toContain('—succeeded without a retry')
  })
})
