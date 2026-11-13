// @vitest-environment jsdom
/**
 * trinity-enterprise#165 S4 — the "Guardrails (24 h)" card on Operations › Health.
 *
 * Mounted over the REAL guardrail-events store, api stubbed. The card reads the
 * fleet counts and must keep silence honest across the fleet: "no events" is
 * said only when some agent is actually reporting, and the agents that are not
 * (never / stopped / no hooks) are named with their own words.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import api from '../../src/api'
import GuardrailStatsCard from '../../src/components/GuardrailStatsCard.vue'
import { useGuardrailEventsStore } from '../../src/stores/guardrailEvents'
import { NEVER_COPY, NO_EVENTS_COPY, NO_HOOKS_COPY } from '../../src/utils/guardrailEvents'

const REP = { state: 'reporting', last_report_at: '2026-11-08T10:00:00.000000Z', runtime: 'claude-code', dropped_total: 0 }

function agent(name, over = {}) {
  return { agent_name: name, runtime: 'claude-code', coverage: 'reporting', events: 0, by_kind: {}, reporting: REP, ...over }
}

function stats(agents) {
  const by_kind = {}
  const coverage = { reporting: 0, stale: 0, never: 0, no_hooks: 0 }
  for (const a of agents) {
    coverage[a.coverage] += 1
    for (const [k, n] of Object.entries(a.by_kind)) by_kind[k] = (by_kind[k] || 0) + n
  }
  return {
    data: {
      hours: 24, since: '2026-11-07T10:00:00.000000Z',
      events: agents.reduce((s, a) => s + a.events, 0), by_kind, coverage, agents,
    },
  }
}

async function mountWith(response) {
  if (response instanceof Error) api.get.mockRejectedValue(response)
  else api.get.mockResolvedValue(response)
  const w = mount(GuardrailStatsCard)
  await flushPromises()
  return w
}

const text = (w) => w.text().replace(/\s+/g, ' ')

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('GuardrailStatsCard', () => {
  it('reads the fleet counts for the last 24 hours through the store', async () => {
    await mountWith(stats([agent('a')]))
    expect(api.get).toHaveBeenCalledWith('/api/ops/guardrail-stats', { params: { hours: 24 } })
  })

  it('is titled for its window', async () => {
    const w = await mountWith(stats([agent('a')]))
    expect(w.find('h2').text()).toBe('Guardrails (24 h)')
  })

  it('shows the total and the per-kind counts', async () => {
    const w = await mountWith(stats([
      agent('loud', { events: 5, by_kind: { deny: 4, credential_redacted: 1 } }),
      agent('quiet'),
    ]))
    expect(w.find('[data-testid="guardrail-stats-total"]').text()).toBe('5')
    const kinds = w.findAll('[data-testid="guardrail-stats-kind"]').map((k) => k.text().replace(/\s+/g, ' '))
    expect(kinds).toEqual(['Refused 4', 'Credential hidden 1'])
    const rows = w.findAll('[data-testid="guardrail-stats-agent"]')
    expect(rows.map((r) => r.attributes('data-agent'))).toEqual(['loud'])
    expect(rows[0].text()).toContain('5')
  })

  it('says "no events" when agents report and nothing happened', async () => {
    const w = await mountWith(stats([agent('a'), agent('b')]))
    expect(w.find('[data-testid="guardrail-stats-empty"]').text()).toBe(NO_EVENTS_COPY)
    expect(w.find('[data-testid="guardrail-stats-total"]').text()).toBe('0')
  })

  it('never says "no events" when no agent is reporting at all', async () => {
    const w = await mountWith(stats([
      agent('old-1', { coverage: 'never', reporting: { state: 'never', last_report_at: null, runtime: null, dropped_total: 0 } }),
      agent('old-2', { coverage: 'never', reporting: { state: 'never', last_report_at: null, runtime: null, dropped_total: 0 } }),
    ]))
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
    expect(w.find('[data-testid="guardrail-stats-none-reporting"]').exists()).toBe(true)
    expect(w.find('[data-testid="guardrail-stats-total"]').text()).toBe('—')
  })

  it('names each agent that is not reporting, with its own words', async () => {
    const w = await mountWith(stats([
      agent('fine'),
      agent('old', { coverage: 'never', reporting: { state: 'never', last_report_at: null, runtime: null, dropped_total: 0 } }),
      agent('quiet', { coverage: 'stale', reporting: { ...REP, state: 'stale', last_report_at: '2026-11-06T08:30:00.000000Z' } }),
      agent('cx', { runtime: 'codex', coverage: 'no_hooks' }),
    ]))
    const rows = Object.fromEntries(w.findAll('[data-testid="guardrail-stats-agent"]')
      .map((r) => [r.attributes('data-agent'), r]))
    expect(Object.keys(rows).sort()).toEqual(['cx', 'old', 'quiet'])
    expect(rows.old.text()).toContain(NEVER_COPY)
    expect(rows.quiet.text()).toMatch(/Not reporting since .*2026/)
    expect(rows.quiet.text()).not.toContain(NEVER_COPY)
    expect(rows.cx.text()).toContain(NO_HOOKS_COPY)
    // The tally line counts them, and "no events" still holds for the reporting one.
    const tally = w.find('[data-testid="guardrail-stats-coverage"]').text().replace(/\s+/g, ' ')
    expect(tally).toContain('1 reporting')
    expect(tally).toContain('1 stopped reporting')
    expect(tally).toContain('1 not reporting')
    expect(tally).toContain('1 no hooks')
  })

  it('a failed first read is a load error, not an empty card', async () => {
    const w = await mountWith(new Error('Network Error'))
    expect(w.find('[data-testid="guardrail-stats-failed"]').exists()).toBe(true)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
  })

  it('a failed refresh keeps the counts and raises the stale banner', async () => {
    const w = await mountWith(stats([agent('loud', { events: 2, by_kind: { deny: 2 } })]))
    api.get.mockRejectedValueOnce(new Error('Network Error'))
    await useGuardrailEventsStore().loadFleet()
    await flushPromises()
    expect(w.find('[data-testid="guardrail-stats-total"]').text()).toBe('2')
    expect(w.find('[data-testid="guardrail-stats-stale"]').exists()).toBe(true)
  })

  it('a refresh never re-flashes the skeleton', async () => {
    const w = await mountWith(stats([agent('a')]))
    api.get.mockReturnValueOnce(new Promise(() => {}))
    useGuardrailEventsStore().loadFleet()
    await flushPromises()
    expect(w.find('[data-testid="guardrail-stats-loading"]').exists()).toBe(false)
    expect(w.find('[data-testid="guardrail-stats-empty"]').exists()).toBe(true)
  })

  it('an instance with no agents says so', async () => {
    const w = await mountWith(stats([]))
    expect(w.find('[data-testid="guardrail-stats-no-agents"]').exists()).toBe(true)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
  })
})
