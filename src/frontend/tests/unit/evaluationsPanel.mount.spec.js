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
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick, defineComponent, h } from 'vue'

const http = vi.hoisted(() => ({ get: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

const RouterLinkStub = defineComponent({
  props: ['to'],
  render() { return h('a', { 'data-to': JSON.stringify(this.to) }, this.$slots.default?.()) },
})

import EvaluationsPanel from '../../src/components/EvaluationsPanel.vue'
import { useEvaluationsStore } from '../../src/stores/evaluations'

const AGG = '/api/evaluations/aggregate'
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

// Answers by window: the previous window is the one whose `until` is SINCE.
const byWindow = ({ prevFails = false } = {}) => async (url, { params }) => {
  if (url !== AGG) throw new Error(`unexpected ${url}`)
  if (params.until === SINCE) {
    if (prevFails) { const e = new Error('500'); e.response = { status: 500, data: { detail: 'down' } }; throw e }
    return { data: PREVIOUS }
  }
  return { data: CURRENT }
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

async function panel(get) {
  http.get = vi.fn(get)
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
    expect(http.get.mock.calls.map(([u]) => u)).toEqual([AGG, AGG])
    expect(http.get.mock.calls[1][1].params).toEqual({ by: 'agent', period: 'custom', since: PREV_SINCE, until: SINCE })
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
    const w = await panel(async () => ({ data: empty }))
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
    expect(http.get.mock.calls[0][1].params).toEqual({ by: 'agent', period: 'week', agent: 'beta' })
    expect(w.find('[data-testid="evaluations-agent-chip"]').text()).toContain('beta')
  })
})
