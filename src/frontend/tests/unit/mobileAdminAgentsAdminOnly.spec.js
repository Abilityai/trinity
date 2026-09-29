// @vitest-environment jsdom
/**
 * #3041 — `/m` tells a non-admin the agent list is admin-only instead of
 * rendering a load failure that no retry can fix.
 *
 * `GET /api/ops/fleet/status` is admin-gated, so for every role below admin it
 * answers 403. That is a permission boundary, not a transient failure: the
 * Agents tab (and the System tab's fleet summary, same endpoint) must say so
 * and point at what the session CAN do, with no retry control.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const routeQuery = { value: {} }
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: routeQuery.value }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { useAuthStore } from '@/stores/auth'
import MobileAdmin from '@/views/MobileAdmin.vue'

const forbidden = () => Object.assign(new Error('Request failed with status code 403'), {
  response: { status: 403, data: { detail: 'Admin access required' } },
})

function serve({ fleet }) {
  http.get.mockImplementation((url) => {
    if (url === '/api/ops/fleet/status') return fleet()
    if (url === '/api/agents/autonomy-status') return Promise.resolve({ data: {} })
    if (url === '/api/agents/execution-stats') return Promise.resolve({ data: { agents: [] } })
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [], count: 0 } })
    if (url === '/api/notifications') return Promise.resolve({ data: { notifications: [] } })
    return Promise.reject(new Error(`unexpected ${url}`))
  })
}

let wrapper

async function mountView({ fleet, tab }) {
  routeQuery.value = tab ? { tab } : {}
  serve({ fleet })
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  return wrapper
}

const fleetCalls = () => http.get.mock.calls.filter(([url]) => url === '/api/ops/fleet/status').length

describe('/m — the agent list is admin-only (#3041)', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

  it('a 403 renders the admin-only state — no failed state, no retry', async () => {
    const w = await mountView({ fleet: () => Promise.reject(forbidden()) })
    const notice = w.find('[data-testid="agents-admin-only"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('admin')
    expect(notice.text()).toContain('Queue')
    expect(w.text()).not.toContain("Couldn't load agents")
    expect(w.find('load-failed-stub').exists()).toBe(false)
    expect(w.find('inline-error-stub').exists()).toBe(false)
    expect(notice.text()).not.toMatch(/try again/i)
  })

  it('the notice offers the Queue as the next step', async () => {
    const w = await mountView({ fleet: () => Promise.reject(forbidden()) })
    await w.find('[data-testid="agents-admin-only-open-queue"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="agents-admin-only"]').exists()).toBe(false)
    expect(w.text()).toContain('No pending items')
  })

  it('the 15s poll stops asking once the answer is 403', async () => {
    vi.useFakeTimers()
    await mountView({ fleet: () => Promise.reject(forbidden()) })
    const before = fleetCalls()
    await vi.advanceTimersByTimeAsync(31000)
    expect(fleetCalls()).toBe(before)
  })

  it('the System tab fleet summary says admin-only too', async () => {
    const w = await mountView({ fleet: () => Promise.reject(forbidden()), tab: 'system' })
    expect(w.find('[data-testid="fleet-admin-only"]').exists()).toBe(true)
    expect(w.text()).not.toContain("Couldn't load fleet health")
  })

  it('any other failure is still the retryable failed state', async () => {
    const w = await mountView({ fleet: () => Promise.reject(Object.assign(new Error('boom'), { response: { status: 500 } })) })
    expect(w.find('[data-testid="agents-admin-only"]').exists()).toBe(false)
    expect(w.find('load-failed-stub').exists()).toBe(true)
  })

  it('an admin sees the list exactly as before', async () => {
    const w = await mountView({
      fleet: () => Promise.resolve({ data: { agents: [{ name: 'agent-a', status: 'running' }], summary: { total: 1, running: 1, stopped: 0, high_context: 0 } } }),
    })
    expect(w.find('[data-testid="agents-admin-only"]').exists()).toBe(false)
    expect(w.text()).toContain('agent-a')
  })
})
