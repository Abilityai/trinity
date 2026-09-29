// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — `/m` shows the exact action an approval asks the
 * operator to approve, on the same card that offers the decision. The view
 * renders the queue the fetch produces, so only a mount proves the block and
 * the data meet (#2918).
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
  question: 'Release the payment?', options: ['approve', 'reject'], created_at: '2026-01-01T00:00:00Z',
  status: 'pending', ...over,
})

let wrapper

async function mountView(items) {
  http.get.mockImplementation((url) => {
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items, count: items.length } })
    if (url === '/api/notifications') return Promise.resolve({ data: { items: [], count: 0 } })
    return Promise.reject(new Error(`unexpected ${url}`))
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  return wrapper
}

describe('/m — an approval shows its proposal', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('shows every field on the card that offers the decision', async () => {
    const w = await mountView([row('pay-1', { proposal: { amount: 500, to: 'vendor-7' } })])
    const card = w.find('[data-item-id="pay-1"]')
    expect(card.exists()).toBe(true)
    const block = card.find('[data-testid="queue-proposal"]')
    expect(block.exists()).toBe(true)
    expect(block.text()).toContain('500')
    expect(block.text()).toContain('vendor-7')
  })

  it('shows nothing when the ask carries no proposal', async () => {
    const w = await mountView([row('pay-2')])
    expect(w.find('[data-item-id="pay-2"] [data-testid="queue-proposal"]').exists()).toBe(false)
  })
})
