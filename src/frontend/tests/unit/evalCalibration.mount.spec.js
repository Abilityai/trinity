// @vitest-environment jsdom
/**
 * trinity-enterprise#938 C4/C5 — the judge's calibration pieces, MOUNTED.
 * Ruled 2026-10-29: enterprise, shown only where `eval_judge` is entitled.
 *
 * C4: the operator rates a run up or down on the execution page; the control
 * shows only when the rating route answers (owner-tier, person-only), saves
 * the thumb and an optional reason, and keeps a failed save beside it.
 * C5: the Evaluations tab adds "Judged runs" and "Judge–person agreement" —
 * "uncalibrated" below 20 person-rated runs, shown and never hidden — and a
 * judged-quality column carrying the same marker.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const http = vi.hoisted(() => ({ get: null, post: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    post: vi.fn((...a) => http.post(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import ExecutionRating from '../../src/components/ExecutionRating.vue'
import EvalKpiRow from '../../src/components/evaluations/EvalKpiRow.vue'
import EvalAgentTable from '../../src/components/evaluations/EvalAgentTable.vue'
import { ratingUrl } from '../../src/stores/evalJudge'
import { useEvaluationsStore } from '../../src/stores/evaluations'

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

const URL = ratingUrl('acme-scout', 'ex-1')

function mountRating(get, post) {
  http.get = vi.fn(get)
  http.post = vi.fn(post || (async (url, body) => ({ data: { rating: body.rating, reason: body.reason ?? null } })))
  setActivePinia(createPinia())
  return mount(ExecutionRating, { props: { agentName: 'acme-scout', executionId: 'ex-1' } })
}

describe('ExecutionRating (C4)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('is not shown when the route refuses this caller', async () => {
    const w = mountRating(async () => { throw { response: { status: 404 } } })
    await flush()
    expect(http.get.mock.calls[0][0]).toBe(URL)
    expect(w.find('[data-testid="execution-rating"]').exists()).toBe(false)
  })

  it('rates the run, marks the pressed thumb, and saves a reason', async () => {
    const w = mountRating(async () => ({ data: { rating: null, reason: null } }))
    await flush()
    expect(w.find('[data-testid="execution-rating-reason"]').exists()).toBe(false)
    await w.find('[data-testid="execution-rating-down"]').trigger('click')
    await flush()
    expect(http.post).toHaveBeenCalledWith(URL, { rating: 'down' })
    expect(w.find('[data-testid="execution-rating-down"]').attributes('aria-pressed')).toBe('true')
    expect(w.find('[data-testid="execution-rating-up"]').attributes('aria-pressed')).toBe('false')
    await w.find('textarea[data-testid="execution-rating-reason"]').setValue('  edited the wrong file ')
    await w.find('[data-testid="execution-rating-save-reason"]').trigger('click')
    await flush()
    expect(http.post).toHaveBeenLastCalledWith(URL, { rating: 'down', reason: 'edited the wrong file' })
  })

  it('opens with the caller\'s own earlier rating and reason', async () => {
    const w = mountRating(async () => ({ data: { rating: 'up', reason: 'clean' } }))
    await flush()
    expect(w.find('[data-testid="execution-rating-up"]').attributes('aria-pressed')).toBe('true')
    expect(w.find('textarea[data-testid="execution-rating-reason"]').element.value).toBe('clean')
  })

  it('keeps a failed save beside the control', async () => {
    const w = mountRating(async () => ({ data: { rating: null, reason: null } }),
      async () => { throw { response: { status: 500 } } })
    await flush()
    await w.find('[data-testid="execution-rating-up"]').trigger('click')
    await flush()
    expect(w.find('[data-testid="execution-rating-error"]').exists()).toBe(true)
  })
})

describe('judge tiles and column (C5)', () => {
  function storeWith(total, groups = []) {
    setActivePinia(createPinia())
    const s = useEvaluationsStore()
    s.aggregate = { by: 'agent', since: 's', until: 'u', total, groups }
    return s
  }

  it('reads "uncalibrated" below 20 person-rated runs, and the count above', async () => {
    let s = storeWith({ evaluated: 10, judged: 6, judge_human_agreement: 0.8, agreement_n: 5, per_check_fail: {} })
    const w = mount(EvalKpiRow, { props: { kpis: [...s.kpis, ...s.judgeKpis] } })
    expect(w.find('[data-testid="eval-kpi-judged"] [data-testid="eval-kpi-value"]').text()).toBe('6')
    const tile = w.find('[data-testid="eval-kpi-agreement"]')
    expect(tile.find('[data-testid="eval-kpi-value"]').text()).toBe('80.0%')
    expect(tile.find('[data-testid="eval-kpi-note"]').text()).toBe('uncalibrated · 5 of 20 person-rated runs')
    expect(s.calibration.calibrated).toBe(false)
    s = storeWith({ evaluated: 10, judged: 30, judge_human_agreement: 0.9, agreement_n: 24, per_check_fail: {} })
    const w2 = mount(EvalKpiRow, { props: { kpis: [...s.kpis, ...s.judgeKpis] } })
    expect(w2.find('[data-testid="eval-kpi-agreement"] [data-testid="eval-kpi-note"]').text()).toBe('over 24 person-rated runs')
  })

  it('a person\'s agent rows carry judged quality, marked while uncalibrated', async () => {
    const s = storeWith({ evaluated: 2, per_check_fail: {} }, [
      { key: { agent: 'acme-scout' }, evaluated: 2, judged: 2, quality_mean: 0.7, per_check_fail: {} }])
    const w = mount(EvalAgentTable, { props: { rows: s.agentRows, showQuality: true, calibrated: false } })
    const cell = w.find('[data-testid="eval-agent-quality"]')
    expect(cell.text()).toContain('3.5 / 5')
    expect(cell.text()).toContain('uncalibrated')
    const off = mount(EvalAgentTable, { props: { rows: s.agentRows } })
    expect(off.find('[data-testid="eval-agent-quality"]').exists()).toBe(false)
    expect(off.text()).not.toContain('Judged quality')
  })
})
