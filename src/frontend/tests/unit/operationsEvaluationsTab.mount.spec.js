// @vitest-environment jsdom
/**
 * trinity-enterprise#938 (C1) — the Evaluations tab on Operations, MOUNTED.
 *
 * The audience is the operator floor (roles operator, creator, admin — ruling
 * T1), and the GUARD is the API (`tests/unit/test_938_tab_guard.py`). This
 * spec pins the UI mirror of that guard: the tab is listed only at the floor;
 * `?tab=evaluations` below the floor bounces to the default tab and the panel
 * never mounts; a role that resolves after mount re-selects or bounces; the
 * strip keeps `?tab=` through `router.replace`. The panel shell itself —
 * loading / failed / empty / stale over the store — is mounted for real below.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick, reactive, defineComponent, h } from 'vue'

// Both the shared api client (axios.create) and any raw axios resolve here.
const http = vi.hoisted(() => ({ get: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

const nav = vi.hoisted(() => ({ route: null, replace: null }))
// EvalTrend's chart pulls in uPlot, which needs a canvas jsdom lacks.
vi.mock('../../src/components/TrendLineChart.vue', () => ({ default: { render: () => null } }))
vi.mock('vue-router', () => ({
  useRoute: () => nav.route,
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn() }),
}))
// <router-link> is a globally registered component in the app; here it is a
// stub that exposes its target so the empty state's link can be asserted.
const RouterLinkStub = defineComponent({
  props: ['to'],
  render() { return h('a', { 'data-to': JSON.stringify(this.to) }, this.$slots.default?.()) },
})

import Operations from '../../src/views/Operations.vue'
import EvaluationsPanel from '../../src/components/EvaluationsPanel.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'

const AGG = '/api/evaluations/aggregate'
const aggregate = (evaluated, extra = {}) => ({
  by: 'agent', since: 's', until: 'u', truncated: false,
  total: { evaluated, completed: evaluated, graded: evaluated, passed: evaluated, per_check_fail: {}, judged: 0 },
  groups: [], ...extra,
})
// Anything the page's other stores ask for on mount: shaped-empty, never a throw.
const quiet = async (url) => ({ data: url.startsWith(AGG) ? aggregate(0) : { items: [], count: 0, agents: [] } })

// The strip as data: one button per tab, the active id exposed.
const TabsStub = defineComponent({
  props: ['tabs', 'modelValue'],
  emits: ['update:modelValue'],
  render() {
    return h('div', { 'data-testid': 'tabs', 'data-active': this.modelValue },
      this.tabs.map((t) => h('button', { 'data-tab': t.id, onClick: () => this.$emit('update:modelValue', t.id) }, t.label)))
  },
})

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

function mountOps({ role, tab, get = quiet }) {
  http.get = vi.fn(get)
  nav.route = reactive({ query: tab ? { tab } : {}, params: {} })
  nav.replace = vi.fn()
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = { username: 'p', role }
  // Polling is the container's concern, not this spec's.
  vi.spyOn(useOperatorQueueStore(), 'startPolling').mockImplementation(() => {})
  const w = mount(Operations, {
    global: {
      plugins: [pinia],
      stubs: {
        NavBar: true, QueueCard: true, ResolvedCard: true, NotificationsPanel: true,
        MonitoringPanel: true, ExecutionsPanel: true, ReportsPanelFleet: true, ConfirmDialog: true,
        OverflowTabs: TabsStub,
        EvaluationsPanel: { template: '<div data-testid="evaluations-panel-stub" />' },
      },
    },
  })
  return { w, auth }
}

const tabIds = (w) => w.findAll('[data-tab]').map((b) => b.attributes('data-tab'))
const active = (w) => w.find('[data-testid="tabs"]').attributes('data-active')
const panelMounted = (w) => w.find('[data-testid="evaluations-panel-stub"]').exists()

describe('Operations › Evaluations tab — who sees it (mirror of the API guard)', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('an admin sees Evaluations beside Health and the deep link opens it', async () => {
    const { w } = mountOps({ role: 'admin', tab: 'evaluations' })
    await flush()
    expect(tabIds(w)).toEqual(['needs-response', 'notifications', 'health', 'executions', 'evaluations', 'reports', 'resolved'])
    expect(active(w)).toBe('evaluations')
    expect(panelMounted(w)).toBe(true)
  })

  it('an operator-role person sees Evaluations but not Health — the floor, not admin-only', async () => {
    const { w } = mountOps({ role: 'operator', tab: 'evaluations' })
    await flush()
    expect(tabIds(w)).toContain('evaluations')
    expect(tabIds(w)).not.toContain('health')
    expect(active(w)).toBe('evaluations')
    expect(panelMounted(w)).toBe(true)
  })

  it('a Workspace-only person is bounced off ?tab=evaluations and the panel never mounts', async () => {
    const { w } = mountOps({ role: 'user', tab: 'evaluations' })
    await flush()
    expect(tabIds(w)).not.toContain('evaluations')
    expect(active(w)).toBe('needs-response')
    expect(panelMounted(w)).toBe(false)
    expect(http.get.mock.calls.some(([u]) => u.startsWith(AGG))).toBe(false)
  })

  it('a role that resolves AFTER mount re-selects the deep-linked tab, and a demotion bounces it', async () => {
    // Before the profile lands the role reads 'user' (auth store default).
    const { w, auth } = mountOps({ role: 'user', tab: 'evaluations' })
    await flush()
    expect(active(w)).toBe('needs-response')
    auth.user = { username: 'p', role: 'operator' }
    await flush()
    expect(active(w)).toBe('evaluations')
    expect(panelMounted(w)).toBe(true)
    auth.user = { username: 'p', role: 'user' }
    await flush()
    expect(active(w)).toBe('needs-response')
    expect(panelMounted(w)).toBe(false)
    expect(nav.replace).toHaveBeenCalledWith({ query: { tab: 'needs-response' } })
  })

  it('picking the tab keeps ?tab= in step through router.replace', async () => {
    const { w } = mountOps({ role: 'creator' })
    await flush()
    await w.find('[data-tab="evaluations"]').trigger('click')
    await flush()
    expect(active(w)).toBe('evaluations')
    expect(nav.replace).toHaveBeenCalledWith({ query: { tab: 'evaluations' } })
    expect(panelMounted(w)).toBe(true)
  })
})

describe('EvaluationsPanel — the shell over the store, mounted', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    nav.route = reactive({ query: {}, params: {} })
    nav.replace = vi.fn()
  })

  async function panel(get) {
    http.get = vi.fn(get)
    return mount(EvaluationsPanel, { global: { stubs: { RouterLink: RouterLinkStub } } })
  }
  // The primitives forward attrs to the native element, so the test id may sit
  // on the <select>/<input> itself or on a wrapper around it.
  const field = (w, id) => {
    const el = w.find(`[data-testid="${id}"]`)
    return ['INPUT', 'SELECT'].includes(el.element.tagName) ? el : el.find('input, select')
  }

  it('reads the ent#937 aggregate and shows a skeleton before it lands — never the empty copy', async () => {
    let resolve
    const w = await panel(() => new Promise((r) => { resolve = r }))
    await nextTick()
    expect(w.find('[data-testid="evaluations-loading"]').exists()).toBe(true)
    expect(w.find('[data-testid="evaluations-empty"]').exists()).toBe(false)
    expect(http.get.mock.calls.map(([u]) => u)).toEqual([AGG])
    expect(http.get.mock.calls[0][1].params).toEqual({ by: 'agent', period: 'week' })
    resolve({ data: aggregate(2) })
    await flush()
    expect(w.find('[data-testid="evaluations-loading"]').exists()).toBe(false)
    expect(w.find('[data-testid="evaluations-summary"]').text()).toMatch(/^2 runs evaluated/)
  })

  it('zero evaluated runs: the empty state names the Tier-0 scope and links to Executions', async () => {
    const w = await panel(async () => ({ data: aggregate(0) }))
    await flush()
    const empty = w.find('[data-testid="evaluations-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('Only scheduled and agent-triggered runs are graded')
    const link = w.find('[data-testid="evaluations-empty-executions-link"]')
    expect(JSON.parse(link.attributes('data-to'))).toEqual({ path: '/operations', query: { tab: 'executions' } })
  })

  it('a failed first load is LoadFailed with retry, not the empty copy', async () => {
    const e = new Error('Request failed with status code 500'); e.response = { status: 500, data: { detail: 'down' } }
    const w = await panel(async () => { throw e })
    await flush()
    expect(w.find('[data-testid="evaluations-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="evaluations-empty"]').exists()).toBe(false)
    expect(w.text()).toContain("Couldn't load evaluations")
  })

  it('a 422 invalid_period on a custom range keeps the data and shows the API\'s message', async () => {
    let n = 0
    const w = await panel(async () => {
      if (n++ === 0) return { data: aggregate(7) }
      const e = new Error('422'); e.response = { status: 422, data: { detail: { code: 'invalid_period', message: 'since must be before until' } } }
      throw e
    })
    await flush()
    expect(w.find('[data-testid="evaluations-summary"]').text()).toMatch(/^7 runs/)
    await field(w, 'evaluations-period').setValue('custom')
    await flush()
    expect(http.get).toHaveBeenCalledTimes(1)              // half-typed range asks nothing
    await field(w, 'evaluations-since').setValue('2026-10-10')
    await field(w, 'evaluations-until').setValue('2026-10-01')
    await flush()
    expect(http.get).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('since must be before until')
    expect(w.find('[data-testid="evaluations-summary"]').text()).toMatch(/^7 runs/)   // data kept
    expect(w.find('[data-testid="evaluations-failed"]').exists()).toBe(false)
  })

  it('the truncated flag is said out loud', async () => {
    const w = await panel(async () => ({ data: aggregate(5000, { truncated: true }) }))
    await flush()
    expect(w.find('[data-testid="evaluations-truncated"]').text()).toContain('capped at 5,000 runs')
  })
})
