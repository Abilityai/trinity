// @vitest-environment jsdom
/**
 * #3247 — a replaced ask, on every Workspace view. The agent ended its own
 * pending ask by raising a newer one; the server projects that as
 * `status: 'cancelled'`, `ended_by: 'agent'`, and — only when this addressee
 * could already see the other ask — `replaced_by` / `replaces` as the other
 * ask's request_id.
 *
 * Mounted (#2918): PortalAsks' ending line and the successor's line, the Inbox
 * row's ended badge and its "Replaces" badge; plus the chat history row and the
 * "needs you" counts, which read the same `openAsks` feed.
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
import { askHistoryLine } from '@/components/portal/portalChatAsks'
import { actionItems, inboxCounts } from '@/components/portal/portalInbox'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-10-05T10:00:00Z',
  ended_at: null, ended_by: null, replaces: null, replaced_by: null, ...over,
})
const OLD = ask('old', {
  status: 'cancelled', ended_by: 'agent', ended_at: '2026-10-05T11:00:00Z', replaced_by: 'payout-v2',
})
const NEW = ask('new', { replaces: 'payout-v1' })
// The successor is addressed to someone else: the server named nobody.
const OLD_UNNAMED = { ...OLD, id: 'old2', replaced_by: null }

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

describe('PortalAsks', () => {
  it('reads "Replaced by the agent" and names the successor — never the operator', async () => {
    store.asks = [OLD]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    const line = wrapper.find('[data-testid="portal-ask-ending"]').text()
    expect(line).toMatch(/^Replaced by the agent with payout-v2/)
    expect(line).not.toContain('operator')
  })

  it('without a visible successor it says who ended it and names nothing', async () => {
    store.asks = [OLD_UNNAMED]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    const line = wrapper.find('[data-testid="portal-ask-ending"]').text()
    expect(line).toMatch(/^Replaced by the agent/)
    expect(line).not.toContain(' with ')
  })

  it('the replacement shows "Replaces <request_id>"; an ordinary ask shows no such line', async () => {
    store.asks = [NEW, ask('plain')]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    const lines = wrapper.findAll('[data-testid="portal-ask-replaces"]')
    expect(lines.map((l) => l.text())).toEqual(['Replaces payout-v1'])
  })

  it('a pending-only strip never shows the replaced ask', async () => {
    store.asks = [OLD, NEW]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('Ask new')
    expect(wrapper.text()).not.toContain('Ask old')
  })
})

describe('PortalInboxList', () => {
  const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name,
    title: a.title, status: a.status, at: a.ended_at || a.created_at, ask: a })

  it("the replaced row's badge names the agent and the successor", async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(OLD), item(OLD_UNNAMED)] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:old"]').text()).toBe('Replaced by the agent with payout-v2')
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:old2"]').text()).toBe('Replaced by the agent')
  })

  it('an operator cancel still reads "Cancelled"', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(ask('op', { status: 'cancelled', ended_by: 'operator' }))] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-ended-ask:op"]').text()).toBe('Cancelled')
  })

  it('the pending replacement carries a "Replaces" badge', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(NEW), item(ask('plain'))] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-replaces-ask:new"]').text()).toBe('Replaces payout-v1')
    expect(wrapper.find('[data-testid="inbox-row-replaces-ask:plain"]').exists()).toBe(false)
  })
})

describe('the chat history row', () => {
  it('reads "Replaced by the agent with <request_id>"', () => {
    expect(askHistoryLine(OLD).ending).toBe('Replaced by the agent with payout-v2')
    expect(askHistoryLine(OLD_UNNAMED).ending).toBe('Replaced by the agent')
  })
})

describe('the "needs you" count', () => {
  it('a replaced ask leaves it; its replacement is the one counted', () => {
    store.asks = [OLD, NEW]
    expect(store.openAsks.map((a) => a.id)).toEqual(['new'])
    expect(actionItems(store.asks).map((i) => i.id)).toEqual(['new'])
    expect(inboxCounts([], store.openAsks).needs).toBe(1)
  })
})
