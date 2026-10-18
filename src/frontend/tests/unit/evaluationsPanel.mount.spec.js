// @vitest-environment jsdom
/**
 * trinity-enterprise#938 (C2) — the Evaluations tab's KPI row and per-agent
 * table, MOUNTED over the real store with only the HTTP edge mocked.
 *
 * Pins: KPI values from the ent#937 aggregate and their deltas against the
 * previous window (same length, ending at `since`, read from the same
 * route); a rate the API returns as `null` reads "—", never 0 %; a failed
 * previous-window read drops the deltas to "—" without a banner; the table
 * opens worst-first with ungraded agents last, sorts by column, names the
 * top failed check, states its total, and narrows the tab to an agent on
 * click. The KPI row carries no judge or agreement tile (ruling addendum —
 * those wait on the calibration placement decision).
 *
 * C3: the failed-check counts and failed runs (ent#937 failed route, paged,
 * each row linking to its run page), the delegated-pairs stub over
 * `by=pair` whose row narrows the failed list to that edge, and the
 * `?agent=` deep link in both directions.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick, defineComponent, h, reactive } from 'vue'

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
vi.mock('vue-router', () => ({
  useRoute: () => nav.route,
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn() }),
}))

const RouterLinkStub = defineComponent({
  props: ['to'],
  render() { return h('a', { 'data-to': JSON.stringify(this.to) }, this.$slots.default?.()) },
})

import EvaluationsPanel from '../../src/components/EvaluationsPanel.vue'
import { useEvaluationsStore } from '../../src/stores/evaluations'

const AGG = '/api/evaluations/aggregate'
const FAILED = '/api/evaluations/failed'
const SINCE = '2026-10-11T12:00:00Z'
const UNTIL = '2026-10-18T12:00:00Z'
const PREV_SINCE = '2026-10-04T12:00:00Z'

const summary = (evaluated, completed, graded, passed, per_check_fail = {}) => ({
  evaluated, completed, graded, passed, per_check_fail, judged: 0,
  completion_rate: evaluated ? completed / evaluated : null,
  checks_passed_rate: graded ? passed / graded : null,
})
const group = (agent, ...a) => ({ key: { agent }, ...summary(...a) })

const CURRENT = {
  by: 'agent', since: SINCE, until: UNTIL, truncated: false,
  total: summary(10, 9, 8, 6, { schema_valid: 2 }),
  groups: [
    group('alpha', 4, 4, 4, 4),
    group('beta', 4, 3, 4, 2, { schema_valid: 2, no_error: 1 }),
    group('gamma', 2, 2, 0, 0),              // nothing graded → rate null
  ],
}
const PREVIOUS = {
  by: 'agent', since: PREV_SINCE, until: SINCE, truncated: false,
  total: summary(8, 8, 8, 4),
  groups: [group('alpha', 4, 4, 4, 2), group('beta', 4, 4, 4, 3)],
}

const run = (id, agent, extra = {}) => ({
  execution_id: id, agent_name: agent, caller_agent: null, schedule_id: null,
  completed_at: '2026-10-17T09:00:00Z', completion: true, failed_checks: [], ...extra,
})
const FAILED_PAGE_1 = {
  since: SINCE, until: UNTIL, total: 3, truncated: false,
  items: [
    run('e1', 'beta', { failed_checks: [{ id: 'schema_valid', reason: 'missing_field' }] }),
    run('e2', 'beta', { completion: false }),
  ],
}
const FAILED_PAGE_2 = { ...FAILED_PAGE_1, items: [run('e3', 'alpha', { failed_checks: [{ id: 'no_error', reason: null }] })] }
const PAIRS = {
  by: 'pair', since: SINCE, until: UNTIL, truncated: false, total: summary(3, 3, 3, 2),
  groups: [
    { key: { caller: 'alpha', callee: 'beta' }, ...summary(2, 2, 2, 1) },
    { key: { caller: 'gamma', callee: 'alpha' }, ...summary(1, 1, 1, 1) },
  ],
}

// Answers by route and window: the previous window is the one whose `until`
// is SINCE; the failed route pages by offset.
const byWindow = ({ prevFails = false, pairs = PAIRS } = {}) => async (url, { params }) => {
  if (url === FAILED) return { data: params.offset ? FAILED_PAGE_2 : FAILED_PAGE_1 }
  if (url !== AGG) throw new Error(`unexpected ${url}`)
  if (params.by === 'pair') return { data: pairs }
  if (params.until === SINCE) {
    if (prevFails) { const e = new Error('500'); e.response = { status: 500, data: { detail: 'down' } }; throw e }
    return { data: PREVIOUS }
  }
  return { data: CURRENT }
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

async function panel(get, query = {}) {
  http.get = vi.fn(get)
  nav.route = reactive({ query: { tab: 'evaluations', ...query }, params: {} })
  nav.replace = vi.fn()
  const w = mount(EvaluationsPanel, { global: { stubs: { RouterLink: RouterLinkStub } } })
  await flush()
  return w
}

const kpi = (w, id) => ({
  value: w.find(`[data-testid="eval-kpi-${id}"] [data-testid="eval-kpi-value"]`).text(),
  delta: w.find(`[data-testid="eval-kpi-${id}"] [data-testid="eval-kpi-delta"]`).text(),
})
const agents = (w) => w.findAll('[data-testid="eval-agent-row"]').map((r) => r.attributes('data-agent'))

describe('EvaluationsPanel — KPI row (C2)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('reads the previous window from the same route: same length, ending at since', async () => {
    await panel(byWindow())
    const aggCalls = http.get.mock.calls.filter(([u, { params }]) => u === AGG && params.by === 'agent')
    expect(aggCalls.map(([, c]) => c.params)).toEqual([
      { by: 'agent', period: 'week' },
      { by: 'agent', period: 'custom', since: PREV_SINCE, until: SINCE },
    ])
  })

  it('shows the window\'s counts and rates with their change against the previous window', async () => {
    const w = await panel(byWindow())
    expect(kpi(w, 'evaluated')).toEqual({ value: '10', delta: '+2 vs previous period' })
    expect(kpi(w, 'completion_rate').value).toBe('90.0%')
    expect(kpi(w, 'completion_rate').delta).toBe('−10.0 pts vs previous period')
    expect(kpi(w, 'checks_passed_rate').value).toBe('75.0%')
    expect(kpi(w, 'checks_passed_rate').delta).toBe('+25.0 pts vs previous period')
  })

  it('carries exactly three tiles — no judge-quality or agreement tile yet', async () => {
    const w = await panel(byWindow())
    const ids = w.findAll('[data-testid^="eval-kpi-"]')
      .map((n) => n.attributes('data-testid'))
      .filter((id) => !['eval-kpi-value', 'eval-kpi-delta'].includes(id) && id !== 'eval-kpis')
    expect(ids).toEqual(['eval-kpi-evaluated', 'eval-kpi-completion_rate', 'eval-kpi-checks_passed_rate'])
    expect(w.text()).not.toMatch(/agreement|judge/i)
  })

  it('nothing graded reads "—", never 0 %', async () => {
    const empty = { ...CURRENT, since: 'x', until: 'y', total: summary(3, 3, 0, 0), groups: [group('gamma', 3, 3, 0, 0)] }
    const w = await panel(async (url) => ({ data: url === FAILED ? { items: [], total: 0, truncated: false } : empty }))
    expect(kpi(w, 'checks_passed_rate').value).toBe('—')
    expect(w.find('[data-testid="eval-agent-passed"]').text()).toBe('—')
  })

  it('a failed previous-window read drops the deltas to "—" and raises no banner', async () => {
    const w = await panel(byWindow({ prevFails: true }))
    expect(kpi(w, 'evaluated')).toEqual({ value: '10', delta: '— vs previous period' })
    expect(kpi(w, 'checks_passed_rate').delta).toBe('— vs previous period')
    expect(w.find('[data-testid="evaluations-failed"]').exists()).toBe(false)
    expect(useEvaluationsStore().error).toBeNull()
  })
})

describe('EvaluationsPanel — per-agent table (C2)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('opens worst checks-passed first, ungraded agents last, with a stated total', async () => {
    const w = await panel(byWindow())
    expect(agents(w)).toEqual(['beta', 'alpha', 'gamma'])
    expect(w.find('[data-testid="eval-agent-total"]').text()).toBe('3 agents')
  })

  it('names each agent\'s top failed check and its checks-passed change', async () => {
    const w = await panel(byWindow())
    const rows = w.findAll('[data-testid="eval-agent-row"]')
    expect(rows[0].find('[data-testid="eval-agent-topcheck"]').text()).toBe('schema_valid ×2')
    expect(rows[0].find('[data-testid="eval-agent-delta"]').text()).toBe('−25.0 pts')
    expect(rows[1].find('[data-testid="eval-agent-delta"]').text()).toBe('+50.0 pts')
    expect(rows[1].find('[data-testid="eval-agent-topcheck"]').text()).toBe('—')
    expect(rows[2].find('[data-testid="eval-agent-delta"]').text()).toBe('—')   // no previous group
  })

  it('sorts by a column and flips on the second click; ungraded stays last both ways', async () => {
    const w = await panel(byWindow())
    await w.find('[data-testid="eval-sort-checksPassedRate"]').trigger('click')
    expect(agents(w)).toEqual(['beta', 'alpha', 'gamma'])
    await w.find('[data-testid="eval-sort-checksPassedRate"]').trigger('click')
    expect(agents(w)).toEqual(['alpha', 'beta', 'gamma'])
    await w.find('[data-testid="eval-sort-agent"]').trigger('click')
    expect(agents(w)).toEqual(['alpha', 'beta', 'gamma'])
    await w.find('[data-testid="eval-sort-agent"]').trigger('click')
    expect(agents(w)).toEqual(['gamma', 'beta', 'alpha'])
  })

  it('clicking an agent narrows the tab to it — the aggregate is re-read with agent=', async () => {
    const w = await panel(byWindow())
    http.get.mockClear()
    await w.findAll('[data-testid="eval-agent-select"]')[0].trigger('click')
    await flush()
    expect(useEvaluationsStore().agent).toBe('beta')
    expect(http.get.mock.calls[0]).toEqual([AGG, { params: { by: 'agent', period: 'week', agent: 'beta' } }])
    expect(http.get.mock.calls.find(([u]) => u === FAILED)[1].params)
      .toEqual({ by: 'agent', period: 'week', agent: 'beta', limit: 50, offset: 0 })
    expect(w.find('[data-testid="evaluations-agent-chip"]').text()).toContain('beta')
  })
})

describe('EvaluationsPanel — failed list (C3)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('reads the ent#937 failed route for the same window and lists the runs, each linking to its run page', async () => {
    const w = await panel(byWindow())
    expect(http.get.mock.calls.find(([u]) => u === FAILED)[1].params)
      .toEqual({ by: 'agent', period: 'week', limit: 50, offset: 0 })
    const rows = w.findAll('[data-testid="eval-failed-row"]')
    expect(rows).toHaveLength(2)
    expect(JSON.parse(rows[0].find('[data-testid="eval-failed-link"]').attributes('data-to')))
      .toEqual({ name: 'ExecutionDetail', params: { name: 'beta', executionId: 'e1' } })
    expect(rows[0].find('[data-testid="eval-failed-why"]').text()).toContain('schema_valid · missing_field')
    expect(rows[1].find('[data-testid="eval-failed-why"]').text()).toContain('did not complete')
    expect(w.find('[data-testid="eval-failed-total"]').text()).toBe('2 of 3')
  })

  it('shows the window\'s failed-check counts, most frequent first', async () => {
    const w = await panel(byWindow())
    expect(w.findAll('[data-testid="eval-failed-check"]').map((b) => b.text().replace(/\s+/g, ' ')))
      .toEqual(['schema_valid 2'])
  })

  it('"Load more" asks for the next page and appends it; it goes away at the total', async () => {
    const w = await panel(byWindow())
    await w.find('[data-testid="eval-failed-more"]').trigger('click')
    await flush()
    expect(http.get.mock.calls.filter(([u]) => u === FAILED).at(-1)[1].params.offset).toBe(2)
    expect(w.findAll('[data-testid="eval-failed-row"]').map((r) => r.find('a').text())).toEqual(['beta', 'beta', 'alpha'])
    expect(w.find('[data-testid="eval-failed-more"]').exists()).toBe(false)
  })

  it('a failed page keeps the KPIs and says so in its own block', async () => {
    const base = byWindow()
    const w = await panel(async (url, c) => {
      if (url === FAILED) { const e = new Error('500'); e.response = { status: 500, data: { detail: 'boom' } }; throw e }
      return base(url, c)
    })
    expect(kpi(w, 'evaluated').value).toBe('10')
    expect(w.find('[data-testid="eval-failed"]').text()).toContain('boom')
    expect(w.find('[data-testid="eval-failed-empty"]').exists()).toBe(false)
  })
})

describe('EvaluationsPanel — pairs stub (C3)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('is wired to the aggregate by=pair over the same window', async () => {
    const w = await panel(byWindow())
    expect(http.get.mock.calls.find(([u, c]) => u === AGG && c.params.by === 'pair')[1].params)
      .toEqual({ by: 'pair', period: 'week' })
    expect(w.findAll('[data-testid="eval-pair-row"]').map((r) => r.find('button').text()))
      .toEqual(['alpha → beta', 'gamma → alpha'])
    expect(w.find('[data-testid="eval-pairs-total"]').text()).toBe('2 pairs')
  })

  it('a pair row narrows the failed list to that caller → callee edge; clearing restores it', async () => {
    const w = await panel(byWindow())
    await w.findAll('[data-testid="eval-pair-select"]')[0].trigger('click')
    await flush()
    expect(http.get.mock.calls.filter(([u]) => u === FAILED).at(-1)[1].params)
      .toEqual({ by: 'pair', caller: 'alpha', callee: 'beta', period: 'week', limit: 50, offset: 0 })
    expect(w.find('[data-testid="eval-failed-pair-chip"]').text()).toContain('alpha → beta')
    await w.find('[data-testid="eval-failed-pair-clear"]').trigger('click')
    await flush()
    expect(http.get.mock.calls.filter(([u]) => u === FAILED).at(-1)[1].params.by).toBe('agent')
  })

  it('no delegated runs: the stub says so', async () => {
    const w = await panel(byWindow({ pairs: { ...PAIRS, groups: [] } }))
    expect(w.find('[data-testid="eval-pairs-empty"]').text()).toBe('No delegated runs graded in this period.')
  })
})

describe('EvaluationsPanel — ?agent= deep link (C3)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('arriving with ?agent= narrows every read to that agent', async () => {
    await panel(byWindow(), { agent: 'beta' })
    expect(useEvaluationsStore().agent).toBe('beta')
    expect(http.get.mock.calls[0]).toEqual([AGG, { params: { by: 'agent', period: 'week', agent: 'beta' } }])
    expect(nav.replace).not.toHaveBeenCalled()
  })

  it('picking an agent writes ?agent= into the URL, keeping ?tab=; clearing removes it', async () => {
    const w = await panel(byWindow())
    await w.findAll('[data-testid="eval-agent-select"]')[0].trigger('click')
    await flush()
    expect(nav.replace).toHaveBeenLastCalledWith({ query: { tab: 'evaluations', agent: 'beta' } })
    nav.route.query = { tab: 'evaluations', agent: 'beta' }
    await w.find('[data-testid="evaluations-agent-clear"]').trigger('click')
    await flush()
    expect(nav.replace).toHaveBeenLastCalledWith({ query: { tab: 'evaluations' } })
  })
})
