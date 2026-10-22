// @vitest-environment jsdom
/**
 * trinity-enterprise#941 — Operations › Outcomes, MOUNTED.
 *
 * The tab is listed only for an admin on an install where `eval_outcomes` is
 * entitled (the API is the guard: test_941_eval_outcomes_wiring.py). The panel
 * renders loading / failed / empty / ready over the store; the proposals list
 * filters by status (server) and owner (client); Accept posts a decision; a
 * reject needs a reason before it can be sent; a failed verb lands in an
 * InlineError next to the list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick, reactive, defineComponent, h } from 'vue'

const http = vi.hoisted(() => ({ get: null, post: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    post: vi.fn((...a) => http.post(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
const nav = vi.hoisted(() => ({ route: null, replace: null }))
vi.mock('../../src/components/TrendLineChart.vue', () => ({ default: { render: () => null } }))
vi.mock('vue-router', () => ({
  useRoute: () => nav.route,
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn() }),
}))

import Operations from '../../src/views/Operations.vue'
import OutcomesPanel from '../../src/components/OutcomesPanel.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useEnterpriseStore } from '../../src/stores/enterprise'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'
import { useOutcomesStore, PROJECTS_URL, PROPOSALS_URL } from '../../src/stores/outcomes'

const proposal = (id, extra = {}) => ({
  id, status: 'proposed', title: `Proposal ${id}`, summary: 'Do the thing.', owner: 'trinity-pm',
  target: { kind: 'process', ref: 'check:credential_echo' },
  expected_effect: { metric: 'checks_failed_rate', direction: 'down' },
  evidence: [{ kind: 'execution', ref: 'x1' }], before: { value: 0.4 }, after: null,
  scope_kind: 'fleet', scope_ref: 'fleet', ...extra,
})
const OVERVIEW = {
  fleet: {
    window_start: '2026-10-12T00:00:00.000Z', inputs: { unreachable: [{ repo: 'acme/gone', reason: 'http_404' }] },
    signals: { completion: { rate: 0.5, done: 1, planned: 2 }, done_claims: { rejection_rate: 0.25, rejected: 1, claims_resolved: 4, reopened: 0 },
      asks: { unanswered: 2, no_human_surface: 1 }, latency: { dispatch_to_verified_hours_median: 3 }, cost: { per_verified_task: 1.5 } },
  },
  projects: [{ id: 'a1', scope_ref: 'p1', project_name: 'Pipeline', window_start: '2026-10-12T00:00:00.000Z',
    signals: { completion: { done: 1, planned: 3 }, stalls: { stalled_count: 1 } } }],
  proposal_counts: { proposed: 2, accepted: 0, rejected: 0, shipped: 0 },
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

function mountPanel({ get, post } = {}) {
  http.get = vi.fn(get || (async (url) => {
    if (url === PROJECTS_URL) return { data: OVERVIEW }
    if (url === PROPOSALS_URL) return { data: { proposals: [proposal('a'), proposal('b', { owner: 'operator' })] } }
    return { data: {} }
  }))
  http.post = vi.fn(post || (async () => ({ data: {} })))
  setActivePinia(createPinia())
  return mount(OutcomesPanel, { global: { stubs: { SkeletonLoader: true } } })
}

describe('OutcomesPanel', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders the fleet tiles, the project row, the unreachable registry and the proposals', async () => {
    const w = mountPanel()
    await flush()
    expect(w.find('[data-testid="outcomes-ready"]').exists()).toBe(true)
    expect(w.find('[data-testid="outcomes-kpi-completion"]').text()).toContain('50%')
    expect(w.find('[data-testid="outcomes-project-p1"]').text()).toContain('Pipeline')
    expect(w.find('[data-testid="outcomes-unreachable"]').text()).toContain('acme/gone (http_404)')
    expect(w.findAll('[data-testid^="outcomes-proposal-"]')).toHaveLength(2)
    expect(http.get).toHaveBeenCalledWith(PROPOSALS_URL, { params: { status: 'proposed' } })
  })

  it('filters by owner on the client and by status on the server', async () => {
    const w = mountPanel()
    await flush()
    await w.find('select[data-testid="outcomes-owner"]').setValue('operator')
    await flush()
    expect(w.findAll('[data-testid^="outcomes-proposal-"]').map((n) => n.attributes('data-testid')))
      .toEqual(['outcomes-proposal-b'])
    await w.find('select[data-testid="outcomes-status"]').setValue('shipped')
    await flush()
    expect(http.get).toHaveBeenLastCalledWith(PROPOSALS_URL, { params: { status: 'shipped' } })
  })

  it('Accept posts a decision; Reject needs a reason first', async () => {
    const w = mountPanel()
    await flush()
    await w.find('[data-testid="outcomes-accept-a"]').trigger('click')
    await flush()
    expect(http.post).toHaveBeenCalledWith(`${PROPOSALS_URL}/a/decision`, { decision: 'accepted' })

    await w.find('[data-testid="outcomes-reject-b"]').trigger('click')
    const confirm = w.find('[data-testid="outcomes-confirm-reject-b"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    await w.find('textarea[data-testid="outcomes-reason-b"]').setValue('not now')
    await w.find('[data-testid="outcomes-confirm-reject-b"]').trigger('click')
    await flush()
    expect(http.post).toHaveBeenCalledWith(`${PROPOSALS_URL}/b/decision`, { decision: 'rejected', reason: 'not now' })
  })

  it('a failed verb shows the API message next to the list and keeps the data', async () => {
    const w = mountPanel({ post: async () => { throw { response: { data: { detail: { code: 'already_decided', message: 'This proposal is already accepted.' } } } } } })
    await flush()
    await w.find('[data-testid="outcomes-accept-a"]').trigger('click')
    await flush()
    expect(w.text()).toContain('This proposal is already accepted.')
    expect(w.findAll('[data-testid^="outcomes-proposal-"]')).toHaveLength(2)
  })

  it('a failed first load is LoadFailed, never the empty copy', async () => {
    const w = mountPanel({ get: async () => { throw new Error('boom') } })
    await flush()
    expect(w.find('[data-testid="outcomes-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="outcomes-empty"]').exists()).toBe(false)
  })

  it('an install with no record and no proposals is the empty state', async () => {
    const w = mountPanel({ get: async (url) => ({ data: url === PROJECTS_URL ? { projects: [], fleet: null, proposal_counts: {} } : { proposals: [] } }) })
    await flush()
    expect(w.find('[data-testid="outcomes-empty"]').exists()).toBe(true)
  })
})

const TabsStub = defineComponent({
  props: ['tabs', 'modelValue'],
  render() { return h('div', { 'data-testid': 'tabs' }, this.tabs.map((t) => h('button', { 'data-tab': t.id }, t.label))) },
})

function mountOps({ role, features, tab }) {
  http.get = vi.fn(async (url) => ({ data: url === PROJECTS_URL ? OVERVIEW : { items: [], count: 0, agents: [], proposals: [], enterprise_features: features } }))
  nav.route = reactive({ query: tab ? { tab } : {}, params: {} })
  nav.replace = vi.fn()
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = { username: 'p', role }
  vi.spyOn(useOperatorQueueStore(), 'startPolling').mockImplementation(() => {})
  useEnterpriseStore()._setFeaturesForTest(features)
  return mount(Operations, {
    global: { stubs: { NavBar: true, OverflowTabs: TabsStub, OutcomesPanel: { render: () => h('div', { 'data-testid': 'outcomes-stub' }) },
      EvaluationsPanel: true, ExecutionsPanel: true, ReportsPanelFleet: true, MonitoringPanel: true,
      NotificationsPanel: true, 'router-link': true } },
  })
}

describe('Operations › Outcomes tab gate', () => {
  it.each([
    ['admin', ['eval_outcomes'], true],
    ['admin', [], false],
    ['operator', ['eval_outcomes'], false],
  ])('role %s, features %j → listed %s', async (role, features, listed) => {
    const w = mountOps({ role, features, tab: 'outcomes' })
    await flush()
    expect(w.find('[data-tab="outcomes"]').exists()).toBe(listed)
    expect(w.find('[data-testid="outcomes-stub"]').exists()).toBe(listed)
  })
})

describe('outcomes store', () => {
  it('issues only eval_outcomes URLs', async () => {
    http.get = vi.fn(async (url) => ({ data: url === PROJECTS_URL ? OVERVIEW : { proposals: [] } }))
    http.post = vi.fn(async () => ({ data: {} }))
    setActivePinia(createPinia())
    const s = useOutcomesStore()
    await s.load()
    await s.markShipped('z')
    const urls = [...http.get.mock.calls, ...http.post.mock.calls].map((c) => c[0])
    expect(urls.every((u) => u.startsWith('/api/enterprise/eval-outcomes/'))).toBe(true)
  })
})
