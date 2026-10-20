// @vitest-environment jsdom
/**
 * trinity-enterprise#901 — an ask the agent withdrew, on the Workspace. The
 * server projects `status: 'cancelled'`, `ended_by: 'agent'`,
 * `agent_ending: 'withdrawn'` and the agent's `ending_reason`.
 *
 * Mounted (#2918): the Inbox row's ended badge stays the short label and the
 * reason sits on its own clamped line (full text on `title`); PortalAsks'
 * ending line and reason; never a Waiting item — no answer box, no Discuss,
 * no Dismiss, and it leaves `openAsks` and the "needs you" count.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalInboxList from '@/components/portal/PortalInboxList.vue'
import { actionItems, inboxCounts } from '@/components/portal/portalInbox'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const REASON = 'The build fixed itself, so the deploy is no longer needed.'
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-10-14T09:00:00Z',
  ended_at: null, ended_by: null, replaces: null, replaced_by: null,
  agent_ending: null, ending_reason: null, ...over,
})
const WITHDRAWN = ask('gone', {
  status: 'cancelled', ended_by: 'agent', ended_at: '2026-10-14T10:00:00Z',
  agent_ending: 'withdrawn', ending_reason: REASON,
})
const PENDING = ask('open')

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.asksLoaded = true
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('PortalInboxList', () => {
  const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name,
    title: a.title, status: a.status, at: a.ended_at || a.created_at, ask: a })

  it('the badge says "Withdrawn by the agent"; the reason is its own clamped line', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(WITHDRAWN)] } })
    await flushPromises()
    const badge = wrapper.find('[data-testid="inbox-row-ended-ask:gone"]')
    expect(badge.text()).toBe('Withdrawn by the agent')
    expect(badge.text()).not.toContain(REASON)
    const reason = wrapper.find('[data-testid="inbox-row-ending-reason-ask:gone"]')
    expect(reason.text()).toBe(REASON)
    expect(reason.attributes('title')).toBe(REASON)
    expect(reason.classes()).toContain('truncate')
  })

  it('a withdrawn row is history, not Waiting: no priority/expiry badges', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item({ ...WITHDRAWN, priority: 'critical' })] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-badge-priority"]').exists()).toBe(false)
  })

  it('no other ending shows a reason line', async () => {
    const op = ask('op', { status: 'cancelled', ended_by: 'operator', ending_reason: null })
    const rep = ask('rep', { status: 'cancelled', ended_by: 'agent', agent_ending: 'replaced', replaced_by: 'v2' })
    wrapper = mount(PortalInboxList, { props: { items: [item(op), item(rep)] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:op"]').text()).toBe('Cancelled')
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:rep"]').text()).toBe('Replaced by the agent with v2')
    expect(wrapper.findAll('[data-testid^="inbox-row-ending-reason-"]')).toHaveLength(0)
  })
})

describe('PortalAsks', () => {
  it('the ending line reads "Withdrawn by the agent" with the reason; nothing to answer', async () => {
    store.asks = [WITHDRAWN]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-ask-ending"]').text()).toMatch(/^Withdrawn by the agent/)
    expect(wrapper.find('[data-testid="portal-ask-ending-reason"]').text()).toBe(REASON)
    // never a Waiting item: no option to pick, no Discuss, no Dismiss
    expect(wrapper.text()).not.toMatch(/\bApprove\b/)
    expect(wrapper.text()).not.toMatch(/Discuss|Dismiss/)
  })

  it('a pending-only strip never shows it', async () => {
    store.asks = [WITHDRAWN, PENDING]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('Ask open')
    expect(wrapper.text()).not.toContain('Ask gone')
  })
})

describe('the "needs you" count', () => {
  it('a withdrawn ask leaves openAsks, the Action list and the count', () => {
    store.asks = [WITHDRAWN, PENDING]
    expect(store.openAsks.map((a) => a.id)).toEqual(['open'])
    expect(actionItems(store.asks).map((i) => i.id)).toEqual(['open'])
    expect(inboxCounts([], store.openAsks).needs).toBe(1)
  })
})
