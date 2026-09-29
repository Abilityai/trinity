// @vitest-environment jsdom
/**
 * trinity-enterprise#611 — the Workspace shows the exact action an approval asks
 * the person to approve. An ask addressed to `primary` lands in the owner's
 * Workspace, so this is where most native approvals are decided; the projection
 * (`WorkspaceAsk`) now names `proposal`, and PortalAsks renders it through the
 * same read-only block as the Operations card — MOUNTED (#2918).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'high',
  title: 'Pay the vendor', question: 'Release the payment?', options: ['approve', 'reject'],
  created_at: '2026-09-28T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

let store

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useClientPortalStore()
})

function mountAsks(list) {
  store.asks = list.map((a) => ({ ...a }))
  store.asksAvailable = true
  return mount(PortalAsks, { props: { agentName: 'scout' } })
}

describe('a Workspace approval shows what it would do (mounted)', () => {
  it('shows every field of the proposal on the ask', () => {
    const w = mountAsks([ask('p1', { proposal: { amount: 500, to: 'vendor-7' } })])
    const block = w.find('[data-testid="portal-ask-p1"] [data-testid="queue-proposal"]')
    expect(block.exists()).toBe(true)
    expect(block.text()).toContain('amount')
    expect(block.text()).toContain('500')
    expect(block.text()).toContain('vendor-7')
  })

  it('keeps showing it once the ask ended, so the person sees what was decided', () => {
    const w = mountAsks([ask('a1', { status: 'answered', ended_by: 'you', ended_at: '2026-09-28T11:00:00Z',
                                     proposal: { amount: 500 } })])
    expect(w.find('[data-testid="portal-ask-a1"] [data-testid="queue-proposal"]').exists()).toBe(true)
  })

  it('shows nothing when the ask carries no proposal', () => {
    const w = mountAsks([ask('q1', { kind: 'question', options: null, proposal: null })])
    expect(w.find('[data-testid="portal-ask-q1"] [data-testid="queue-proposal"]').exists()).toBe(false)
  })
})
