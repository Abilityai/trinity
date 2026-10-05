// @vitest-environment jsdom
/**
 * #3242 — `/m`: the "Something else" chip opens the form, Send stays disabled
 * until the instruction is typed, and the POSTed body carries the literal with
 * the instruction in `response_text`. Hidden on a gate approval.
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

const base = {
  id: 'm1', request_id: 'approval-1', agent_name: 'bot', type: 'approval', priority: 'high',
  title: 'Deploy?', question: 'Deploy to prod?', options: ['Approve', 'Deny'],
  created_at: '2026-10-05T10:00:00Z', status: 'pending',
}

let wrapper

async function mountView(over = {}) {
  http.get.mockImplementation((url) => {
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [{ ...base, ...over }], count: 1 } })
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

describe('/m — Something else (#3242)', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('the chip opens the form; Send needs the instruction; the POST carries both', async () => {
    const w = await mountView()
    http.post.mockResolvedValueOnce({ data: {} })
    const card = () => w.find('[data-item-id="m1"]')
    await card().find('[data-testid="queue-something-else"]').trigger('click')
    expect(card().find('[data-testid="queue-consequence"]').text()).toContain('carry out none of the options')
    const send = () => card().find('[data-testid="queue-send"]')
    expect(send().text()).toBe('Send instruction')
    expect(send().attributes('disabled')).toBeDefined()
    await card().find('[data-testid="queue-note"]').setValue('Ship to staging first')
    expect(send().attributes('disabled')).toBeUndefined()
    await send().trigger('click')
    await flushPromises()
    expect(http.post).toHaveBeenCalledTimes(1)
    expect(http.post.mock.calls[0][1]).toEqual({ response: '(something else)', response_text: 'Ship to staging first' })
  })

  it('an offered option still sends as before', async () => {
    const w = await mountView()
    const card = () => w.find('[data-item-id="m1"]')
    await card().findAll('button').find(b => b.text() === 'Deny').trigger('click')
    expect(card().find('[data-testid="queue-send"]').text()).toBe('Send: Deny')
  })

  it('is hidden on a gate approval', async () => {
    const w = await mountView({ request_id: 'gate-abc' })
    expect(w.find('[data-item-id="m1"] [data-testid="queue-something-else"]').exists()).toBe(false)
  })
})
