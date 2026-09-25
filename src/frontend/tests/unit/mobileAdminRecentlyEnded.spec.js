// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — `/m` shows how asks ENDED, not only what is pending.
 *
 * The view already fetched every status and threw the ended ones away, so an
 * ask cancelled from a laptop simply vanished from the phone. MOUNTED (#2918):
 * the strip is a template branch over data the fetch produces, and only a
 * render proves both halves meet.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'ops' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { useAuthStore } from '@/stores/auth'
import MobileAdmin from '@/views/MobileAdmin.vue'

const row = (id, over = {}) => ({
  id, agent_name: 'agent-a', type: 'approval', priority: 'high', title: `Ask ${id}`,
  question: 'Release 500 USDC?', options: ['approve', 'reject'], created_at: '2026-01-01T00:00:00Z',
  status: 'pending', ...over,
})

function serve(items) {
  http.get.mockImplementation((url) => {
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items, count: items.length } })
    if (url === '/api/notifications') return Promise.resolve({ data: { items: [], count: 0 } })
    return Promise.reject(new Error(`unexpected ${url}`))
  })
}

let wrapper

async function mountView(items) {
  serve(items)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  return wrapper
}

describe('/m — Recently ended', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('lists ended asks, newest ending first, with who ended them — pending stays in the queue', async () => {
    const w = await mountView([
      row('pending-1'),
      row('cut', { status: 'cancelled', disposition: 'cancelled', disposed_by_email: 'op@example.com', disposed_at: '2026-09-24T10:00:00Z' }),
      row('gone', { status: 'expired', disposition: 'expired', disposed_by: 'timeout', disposed_at: '2026-09-25T10:00:00Z' }),
    ])
    expect(w.findAll('[data-testid="queue-card"]').map((c) => c.attributes('data-item-id'))).toEqual(['pending-1'])
    const strip = w.find('[data-testid="queue-recently-ended"]')
    expect(strip.exists()).toBe(true)
    const cards = strip.findAll('[data-testid="queue-ended-card"]')
    expect(cards.map((c) => c.attributes('data-item-id'))).toEqual(['gone', 'cut'])
    expect(cards[0].text()).toContain('Expired — nobody answered in time')
    expect(cards[1].text()).toContain('Cancelled by op@example.com')
  })

  it('shows the strip even when nothing is pending', async () => {
    const w = await mountView([
      row('done', { status: 'responded', response: 'approve', disposition: 'answered', disposed_by_email: 'op@example.com', disposed_at: '2026-09-25T10:00:00Z' }),
    ])
    expect(w.text()).toContain('No pending items')
    expect(w.find('[data-testid="queue-recently-ended"]').text()).toContain('Answered by op@example.com')
  })

  it('renders no strip when nothing has ended', async () => {
    const w = await mountView([row('pending-1')])
    expect(w.find('[data-testid="queue-recently-ended"]').exists()).toBe(false)
  })
})
