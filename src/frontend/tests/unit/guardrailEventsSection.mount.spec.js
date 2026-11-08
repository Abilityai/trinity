// @vitest-environment jsdom
/**
 * trinity-enterprise#165 S4 — Settings › Guardrails › "Recent events".
 *
 * Mounted over the REAL guardrail-events store with only the api client
 * stubbed, so the request the list makes and the state it renders are the
 * store's, not a mock's. What is pinned is the honest-state contract (GUARD-005,
 * review R12): silence never reads as healthy, and each kind of silence has its
 * own words —
 *
 *   reporting, zero events  → "No guardrail events in the last 24 h"
 *   never reported          → "This agent isn't reporting guardrail events (older image)"
 *   stopped reporting       → "Not reporting since <time>" (distinct from never)
 *   Codex / Gemini agent    → "Guardrail hooks run on the Claude Code runtime only"
 *   failed fetch            → LoadFailed, never any of the empty copies (#1926)
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import GuardrailEventsSection from '../../src/components/agent-settings/GuardrailEventsSection.vue'
import {
  NEVER_COPY,
  NO_EVENTS_COPY,
  NO_HOOKS_COPY,
} from '../../src/utils/guardrailEvents'

const REPORTING = { state: 'reporting', last_report_at: '2026-11-08T10:00:00.000000Z', runtime: 'claude-code', dropped_total: 0 }
const NEVER = { state: 'never', last_report_at: null, runtime: null, dropped_total: 0 }
const STALE = { state: 'stale', last_report_at: '2026-11-06T08:30:00.000000Z', runtime: 'claude-code', dropped_total: 0 }

function ev(id, over = {}) {
  return {
    event_id: id, occurred_at: '2026-11-08T09:59:00.000000Z', received_at: '2026-11-08T09:59:01.000000Z',
    kind: 'deny', hook: 'turn_tier_guard', tool: 'Bash', code: 'shell_owner_only',
    execution_id: 'exec-1', details: {}, ...over,
  }
}

function page(events, reporting, next_cursor = null) {
  return { data: { events, reporting, next_cursor } }
}

async function mountWith(response, props = {}) {
  if (response instanceof Error) api.get.mockRejectedValue(response)
  else api.get.mockResolvedValue(response)
  const w = mount(GuardrailEventsSection, { props: { agentName: 'alpha', ...props } })
  await flushPromises()
  return w
}

const text = (w) => w.text().replace(/\s+/g, ' ')

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('GuardrailEventsSection — the read it makes', () => {
  it("asks for this agent's events of the last 24 hours, through the store", async () => {
    await mountWith(page([], REPORTING))
    expect(api.get).toHaveBeenCalledTimes(1)
    const [url, opts] = api.get.mock.calls[0]
    expect(url).toBe('/api/agents/alpha/guardrail-events')
    const since = Date.parse(opts.params.since)
    expect(Math.abs(Date.now() - 24 * 3600 * 1000 - since)).toBeLessThan(60 * 1000)
    expect(opts.params.limit).toBe(50)
  })
})

describe('GuardrailEventsSection — honest states', () => {
  it('is a skeleton, not an empty claim, before the first read lands', async () => {
    api.get.mockReturnValue(new Promise(() => {}))
    const w = mount(GuardrailEventsSection, { props: { agentName: 'alpha' } })
    await flushPromises()
    expect(w.find('[data-testid="guardrail-events-loading"]').exists()).toBe(true)
    expect(w.find('[aria-busy="true"]').exists()).toBe(true)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
    expect(text(w)).not.toContain(NEVER_COPY)
  })

  it('says there were no events only when the agent is reporting', async () => {
    const w = await mountWith(page([], REPORTING))
    expect(w.find('[data-testid="guardrail-events-empty"]').text()).toBe(NO_EVENTS_COPY)
    expect(w.find('[data-testid="guardrail-events-notice"]').exists()).toBe(false)
  })

  it('says the agent is not reporting when it never has — not "no events"', async () => {
    const w = await mountWith(page([], NEVER))
    expect(w.find('[data-testid="guardrail-events-notice"]').text()).toContain(NEVER_COPY)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
  })

  it('says since when a stopped reporter has been silent, distinct from never', async () => {
    const w = await mountWith(page([], STALE))
    const notice = w.find('[data-testid="guardrail-events-notice"]')
    expect(notice.attributes('data-coverage')).toBe('stale')
    expect(notice.find('p').text()).toMatch(/^Not reporting since .*2026/)
    expect(notice.text()).not.toContain(NEVER_COPY)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
  })

  it('a stopped reporter still shows the events it sent before it went quiet', async () => {
    const w = await mountWith(page([ev('e-1')], STALE))
    expect(w.find('[data-testid="guardrail-events-notice"]').attributes('data-coverage')).toBe('stale')
    expect(w.findAll('[data-testid="guardrail-event-row"]')).toHaveLength(1)
  })

  it('says a Codex agent has no hooks, even while its keepalives read "reporting"', async () => {
    const w = await mountWith(page([], { ...REPORTING, runtime: 'codex' }), { runtime: 'codex' })
    const notice = w.find('[data-testid="guardrail-events-notice"]')
    expect(notice.attributes('data-coverage')).toBe('no_hooks')
    expect(notice.text()).toContain(NO_HOOKS_COPY)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
  })

  it('takes the runtime from the report when the page does not name one', async () => {
    const w = await mountWith(page([], { ...REPORTING, runtime: 'gemini' }))
    expect(w.find('[data-testid="guardrail-events-notice"]').attributes('data-coverage')).toBe('no_hooks')
  })

  it('a failed read is a load error, never an empty state', async () => {
    const w = await mountWith(new Error('Network Error'))
    expect(w.find('[data-testid="guardrail-events-failed"]').exists()).toBe(true)
    expect(text(w)).not.toContain(NO_EVENTS_COPY)
    expect(text(w)).not.toContain(NEVER_COPY)
    expect(text(w)).not.toContain(NO_HOOKS_COPY)
  })

  it('a failed refresh keeps the rows on screen and says so', async () => {
    const w = await mountWith(page([ev('e-1'), ev('e-2')], REPORTING))
    api.get.mockRejectedValueOnce(new Error('Network Error'))
    await w.vm.refresh()
    await flushPromises()
    expect(w.findAll('[data-testid="guardrail-event-row"]')).toHaveLength(2)
    expect(w.find('[data-testid="guardrail-events-stale"]').exists()).toBe(true)
    expect(w.find('[data-testid="guardrail-events-failed"]').exists()).toBe(false)
  })
})

describe('GuardrailEventsSection — the rows', () => {
  it('lists each event with its kind, source and time', async () => {
    const w = await mountWith(page([
      ev('e-1'),
      ev('e-2', { kind: 'credential_redacted', hook: 'output_scanner', code: 'sk', details: { patterns: ['sk'] } }),
    ], REPORTING))
    const rows = w.findAll('[data-testid="guardrail-event-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Refused')
    expect(rows[0].text()).toContain('turn_tier_guard · shell_owner_only')
    expect(rows[0].find('time').attributes('datetime')).toBe('2026-11-08T09:59:00.000000Z')
    expect(rows[0].find('time').attributes('title')).toMatch(/2026/)
    expect(rows[1].text()).toContain('Credential hidden')
  })

  it('states the total, and that only the latest are shown when there are more', async () => {
    const w = await mountWith(page([ev('e-1'), ev('e-2')], REPORTING, 'cursor-1'))
    expect(w.find('[data-testid="guardrail-events-total"]').text()).toBe('Latest 2 shown')
    const w2 = await mountWith(page([ev('e-1')], REPORTING))
    expect(w2.find('[data-testid="guardrail-events-total"]').text()).toBe('1 event')
  })

  it("never shows another agent's rows after the page switches agents", async () => {
    let resolveAlpha
    api.get.mockImplementationOnce(() => new Promise((r) => { resolveAlpha = r }))
    const w = mount(GuardrailEventsSection, { props: { agentName: 'alpha' } })
    api.get.mockResolvedValueOnce(page([], REPORTING))
    await w.setProps({ agentName: 'beta' })
    await flushPromises()
    resolveAlpha(page([ev('alpha-1')], REPORTING))
    await flushPromises()
    expect(w.findAll('[data-testid="guardrail-event-row"]')).toHaveLength(0)
    expect(w.find('[data-testid="guardrail-events-empty"]').text()).toBe(NO_EVENTS_COPY)
  })
})
