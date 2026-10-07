// @vitest-environment jsdom
/**
 * #3246 — platform alerts are ONE row per condition. Two things every queue
 * surface must render, MOUNTED (#2918) because each is a template branch:
 *
 *   1. A row the PLATFORM ended (`disposed_by = 'platform'`, reason
 *      `condition_cleared` / `superseded`) reads "Ended by the platform — <why>",
 *      never as a person's answer, never as a timeout, and never with the raw
 *      reason token printed as though an operator wrote it.
 *   2. A pending alert seen more than once says "seen N times · last seen …",
 *      so a row updated in place does not read as a fresh alert.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { reactive } from 'vue'

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
import { useAuthStore } from '@/stores/auth'
import {
  queueEnding, queueEndingText, queueSeenLine, PLATFORM_ENDING_REASONS,
} from '@/utils/operatorQueue'
import ResolvedCard from '@/components/operator/ResolvedCard.vue'
import QueueCard from '@/components/operator/QueueCard.vue'
import MobileAdmin from '@/views/MobileAdmin.vue'

const ENDED = '2026-10-05T10:00:00Z'
const NOW = Date.parse('2026-10-05T12:00:00Z')

const alertRow = (over = {}) => ({
  id: 'a1', agent_name: '_sub-headroom', type: 'alert', priority: 'high',
  title: 'Subscription near its weekly limit', question: '87% used', options: [],
  context: {}, created_at: '2026-10-01T00:00:00Z', status: 'pending', ...over,
})
const platformEnded = (reason) => alertRow({
  status: 'cancelled', disposition: 'cancelled', disposed_by: 'platform',
  disposed_by_email: null, disposed_at: ENDED, disposition_reason: reason,
})

describe('the ending rule (utils/operatorQueue.js)', () => {
  it.each([
    ['condition_cleared', 'Ended by the platform — the condition cleared'],
    ['superseded', 'Ended by the platform — superseded by a newer reading'],
    ['something_new', 'Ended by the platform'],
  ])('a platform ending (%s) reads as the platform with its reason', (reason, text) => {
    const e = queueEnding(platformEnded(reason))
    expect(e.who).toBe('the platform')
    expect(e.kind).toBe('cancelled')
    expect(queueEndingText(e)).toBe(text)
  })

  it('the Workspace projection (`ended_by = platform`) reads the same way', () => {
    const e = queueEnding({ status: 'cancelled', ended_by: 'platform', ended_at: ENDED })
    expect(queueEndingText(e)).toBe('Ended by the platform')
  })

  it('an expired alert says nobody acted on it; an expired ask keeps its sentence', () => {
    expect(queueEndingText(queueEnding(alertRow({ status: 'expired', disposition: 'expired', disposed_by: 'timeout' }))))
      .toBe('Expired — nobody acted on it')
    expect(queueEndingText(queueEnding({ type: 'approval', status: 'expired', disposition: 'expired' })))
      .toBe('Expired — nobody answered in time')
  })

  it('a person cancel is unchanged', () => {
    const e = queueEnding(alertRow({ status: 'cancelled', disposition: 'cancelled', disposed_by: 'person', disposed_by_email: 'op@example.com' }))
    expect(queueEndingText(e)).toBe('Cancelled by op@example.com')
  })

  it('the seen line: only a pending alert seen more than once', () => {
    const seen = alertRow({ context: { seen_count: 4 }, last_seen_at: '2026-10-05T11:55:00Z' })
    expect(queueSeenLine(seen, NOW)).toBe('seen 4 times · last seen 5m ago')
    expect(queueSeenLine(alertRow({ context: { seen_count: 1 } }), NOW)).toBe('')
    expect(queueSeenLine(alertRow(), NOW)).toBe('')
    expect(queueSeenLine({ ...seen, status: 'cancelled' }, NOW)).toBe('')
    expect(queueSeenLine({ ...seen, type: 'approval' }, NOW)).toBe('')
    expect(queueSeenLine(alertRow({ context: { seen_count: 3 } }), NOW)).toBe('seen 3 times')
  })

  it('names every reason the backend writes', () => {
    expect(Object.keys(PLATFORM_ENDING_REASONS).sort()).toEqual(['condition_cleared', 'superseded'])
  })
})

describe('ResolvedCard (mounted)', () => {
  it('a platform ending names the platform and the reason — the raw token is not the note', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const w = mount(ResolvedCard, {
      props: { item: platformEnded('condition_cleared') },
      global: { plugins: [pinia], stubs: { AgentAvatar: true } },
    })
    expect(w.find('[data-testid="queue-ending"]').text()).toBe('Ended by the platform — the condition cleared')
    expect(w.text()).not.toContain('condition_cleared')
  })
})

describe('QueueCard (mounted)', () => {
  function mountCard(item) {
    const pinia = createPinia()
    setActivePinia(pinia)
    return mount(QueueCard, { props: { item }, global: { plugins: [pinia], stubs: { AgentAvatar: true } } })
  }

  it('a pending alert seen 3 times says so', () => {
    const w = mountCard(alertRow({ context: { seen_count: 3 }, last_seen_at: new Date().toISOString() }))
    expect(w.find('[data-testid="queue-seen-line"]').text()).toBe('seen 3 times · last seen just now')
  })

  it('a first reading shows no seen line', () => {
    const w = mountCard(alertRow({ context: { seen_count: 1 } }))
    expect(w.find('[data-testid="queue-seen-line"]').exists()).toBe(false)
  })
})

describe('QueueItemDetail (mounted)', () => {
  const store = reactive({ selectedItem: null, selectedItemId: null })

  async function mountDetail(item) {
    vi.resetModules()
    vi.doMock('@/stores/operatorQueue', () => ({ useOperatorQueueStore: () => store }))
    vi.doMock('@/stores/agents', () => ({ useAgentsStore: () => ({ agents: [], agentRefForSlug: (name) => ({ name }) }) }))
    const { default: QueueItemDetail } = await import('@/components/operator/QueueItemDetail.vue')
    store.selectedItem = item
    store.selectedItemId = item.id
    return mount(QueueItemDetail)
  }

  afterEach(() => {
    vi.doUnmock('@/stores/operatorQueue')
    vi.doUnmock('@/stores/agents')
  })

  it('a platform ending is not shown as a person\'s answer', async () => {
    const w = await mountDetail(platformEnded('superseded'))
    expect(w.find('[data-testid="queue-detail-ending"]').text()).toContain('Ended by the platform — superseded by a newer reading')
    expect(w.text()).not.toMatch(/\bby\s+·/)
  })

  it('a person\'s answer still names who answered', async () => {
    const w = await mountDetail(alertRow({
      status: 'responded', response: 'acknowledged', disposition: 'answered', disposed_by: 'person',
      responded_by_email: 'op@example.com', responded_at: ENDED,
    }))
    expect(w.find('[data-testid="queue-detail-ending"]').exists()).toBe(false)
    expect(w.text()).toContain('by op@example.com')
  })

  it('a pending alert seen 2 times shows the seen line', async () => {
    const w = await mountDetail(alertRow({ context: { seen_count: 2 }, last_seen_at: new Date().toISOString() }))
    expect(w.find('[data-testid="queue-seen-line"]').text()).toBe('seen 2 times · last seen just now')
  })
})

describe('/m (mounted)', () => {
  let wrapper
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

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

  it('a pending alert seen 5 times and a platform-ended alert both read right', async () => {
    const w = await mountView([
      alertRow({ id: 'live', context: { seen_count: 5 }, last_seen_at: new Date().toISOString() }),
      platformEnded('condition_cleared'),
    ])
    expect(w.find('[data-testid="queue-seen-line"]').text()).toBe('seen 5 times · last seen just now')
    const ended = w.find('[data-testid="queue-recently-ended"]').findAll('[data-testid="queue-ended-card"]')
    expect(ended.map((c) => c.text()).join(' ')).toContain('Ended by the platform — the condition cleared')
  })
})
