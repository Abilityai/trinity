// @vitest-environment jsdom
/**
 * #3001 — the Work card never says "<agent> doesn't report steps".
 *
 * The sentence was ruled for ent#525, before #620 put the agent's live activity
 * line on the same card; beside "Thinking" / "Reading …" it read as the card
 * contradicting itself, and before the first line it read as a fault. Ruled
 * (2026-10-01): remove it outright. A running card shows the live activity
 * line, or the stages when the agent publishes them, or nothing. "Steps could
 * not be read right now." stays — it reports a real failure.
 *
 * Mounted (#2918): what matters is what the card renders, including the chat's
 * reserved one-line row (`reserveLiveRows`, #2964), which must stay — blank —
 * so the card keeps its shape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalWorkCard from '@/components/portal/PortalWorkCard.vue'
import { stepsLine } from '@/components/portal/portalWork'

function item(over = {}) {
  return {
    id: 'e1', agent_name: 'acme-analyst', kind: 'turn', status: 'running', outcome: 'running',
    title: '/board-prep', started_at: new Date().toISOString(), stale: false,
    steps: { state: 'none', stages: [] },
    ...over,
  }
}

const SENTENCE = "doesn't report steps"
beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('stepsLine (#3001)', () => {
  it('says nothing for an agent that publishes no stages', () => {
    expect(stepsLine({ state: 'none' })).toEqual({ kind: 'none', text: '' })
    expect(stepsLine({ state: 'reported', stages: [] })).toEqual({ kind: 'none', text: '' })
  })
  it('keeps "could not be read" — a real failure', () => {
    expect(stepsLine({ state: 'unknown' }).text).toBe('Steps could not be read right now.')
  })
})

describe('PortalWorkCard (#3001)', () => {
  it.each([
    ['before the first activity line', null],
    ['beside a live activity line', 'Thinking'],
  ])('never says it %s', async (_label, liveStep) => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep } })
    await w.vm.$nextTick()
    expect(w.text()).not.toContain(SENTENCE)
    expect(w.find('[data-testid="portal-work-steps-none"]').exists()).toBe(false)
  })

  it('not between heartbeats either', async () => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep: 'Thinking' } })
    await w.setProps({ liveStep: null })
    vi.advanceTimersByTime(2000)
    await w.vm.$nextTick()
    expect(w.text()).not.toContain(SENTENCE)
  })

  it('still says "could not be read" when the steps are unreadable', () => {
    const w = mount(PortalWorkCard, { props: { item: item({ steps: { state: 'unknown' } }), liveStep: null } })
    expect(w.get('[data-testid="portal-work-steps-unknown"]').text()).toBe('Steps could not be read right now.')
  })

  it('in the chat card, keeps the reserved row — blank, aria-hidden, one line high', async () => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep: null, reserveLiveRows: true } })
    await w.vm.$nextTick()
    const row = w.get('[data-testid="portal-work-reserved-steps"]')
    expect(row.text()).toBe('')
    expect(row.attributes('aria-hidden')).toBe('true')
    expect(row.classes()).toContain('h-4')
    expect(w.text()).not.toContain(SENTENCE)
  })
})
