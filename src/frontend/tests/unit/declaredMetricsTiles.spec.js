// @vitest-environment jsdom
/**
 * `DeclaredMetricsTiles.vue` — the agent's declared metrics as tiles (ent#479).
 *
 * MOUNTED, not regex'd. Every claim below is about what an operator SEES:
 * that a stale number is marked as stale rather than rendered as current, that
 * a tile always says when its point was recorded, that the empty copy names
 * the action this particular agent can actually take, and that a scheduled
 * refresh swaps values in place instead of resetting the surface. A source
 * scan would pass on an inverted `v-if` for any of them (#2918 / #2756).
 *
 * The backend is the only place the stale rule lives, so the payloads here
 * carry `freshness` / `stale` as the route would return them — a tile that
 * recomputed staleness in the browser would be a second rule.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import DeclaredMetricsTiles from '../../src/components/DeclaredMetricsTiles.vue'
import { useAgentsStore } from '../../src/stores/agents'

const AGENT = 'tiles-agent'

// SparklineChart draws with uPlot, which reads `matchMedia` at MODULE load and
// wants a real 2D context — neither exists in jsdom. A `global.stubs` entry is
// too late (the import has already run), so the module is mocked instead. What
// the sparkline draws is not what any assertion below is about.
vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div class="sparkline-stub" />' },
}))

function metric(overrides = {}) {
  return {
    name: 'revenue',
    type: 'gauge',
    label: 'Revenue',
    description: null,
    unit: 'USD',
    direction: 'up_good',
    aggregation: 'last',
    cadence: '1h',
    cadence_seconds: 3600,
    warning_threshold: null,
    critical_threshold: null,
    values: null,
    dimensions: [],
    status: 'active',
    retired_at: null,
    type_conflict: null,
    latest: { value: 4200, ts: '2026-09-22T09:00:00Z', dims: null },
    latest_by_series: [],
    last_point_at: '2026-09-22T09:00:00Z',
    stale: false,
    freshness: 'fresh',
    stale_after: null,
    series_count: 1,
    series: [],
    stats: null,
    message: null,
    ...overrides,
  }
}

function payload(metrics, extra = {}) {
  return {
    agent_name: AGENT,
    declared: metrics.length > 0,
    window: { kind: 'auto', since: null, until: null },
    generated_at: '2026-09-22T10:00:00Z',
    metrics,
    findings: [],
    findings_evaluated_at: null,
    policy: null,
    message: null,
    ...extra,
  }
}

/** Mount with the store's fetch stubbed, and wait for the first load to land. */
async function mountTiles(responder) {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  const getAgentMetrics = vi.fn(responder)
  store.getAgentMetrics = getAgentMetrics
  const wrapper = mount(DeclaredMetricsTiles, {
    props: { agentName: AGENT },
  })
  await flush()
  return { wrapper, getAgentMetrics }
}

async function flush() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  vi.useRealTimers()
})

describe('a tile states WHEN its number was recorded', () => {
  it('renders the point time on every tile that has a point', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()]))
    const time = wrapper.find('[data-testid="metric-point-time"]')
    expect(time.exists()).toBe(true)
    expect(time.text()).not.toBe('')
    // Relative on the face, absolute on hover — a number with no time is a
    // claim about now, and for a recorded metric that is usually false.
    expect(time.attributes('title')).toBeTruthy()
  })

  it('renders the value formatted for its declared type', async () => {
    const { wrapper } = await mountTiles(async () => payload([
      metric({ name: 'p95', type: 'duration', unit: 'seconds', latest: { value: 5400, ts: '2026-09-22T09:00:00Z', dims: null } }),
    ]))
    expect(wrapper.find('[data-testid="metric-value"]').text()).toContain('1h 30m')
  })
})

describe('a stale metric is MARKED stale, never rendered as current', () => {
  it('shows the stale chip and keeps showing the last known number', async () => {
    const { wrapper } = await mountTiles(async () => payload([
      metric({ stale: true, freshness: 'stale', last_point_at: '2026-09-19T09:00:00Z' }),
    ]))
    const chip = wrapper.find('[data-testid="metric-freshness-chip"]')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toBe('Stale')
    expect(chip.attributes('class')).toContain('status-warning')
    // The number is still there — hiding it would lose the last thing known.
    expect(wrapper.find('[data-testid="metric-value"]').text()).toContain('4,200')
  })

  it('does NOT chip a fresh metric — the point time already says it', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()]))
    expect(wrapper.find('[data-testid="metric-freshness-chip"]').exists()).toBe(false)
  })

  it('chips a metric with no declared cadence WITHOUT calling it stale', async () => {
    const { wrapper } = await mountTiles(async () => payload([
      metric({ cadence: null, cadence_seconds: null, stale: null, freshness: 'no_cadence' }),
    ]))
    const chip = wrapper.find('[data-testid="metric-freshness-chip"]')
    expect(chip.text()).toBe('No cadence declared')
    expect(chip.text()).not.toContain('Stale')
    expect(chip.attributes('class')).not.toContain('status-warning')
  })
})

describe('the declared-but-empty state names the next action', () => {
  it('shows the message the backend composed for a metric with no points', async () => {
    // Which action depends on whether this agent HAS `/update-dashboard`; the
    // route decides that and the tile renders what it decided, so the two
    // cannot say different things.
    const { wrapper } = await mountTiles(async () => payload([
      metric({
        latest: null, last_point_at: null, freshness: 'no_points', series_count: 0,
        message: 'declared, no points yet — schedule `/update-dashboard`',
      }),
    ]))
    const copy = wrapper.find('[data-testid="metric-empty-message"]')
    expect(copy.text()).toContain('no points yet')
    expect(copy.text()).toContain('/update-dashboard')
    expect(wrapper.find('[data-testid="metric-value"]').text()).toContain('—')
  })

  it('shows the record_metrics wording when that is what the backend sent', async () => {
    const { wrapper } = await mountTiles(async () => payload([
      metric({
        latest: null, last_point_at: null, freshness: 'no_points',
        message: 'declared, no points yet — record points with `record_metrics`',
      }),
    ]))
    expect(wrapper.find('[data-testid="metric-empty-message"]').text())
      .toContain('record_metrics')
  })

  it('shows the zero-declaration copy, not a blank grid', async () => {
    const { wrapper } = await mountTiles(async () => payload([], {
      declared: false,
      message: 'no metrics: block in template.yaml — declare one and pull',
    }))
    const empty = wrapper.find('[data-testid="declared-metrics-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('template.yaml')
    expect(wrapper.find('[data-testid="metric-tile"]').exists()).toBe(false)
  })
})

describe('the superseded metrics.json finding is surfaced, not swallowed', () => {
  it('renders the D-010 echo above the tiles', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()], {
      findings: [{
        code: 'metrics_json_superseded',
        message: 'metrics.json is superseded and no longer served',
        detail: 'keys with no registry entry: orphan',
      }],
    }))
    const finding = wrapper.find('[data-testid="declared-metrics-finding"]')
    expect(finding.text()).toContain('superseded')
    expect(finding.text()).toContain('orphan')
  })
})

describe('refresh is in place (ent#253 / design-system p5, p13, p14)', () => {
  it('keeps the same DOM node and its scrollTop across a refresh', async () => {
    let value = 4200
    const { wrapper } = await mountTiles(async () => payload([
      metric({ latest: { value, ts: '2026-09-22T09:00:00Z', dims: null } }),
    ]))
    const tileBefore = wrapper.find('[data-metric="revenue"]').element
    tileBefore.scrollTop = 40

    value = 4300
    await wrapper.find('[data-testid="declared-metrics-refresh"]').trigger('click')
    await flush()

    const tileAfter = wrapper.find('[data-metric="revenue"]').element
    expect(tileAfter).toBe(tileBefore)          // not remounted
    expect(tileAfter.scrollTop).toBe(40)        // nothing reset
    expect(wrapper.find('[data-testid="metric-value"]').text()).toContain('4,300')
  })

  it('never re-shows the skeleton once data has arrived', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()]))
    await wrapper.find('[data-testid="declared-metrics-refresh"]').trigger('click')
    await nextTick()
    // Mid-flight, with data on screen: "loading" means "no data yet", never
    // "fetch in flight".
    expect(wrapper.find('[data-testid="declared-metrics-skeleton"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="metric-tile"]').exists()).toBe(true)
  })

  it('a FAILED refresh keeps the numbers and raises the stale banner', async () => {
    let fail = false
    const { wrapper } = await mountTiles(async () => {
      if (fail) throw new Error('backend exploded')
      return payload([metric()])
    })

    fail = true
    await wrapper.find('[data-testid="declared-metrics-refresh"]').trigger('click')
    await flush()

    // The #1926 class: never overwrite data with a synthetic empty payload in
    // a catch. "The request failed" must not become "this agent has none".
    expect(wrapper.find('[data-testid="metric-tile"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="declared-metrics-empty"]').exists()).toBe(false)
    expect(wrapper.text()).toContain("Couldn't refresh the declared metrics")
  })

  it('a failed FIRST load renders LoadFailed, never the empty copy', async () => {
    const { wrapper } = await mountTiles(async () => { throw new Error('nope') })
    expect(wrapper.find('[data-testid="declared-metrics-empty"]').exists()).toBe(false)
    expect(wrapper.text()).toContain("Couldn't load the declared metrics")
  })
})

describe('the window selector refetches without collapsing the surface', () => {
  it('passes the chosen window to the store', async () => {
    const { wrapper, getAgentMetrics } = await mountTiles(async () => payload([metric()]))
    expect(getAgentMetrics).toHaveBeenLastCalledWith(AGENT, { window: 'auto' })

    await wrapper.find('select').setValue('7d')
    await flush()
    expect(getAgentMetrics).toHaveBeenLastCalledWith(AGENT, { window: '7d' })
    expect(wrapper.find('[data-testid="metric-tile"]').exists()).toBe(true)
  })
})

describe('the tiles never ask whether the agent is running', () => {
  it('takes no status prop and fetches on mount regardless', async () => {
    // The whole reason this is a sibling of DashboardPanel rather than an arm
    // inside it: the read is store-backed, so a stopped agent's recorded
    // numbers are exactly as available as a running one's.
    const { getAgentMetrics } = await mountTiles(async () => payload([metric()]))
    expect(getAgentMetrics).toHaveBeenCalledTimes(1)
    expect(Object.keys(DeclaredMetricsTiles.props || {})).not.toContain('agentStatus')
  })
})
