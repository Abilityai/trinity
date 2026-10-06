// @vitest-environment jsdom
/**
 * A `dashboard.yaml` widget bound to a declared metric (ent#479 C7).
 *
 * MOUNTED. The claims are about what the widget SHOWS once the backend has
 * filled it from the registry: that it states when the value was recorded,
 * that a stale point is marked, and — the one that matters most — that a
 * binding error renders the REASON instead of a number the file happens to
 * still contain. A regex over `DashboardPanel.vue` would pass on all three
 * with the `v-if` inverted (#2918).
 *
 * `BoundMetricMark` is mounted through `DashboardPanel` rather than on its own,
 * because the thing at risk is the wiring: three widget arms take the binding
 * and a missing mark on one of them is invisible from the child's own spec.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import DashboardPanel from '../../src/components/DashboardPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'

// uPlot reads `matchMedia` at module load; see declaredMetricsTiles.spec.js.
vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div class="sparkline-stub" />' },
}))
// The panel asks `/api/agents/{name}/playbooks` directly through axios for the
// "Update Dashboard" button. Not what this spec is about — but `src/api.js`
// calls `axios.create` at import time down the store's import chain, so the
// mock has to keep that shape or nothing loads.
vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: { skills: [] } })),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

const AGENT = 'bound-agent'

function dashboard(widgets) {
  return {
    has_dashboard: true,
    stale: false,
    config: { title: 'Ops', sections: [{ title: 'Numbers', widgets }] },
    last_modified: '2026-09-22T09:00:00Z',
  }
}

async function mountPanel(data, props = {}) {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  store.getAgentDashboard = vi.fn(async () => data)
  const wrapper = mount(DashboardPanel, {
    props: { agentName: AGENT, agentStatus: 'running', ...props },
  })
  await flush()
  return wrapper
}

async function flush() {
  for (let i = 0; i < 4; i++) {
    await nextTick()
    await Promise.resolve()
  }
}

beforeEach(() => { vi.clearAllMocks() })

describe('a bound widget shows the registry value WITH its point time', () => {
  it('renders the metric chip and the recorded time on a metric widget', async () => {
    const wrapper = await mountPanel(dashboard([{
      type: 'metric', label: 'Revenue', metric: 'revenue', bound: true,
      value: 4200, unit: 'USD', stale: false, freshness: 'fresh',
      last_point_at: '2026-09-22T09:00:00Z',
    }]))

    const mark = wrapper.find('[data-testid="bound-mark"]')
    expect(mark.exists()).toBe(true)
    expect(wrapper.find('[data-testid="bound-chip"]').text()).toBe('revenue')
    // The panel's own header timestamp says when the DASHBOARD was fetched,
    // which for a bound number says nothing about the number.
    const time = wrapper.find('[data-testid="bound-point-time"]')
    expect(time.exists()).toBe(true)
    expect(time.attributes('title')).toBeTruthy()
  })

  it('marks a stale bound widget', async () => {
    const wrapper = await mountPanel(dashboard([{
      type: 'metric', label: 'Revenue', metric: 'revenue', bound: true,
      value: 4200, stale: true, freshness: 'stale', cadence: '1h',
      last_point_at: '2026-09-19T09:00:00Z',
    }]))
    const chip = wrapper.find('[data-testid="bound-freshness-chip"]')
    expect(chip.text()).toBe('Stale')
    expect(chip.attributes('class')).toContain('status-warning')
  })

  it('marks a bound STATUS widget and a bound PROGRESS widget too', async () => {
    // Three arms take the binding; a mark wired into only the metric arm is
    // the regression this case exists for.
    const wrapper = await mountPanel(dashboard([
      {
        type: 'status', label: 'Pipeline', metric: 'pipeline', bound: true,
        value: 'healthy', color: 'green', stale: false, freshness: 'fresh',
        last_point_at: '2026-09-22T09:00:00Z',
      },
      {
        type: 'progress', label: 'Coverage', metric: 'coverage', bound: true,
        value: 81, stale: true, freshness: 'stale',
        last_point_at: '2026-09-19T09:00:00Z',
      },
    ]))
    const chips = wrapper.findAll('[data-testid="bound-chip"]').map((c) => c.text())
    expect(chips).toEqual(['pipeline', 'coverage'])
    expect(wrapper.findAll('[data-testid="bound-freshness-chip"]')).toHaveLength(1)
  })
})

describe('a binding that failed shows the reason, never a number', () => {
  it('renders the undeclared-metric copy and no value', async () => {
    // The backend removes `value` for an undeclared name — a wrong number is
    // worse than no number — so the widget must explain the gap rather than
    // rendering a bare dash the operator cannot interpret.
    const wrapper = await mountPanel(dashboard([{
      type: 'metric', label: 'Mystery', metric: 'nope', bound: false,
      binding_error: "metric 'nope' is not declared in template.yaml",
    }]))
    const error = wrapper.find('[data-testid="bound-error"]')
    expect(error.text()).toContain("not declared in template.yaml")
    expect(wrapper.find('[data-testid="bound-point-time"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="bound-freshness-chip"]').exists()).toBe(false)
  })

  it('renders the store-outage copy per widget, with the dashboard still up', async () => {
    const wrapper = await mountPanel(dashboard([
      { type: 'metric', label: 'Bound', metric: 'revenue', bound: false,
        binding_error: 'metric store unavailable' },
      { type: 'metric', label: 'Manual', value: 7 },
    ]))
    expect(wrapper.find('[data-testid="bound-error"]').text())
      .toBe('metric store unavailable')
    // A dashboard is never taken down because one widget named a metric.
    expect(wrapper.text()).toContain('Manual')
    expect(wrapper.text()).toContain('7')
  })

  it('renders the retired-metric refusal and no number', async () => {
    // TD-10 refuses `metric=<retired>` on the route so a retired metric never
    // silently reads as current; a widget is that same read with nobody there
    // to pass `include_retired`, so the bind refuses too. The widget must say
    // WHY rather than showing the last value it happened to have.
    const wrapper = await mountPanel(dashboard([{
      type: 'metric', label: 'Old KPI', metric: 'revenue', bound: false,
      binding_error: "metric 'revenue' was retired at 2026-09-01T00:00:00Z — "
        + 'bind a declared metric or re-declare this one in template.yaml',
      binding_error_code: 'metric_retired',
      retired_at: '2026-09-01T00:00:00Z',
    }]))
    const error = wrapper.find('[data-testid="bound-error"]')
    expect(error.text()).toContain('was retired at 2026-09-01T00:00:00Z')
    expect(wrapper.find('[data-testid="bound-freshness-chip"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="bound-point-time"]').exists()).toBe(false)
  })

  it('says "no points yet" for a declared metric that has never recorded one', async () => {
    const wrapper = await mountPanel(dashboard([{
      type: 'metric', label: 'Revenue', metric: 'revenue', bound: true,
      stale: false, freshness: 'no_points', last_point_at: null,
    }]))
    expect(wrapper.find('[data-testid="bound-no-points"]').text())
      .toContain('no points yet')
  })
})

describe('an UNBOUND widget is untouched', () => {
  it('carries no mark at all', async () => {
    const wrapper = await mountPanel(dashboard([
      { type: 'metric', label: 'Manual', value: 42 },
    ]))
    expect(wrapper.find('[data-testid="bound-mark"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('42')
  })
})

describe('the empty state knows the tiles are beside it (ent#479)', () => {
  it('still says the agent has no dashboard.yaml when it has no metrics either', async () => {
    const wrapper = await mountPanel({ has_dashboard: false })
    expect(wrapper.find('[data-testid="no-dashboard-copy"]').text())
      .toContain('does not have a dashboard.yaml')
  })

  it('does NOT imply there is nothing to show when metrics are declared', async () => {
    const wrapper = await mountPanel({ has_dashboard: false },
      { hasDeclaredMetrics: true })
    const copy = wrapper.find('[data-testid="no-dashboard-copy"]').text()
    expect(copy).not.toContain('does not have a dashboard.yaml')
    expect(copy).toContain('declared metrics above')
  })

  it('tells a stopped agent its recorded metrics do not need it running', async () => {
    const wrapper = await mountPanel({ has_dashboard: false },
      { agentStatus: 'stopped', hasDeclaredMetrics: true })
    expect(wrapper.find('[data-testid="not-running-copy"]').text())
      .toContain('do not need the agent running')
  })
})

// ---------------------------------------------------------------------------
// ent#730: a bound tile says which series its number is
// ---------------------------------------------------------------------------

function selected(channel, value, extra = {}) {
  return {
    type: 'metric', label: `Spend ${channel}`, metric: 'ad_spend', bound: true,
    value, stale: false, freshness: 'fresh', last_point_at: '2026-09-22T09:00:00Z',
    bound_series: { basis: 'selected', aggregation: 'sum', series_count: 3,
      dims: { channel }, dimensions: ['channel'] },
    ...extra,
  }
}

function notFound(extra = {}) {
  return {
    type: 'metric', label: 'Spend tiktok', metric: 'ad_spend', bound: false,
    binding_error: "metric 'ad_spend': no recent data for channel=tiktok",
    binding_error_code: 'metric_series_not_found',
    binding_detail: { selector: { channel: 'tiktok' }, recent_series: [{ channel: 'meta' }],
      more: 0, window_points: 200, series_cap: null, near: [] },
    ...extra,
  }
}

describe('a bound tile captions which series it shows (ent#730)', () => {
  it('gives three per-channel tiles three different captions', async () => {
    const wrapper = await mountPanel(dashboard([
      selected('meta', 623.88), selected('google', 410), selected('linkedin', 95.5),
    ]))
    const captions = wrapper.findAll('[data-testid="bound-series"]').map((c) => c.text())
    expect(captions).toEqual(['channel=meta', 'channel=google', 'channel=linkedin'])
  })

  it('captions a fold, and does it on the status and progress arms too', async () => {
    const fold = { basis: 'folded', aggregation: 'sum', series_count: 3, dims: null,
      dimensions: ['channel', 'geo'], stale_count: 0 }
    const wrapper = await mountPanel(dashboard([
      { type: 'metric', label: 'Total', metric: 'ad_spend', bound: true, value: 1129.38,
        stale: false, freshness: 'fresh', last_point_at: '2026-09-22T09:00:00Z', bound_series: fold },
      { type: 'status', label: 'Pipeline', metric: 'pipeline', bound: true, value: 'ok', color: 'green',
        stale: false, freshness: 'fresh', last_point_at: '2026-09-22T09:00:00Z',
        bound_series: { basis: 'selected', aggregation: 'last', series_count: 2,
          dims: { region: 'eu' }, dimensions: ['region'] } },
      { type: 'progress', label: 'Coverage', metric: 'coverage', bound: true, value: 81,
        stale: false, freshness: 'fresh', last_point_at: '2026-09-22T09:00:00Z',
        bound_series: { basis: 'selected', aggregation: 'last', series_count: 2,
          dims: { region: 'us' }, dimensions: ['region'] } },
    ]))
    const captions = wrapper.findAll('[data-testid="bound-series"]').map((c) => c.text())
    expect(captions).toEqual(['sum of 3 series', 'region=eu', 'region=us'])
  })

  it('renders a not-found refusal as a calm footer row, not the warning paragraph', async () => {
    const wrapper = await mountPanel(dashboard([notFound()]))
    expect(wrapper.find('[data-testid="bound-chip"]').text()).toBe('ad_spend')
    expect(wrapper.find('[data-testid="bound-series"]').text()).toBe('channel=tiktok')
    expect(wrapper.find('[data-testid="bound-not-found"]').text()).toBe('no recent data')
    expect(wrapper.find('[data-testid="bound-error-hint"]').text()).toContain('Recent channel: meta')
    expect(wrapper.find('[data-testid="bound-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="bound-no-points"]').exists()).toBe(false)
  })

  it('tells a selected tile on a metric with no points that there are none yet', async () => {
    const wrapper = await mountPanel(dashboard([notFound({ binding_detail: {
      selector: { channel: 'google' }, recent_series: [], more: 0, window_points: 200,
      series_cap: null, near: [] } })]))
    expect(wrapper.find('[data-testid="bound-series"]').text()).toBe('channel=google')
    expect(wrapper.find('[data-testid="bound-error-hint"]').text()).toBe('This metric has no points yet.')
    expect(wrapper.find('[data-testid="bound-no-points"]').exists()).toBe(false)
  })

  it('links the docs on every dims refusal, including those with no facts', async () => {
    const wrapper = await mountPanel(dashboard([
      notFound(),
      { type: 'metric', label: 'Bad', metric: 'ad_spend', bound: false,
        binding_error: "metric 'ad_spend': dims must be a mapping of dimension: value",
        binding_error_code: 'metric_dimension_invalid' },
      { type: 'metric', label: 'Region', metric: 'ad_spend', bound: false,
        binding_error: "metric 'ad_spend': dimension 'region' is not declared for this metric; declared: channel",
        binding_error_code: 'metric_dimension_undeclared' },
      { type: 'metric', label: 'Gone', metric: 'nope', bound: false,
        binding_error: "metric 'nope' is not declared in template.yaml",
        binding_error_code: 'metric_undeclared' },
    ]))
    expect(wrapper.findAll('[data-testid="bound-docs-link"]')).toHaveLength(3)
    const errors = wrapper.findAll('[data-testid="bound-error"]')
    expect(errors).toHaveLength(3)
    expect(errors[1].text()).toContain("dimension 'region' is not declared")
    expect(errors[1].attributes('class')).toContain('text-status-warning-700')
  })
})
