// @vitest-environment jsdom
/**
 * #3247 — an agent replaced one of its own pending asks. The predecessor ended
 * `cancelled` / `disposed_by='agent'` / reason `replaced`, and each row names
 * the other (`replaces` on the successor, `replaced_by` on the predecessor —
 * platform ids). Every Operations surface reads "Replaced by the agent", never
 * a person's cancel, a timeout or a raw reason token, and the two asks name
 * each other. One rule (utils/operatorQueue.js), MOUNTED per surface (#2918):
 * ResolvedCard, QueueCard and the `/m` strip.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'ops' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { queueEnding, queueEndingText, queueReaskBadges } from '@/utils/operatorQueue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { useAuthStore } from '@/stores/auth'
import QueueCard from '@/components/operator/QueueCard.vue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'
import MobileAdmin from '@/views/MobileAdmin.vue'

const OLD = {
  id: 'uuid-old', request_id: 'approval-r1-payout', agent_name: 'agent-a', type: 'approval',
  priority: 'high', title: 'Pay vendor 500?', question: 'Release?', options: ['approve', 'reject'],
  context: {}, created_at: '2026-10-05T08:00:00Z', status: 'cancelled', disposition: 'cancelled',
  disposed_by: 'agent', disposed_by_email: null, disposition_reason: 'replaced',
  disposed_at: '2026-10-05T09:00:00Z', replaced_by: 'uuid-new',
}
const NEW = {
  id: 'uuid-new', request_id: 'approval-r2-payout', agent_name: 'agent-a', type: 'approval',
  priority: 'high', title: 'Pay vendor 450 (invoice corrected)?', question: 'Release?',
  options: ['approve', 'reject'], context: {}, created_at: '2026-10-05T09:00:00Z',
  status: 'pending', replaces: 'uuid-old',
}

describe('the rule', () => {
  it('a replaced ask ends as Replaced by the agent — not a cancel, a timeout or a person', () => {
    const e = queueEnding(OLD)
    expect(e.kind).toBe('replaced')
    expect(queueEndingText(e)).toBe('Replaced by the agent')
  })

  it('a person\'s cancel and a timeout keep their own words', () => {
    expect(queueEndingText(queueEnding({ ...OLD, disposed_by: 'person', disposed_by_email: 'op@example.com', disposition_reason: 'no' })))
      .toBe('Cancelled by op@example.com')
    expect(queueEndingText(queueEnding({ ...OLD, status: 'expired', disposition: 'expired', disposed_by: 'timeout' })))
      .toBe('Expired — nobody answered in time')
  })

  it('the two asks name each other, from their own rows', () => {
    expect(queueReaskBadges(NEW, [OLD, NEW])).toEqual([
      { key: 'replaces', prefix: 'Replaces', id: 'approval-r1-payout', title: 'The agent replaced "Pay vendor 500?" with this ask.' },
    ])
    expect(queueReaskBadges(OLD, [OLD, NEW])).toEqual([
      { key: 'replaced-by', prefix: 'Replaced by', id: 'approval-r2-payout', title: 'The agent replaced this ask with "Pay vendor 450 (invoice corrected)?".' },
    ])
  })

  it('the other ask need not be loaded for the row to say it', () => {
    expect(queueReaskBadges(NEW, [NEW])[0]).toMatchObject({ key: 'replaces', prefix: 'Replaces an earlier ask', id: null })
    expect(queueReaskBadges(OLD, [OLD])[0]).toMatchObject({ key: 'replaced-by', prefix: 'Replaced by a newer ask', id: null })
  })
})

function mountWith(Component, item, items) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useOperatorQueueStore().items = items
  return mount(Component, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
}

describe('Operations cards (mounted)', () => {
  it('the replaced card reads Replaced by the agent, names its successor and hides the raw reason', () => {
    const w = mountWith(ResolvedCard, OLD, [OLD, NEW])
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Replaced by the agent')
    expect(w.find('[data-testid="queue-replaced-by"]').text()).toBe('Replaced by approval-r2-payout')
    expect(w.find('[data-testid="resolved-response"]').exists()).toBe(false)
    expect(w.text()).not.toMatch(/—\s*replaced/)
    expect(w.text()).not.toContain('Cancelled')
  })

  it('a person\'s cancel still shows the reason they gave', () => {
    const cut = { ...OLD, disposed_by: 'person', disposed_by_email: 'op@example.com', disposition_reason: 'not now', replaced_by: null }
    const w = mountWith(ResolvedCard, cut, [cut])
    expect(w.text()).toContain('not now')
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Cancelled by op@example.com')
  })

  it('the pending successor says what it replaces', () => {
    const w = mountWith(QueueCard, NEW, [OLD, NEW])
    expect(w.find('[data-testid="queue-replaces"]').text()).toBe('Replaces approval-r1-payout')
    expect(w.find('[data-testid="queue-replaced-by"]').exists()).toBe(false)
  })
})

describe('/m Recently ended (mounted)', () => {
  let wrapper
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('a replaced ask reads Replaced by the agent', async () => {
    http.get.mockImplementation((url) => {
      if (url === '/api/operator-queue') return Promise.resolve({ data: { items: [NEW, OLD], count: 2 } })
      if (url === '/api/notifications') return Promise.resolve({ data: { items: [], count: 0 } })
      return Promise.reject(new Error(`unexpected ${url}`))
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().isAuthenticated = true
    wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
    await flushPromises()
    const card = wrapper.find('[data-testid="queue-ended-card"]')
    expect(card.attributes('data-item-id')).toBe('uuid-old')
    expect(card.text()).toContain('Replaced by the agent')
    expect(card.text()).not.toContain('Cancelled')
  })
})
