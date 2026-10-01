// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — manual sign-off, round 4.
 *
 *  1. An ask's body is agent-written markdown. PortalAsks printed it as plain
 *     text on every surface (chat, Work tab, Inbox), `**bold**` and all.
 *  2. The Inbox pane's header for an ask repeated the card's title under it —
 *     it now names the agent and the kind; the card carries the title.
 *  3. The Inbox list's scroll box was not a containing block, so each row's
 *     absolute `sr-only` kind escaped it and stretched the PAGE to the list's
 *     full height: the whole shell scrolled away under a blank tail.
 *  4. A chat pinned EVERY ask its agent had above the composer, uncapped —
 *     seven cards crushed the conversation to nothing. It pins this chat's
 *     asks (Main also takes the unattached), capped, and names the rest.
 *
 * @source-text-pin: 3 and 4's height cap are LAYOUT (a containing block, a
 * max-height scroll box) — jsdom computes no layout, so a mount cannot see them;
 * the live probe (page scrollHeight 1749 → 1000) is their behavioural proof.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'
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
import { paneHeading } from '@/components/portal/portalInbox'
import { splitChatAsks } from '@/components/portal/portalUtils'

const PORTAL = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'components', 'portal')
const src = (rel) => readFileSync(join(PORTAL, rel), 'utf8')

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: 'main-1', sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

beforeEach(() => { setActivePinia(createPinia()) })

describe('1 — an ask body renders as markdown', () => {
  it('bold, a list and a code fence render as elements, not as their syntax', () => {
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asks = [ask('m1', { question: 'It has **bold** and:\n\n- one\n- two\n\n```python\nprint(1)\n```' })]
    const w = mount(PortalAsks, { props: { agentName: 'scout' } })
    const body = w.find('[data-testid="portal-ask-question-m1"]')
    expect(body.exists()).toBe(true)
    expect(body.find('strong').text()).toBe('bold')
    expect(body.findAll('li')).toHaveLength(2)
    expect(body.text()).not.toContain('**')
    expect(body.text()).not.toContain('```')
  })

  it('a script in an ask is sanitized, never executed markup', () => {
    const store = useClientPortalStore()
    store.asksAvailable = true
    store.asks = [ask('x1', { question: 'hi <img src=x onerror="window.__pwn=1"><script>window.__pwn=2</script>' })]
    const w = mount(PortalAsks, { props: { agentName: 'scout' } })
    const html = w.find('[data-testid="portal-ask-question-x1"]').html()
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('<script')
  })
})

describe('2 — the Inbox pane header for an ask', () => {
  it('names the agent and the kind, never the title the card already shows', () => {
    const h = paneHeading({ type: 'ask', title: 'Sign the Acme NDA as-is?', ask: { kind: 'approval' } }, 'legal-reviewer')
    expect(h).toBe('legal-reviewer · Needs approval')
    expect(h).not.toContain('Sign the Acme')
  })
  it('a chat keeps agent · chat title', () => {
    expect(paneHeading({ type: 'thread', is_main: true }, 'scout')).toBe('scout · Main')
    expect(paneHeading({ type: 'thread', title: 'Q3' }, 'scout')).toBe('scout · Q3')
  })
})

describe('3 — the Inbox list scroll box contains its rows', () => {
  it('the scrolling <ul> is positioned, so an absolute sr-only child cannot escape it', () => {
    const ul = src('PortalInboxList.vue').match(/<ul class="([^"]*)"[^>]*data-testid="inbox-list"/)
    expect(ul).not.toBeNull()
    expect(ul[1].split(/\s+/)).toContain('relative')
    expect(ul[1].split(/\s+/)).toContain('overflow-y-auto')
  })
})

describe('4 — which asks a chat pins', () => {
  const asks = [
    ask('here', { chat_id: 's1' }),
    ask('main', { chat_id: 'main-1' }),
    ask('loose', { chat_id: null }),
    ask('done', { chat_id: 'main-1', status: 'answered' }),
  ]
  it('a non-Main chat pins only its own; the rest that still wait are counted', () => {
    const { here, elsewhere } = splitChatAsks(asks, { sessionId: 's1', isMain: false })
    expect(here.map((a) => a.id)).toEqual(['here'])
    expect(elsewhere.map((a) => a.id)).toEqual(['main', 'loose'])
  })
  it('Main also takes an unattached ask; an ended one elsewhere is not "waiting"', () => {
    const { here, elsewhere } = splitChatAsks(asks, { sessionId: 'main-1', isMain: true })
    expect(here.map((a) => a.id)).toEqual(['main', 'loose', 'done'])
    expect(elsewhere.map((a) => a.id)).toEqual(['here'])
  })
  it('the pinned block is height-capped and the conversation passes only this chat\'s ids', () => {
    const conv = src('PortalConversation.vue')
    expect(conv).toMatch(/data-testid="portal-chat-asks"/)
    expect(conv).toMatch(/max-h-\[\d+vh\][^"]*overflow-y-auto|overflow-y-auto[^"]*max-h-\[\d+vh\]/)
    expect(conv).toMatch(/:ask-ids="chatAskIds"/)
    expect(conv).toMatch(/data-testid="portal-chat-asks-elsewhere"/)
  })
})
