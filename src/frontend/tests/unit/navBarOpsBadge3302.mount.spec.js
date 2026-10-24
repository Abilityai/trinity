// @vitest-environment jsdom
/**
 * #3302 — the NavBar's Ops badge counts the operator queue on every page.
 *
 * The badge read the queue store, and the store only read the queue on the
 * Operations page or after a queue event — so on any other page the badge's
 * queue part was 0 until something happened. NavBar now reads the two counts
 * when it mounts: `total` of two one-row reads (pending, pending + critical),
 * never a window of rows. MOUNTED: the read is a mount hook and the number is
 * template output; only a render proves they meet.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref, h, defineComponent, onMounted } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: () => ({ currentRoute: ref({ path: '/', query: {} }), push: vi.fn() }),
}))
vi.mock('@/utils/websocket', () => ({ useWebSocket: () => ({ isConnected: ref(true) }) }))
vi.mock('@/composables/useBuildInfo', () => ({
  useBuildInfo: () => ({ load: () => Promise.resolve(), info: ref(null), displayVersion: ref(''), isMissing: ref(false) }),
}))
vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import axios from 'axios'
import NavBar from '@/components/NavBar.vue'
import { useAuthStore } from '@/stores/auth'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'

let wrapper

async function mountAs(role, queue, component = NavBar) {
  axios.get.mockImplementation((url, config = {}) => {
    if (url === '/api/operator-queue') return queue.get(config.params)
    if (url.startsWith('/api/notifications/count')) return Promise.resolve({ data: { count: 0 } })
    return Promise.resolve({ data: {} })
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = { username: 'someone', role }
  wrapper = mount(component, {
    global: {
      plugins: [pinia],
      stubs: { RouterLink: { props: ['to'], template: '<a><slot /></a>' }, BaseModal: true, ThemeChoice: true },
    },
  })
  await flushPromises()
  return wrapper
}

describe('NavBar — the Ops badge counts the queue on load (#3302)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    globalThis.ResizeObserver = class { observe() {} disconnect() {} }
  })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('reads the pending and critical totals on mount, and the badge shows the count', async () => {
    const queue = makeQueue([
      ...rowsOf(41, { agents: 9 }),
      ...rowsOf(1, { prefix: 'crit', priority: 'critical' }),
      ...rowsOf(30, { prefix: 'done', status: 'responded' }),
    ])
    const w = await mountAs('admin', queue)
    expect(queue.calls).toEqual(expect.arrayContaining([
      { status: 'pending', limit: 1 },
      { status: 'pending', priority: 'critical', limit: 1 },
    ]))
    expect(queue.calls.every((c) => c.limit === 1)).toBe(true)
    expect(w.text()).toContain('42')
  })

  it('on a page that reads the list (Operations), the list read covers the counts — no extra reads ahead of it', async () => {
    // NavBar is a child of the page, so its mount runs BEFORE the page starts
    // polling. Reading the counts there put two requests ahead of the page's
    // list read — and the e2e specs that serve the FIRST queue request a payload.
    const ListPage = defineComponent({
      setup() {
        const store = useOperatorQueueStore()
        onMounted(() => store.startPolling(60_000))
        return () => h(NavBar)
      },
    })
    const queue = makeQueue(rowsOf(3))
    await mountAs('admin', queue, ListPage)
    expect(queue.calls[0]).toEqual({ limit: 200 })
    expect(queue.calls.filter((c) => c.limit === 1)).toEqual([])
    useOperatorQueueStore().stopPolling()
  })

  it('a Workspace-only person has no operator queue to count, so nothing is read', async () => {
    const queue = makeQueue(rowsOf(3))
    await mountAs('user', queue)
    expect(queue.calls).toEqual([])
  })
})
