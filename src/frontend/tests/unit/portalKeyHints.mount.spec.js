// @vitest-environment jsdom
/**
 * ent#621 [C] — the keys are discoverable from the controls they drive.
 *
 * A modifier chord nobody can see is a feature only its author uses, so every
 * control the keys move carries the chord twice: in the tooltip people read and
 * in `aria-keyshortcuts` assistive tech reads. Both are DERIVED from
 * `WORKSPACE_KEYMAP` — which is what these cases check, by asserting against
 * the map rather than against a repeated string. A hand-typed glyph passes a
 * literal assertion and then goes stale alone.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { keyHint, keyShortcutsFor, hostPlatform } from '@/components/portal/portalKeymap'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(inst(), { create: inst }) }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalRail from '@/components/portal/PortalRail.vue'
import PortalChatTabs from '@/components/portal/PortalChatTabs.vue'

const P = hostPlatform()
globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: { template: '<div />' } },
      { path: '/workspace/inbox', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace')
  await router.isReady()
})

describe('ent#621 — the sidebar shows the agent keys', () => {
  const mountSidebar = () => mount(PortalSidebar, {
    attachTo: document.body,
    props: {
      roster: [{ name: 'scout' }, { name: 'sage' }],
      activeAgentName: 'scout',
      threads: [],
      clientEmail: 'me@example.com',
    },
    global: { plugins: [router] },
  })

  it('every agent row names the switch-agent keys, in both channels', () => {
    const w = mountSidebar()
    const row = w.findAll('button').find((b) => (b.attributes('title') || '').includes('scout'))
    expect(row).toBeTruthy()
    expect(row.attributes('title')).toContain(keyHint(['agent-prev', 'agent-next'], P))
    expect(row.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor(['agent-prev', 'agent-next'], P))
    // The ARIA spelling is not the glyph one — AT reads key NAMES.
    expect(row.attributes('aria-keyshortcuts')).toContain('ArrowDown')
    w.unmount()
  })

  it('has a button for the key list, so a mouse reaches what ⌘/ reaches', async () => {
    const w = mountSidebar()
    const btn = w.find('[data-testid="portal-sidebar-keys"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('title')).toContain(keyHint('key-list', P))
    expect(btn.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('key-list', P))
    await btn.trigger('click')
    expect(w.emitted('open-keys')).toHaveLength(1)
    w.unmount()
  })
})

describe('ent#621 — the rail shows its two keys', () => {
  const TABS = [{ id: 'info', label: 'Info' }, { id: 'files', label: 'Files' }]
  const mountRail = (props = {}) => mount(PortalRail, {
    attachTo: document.body,
    props: { tabs: TABS, activeTab: 'info', open: false, ...props },
  })

  it('the expand button names ⌘.', () => {
    const w = mountRail()
    const btn = w.find('[data-testid="portal-rail-expand"]')
    expect(btn.attributes('title')).toContain(keyHint('rail-toggle', P))
    expect(btn.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('rail-toggle', P))
    w.unmount()
  })

  it('the collapse button names the same key — one chord, both directions', () => {
    const w = mountRail({ open: true })
    const btn = w.find('[data-testid="portal-rail-collapse"]')
    expect(btn.attributes('title')).toContain(keyHint('rail-toggle', P))
    expect(btn.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('rail-toggle', P))
    w.unmount()
  })

  it('every tab names ⌥. — collapsed strip and open strip alike', () => {
    const collapsed = mountRail()
    const stripBtn = collapsed.find('[data-testid="portal-rail-tab-info"]')
    expect(stripBtn.attributes('title')).toContain(keyHint('rail-tab-next', P))
    expect(stripBtn.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('rail-tab-next', P))
    collapsed.unmount()

    const open = mountRail({ open: true })
    const tab = open.findAll('button').find((b) => (b.attributes('title') || '').startsWith('Info'))
    expect(tab).toBeTruthy()
    expect(tab.attributes('title')).toContain(keyHint('rail-tab-next', P))
    expect(tab.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('rail-tab-next', P))
    open.unmount()
  })
})

describe('ent#621 — the chat strip shows the chat keys', () => {
  it('each tab names ⌥⇧↑ / ⌥⇧↓ without losing what the tab already said', () => {
    const threads = [
      { id: 'm', session_id: 'm', agent_name: 'scout', is_main: true, title: 'Main', last_message_at: '2026-09-27T12:00:00Z' },
      { id: 't2', session_id: 't2', agent_name: 'scout', title: 'Pricing page', last_message_at: '2026-09-26T12:00:00Z' },
    ]
    const w = mount(PortalChatTabs, {
      attachTo: document.body,
      props: { threads, agentName: 'scout', activeId: 'm' },
    })
    const tab = w.findAll('button').find((b) => (b.attributes('title') || '').includes('Pricing page'))
    expect(tab).toBeTruthy()
    expect(tab.attributes('title')).toContain(keyHint(['chat-prev', 'chat-next'], P))
    expect(tab.attributes('aria-keyshortcuts')).toBe(keyShortcutsFor(['chat-prev', 'chat-next'], P))
    // The chat keys are NOT the agent keys: a tooltip that said `⌥↓` here
    // would teach the wrong chord for the control it sits on.
    expect(tab.attributes('aria-keyshortcuts')).toContain('Shift')
    w.unmount()
  })
})

describe('ent#621 (follow-up) — the search field shows and answers its key', () => {
  const mountSidebar = (props = {}) => mount(PortalSidebar, {
    attachTo: document.body,
    props: {
      roster: [{ name: 'scout' }, { name: 'sage' }],
      activeAgentName: 'scout',
      threads: [],
      clientEmail: 'me@example.com',
      ...props,
    },
    global: { plugins: [router] },
  })
  const field = (w) => w.find('input[type="search"]')
  const cap = (w) => w.find('[data-testid="portal-sidebar-search-key"]')

  it('carries the chord in both channels, read from the map', () => {
    const w = mountSidebar()
    expect(field(w).attributes('aria-keyshortcuts')).toBe(keyShortcutsFor('search-focus', P))
    expect(field(w).attributes('title')).toContain(keyHint('search-focus', P))
    expect(cap(w).exists()).toBe(true)
    expect(cap(w).text()).toBe(keyHint('search-focus', P))
    w.unmount()
  })

  it('the key cap steps aside for text and for focus', async () => {
    // It sits where the field's own clear control appears, and a hint that
    // covers what you are typing is worse than none.
    const typed = mountSidebar({ search: 'sc' })
    expect(cap(typed).exists()).toBe(false)
    typed.unmount()

    const w = mountSidebar()
    await field(w).trigger('focus')
    expect(cap(w).exists()).toBe(false)
    await field(w).trigger('blur')
    expect(cap(w).exists()).toBe(true)
    w.unmount()
  })

  it('exposes focusSearch(), which focuses the field and selects what is in it', async () => {
    const w = mountSidebar({ search: 'scout' })
    const el = field(w).element
    expect(w.vm.searchHasFocus()).toBe(false)
    w.vm.focusSearch()
    expect(document.activeElement).toBe(el)
    expect(w.vm.searchHasFocus()).toBe(true)
    expect(el.selectionStart).toBe(0)
    expect(el.selectionEnd).toBe('scout'.length)
    w.unmount()
  })
})
