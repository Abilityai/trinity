// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — the person an ask was addressed to sees how it
 * ENDED (#606: "surfaced as expired … never a silent refusal"), for 7 days, with
 * a coarse who (you / the operator / timeout) and never an operator's email or
 * the cancel reason. The sidebar count and the Work tab's "Waiting on you" stay
 * pending-only. MOUNTED where a template branch decides it (#2918).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import { usePortalWorkStore } from '@/stores/portalWork'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalWork from '@/components/portal/PortalWork.vue'

const ENDED = '2026-09-25T10:00:00.000000Z'
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})
const LIST = [
  ask('p1'),
  ask('c1', { status: 'cancelled', ended_by: 'operator', ended_at: ENDED }),
  ask('a1', { status: 'answered', ended_by: 'you', ended_at: ENDED }),
  ask('e1', { status: 'expired', ended_by: 'timeout', ended_at: ENDED, expires_at: '2026-09-25T09:59:58Z' }),
]

let store

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useClientPortalStore()
})

describe('the store keeps ended asks in the one list', () => {
  it('asks for the ended ones too', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: LIST })
    await store.fetchAsks('scout')
    const [, config] = portalHttp.get.mock.calls[0]
    expect(config.params).toEqual({ agent_name: 'scout', include_ended: true })
    expect(store.askCount).toBe(1)                     // the badge: pending only
    expect(store.asksForAgent('scout')).toHaveLength(4)
  })

  it('an answered ask stays listed as the server now reads it — answered, by you', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('p1'), ask('p2')] })
    await store.fetchAsks()
    portalHttp.post.mockResolvedValueOnce({ data: ask('p1', { status: 'answered', ended_by: 'you', ended_at: ENDED }) })
    await store.answerAsk('p1', { response: 'yes' })
    expect(store.askCount).toBe(1)
    const answered = store.asks.find((a) => a.id === 'p1')
    expect(answered.status).toBe('answered')
    expect(answered.ended_by).toBe('you')
    expect(portalHttp.get).toHaveBeenCalledTimes(1)    // no refetch needed
  })
})

function mountAsks(props = {}) {
  store.asks = LIST.map((a) => ({ ...a }))
  store.asksAvailable = true
  return mount(PortalAsks, { props: { agentName: 'scout', ...props } })
}

describe('PortalAsks renders how an ask ended (mounted)', () => {
  it('an ended ask says how it ended and offers no way to answer it', () => {
    const w = mountAsks()
    for (const id of ['c1', 'a1', 'e1']) {
      const card = w.find(`[data-testid="portal-ask-${id}"]`)
      expect(card.find('input').exists(), id).toBe(false)
      expect(card.find('button[type="submit"]').exists(), id).toBe(false)
      expect(card.find('[data-testid="portal-ask-ending"]').exists(), id).toBe(true)
    }
    expect(w.find('[data-testid="portal-ask-c1"] [data-testid="portal-ask-ending"]').text()).toContain('Cancelled by the operator')
    expect(w.find('[data-testid="portal-ask-a1"] [data-testid="portal-ask-ending"]').text()).toContain('Answered by you')
    expect(w.find('[data-testid="portal-ask-e1"] [data-testid="portal-ask-ending"]').text()).toContain('expired')
    expect(w.find('[data-testid="portal-ask-a1"] [data-testid="portal-ask-ending"]').attributes('title')).toContain('2026')
  })

  it('a pending ask keeps its answer box', () => {
    const w = mountAsks()
    expect(w.find('[data-testid="portal-ask-p1"] input').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-p1"] [data-testid="portal-ask-ending"]').exists()).toBe(false)
  })

  it('a sync badge speaks only about an ask that is still waiting', () => {
    store.asks = [
      ask('p1', { sync: 'unconfirmed' }),
      ask('c1', { status: 'cancelled', ended_by: 'operator', ended_at: ENDED, sync: 'unconfirmed' }),
    ]
    store.asksAvailable = true
    const w = mount(PortalAsks, { props: { agentName: 'scout' } })
    expect(w.find('[data-testid="portal-ask-p1"] [data-testid="queue-sync-badge"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-c1"] [data-testid="queue-sync-badge"]').exists()).toBe(false)
  })

  it('pending-only renders what is still waiting, and nothing else', () => {
    const w = mountAsks({ agentName: null, agentNames: ['scout'], pendingOnly: true })
    expect(w.findAll('[data-testid^="portal-ask-"][data-status]').map((c) => c.attributes('data-status'))).toEqual(['pending'])
  })
})

describe('the Work tab\'s "Waiting on you" stays pending-only (mounted)', () => {
  it('lists the pending ask of a participant, never one that ended', async () => {
    store.asks = LIST.map((a) => ({ ...a }))
    store.asksAvailable = true
    const work = usePortalWorkStore()
    work.hasLoaded = true
    portalHttp.get.mockResolvedValue({ data: { now: [], earlier: [], earlier_total: 0 } })
    const w = mount(PortalWork, {
      props: { participants: ['scout'] },
      global: { stubs: { PortalWorkCard: true, PortalAvatar: true, PortalSkeleton: true, LoadFailed: true } },
    })
    await flushPromises()
    const waiting = w.find('[data-testid="portal-work-waiting"]')
    expect(waiting.exists()).toBe(true)
    expect(waiting.findAll('[data-status]').map((c) => c.attributes('data-status'))).toEqual(['pending'])
  })
})
