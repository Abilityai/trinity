// @vitest-environment jsdom
/**
 * trinity-enterprise#939 — Agent Overview › Behavior, MOUNTED.
 *
 * Six indicators, each its own measure with its sample; a thin sample reads
 * "Not enough runs", never 0%; a click on a tile opens the runs behind it
 * grouped by source; the fix-path table links to the skill / schedule; the
 * window select refetches; a failed first load is LoadFailed. The Workspace
 * block renders tones as sentences, no numbers.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const http = vi.hoisted(() => ({ get: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    post: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import AgentBehavior from '../../src/components/indicators/AgentBehavior.vue'
import PortalAgentBehavior from '../../src/components/portal/PortalAgentBehavior.vue'
import { useClientPortalStore } from '../../src/stores/clientPortal'

const IND = {
  agent_name: 'scout', days: 14, since: 's', until: 'u', min_runs: 5, runs: 42, truncated: false,
  latest_run_at: new Date(Date.now() - 6 * 60000).toISOString(),
  indicators: [
    { id: 'completion', state: 'ok', value: 0.88, num: 37, n: 42, delta: 4 },
    { id: 'checks_passed', state: 'ok', value: 0.74, num: 31, n: 42, delta: -9 },
    { id: 'scope_fidelity', state: 'ok', value: 0.95, num: 19, n: 20, delta: 0 },
    { id: 'escalation_correctness', state: 'insufficient', value: null, num: 3, n: 3, delta: null },
    { id: 'quality', state: 'ok', value: 0.82, n: 20, agreement: 0.81, agreement_n: 16, delta: null },
    { id: 'cost_vs_baseline', state: 'ok', value: 1.3, n: 42, median: 0.42, baseline: 0.32, baseline_n: 30, delta: null },
  ],
  fix_paths: [
    { check: 'deliverable_landed', judge: false, source: { kind: 'playbook', ref: 'lead-research' }, failed: 9 },
    { check: 'deliverable_landed', judge: false, source: { kind: 'schedule', ref: 's1', label: 'Weekly pipeline digest' }, failed: 2 },
    { check: 'scope_fidelity', judge: true, source: { kind: 'chat', ref: null }, failed: 1 },
  ],
}
const RUNS = {
  agent_name: 'scout', indicator: 'checks_passed', check: null, days: 14, since: 's', until: 'u', truncated: false,
  groups: [
    { source: { kind: 'playbook', ref: 'lead-research' }, runs: 14, failed: 9,
      items: [{ execution_id: 'a1f3000000009c', completed_at: '2026-10-20T09:00:00Z', reasons: ['deliverable_landed:claim_without_receipt'], cost: 0.51 }] },
  ],
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

function mountIt() {
  return mount(AgentBehavior, {
    props: { agentName: 'scout' },
    global: { stubs: { 'router-link': { template: '<a><slot /></a>' } } },
  })
}

describe('AgentBehavior', () => {
  let calls
  beforeEach(() => {
    setActivePinia(createPinia())
    calls = []
    http.get = vi.fn(async (url, opts) => {
      calls.push({ url, params: opts?.params })
      if (url.endsWith('/runs')) return { data: RUNS }
      return { data: IND }
    })
  })

  it('renders each indicator with its sample, and a thin one as not enough runs', async () => {
    const w = mountIt()
    await flush()
    expect(calls[0].url).toBe('/api/agents/scout/evaluations/indicators')
    expect(calls[0].params).toEqual({ days: 14 })
    const tile = (id) => w.find(`[data-testid="behavior-tile-${id}"]`)
    expect(tile('completion').text()).toContain('88%')
    expect(tile('completion').text()).toContain('37 of 42 runs finished the task')
    expect(tile('checks_passed').text()).toContain('▼ 9 pts')
    const esc = tile('escalation_correctness')
    expect(esc.find('[data-testid="behavior-tile-value"]').text()).toBe('—')
    expect(esc.text()).toContain('Not enough runs: 3 judged, needs 5')
    expect(esc.element.tagName).toBe('BUTTON') // 3 runs still listable
    expect(tile('quality').text()).toContain('4.1 / 5')
    expect(tile('cost_vs_baseline').text()).toContain('1.3×')
    expect(tile('cost_vs_baseline').element.tagName).toBe('DIV') // nothing to drill into
    expect(w.text()).not.toMatch(/overall|score:/i)
  })

  it('a tile click opens the runs grouped by source, and back returns', async () => {
    const w = mountIt()
    await flush()
    await w.find('[data-testid="behavior-tile-checks_passed"]').trigger('click')
    await flush()
    expect(calls.at(-1).url).toBe('/api/agents/scout/evaluations/indicators/checks_passed/runs')
    const group = w.find('[data-testid="behavior-drill-group"]')
    expect(group.text()).toContain('skill /lead-research')
    expect(group.text()).toContain('9 of 14 runs failed this check')
    expect(group.text()).toContain('no record of the result landing')
    await w.find('[data-testid="behavior-drill-back"]').trigger('click')
    await flush()
    expect(w.find('[data-testid="behavior-tiles"]').exists()).toBe(true)
  })

  it('fix paths name the source and narrow the drill-down to the check', async () => {
    const w = mountIt()
    await flush()
    const rows = w.findAll('[data-testid="behavior-fix-path"]')
    expect(rows).toHaveLength(3)
    expect(rows[1].text()).toContain('schedule “Weekly pipeline digest”')
    await rows[0].find('button').trigger('click')
    expect(w.emitted('navigate-tab')[0]).toEqual(['skills'])
    await w.find('[data-testid="behavior-fix-runs-deliverable_landed"]').trigger('click')
    await flush()
    expect(calls.at(-1).params).toEqual({ days: 14, check: 'deliverable_landed' })
  })

  it('a removed schedule is named as such, with no dead fix link', async () => {
    const data = JSON.parse(JSON.stringify(IND))
    data.fix_paths = [{ check: 'deliverable_landed', judge: false, source: { kind: 'schedule', ref: 'gone', label: null }, failed: 1 }]
    http.get = vi.fn(async () => ({ data }))
    const w = mountIt()
    await flush()
    const row = w.find('[data-testid="behavior-fix-path"]')
    expect(row.text()).toContain('a removed schedule')
    expect(row.text()).not.toContain('gone')
    expect(row.text()).not.toContain('Open the schedule')
  })

  it('the window select refetches with the new window', async () => {
    const w = mountIt()
    await flush()
    await w.find('select[data-testid="behavior-window"]').setValue('30')
    await flush()
    expect(calls.at(-1).params).toEqual({ days: 30 })
  })

  it('a failed first load is LoadFailed, not an empty grid', async () => {
    http.get = vi.fn(async () => { throw Object.assign(new Error('x'), { response: { status: 500, data: {} } }) })
    const w = mountIt()
    await flush()
    expect(w.text()).toContain("Couldn't load behavior")
    expect(w.find('[data-testid="behavior-tiles"]').exists()).toBe(false)
  })
})

describe('PortalAgentBehavior', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders tones as plain sentences with the task count, no figures', async () => {
    const store = useClientPortalStore()
    store.roleAgent = 'scout'
    store.role = { agent_name: 'scout', role: null, behavior: { days: 14, runs: 42, lines: [
      { indicator: 'completion', tone: 'good' },
      { indicator: 'checks_passed', tone: 'watch' },
      { indicator: 'escalation_correctness', tone: 'unknown' },
    ] } }
    const w = mount(PortalAgentBehavior, { props: { agentName: 'scout' } })
    expect(w.text()).toContain('Based on 42 tasks over the last two weeks')
    expect(w.find('[data-testid="portal-behavior-checks_passed"]').attributes('data-tone')).toBe('watch')
    expect(w.text()).toContain('before the result is actually there')
    expect(w.text()).not.toMatch(/%|\d\.\d/)
  })

  it('no runs says so; no behavior renders nothing', async () => {
    const store = useClientPortalStore()
    store.roleAgent = 'scout'
    store.role = { agent_name: 'scout', role: null, behavior: { days: 14, runs: 0, lines: [] } }
    const w = mount(PortalAgentBehavior, { props: { agentName: 'scout' } })
    expect(w.find('[data-testid="portal-behavior-none"]').exists()).toBe(true)
    store.role = { agent_name: 'scout', role: null, behavior: null }
    await nextTick()
    expect(w.find('[data-testid="portal-agent-behavior"]').exists()).toBe(false)
  })
})
