// @vitest-environment jsdom
/**
 * trinity-enterprise#816 — an ask to a role several people fill is ONE ask
 * delivered to every one of them (`shared`). The first answer wins and the
 * others read who answered; Dismiss and Discuss are refused on a shared ask by
 * the server (`shared_ask`), so the card does not offer them. MOUNTED: the
 * gates are template branches.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
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

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { queueEnding, queueEndingText } from '@/utils/operatorQueue'

const ENDED = '2026-10-07T10:00:00.000000Z'
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: ['approve', 'reject'],
  created_at: '2026-10-07T09:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, raised_in_turn: false, discussion_chat_id: null,
  sync: 'confirmed', aging: false, ended_at: null, ended_by: null,
  answered_by: null, shared: false, ...over,
})

let store
let wrapper
const tid = (id) => wrapper.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

function mountAsks(asks) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
}

describe('a shared ask (ent#816)', () => {
  it('offers neither Dismiss nor Discuss, but can still be answered', () => {
    mountAsks([ask('s1', { shared: true }), ask('o1')])

    expect(tid('portal-ask-dismiss-s1').exists()).toBe(false)
    expect(tid('portal-ask-discuss-s1').exists()).toBe(false)
    expect(tid('portal-ask-s1').find('button').exists()).toBe(true)
    // the control: a one-person ask keeps both
    expect(tid('portal-ask-dismiss-o1').exists()).toBe(true)
    expect(tid('portal-ask-discuss-o1').exists()).toBe(true)
  })

  it('answered by someone else first, it says who', () => {
    mountAsks([ask('s2', {
      shared: true, status: 'answered', ended_by: 'someone_else',
      answered_by: 'Alice Example', ended_at: ENDED,
    })])

    expect(tid('portal-ask-s2').find('[data-testid="portal-ask-ending"]').text())
      .toContain('Answered by Alice Example')
  })

  it('without a name it still says someone else answered — never the operator', () => {
    const ending = queueEnding(ask('s3', {
      shared: true, status: 'answered', ended_by: 'someone_else', ended_at: ENDED,
    }))
    expect(queueEndingText(ending)).toBe('Answered by someone else')
  })
})
