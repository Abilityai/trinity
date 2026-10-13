// @vitest-environment jsdom
/**
 * #3672 — the Workspace sidebar holds its layout while it loads.
 *
 * On a cold load the roster, the chat list and the asks feed arrive at
 * different times, and each one used to re-lay the list as it landed: agent
 * rows rendered, then re-ordered and grew a subtitle when the chats arrived,
 * then an asked agent was appended past the collapse limit, while the chat
 * groups popped in below. The fix is ONE verdict for the whole list:
 *
 *   - `sidebarSettled` (pure) is true once bootstrap has placed the caller
 *     (roster + chats read, success or failure) AND the asks feed has a
 *     verdict (loaded, absent, or failed) — never a timer;
 *   - until then BOTH sections show skeleton rows and none of the content or
 *     empty copy renders, so nothing arrives piecemeal;
 *   - once settled, both sections render in one pass, inside the same fade
 *     class (`ws-fade-in`, which `prefers-reduced-motion` turns off in CSS);
 *   - an empty chat list keeps a stable empty state instead of collapsing;
 *   - the Projects link, whose capability rides the roster, has its slot held
 *     for a platform session while there is no verdict, so its arrival does
 *     not push the search box and both lists down (the measured residual).
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
import { sidebarSettled } from '@/components/portal/portalUtils'

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }] })
  await router.push('/workspace')
  await router.isReady()
})

const now = Date.now()
const ROSTER = [{ name: 'scout', display_label: 'Scout' }, { name: 'bard' }]
const THREADS = [
  { id: 't1', agent_name: 'scout', title: 'Quarterly numbers', last_message_at: new Date(now - 60_000).toISOString(), unread: 0 },
]

const sidebar = (props) => mount(PortalSidebar, {
  props: { clientEmail: 'me@example.com', ...props },
  global: { plugins: [router] },
})

describe('#3672 — sidebarSettled, the one verdict', () => {
  it('waits for bootstrap AND an asks verdict', () => {
    expect(sidebarSettled({})).toBe(false)
    expect(sidebarSettled({ resolved: true })).toBe(false)
    expect(sidebarSettled({ asksLoaded: true })).toBe(false)
    expect(sidebarSettled({ resolved: true, asksLoaded: true })).toBe(true)
  })
  it('a 404/403 or a failed asks read is still a verdict — the list never waits forever', () => {
    expect(sidebarSettled({ resolved: true, asksAbsent: true })).toBe(true)
    expect(sidebarSettled({ resolved: true, asksFailed: true })).toBe(true)
  })
  it('strict: truthy is not true', () => {
    expect(sidebarSettled({ resolved: 'yes', asksLoaded: true })).toBe(false)
    expect(sidebarSettled({ resolved: true, asksLoaded: 1 })).toBe(false)
  })
})

describe('#3672 — before the verdict, both sections hold skeleton rows', () => {
  it('shows agent and chat skeletons, and no row, no empty copy — even with data already in hand', async () => {
    const w = sidebar({ roster: ROSTER, threads: THREADS, settled: false })
    await flushPromises()
    expect(w.find('[data-testid="portal-skeleton-sidebar-agents"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-skeleton-sidebar-chats"]').exists()).toBe(true)
    expect(w.text()).not.toContain('Scout')
    expect(w.text()).not.toContain('Quarterly numbers')
    expect(w.text()).not.toContain('No agents')
    expect(w.text()).not.toContain('No chats yet')
    // One spoken line per section, not a pile of pulsing rectangles.
    expect(w.findAll('[aria-busy="true"]').length).toBe(2)
  })

  it('an empty roster before the verdict is a skeleton, never "No agents yet"', async () => {
    const w = sidebar({ roster: [], threads: [], settled: false, isPlatformSession: true })
    await flushPromises()
    expect(w.text()).not.toContain('No agents yet')
    expect(w.find('[data-testid="portal-skeleton-sidebar-agents"]').exists()).toBe(true)
  })
})

describe('#3672 — after the verdict, everything lands in one pass with one fade', () => {
  it('agents and chats render together, each inside the same fade class', async () => {
    const w = sidebar({ roster: ROSTER, threads: THREADS, settled: true })
    await flushPromises()
    expect(w.find('[data-testid="portal-skeleton-sidebar-agents"]').exists()).toBe(false)
    expect(w.find('[data-testid="portal-skeleton-sidebar-chats"]').exists()).toBe(false)
    const agents = w.find('[data-testid="sidebar-agents-content"]')
    const chats = w.find('[data-testid="sidebar-chats-content"]')
    expect(agents.classes()).toContain('ws-fade-in')
    expect(chats.classes()).toContain('ws-fade-in')
    expect(agents.text()).toContain('Scout')
    expect(chats.text()).toContain('Quarterly numbers')
  })

  it('the flip from loading to settled swaps skeletons for content without remounting the chrome', async () => {
    const w = sidebar({ roster: ROSTER, threads: THREADS, settled: false })
    await flushPromises()
    const search = w.find('input[type="search"]').element
    await w.setProps({ settled: true })
    expect(w.find('input[type="search"]').element).toBe(search)
    expect(w.find('[data-testid="sidebar-agents-content"]').text()).toContain('Scout')
  })

  it('an empty chat list keeps a stable empty state', async () => {
    const w = sidebar({ roster: ROSTER, threads: [], settled: true })
    await flushPromises()
    const empty = w.find('[data-testid="sidebar-chats-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('No chats yet')
  })

  it('defaults to settled, so a sidebar mounted without the verdict renders as before', async () => {
    const w = sidebar({ roster: ROSTER, threads: THREADS })
    await flushPromises()
    expect(w.text()).toContain('Scout')
  })
})

describe('#3672 — the Projects link does not push the lists down when the roster lands', () => {
  it('a platform session holds the link\'s slot while there is no verdict', async () => {
    const w = sidebar({ roster: [], threads: [], settled: false, isPlatformSession: true })
    await flushPromises()
    expect(w.find('[data-testid="portal-skeleton-sidebar-link"]').exists()).toBe(true)
    // Once the roster says Projects is available, the link takes the slot.
    await w.setProps({ projectsAvailable: true })
    expect(w.find('[data-testid="portal-skeleton-sidebar-link"]').exists()).toBe(false)
    expect(w.find('[data-testid="sidebar-projects"]').exists()).toBe(true)
  })

  it('no slot once settled without the capability, and none for an outside client', async () => {
    const settled = sidebar({ roster: ROSTER, threads: THREADS, settled: true, isPlatformSession: true })
    await flushPromises()
    expect(settled.find('[data-testid="portal-skeleton-sidebar-link"]').exists()).toBe(false)
    const client = sidebar({ roster: [], threads: [], settled: false, isPlatformSession: false })
    await flushPromises()
    expect(client.find('[data-testid="portal-skeleton-sidebar-link"]').exists()).toBe(false)
  })
})
