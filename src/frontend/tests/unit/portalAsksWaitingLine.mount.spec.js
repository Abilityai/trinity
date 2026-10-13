// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 round 2 — the ruling's "one line to the Inbox"
 * as ONE component, mounted by Work (platform) and Info (every principal: a
 * client has no Work tab, and the ruling gives platform users and clients the
 * same answer). It counts from `openAsks` — the feed the sidebar marks and the
 * pinned Inbox row count — and links to Inbox → Action `?from=<agent>`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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
  return { default: Object.assign({ get: vi.fn(), post: vi.fn(), create: mk },
    { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } }, defaults: { headers: { common: {} } } }) }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsksWaitingLine from '@/components/portal/PortalAsksWaitingLine.vue'

const ask = (id, agent = 'scout', status = 'pending') => ({ id, agent_name: agent, status, kind: 'approval' })
let wrapper
let router
let store
beforeEach(async () => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.isPlatformSession = false                 // a client
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/workspace')
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountLine(props) {
  wrapper = mount(PortalAsksWaitingLine, { props, global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('the waiting line', () => {
  it("counts this agent's waiting asks and links to its Inbox filter — for a client too", async () => {
    store.asks = [ask('a'), ask('b'), ask('c', 'relay'), ask('d', 'scout', 'answered')]
    const w = await mountLine({ agentNames: ['scout'], testidPrefix: 'portal-info' })
    expect(w.find('[data-testid="portal-info-waiting-line"]').text()).toContain('2 asks waiting on you')
    expect(w.find('[data-testid="portal-info-open-inbox"]').attributes('href')).toBe('/workspace/inbox?tab=action&from=scout')
  })
  it('nothing waiting draws nothing', async () => {
    store.asks = [ask('d', 'scout', 'answered')]
    const w = await mountLine({ agentNames: ['scout'] })
    expect(w.find('[data-testid="portal-work-waiting-line"]').exists()).toBe(false)
  })
  it('two agents waiting link to the whole Action tab', async () => {
    store.asks = [ask('a'), ask('b', 'relay')]
    const w = await mountLine({ agentNames: ['scout', 'relay'] })
    expect(w.find('[data-testid="portal-work-open-inbox"]').attributes('href')).toBe('/workspace/inbox?tab=action')
  })
})
