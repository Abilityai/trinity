// @vitest-environment jsdom
/**
 * trinity-enterprise#74 — Operations › Anomalies, MOUNTED (ruled 2026-10-29).
 *
 * An admin could see one agent's open incident at a time, on that agent's
 * Schedules tab. The fleet list puts every agent's incidents in one place: the
 * tab is listed only for an admin where `anomaly_detection` is entitled (the
 * API is the guard), it badges the open count, the panel names agent, schedule
 * and what moved, links to the agent's Schedules tab, closes an open incident
 * and re-reads, switches to resolved, and keeps the empty / failed states.
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
import AnomalyIncidentsPanel from '../../src/components/anomaly/AnomalyIncidentsPanel.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useEnterpriseStore } from '../../src/stores/enterprise'
import { useOperatorQueueStore } from '../../src/stores/operatorQueue'
import { INCIDENTS_URL, resolveUrl } from '../../src/stores/anomalyDetection'

const RouterLinkStub = defineComponent({
  props: ['to'],
  render() { return h('a', { 'data-to': JSON.stringify(this.to) }, this.$slots.default?.()) },
})

const incident = (id, extra = {}) => ({
  id, agent_name: 'acme-scout', schedule_id: 's1', schedule_name: 'nightly digest', status: 'open',
  occurrences: 2, created_at: '2026-10-28T10:00:00.000Z', updated_at: '2026-10-28T10:00:00.000Z',
  metrics: [{ metric: 'duration_ms', value: 600000, median: 60000, ratio: 10 }], ...extra,
})

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

function mountPanel(get) {
  http.get = vi.fn(get)
  http.post = vi.fn(async () => ({ data: {} }))
  setActivePinia(createPinia())
  return mount(AnomalyIncidentsPanel, { global: { stubs: { SkeletonLoader: true, 'router-link': RouterLinkStub } } })
}

describe('AnomalyIncidentsPanel', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('lists every agent\'s open incidents with schedule, what moved and a link', async () => {
    const w = mountPanel(async () => ({ data: { incidents: [incident('i1'), incident('i2', {
      agent_name: 'acme-sage', schedule_name: null })] } }))
    await flush()
    expect(http.get.mock.calls[0]).toEqual([INCIDENTS_URL, { params: { status: 'open', limit: 200 } }])
    const row = w.find('[data-testid="anomaly-incident-row-i1"]')
    expect(row.text()).toContain('acme-scout')
    expect(row.text()).toContain('nightly digest')
    expect(row.find('[data-testid="anomaly-incident-moved"]').text()).toBe('Duration 10.0×')
    expect(JSON.parse(row.find('[data-testid="anomaly-incident-open"]').attributes('data-to')))
      .toEqual({ path: '/agents/acme-scout', query: { tab: 'schedules' } })
    expect(w.find('[data-testid="anomaly-incident-row-i2"]').text()).toContain('schedule removed')
    expect(w.find('[data-testid="anomaly-incidents-total"]').text()).toBe('2 open')
  })

  it('closes an open incident and reads the list again', async () => {
    let open = [incident('i1')]
    const w = mountPanel(async () => ({ data: { incidents: open } }))
    await flush()
    http.post = vi.fn(async () => { open = []; return { data: {} } })
    await w.find('[data-testid="anomaly-incident-close"]').trigger('click')
    await flush()
    expect(http.post).toHaveBeenCalledWith(resolveUrl('i1'))
    expect(w.find('[data-testid="anomaly-incidents-empty"]').text()).toContain('No open incidents')
  })

  it('switches to resolved and says who closed each', async () => {
    const w = mountPanel(async (url, cfg) => ({ data: { incidents: cfg.params.status === 'resolved'
      ? [incident('r1', { status: 'resolved', resolved_by: 'platform:normal', resolved_at: '2026-10-28T12:00:00.000Z' }),
        incident('r2', { status: 'resolved', resolved_by: 'ops@example.com', resolved_at: '2026-10-28T12:00:00.000Z' })]
      : [] } }))
    await flush()
    await w.find('[data-testid="anomaly-incidents-status"]').setValue('resolved')
    await flush()
    expect(http.get.mock.calls.at(-1)[1]).toEqual({ params: { status: 'resolved', limit: 200 } })
    expect(w.find('[data-testid="anomaly-incident-row-r1"]').text()).toContain('closed after normal runs')
    expect(w.find('[data-testid="anomaly-incident-row-r2"]').text()).toContain('by ops@example.com')
    expect(w.find('[data-testid="anomaly-incident-close"]').exists()).toBe(false)
  })

  it('a failed first read is the failed state, not the empty one', async () => {
    const w = mountPanel(async () => { throw new Error('down') })
    await flush()
    expect(w.find('[data-testid="anomaly-incidents-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="anomaly-incidents-empty"]').exists()).toBe(false)
  })
})

const TabsStub = defineComponent({
  props: ['tabs', 'modelValue'],
  render() {
    return h('div', { 'data-testid': 'tabs' },
      this.tabs.map((t) => h('button', { 'data-tab': t.id, 'data-badge': t.badge ?? '' }, t.label)))
  },
})

function mountOps({ role, features, tab }) {
  http.get = vi.fn(async (url) => ({ data: url === INCIDENTS_URL
    ? { incidents: [incident('i1'), incident('i2')] }
    : { items: [], count: 0, agents: [], enterprise_features: features } }))
  nav.route = reactive({ query: tab ? { tab } : {}, params: {} })
  nav.replace = vi.fn()
  setActivePinia(createPinia())
  useAuthStore().user = { username: 'p', role }
  vi.spyOn(useOperatorQueueStore(), 'startPolling').mockImplementation(() => {})
  useEnterpriseStore()._setFeaturesForTest(features)
  return mount(Operations, {
    global: { stubs: { NavBar: true, OverflowTabs: TabsStub,
      AnomalyIncidentsPanel: { render: () => h('div', { 'data-testid': 'anomalies-stub' }) },
      EvaluationsPanel: true, OutcomesPanel: true, ExecutionsPanel: true, ReportsPanelFleet: true,
      MonitoringPanel: true, NotificationsPanel: true, 'router-link': true } },
  })
}

describe('Operations › Anomalies tab gate', () => {
  it.each([
    ['admin', ['anomaly_detection'], true],
    ['admin', [], false],
    ['operator', ['anomaly_detection'], false],
  ])('role %s, features %j → listed %s', async (role, features, listed) => {
    const w = mountOps({ role, features, tab: 'anomalies' })
    await flush()
    expect(w.find('[data-tab="anomalies"]').exists()).toBe(listed)
    expect(w.find('[data-testid="anomalies-stub"]').exists()).toBe(listed)
  })

  it('badges the open count from another tab', async () => {
    const w = mountOps({ role: 'admin', features: ['anomaly_detection'] })
    await flush()
    expect(w.find('[data-tab="anomalies"]').attributes('data-badge')).toBe('2')
  })
})
