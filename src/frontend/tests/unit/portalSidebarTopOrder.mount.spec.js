// @vitest-environment jsdom
/**
 * #3695 — the Workspace sidebar's top controls read, top to bottom:
 * Inbox, Projects, New chat, Search. The two navigation destinations sit
 * together, then the two actions on the chat list. With New chat between
 * them Projects read as a chat-list control rather than a place to go.
 *
 * Mounted, so the DOM order (which is the tab order) is what is pinned — a
 * regex over the template would pass with the blocks swapped if their text
 * happened to match. Without the capability the order is Inbox, New chat,
 * Search with nothing between them (#3672: no held slot).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

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

import PortalSidebar from '@/components/portal/PortalSidebar.vue'

let router
beforeEach(async () => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
  await router.push('/workspace')
  await router.isReady()
})

const ROSTER = [{ name: 'scout' }, { name: 'bard' }]

const sidebar = (props) => mount(PortalSidebar, {
  props: { clientEmail: 'me@example.com', roster: ROSTER, threads: [], settled: true, isPlatformSession: true, ...props },
  global: { plugins: [router] },
  attachTo: document.body,
})

/** The top block's controls, in DOM order, named. */
function topOrder(w) {
  const block = w.find('[data-testid="sidebar-inbox"]').element.parentElement
  return [...block.children].map((el) => {
    if (el.getAttribute('data-testid') === 'sidebar-inbox') return 'inbox'
    if (el.getAttribute('data-testid') === 'sidebar-projects') return 'projects'
    if (el.tagName === 'BUTTON' && el.textContent.includes('New chat')) return 'new-chat'
    if (el.querySelector('input[type="search"]')) return 'search'
    return `?${el.tagName}`
  })
}

describe('#3695 — Inbox, Projects, New chat, Search', () => {
  it('with Projects available the order is Inbox, Projects, New chat, Search', async () => {
    const w = sidebar({ projectsAvailable: true })
    await flushPromises()
    expect(topOrder(w)).toEqual(['inbox', 'projects', 'new-chat', 'search'])
  })

  it('without Projects the order is Inbox, New chat, Search — no slot, no gap', async () => {
    const w = sidebar({ projectsAvailable: false })
    await flushPromises()
    expect(topOrder(w)).toEqual(['inbox', 'new-chat', 'search'])
  })

  it('tab order follows: the first focusable after the Inbox row is Projects', async () => {
    const w = sidebar({ projectsAvailable: true })
    await flushPromises()
    const inbox = w.find('[data-testid="sidebar-inbox"]').element
    let next = inbox.nextElementSibling
    const focusable = next.matches('button, a[href], input') ? next : next.querySelector('button, a[href], input')
    expect(focusable.getAttribute('data-testid')).toBe('sidebar-projects')
  })

  it('the Projects active state and its click still work from the new position', async () => {
    const w = sidebar({ projectsAvailable: true, projectsActive: true })
    await flushPromises()
    const link = w.find('[data-testid="sidebar-projects"]')
    expect(link.attributes('aria-current')).toBe('page')
    await link.trigger('click')
    expect(w.emitted('open-projects')).toHaveLength(1)
  })

  it('New chat and the search field keep their contracts', async () => {
    const w = sidebar({ projectsAvailable: true })
    await flushPromises()
    const newChat = w.findAll('button').find((b) => b.text().includes('New chat'))
    await newChat.trigger('click')
    expect(w.emitted('new-chat')).toHaveLength(1)
    expect(w.find('input[type="search"]').attributes('aria-keyshortcuts')).toBeTruthy()
    w.vm.focusSearch()
    expect(w.vm.searchHasFocus() || document.activeElement === w.find('input[type="search"]').element).toBe(true)
  })
})
