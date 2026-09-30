// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — manual sign-off, round 5: an ask shows in ONE
 * place at a time, and Work is where it lives.
 *
 *  - A chat no longer pins every ask as a card: one "N asks waiting on you"
 *    row, CLOSED by default, that expands inline (like "Delivered here"). An
 *    ask answered while you watch stays drawn, ended, until you leave the chat.
 *  - In the Inbox, the Work tab leaves out the ask already open in the pane.
 *  - Info stops suggesting "Answer what this agent asked you": its dot then
 *    means a real suggestion, and the tab's dot says what it means on hover.
 *
 * @source-text-pin: the chat's collapsed-by-default row lives in
 * PortalConversation, whose mount needs the whole chat stack; its predicates
 * (pinnedAskIds, the label) are proven here as pure helpers and the wiring is
 * pinned by spelling.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalSuggestions from '@/components/portal/PortalSuggestions.vue'
import OverflowTabs from '@/components/OverflowTabs.vue'
import { infoSignalFrom } from '@/components/portal/portalRail'
import { askTileMode } from '@/components/portal/portalChatAsks'

const PORTAL = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'components', 'portal')
const src = (rel) => readFileSync(join(PORTAL, rel), 'utf8')

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: 'main-1', sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

beforeEach(() => { setActivePinia(createPinia()); axios.get.mockReset() })

// The 09-30 ruling (amended) replaced round 5's closed "N asks waiting on you"
// row with a tile per chat-turn ask inside the thread. Round 5's "an ask you just
// answered stays drawn, ended, until you leave the chat" survives as the tile's
// card/row choice.
describe("the chat's asks are thread rows, not a row above the composer", () => {
  it('askTileMode: a waiting ask is a card; so is one seen waiting during this visit', () => {
    expect(askTileMode(ask('p'), new Set())).toBe('card')
    expect(askTileMode(ask('done', { status: 'answered' }), new Set(['done']))).toBe('card')
    expect(askTileMode(ask('old', { status: 'expired' }), new Set())).toBe('row')
  })
  it('there is no disclosure row and no toggle above the composer', () => {
    const conv = src('PortalConversation.vue')
    expect(conv).not.toMatch(/asksOpen/)
    expect(conv).not.toMatch(/data-testid="portal-chat-asks-toggle"/)
    expect(conv).not.toMatch(/asks waiting on you/)
  })
})

// Round 5 had Work skip the ask open in the Inbox pane, so one ask was never
// drawn twice side by side. The 09-30 ruling removes the cause: Work draws no
// ask (portalWorkAsksLine.spec.js), so there is nothing to skip, and the one
// line it keeps counts the pane's ask too.
describe('Work draws no ask, so nothing is drawn twice beside the Inbox pane', () => {
  it('PortalAsks renders every ask it is given — the exclude list is gone', () => {
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asks = [ask('a1'), ask('a2')]
    const w = mount(PortalAsks, { props: { agentNames: ['scout'], pendingOnly: true, excludeIds: ['a1'] } })
    expect(w.find('[data-testid="portal-ask-a1"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-a2"]').exists()).toBe(true)
  })
  it('the shell hands Work no open-ask list, and Work mounts no PortalAsks', () => {
    const work = src('PortalWork.vue')
    expect(work).not.toMatch(/excludeAskIds|<PortalAsks\b/)
    const shell = readFileSync(join(PORTAL, '..', '..', 'views', 'Portal.vue'), 'utf8')
    expect(shell).not.toMatch(/inboxOpenAskIds/)
  })
})

describe('Info no longer points at the asks', () => {
  it('the Info dot ignores the asks suggestion', () => {
    const only = { total: 1, suggestions: [{ key: 'asks', source: 'asks' }] }
    expect(infoSignalFrom(only)).toBeNull()
    const two = { total: 2, suggestions: [{ key: 'asks', source: 'asks' }, { key: 'x', source: 'capability' }] }
    expect(infoSignalFrom(two)).toEqual({ updated: true, note: '1 suggestion' })
    expect(infoSignalFrom(null)).toBeNull()
  })
  it('PortalSuggestions omitSources hides that source and its count', async () => {
    axios.get.mockResolvedValue({ data: {
      agent_name: 'scout', total: 2, capabilities: 'available', basis: 'history',
      suggestions: [
        { key: 'asks', kind: 'invoke', source: 'asks', title: 'Answer what this agent asked you', signal: '3 questions', action: { type: 'open_section', value: 'asks' } },
        { key: 'w', kind: 'invoke', source: 'capability', title: 'Weekly report', signal: 's', action: { type: 'prefill', value: '/w ' } },
      ],
    } })
    const w = mount(PortalSuggestions, { props: { agentName: 'scout', omitSources: ['asks'] } })
    await flushPromises()
    const titles = w.findAll('[data-testid="portal-suggestion"]').map((n) => n.text())
    expect(titles).toHaveLength(1)
    expect(titles[0]).toContain('Weekly report')
    expect(w.text()).not.toContain('Answer what this agent asked you')
  })
  it('Info mounts its suggestions without the asks source', () => {
    expect(src('PortalAgentDetails.vue')).toMatch(/<PortalSuggestions[\s\S]{0,200}:omit-sources="\['asks'\]"/)
  })
  it('a tab dot carries its meaning as the tab title', () => {
    globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
    const w = mount(OverflowTabs, { props: {
      tabs: [{ id: 'work', label: 'Work' }, { id: 'info', label: 'Info', signal: 'updated', signalTitle: 'Info · 2 suggestions' }],
      modelValue: 'work',
    } })
    const info = w.findAll('button').find((b) => b.text() === 'Info')
    expect(info.attributes('title')).toBe('Info · 2 suggestions')
    w.unmount()
  })
})
