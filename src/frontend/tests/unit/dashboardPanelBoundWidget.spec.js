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
import BaseBadge from '../../src/components/base/BaseBadge.vue'
import BoundMetricMark from '../../src/components/BoundMetricMark.vue'
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
      selected('meta', 587.25), selected('google', 410), selected('linkedin', 95.5),
    ]))
    const captions = wrapper.findAll('[data-testid="bound-series"]').map((c) => c.text())
    expect(captions).toEqual(['channel=meta', 'channel=google', 'channel=linkedin'])
  })

  it('captions a fold, and does it on the status and progress arms too', async () => {
    const fold = { basis: 'folded', aggregation: 'sum', series_count: 3, dims: null,
      dimensions: ['channel', 'geo'], stale_count: 0 }
    const wrapper = await mountPanel(dashboard([
      { type: 'metric', label: 'Total', metric: 'ad_spend', bound: true, value: 1092.75,
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

// ---------------------------------------------------------------------------
// ent#730: the threshold verdict, and trend colours by declared direction
// ---------------------------------------------------------------------------

function judged(level, threshold, extra = {}) {
  return selected('meta', 587.25, { direction: 'down_good',
    threshold_verdict: { level, threshold }, ...extra })
}

describe('a bound metric tile shows its threshold verdict (ent#730)', () => {
  it('renders Critical as a danger badge naming the threshold', async () => {
    const wrapper = await mountPanel(dashboard([judged('critical', 500)]))
    const badge = wrapper.find('[data-testid="bound-verdict"]')
    expect(badge.text()).toBe('Critical')
    expect(wrapper.findComponent(BaseBadge).props('variant')).toBe('danger')
    expect(badge.classes()).not.toContain('invisible')
    expect(badge.attributes('title')).toContain('500')
  })

  it('renders Warning as a warning badge', async () => {
    const wrapper = await mountPanel(dashboard([judged('warning', 400)]))
    expect(wrapper.find('[data-testid="bound-verdict"]').text()).toBe('Warning')
    expect(wrapper.findComponent(BaseBadge).props('variant')).toBe('warning')
  })

  it('reserves the slot on ok, hidden from sight and from assistive tech', async () => {
    const wrapper = await mountPanel(dashboard([judged('ok', null)]))
    const badge = wrapper.find('[data-testid="bound-verdict"]')
    expect(badge.exists()).toBe(true)
    expect(badge.classes()).toContain('invisible')
    expect(badge.attributes('aria-hidden')).toBe('true')
  })

  it('swaps the badge in place when a poll crosses the threshold', async () => {
    // The footer itself, re-rendered with the next poll's widget: the same
    // element must carry the new verdict (a swap, not an insert).
    const wrapper = mount(BoundMetricMark, { props: { widget: judged('ok', null) } })
    const before = wrapper.find('[data-testid="bound-verdict"]').element
    await wrapper.setProps({ widget: judged('critical', 500) })
    const after = wrapper.findAll('[data-testid="bound-verdict"]')
    expect(after).toHaveLength(1)
    expect(after[0].element).toBe(before)
    expect(after[0].text()).toBe('Critical')
    expect(after[0].classes()).not.toContain('invisible')
  })

  it('never renders on status or progress tiles, an unbound tile, or an unknown level', async () => {
    const verdict = { level: 'critical', threshold: 500 }
    const wrapper = await mountPanel(dashboard([
      { ...judged('critical', 500), type: 'status', value: 'ok', color: 'red' },
      { ...judged('critical', 500), type: 'progress', value: 80 },
      { type: 'metric', label: 'Manual', metric: 'ad_spend', bound: false, value: 9,
        binding_error: 'metric store unavailable', binding_error_code: 'metric_store_unavailable',
        threshold_verdict: verdict },
      judged('apocalyptic', 1),
    ]))
    expect(wrapper.find('[data-testid="bound-verdict"]').exists()).toBe(false)
  })
})

describe('a bound tile colours its trend by the declared direction (ent#730)', () => {
  function trending(direction, trend = 'up', extra = {}) {
    return selected('meta', 587.25, { direction,
      history: { values: [{ t: 'a', v: 1 }, { t: 'b', v: 2 }], trend, trend_percent: 12 }, ...extra })
  }

  async function colours(widget) {
    const wrapper = await mountPanel(dashboard([widget]))
    return {
      arrow: wrapper.find('[data-testid="widget-trend"]').attributes('class'),
      line: wrapper.find('.sparkline-stub').attributes('color'),
    }
  }

  it('reads a rising down_good cost as bad', async () => {
    const { arrow, line } = await colours(trending('down_good'))
    expect(arrow).toContain('text-status-danger-600')
    expect(line).toBe('#ef4444')
  })

  it('reads a rising up_good number as good', async () => {
    const { arrow, line } = await colours(trending('up_good'))
    expect(arrow).toContain('text-status-success-600')
    expect(line).toBe('#10b981')
  })

  it('declines to judge a neutral metric', async () => {
    const { arrow, line } = await colours(trending('neutral'))
    expect(arrow).toContain('text-gray-500')
    expect(arrow).not.toContain('status-')
    expect(line).toBe('#3b82f6')
  })

  it('applies to the progress arm too', async () => {
    const { arrow } = await colours(trending('down_good', 'up', { type: 'progress', value: 80 }))
    expect(arrow).toContain('text-status-danger-600')
  })

  it('leaves an UNBOUND widget on the legacy colours', async () => {
    const { arrow, line } = await colours({ type: 'metric', label: 'Manual', value: 3,
      history: { values: [{ t: 'a', v: 1 }, { t: 'b', v: 2 }], trend: 'up' } })
    expect(arrow).toContain('text-status-success-600')
    expect(line).toBe('#10b981')
  })

  it('ignores an author-typed bound: true on a widget with no metric', async () => {
    // The backend never touches a widget without `metric:`, so `bound` and
    // `direction` there are whatever the author typed.
    const { arrow, line } = await colours({ type: 'metric', label: 'Manual', value: 3,
      bound: true, direction: 'down_good',
      history: { values: [{ t: 'a', v: 1 }, { t: 'b', v: 2 }], trend: 'up' } })
    expect(arrow).toContain('text-status-success-600')
    expect(line).toBe('#10b981')
  })
})
