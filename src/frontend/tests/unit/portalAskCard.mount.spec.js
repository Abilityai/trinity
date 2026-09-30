// @vitest-environment jsdom
/**
 * #3055 (trinity-enterprise#610 §3g L0) — an ask attached to ANOTHER chat keeps
 * its answer controls.
 *
 * ent#429 put the "Open the conversation" button between the ending line and the
 * controls, and the controls' `v-else-if` bound to that button. So whenever the
 * link rendered, the controls did not. Ingestion attaches every addressed ask to
 * Main, which made every ask read outside Main (a non-Main chat, the rail's Work
 * tab) a link with no way to answer. The link is additive: it must never take
 * the controls away. MOUNTED, because the defect is which template branch Vue
 * picks — no pure helper can see it.
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
// `chat_id` is the platform-stamped thread the ask was raised against. Main's
// id here is deliberately NOT null: a null chat_id renders no link, which is
// exactly how the round-1 review seed hid this bug. `raised_in_turn`: only an
// ask a chat turn raised links back to its chat (ent#610, the 09-30 ruling).
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: ['Yes', 'No'],
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: 'main-1', raised_in_turn: true, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
let store

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useClientPortalStore()
})

function mountAsks(asks, props = {}) {
  store.asks = asks
  store.asksAvailable = true
  return mount(PortalAsks, { props: { agentName: 'scout', ...props } })
}

describe('an ask attached to another chat (mounted)', () => {
  it('an approval shows its options, Send AND the link back to its chat', () => {
    const w = mountAsks([ask('ap1')], { currentSessionId: null })
    expect(tid(w, 'portal-ask-open-thread-ap1').exists()).toBe(true)
    expect(w.findAll('[data-testid="portal-ask-option-ap1"]').map((b) => b.text())).toEqual(['Yes', 'No'])
    expect(tid(w, 'portal-ask-send-ap1').exists()).toBe(true)
  })

  it('a question read from a different chat keeps its answer box beside the link', () => {
    const w = mountAsks([ask('q1', { kind: 'question', options: null })], { currentSessionId: 'other-2' })
    expect(tid(w, 'portal-ask-open-thread-q1').exists()).toBe(true)
    expect(tid(w, 'portal-ask-input-q1').exists()).toBe(true)
  })

  it('an alert keeps "Got it" beside the link', () => {
    const w = mountAsks([ask('al1', { kind: 'alert', options: null })], { currentSessionId: 'other-2' })
    expect(tid(w, 'portal-ask-open-thread-al1').exists()).toBe(true)
    expect(tid(w, 'portal-ask-ack-al1').exists()).toBe(true)
  })

  it('read in its own chat: controls, and no link to where the reader already is', () => {
    const w = mountAsks([ask('ap1')], { currentSessionId: 'main-1' })
    expect(tid(w, 'portal-ask-open-thread-ap1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-send-ap1').exists()).toBe(true)
  })

  it('an ended ask keeps its link and still offers no way to answer', () => {
    const w = mountAsks(
      [ask('a1', { status: 'answered', ended_by: 'you', ended_at: ENDED })],
      { currentSessionId: 'other-2' },
    )
    expect(tid(w, 'portal-ask-open-thread-a1').exists()).toBe(true)
    expect(tid(w, 'portal-ask-ending').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-a1"] input').exists()).toBe(false)
    expect(tid(w, 'portal-ask-send-a1').exists()).toBe(false)
  })

  it('threadLink=false drops the link and keeps the controls', () => {
    const w = mountAsks([ask('ap1')], { currentSessionId: null, threadLink: false })
    expect(tid(w, 'portal-ask-open-thread-ap1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-option-ap1').exists()).toBe(true)
    expect(tid(w, 'portal-ask-send-ap1').exists()).toBe(true)
  })
})

describe('the Work tab\'s "Waiting on you" is answerable in place (mounted)', () => {
  it('an ask attached to Main, read from another chat\'s rail, shows its controls', async () => {
    store.asks = [ask('ap1')]
    store.asksAvailable = true
    const work = usePortalWorkStore()
    work.hasLoaded = true
    portalHttp.get.mockResolvedValue({ data: { now: [], earlier: [], earlier_total: 0 } })
    const w = mount(PortalWork, {
      props: { participants: ['scout'], chatId: 'other-2' },
      global: { stubs: { PortalWorkCard: true, PortalAvatar: true, PortalSkeleton: true, LoadFailed: true } },
    })
    await flushPromises()
    const waiting = w.find('[data-testid="portal-work-waiting"]')
    expect(waiting.exists()).toBe(true)
    expect(waiting.find('[data-testid="portal-ask-open-thread-ap1"]').exists()).toBe(true)
    expect(waiting.find('[data-testid="portal-ask-option-ap1"]').exists()).toBe(true)
    expect(waiting.find('[data-testid="portal-ask-send-ap1"]').exists()).toBe(true)
  })
})
