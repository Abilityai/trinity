// @vitest-environment jsdom
/**
 * trinity-enterprise#935 — the Tier-0 result on the execution page, MOUNTED.
 *
 * The panel reads `GET /api/agents/{name}/executions/{id}/evaluation` (the
 * B.2 route) and shows ONE headline, derived from the GATE checks only (TD-7):
 * a failed gate fails the run, skips never count against it, and signals are
 * listed as recorded observations — never worded or coloured as pass/fail.
 * A 404 `not_evaluated` is a state of the run, not an error of the page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

// Both the shared api client and the view's raw axios resolve to this
// instance (`axios.create` returns it), as in executionGateMarker.mount.spec.js.
const routes = vi.hoisted(() => ({ get: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => routes.get(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { name: 'fin', executionId: 'exec-1' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}))

import ExecutionChecks from '../../src/components/ExecutionChecks.vue'
import ExecutionDetail from '../../src/views/ExecutionDetail.vue'

const EVAL_URL = '/api/agents/fin/executions/exec-1/evaluation'

const check = (id, tier, status, over = {}) => ({
  id, tier, source: 'platform', status, evidence: {}, ...over,
})

function evaluation(checks, headline) {
  return {
    id: 'ev-1', agent_name: 'fin', execution_id: 'exec-1', evaluator: 'platform:tier0',
    completion: true, created_at: '2026-10-18T04:00:00Z',
    checks: { schema: 1, evaluated_at: '2026-10-18T04:00:00Z', headline, checks },
  }
}

const notFound = () => {
  const e = new Error('Request failed with status code 404')
  e.response = { status: 404, data: { detail: 'not_evaluated' } }
  return e
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

async function panel(get) {
  routes.get = vi.fn(get)
  const w = mount(ExecutionChecks, { props: { agentName: 'fin', executionId: 'exec-1' } })
  await flush()
  return w
}

const headlineOf = (w) => w.find('[data-testid="execution-checks-headline"]')

describe('ExecutionChecks — the gate-derived headline', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('reads the B.2 route for this agent and execution', async () => {
    await panel(async () => ({ data: evaluation([], { passed: true, gate_pass: 0, gate_fail: 0, gate_skip: 0 }) }))
    expect(routes.get.mock.calls.map(([u]) => u)).toEqual([EVAL_URL])
  })

  it('pass: no gate failed — "Checks passed", counted over gates only', async () => {
    const w = await panel(async () => ({ data: evaluation([
      check('execution_exists', 'gate', 'pass'),
      check('credential_echo', 'gate', 'pass'),
      check('validate_001', 'gate', 'skip', { reason: 'not_run_yet' }),
      // A failed SIGNAL never fails the run and is never counted.
      check('budget_vs_baseline', 'signal', 'fail', { reason: 'outside_baseline' }),
    ], { passed: true, gate_pass: 2, gate_fail: 0, gate_skip: 1 }) }))
    expect(headlineOf(w).text()).toBe('Checks passed')
    expect(headlineOf(w).attributes('data-verdict')).toBe('pass')
    expect(w.find('[data-testid="execution-checks-count"]').text()).toBe('2 of 2 gate checks passed · 1 skipped')
    expect(w.find('[data-testid="execution-checks-failed"]').exists()).toBe(false)
  })

  it('fail: one failed gate fails the run and is listed with its reason and evidence', async () => {
    const w = await panel(async () => ({ data: evaluation([
      check('execution_exists', 'gate', 'pass'),
      check('deliverable_landed', 'gate', 'fail', { reason: 'claimed_without_receipt', evidence: { receipts: 0 } }),
      check('budget_vs_baseline', 'signal', 'pass'),
    ], { passed: false, gate_pass: 1, gate_fail: 1, gate_skip: 0 }) }))
    expect(headlineOf(w).text()).toBe('Checks failed')
    expect(headlineOf(w).attributes('data-verdict')).toBe('fail')
    expect(w.find('[data-testid="execution-checks-count"]').text()).toBe('1 of 2 gate checks failed')
    const failed = w.findAll('[data-testid="execution-checks-failed"] li')
    expect(failed).toHaveLength(1)
    expect(failed[0].text()).toContain('deliverable_landed')
    expect(failed[0].text()).toContain('claimed without receipt')
    expect(failed[0].text()).toContain('receipts: 0')
  })

  it('skip: every gate skipped (e.g. an evaluator timeout) is neither a pass nor a fail', async () => {
    const w = await panel(async () => ({ data: evaluation([
      check('execution_exists', 'gate', 'skip', { reason: 'evaluator_timeout' }),
      check('deliverable_landed', 'gate', 'skip', { reason: 'evaluator_timeout' }),
    ], { passed: true, gate_pass: 0, gate_fail: 0, gate_skip: 2 }) }))
    expect(headlineOf(w).text()).toBe('No gate check applied')
    expect(headlineOf(w).attributes('data-verdict')).toBe('skip')
    expect(w.find('[data-testid="execution-checks-count"]').text()).toBe('2 skipped')
  })

  it('a row without a stored headline is derived from its gate entries, signals excluded', async () => {
    const w = await panel(async () => ({ data: evaluation([
      check('execution_exists', 'gate', 'pass'),
      check('budget_vs_baseline', 'signal', 'fail', { reason: 'outside_baseline' }),
    ], undefined) }))
    expect(headlineOf(w).attributes('data-verdict')).toBe('pass')
    expect(w.find('[data-testid="execution-checks-count"]').text()).toBe('1 of 1 gate checks passed')
  })

  it('signals are listed as observations, never worded or badged as pass/fail', async () => {
    const w = await panel(async () => ({ data: evaluation([
      check('execution_exists', 'gate', 'pass'),
      check('budget_vs_baseline', 'signal', 'fail', { reason: 'outside_baseline' }),
    ], { passed: true, gate_pass: 1, gate_fail: 0, gate_skip: 0 }) }))
    const signals = w.find('[data-testid="execution-checks-signals"]')
    expect(signals.text()).toContain('not counted')
    expect(signals.text()).toContain('budget_vs_baseline')
    expect(signals.text()).toContain('outside baseline')
    expect(signals.text()).not.toMatch(/\b(pass|passed|fail|failed)\b/i)
    expect(signals.find('[data-verdict]').exists()).toBe(false)
  })

  it('404 not_evaluated renders "Not evaluated", not an error', async () => {
    const w = await panel(async () => { throw notFound() })
    expect(headlineOf(w).text()).toBe('Not evaluated')
    expect(headlineOf(w).attributes('data-verdict')).toBe('none')
    expect(w.find('[data-testid="execution-checks-error"]').exists()).toBe(false)
  })

  it('any other failure says the checks could not be loaded — never "Not evaluated"', async () => {
    const w = await panel(async () => {
      const e = new Error('boom'); e.response = { status: 500, data: {} }; throw e
    })
    expect(w.find('[data-testid="execution-checks-error"]').exists()).toBe(true)
    expect(headlineOf(w).exists()).toBe(false)
  })
})

describe('wired into the execution page', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
  })

  const run = (over = {}) => ({
    id: 'exec-1', schedule_id: 's1', agent_name: 'fin', status: 'success', message: '/digest',
    triggered_by: 'schedule', started_at: '2026-10-18T03:00:00Z', completed_at: '2026-10-18T03:01:00Z',
    duration_ms: 60000, response: 'done', error: null, ...over,
  })

  it('a finished run shows its Tier-0 headline', async () => {
    routes.get = vi.fn(async (url) => {
      if (url === EVAL_URL) {
        return { data: evaluation([check('execution_exists', 'gate', 'fail', { reason: 'no_trace' })],
          { passed: false, gate_pass: 0, gate_fail: 1, gate_skip: 0 }) }
      }
      if (url.endsWith('/executions/exec-1')) return { data: run() }
      return { data: {} }
    })
    const w = mount(ExecutionDetail, { global: { stubs: { 'router-link': true } } })
    await flush()
    expect(headlineOf(w).text()).toBe('Checks failed')
    w.unmount()
  })

  it('a running run asks for no grade yet', async () => {
    routes.get = vi.fn(async (url) => {
      if (url.endsWith('/executions/exec-1')) return { data: run({ status: 'running', completed_at: null }) }
      return { data: {} }
    })
    const w = mount(ExecutionDetail, { global: { stubs: { 'router-link': true } } })
    await flush()
    expect(routes.get.mock.calls.some(([u]) => u === EVAL_URL)).toBe(false)
    expect(w.find('[data-testid="execution-checks"]').exists()).toBe(false)
    w.unmount()
  })
})
