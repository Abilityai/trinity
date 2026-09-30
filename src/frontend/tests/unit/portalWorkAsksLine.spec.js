// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 — the 2026-09-30 ruling, item 2 (as item 3 of
 * the rework): the agent's asks home is the Inbox filtered to that agent, on
 * every door. Work stops being "the asks' home" (sign-off round 5, reversed):
 *
 *   - Work shows Now / Earlier only — no "Waiting on you" cards;
 *   - plus ONE line while something waits: "N waiting on you · Open in Inbox",
 *     to Inbox `?tab=action&from=<agent>` (§3g C2's filter, kept);
 *   - it counts every waiting ask of the chat's agents, the one open in the
 *     Inbox pane included (round 5's "Work skips the pane's ask" is undone —
 *     Work draws no ask to skip);
 *   - the line sits on the Now heading's row, which is always there, so it
 *     arriving or leaving moves nothing (principle 30);
 *   - the other doors that sent "the asks" to Work (the briefing's asks
 *     suggestion, Info's section link) go to the same Inbox route.
 *
 * @source-text-pin: the two other doors are handlers inside the full shell
 * (Portal.vue's briefing slot) and the rail's Info panel, reachable only through
 * a whole-shell mount; the rule itself (asksHomeRoute) and Work are mounted above.
 * The PortalAsks read is a removed-prop guard: the exclude list must not return.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

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

import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'
import { usePortalWorkStore } from '@/stores/portalWork'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalWork from '@/components/portal/PortalWork.vue'
import { asksHomeRoute, asksWaitingLabel, WORKSPACE_INBOX } from '@/components/portal/portalUtils'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8')

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Yes', 'No'], created_at: '2026-09-30T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: 'main-1', raised_in_turn: false, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

let store
let wrapper
let router
beforeEach(async () => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.isPlatformSession = true
  const work = usePortalWorkStore()
  work.hasLoaded = true
  portalHttp.get.mockResolvedValue({ data: { now: [], earlier: [], earlier_total: 0 } })
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  await router.push('/workspace')
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountWork(props = {}) {
  wrapper = mount(PortalWork, {
    props: { participants: ['scout'], ...props },
    global: { plugins: [router], stubs: { PortalWorkCard: true, PortalAvatar: true, PortalSkeleton: true, LoadFailed: true } },
  })
  await flushPromises()
  return wrapper
}
const line = () => wrapper.find('[data-testid="portal-work-waiting-line"]')
const link = () => wrapper.find('[data-testid="portal-work-open-inbox"]')

describe('the route and the words', () => {
  it("the asks home is the Inbox's Action tab, narrowed to the agent when there is one", () => {
    expect(asksHomeRoute('scout')).toEqual({ path: WORKSPACE_INBOX, query: { tab: 'action', from: 'scout' } })
    expect(asksHomeRoute(null)).toEqual({ path: WORKSPACE_INBOX, query: { tab: 'action' } })
  })
  it('the line counts, in words', () => {
    expect(asksWaitingLabel(1)).toBe('1 waiting on you')
    expect(asksWaitingLabel(4)).toBe('4 waiting on you')
    expect(asksWaitingLabel(0)).toBe('')
  })
})

describe('Work (mounted)', () => {
  it('draws no ask — only the one line, with the Inbox link narrowed to the agent', async () => {
    store.asks = [ask('a1'), ask('a2', { raised_in_turn: true, chat_id: 's1' }), ask('gone', { status: 'answered' })]
    await mountWork()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.find('[data-testid="portal-work-waiting"]').exists()).toBe(false)
    expect(line().text()).toContain('2 waiting on you')
    expect(link().text()).toBe('Open in Inbox')
    expect(link().attributes('href')).toBe('/workspace/inbox?tab=action&from=scout')
  })

  it("counts the ask open in the Inbox pane too — Work no longer skips it", async () => {
    store.asks = [ask('a1')]
    await mountWork({ excludeAskIds: ['a1'] })
    expect(line().text()).toContain('1 waiting on you')
  })

  it('a room with waiting asks from two agents links to the whole Action tab', async () => {
    store.asks = [ask('a1'), ask('b1', { agent_name: 'bard' }), ask('x1', { agent_name: 'outsider' })]
    await mountWork({ participants: ['scout', 'bard'] })
    expect(line().text()).toContain('2 waiting on you')
    expect(link().attributes('href')).toBe('/workspace/inbox?tab=action')
  })

  it('nothing waiting, no line; the Now heading row is there either way', async () => {
    store.asks = [ask('gone', { status: 'expired' })]
    usePortalWorkStore().now = [{ id: 'e1', agent_name: 'scout', status: 'running' }]
    await mountWork()
    expect(line().exists()).toBe(false)
    const heads = wrapper.findAll('[data-testid="portal-work-now-head"]')
    expect(heads).toHaveLength(1)
  })

  it('the line sits on the Now heading row, never above the sections', async () => {
    store.asks = [ask('a1')]
    usePortalWorkStore().now = [{ id: 'e1', agent_name: 'scout', status: 'running' }]
    await mountWork()
    const head = wrapper.find('[data-testid="portal-work-now-head"]')
    expect(head.find('[data-testid="portal-work-waiting-line"]').exists()).toBe(true)
  })

  it('an empty Work still says what waits, under its empty copy', async () => {
    store.asks = [ask('a1')]
    await mountWork()
    const empty = wrapper.find('[data-testid="portal-work-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.find('[data-testid="portal-work-waiting-line"]').exists()).toBe(true)
  })
})

describe('the other doors go to the Inbox too (source-asserted)', () => {
  it("the shell's asks suggestion routes to the asks home, not the Work tab", () => {
    const shell = read('views/Portal.vue')
    expect(shell).not.toMatch(/name === 'asks' \? 'work'/)
    expect(shell).toMatch(/if \(name === 'asks'\) \{ router\.push\(asksHomeRoute\(/)
    expect(shell).not.toMatch(/:exclude-ask-ids=/)
  })
  it("Info's asks section link routes to the asks home", () => {
    const info = read('components/portal/PortalAgentDetails.vue')
    expect(info).toMatch(/name === 'asks'\) router\.push\(asksHomeRoute\(props\.agentName\)\)/)
  })
  it('PortalAsks has no exclude list left: nothing renders an ask to skip', () => {
    expect(read('components/portal/PortalAsks.vue')).not.toMatch(/excludeIds/)
  })
})
