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
    chart: null,
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
  it('renders the whole sentence the backend composed, both actions in it', async () => {
    // The route decides the copy and the tile renders what it decided, so the
    // two cannot say different things. It names BOTH actions because a
    // store-only read cannot know which playbooks the agent holds — the
    // earlier conditional was a branch no caller could take, which left the
    // `/update-dashboard` half unreachable.
    const { wrapper } = await mountTiles(async () => payload([
      metric({
        latest: null, last_point_at: null, freshness: 'no_points', series_count: 0,
        message: 'declared, no points yet — record points with `record_metrics` '
          + '(or schedule `/update-dashboard` if the agent has that playbook)',
      }),
    ]))
    const copy = wrapper.find('[data-testid="metric-empty-message"]')
    expect(copy.text()).toContain('no points yet')
    expect(copy.text()).toContain('record_metrics')
    expect(copy.text()).toContain('/update-dashboard')
    expect(wrapper.find('[data-testid="metric-value"]').text()).toContain('—')
  })

  it('carries no empty copy on a tile that HAS a number', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()]))
    expect(wrapper.find('[data-testid="metric-empty-message"]').exists()).toBe(false)
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
  // The fixture is the shape `static_checks.c_d010` actually emits — an
  // OBJECT, `{keys, undeclared}`, persisted into `checks_json` and echoed
  // verbatim by the route. The earlier fixture passed a pre-flattened string
  // the backend never produces, so the spec was green while the operator got
  // pretty-printed JSON braces on the dashboard.
  const D010 = {
    code: 'metrics_json_superseded',
    message: 'metrics.json is superseded and no longer served — record these values with `record_metrics`',
    detail: { keys: ['revenue', 'orphan'], undeclared: ['orphan'] },
  }

  it('renders the D-010 echo above the tiles', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()], {
      findings: [D010],
    }))
    const finding = wrapper.find('[data-testid="declared-metrics-finding"]')
    expect(finding.text()).toContain('superseded')
    expect(finding.text()).toContain('orphan')
  })

  it('spells the detail object out as a sentence, not as JSON braces', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()], {
      findings: [D010],
    }))
    const detail = wrapper.find('[data-testid="declared-metrics-finding-detail"]')
    expect(detail.text()).toContain('keys in the file: revenue, orphan')
    expect(detail.text()).toContain('declared nowhere: orphan')
    expect(detail.text()).not.toContain('{')
    expect(detail.text()).not.toContain('"keys"')
  })

  it('still passes a plain-string detail straight through', async () => {
    const { wrapper } = await mountTiles(async () => payload([metric()], {
      findings: [{ ...D010, detail: 'an older finding that sent a sentence' }],
    }))
    expect(wrapper.find('[data-testid="declared-metrics-finding-detail"]').text())
      .toContain('an older finding that sent a sentence')
  })
})

describe('the chart and the number describe the same thing', () => {
  it('captions a one-series chart under a multi-series metric', async () => {
    // A `last` metric has no cross-series fold, so the chart is ONE series
    // while the number names several. The caveat is the difference between a
    // labelled chart and a misleading one.
    const { wrapper } = await mountTiles(async () => payload([
      metric({
        aggregation: 'last',
        series_count: 2,
        latest_by_series: [
          { dims: { region: 'eu' }, value: 4200, ts: '2026-09-22T09:00:00Z' },
          { dims: { region: 'us' }, value: 100, ts: '2026-09-22T08:00:00Z' },
        ],
        chart: {
          basis: 'series',
          aggregation: 'last',
          series_count: 2,
          dims: { region: 'eu' },
          buckets: [{ i: 1, ts: 't1', value: 10 }, { i: 2, ts: 't2', value: 20 }],
        },
      }),
    ]))
    const note = wrapper.find('[data-testid="metric-chart-basis"]')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('region=eu')
    expect(note.attributes('title')).toContain('2 dimension series')
  })

  it('adds no caveat when the chart IS the fold across every series', async () => {
    const { wrapper } = await mountTiles(async () => payload([
      metric({
        aggregation: 'sum',
        series_count: 2,
        chart: {
          basis: 'folded',
          aggregation: 'sum',
          series_count: 2,
          dims: null,
          buckets: [{ i: 1, ts: 't1', value: 15 }, { i: 2, ts: 't2', value: 20 }],
        },
      }),
    ]))
    expect(wrapper.find('[data-testid="metric-chart-basis"]').exists()).toBe(false)
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
