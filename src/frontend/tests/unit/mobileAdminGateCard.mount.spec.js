// @vitest-environment jsdom
/**
 * trinity-enterprise#755 — `/m` draws the gated-skill approval card, and offers
 * its controls only to a person the approval is addressed to. MOUNTED (#2918).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
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

const item = (canDecide) => ({
  id: 'g1', agent_name: 'finance', type: 'approval', priority: 'high', raised_by: 'gate',
  title: 'Approve the skill pay-invoice on finance', question: 'Agent marketing asked finance.',
  options: ['Approve', 'Reject'], decided_by_options: true, status: 'pending',
  created_at: '2026-10-12T08:00:00Z', expires_at: '2026-10-13T08:00:00Z',
  proposal: { kind: 'gated_skill', input: '/pay-invoice INV-8' },
  gate: {
    skills: [{ name: 'pay-invoice', description: 'Pays one supplier invoice.' }],
    requester: { kind: 'agent', label: 'Agent marketing', agent: 'marketing', execution_id: null },
    state: 'pending', execution_id: null, waiting_on: ['Dana Reyes'], can_decide: canDecide,
  },
})

let wrapper

async function mountView(canDecide) {
  http.get.mockImplementation((url) => {
    if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [item(canDecide)], count: 1 } })
    if (url === '/api/notifications') return Promise.resolve({ data: { items: [], count: 0 } })
    return Promise.reject(new Error(`unexpected ${url}`))
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, {
    global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true, RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return wrapper.find('[data-item-id="g1"]')
}

describe('/m — the gated-skill approval card', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('someone it is not addressed to reads who it waits on and gets no options', async () => {
    const card = await mountView(false)
    expect(card.find('[data-testid="gate-card-waiting-on"]').text()).toBe('Dana Reyes')
    expect(card.find('[data-testid="queue-proposal"]').exists()).toBe(false)
    expect(card.findAll('[data-testid="queue-option"]')).toHaveLength(0)
  })

  it('the addressee gets Approve and Reject, and Reject asks for the reason', async () => {
    const card = await mountView(true)
    const options = card.findAll('[data-testid="queue-option"]')
    expect(options.map((b) => b.text())).toEqual(['Approve', 'Reject'])
    expect(card.find('[data-testid="gate-card-waiting-on"]').exists()).toBe(false)
    await options[1].trigger('click')
    expect(card.find('[data-testid="queue-note"]').attributes('placeholder')).toContain('Reason (optional)')
  })
})
