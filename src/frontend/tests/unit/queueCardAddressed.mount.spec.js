// @vitest-environment jsdom
/**
 * trinity-enterprise#980 — an approval addressed to someone else, in Operations.
 *
 * The queue lists an ask to anyone who can use its agent, but a platform-raised
 * approval belongs to the people it was addressed to (`ask_service.may_end`).
 * The row's `addressed` says who and whether THIS viewer may decide. A viewer
 * who may not — an admin looking at a project creator's decision — reads who it
 * waits on and gets no Approve / Decline, instead of learning it from a refusal.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn(), user: { role: 'admin' } }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(mk(), { create: mk }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import QueueCard from '@/components/operator/QueueCard.vue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'

const row = (over = {}) => ({
  id: 'p1', agent_name: 'compliance-steward', request_id: 'gate-addressee-only-project-proposal-ppr_1',
  type: 'approval', status: 'pending', priority: 'medium', raised_by: 'gate',
  title: "Approve compliance-steward's changes to “SOC 2 compliance”", question: 'The steward proposes the changes below.',
  options: ['Approve', 'Decline'], decided_by_options: true, context: {},
  created_at: '2026-10-27T08:00:00Z', expires_at: '2026-11-03T08:00:00Z',
  proposal: { 'Add members': 'bob@example.com' }, gate: null,
  addressed: { waiting_on: ['alice@example.com'], can_decide: false }, ...over,
})
const tid = (w, id) => w.find(`[data-testid="${id}"]`)

async function mountCard(over = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useOperatorQueueStore()
  store.items = [row(over)]
  store.expandedItemId = 'p1'
  const w = mount(QueueCard, {
    props: { item: store.items[0] },
    global: { plugins: [pinia], stubs: { RouterLink: RouterLinkStub, AgentAvatar: true } },
  })
  await nextTick()
  return w
}

beforeEach(() => vi.clearAllMocks())

describe('ent#980 — an approval addressed to someone else', () => {
  it('a viewer who may not decide reads who it waits on, and gets no controls', async () => {
    const w = await mountCard()
    expect(tid(w, 'queue-addressed-waiting').text()).toContain('alice@example.com')
    expect(tid(w, 'queue-addressed-waiting').text()).toContain('Only they can decide')
    expect(w.findAll('button').some((b) => b.text() === 'Approve')).toBe(false)
    expect(tid(w, 'approval-send').exists()).toBe(false)
    expect(tid(w, 'queue-proposal').exists()).toBe(true)   // the change is still readable
  })

  it('the person it is addressed to keeps the controls and no waiting line', async () => {
    const w = await mountCard({ addressed: { waiting_on: ['alice@example.com'], can_decide: true } })
    expect(tid(w, 'queue-addressed-waiting').exists()).toBe(false)
    expect(w.findAll('button').some((b) => b.text() === 'Approve')).toBe(true)
    expect(tid(w, 'approval-send').exists()).toBe(true)
  })

  it('an ordinary approval (no `addressed`) is unchanged', async () => {
    const w = await mountCard({ addressed: null, raised_by: 'agent' })
    expect(tid(w, 'queue-addressed-waiting').exists()).toBe(false)
    expect(tid(w, 'approval-send').exists()).toBe(true)
  })

  it('once it has ended, the waiting line is gone', async () => {
    const w = await mountCard({ status: 'responded' })
    expect(tid(w, 'queue-addressed-waiting').exists()).toBe(false)
  })
})
