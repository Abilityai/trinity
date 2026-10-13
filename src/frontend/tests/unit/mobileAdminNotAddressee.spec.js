// @vitest-environment jsdom
/**
 * trinity-enterprise#751 — on `/m`, an answer to a gated-skill approval that was
 * addressed to someone else is refused (403 `not_addressee`). The card must say
 * so, not "Couldn't send your response — try again": a retry can never succeed.
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
import { QUEUE_RESPONSE_NOT_ADDRESSEE } from '@/utils/operatorQueue'

const item = {
  id: 'g1', agent_name: 'testfix', type: 'approval', priority: 'high', raised_by: 'gate',
  title: 'Approve the skill pay-invoice on testfix', question: 'Someone asked testfix.',
  options: ['Approve', 'Reject'], created_at: '2026-10-03T10:00:00Z', status: 'pending',
}

let wrapper

async function mountView() {
  http.get.mockImplementation((url) => {
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [{ ...item }], count: 1 } })
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

describe('/m — an answer refused because it was not addressed to you', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('names the refusal on the card and keeps the card', async () => {
    const w = await mountView()
    http.post.mockRejectedValueOnce({
      response: { status: 403, data: { detail: { code: 'not_addressee', message: 'addressed to someone else' } } },
    })
    const card = () => w.find('[data-item-id="g1"]')
    await card().findAll('button').find(b => b.text() === 'Approve').trigger('click')
    await card().find('[data-testid="queue-send"]').trigger('click')
    await flushPromises()

    expect(http.post).toHaveBeenCalledTimes(1)
    expect(card().exists()).toBe(true)
    const notice = card().find('[data-testid="queue-respond-error"] inline-error-stub')
    expect(notice.attributes('message')).toBe(QUEUE_RESPONSE_NOT_ADDRESSEE)
  })
})
