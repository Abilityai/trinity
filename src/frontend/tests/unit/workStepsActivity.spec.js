// @vitest-environment jsdom
/**
 * #3001 — a live Work card must not say "<agent> doesn't report steps" while it
 * shows (or has shown this run) that agent's live activity line (#620).
 *
 * The #919 read's `none` is accurate — the agent publishes no pipeline — but
 * the sentence was ruled for ent#525, before the activity row existed on the
 * same card. Mounted (#2918): the rule lives where the card composes the two,
 * and the "stays suppressed between beats" half is a behaviour over time that
 * a regex over the SFC cannot see.
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

const NONE = '[data-testid="portal-work-steps-none"]'

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('stepsLine (#3001)', () => {
  it('says nothing about steps once the agent has reported live activity', () => {
    expect(stepsLine({ state: 'none' }, 'scout', { activitySeen: true })).toEqual({ kind: 'activity', text: '' })
  })
  it('keeps the ruled sentence for an agent that reports neither stages nor activity', () => {
    expect(stepsLine({ state: 'none' }, 'scout', { activitySeen: false }))
      .toEqual({ kind: 'none', text: "scout doesn't report steps." })
    expect(stepsLine({ state: 'none' }, 'scout')).toEqual({ kind: 'none', text: "scout doesn't report steps." })
  })
  it('leaves "could not be read" alone — it stays true while activity shows', () => {
    expect(stepsLine({ state: 'unknown' }, 'scout', { activitySeen: true }).kind).toBe('unknown')
  })
})

describe('PortalWorkCard (#3001)', () => {
  it('shows the sentence when there is no live line at all', () => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep: null } })
    expect(w.find(NONE).text()).toBe("acme-analyst doesn't report steps.")
  })

  it('never shows it beside a live activity line', async () => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep: 'Thinking' } })
    expect(w.get('[data-testid="portal-work-step"]').text()).toBe('Thinking')
    expect(w.find(NONE).exists()).toBe(false)
  })

  it('stays suppressed between heartbeats, and resets for the next run', async () => {
    const w = mount(PortalWorkCard, { props: { item: item(), liveStep: 'Thinking' } })
    await w.setProps({ liveStep: null })          // the beat expired
    vi.advanceTimersByTime(2000)
    await w.vm.$nextTick()
    expect(w.find(NONE).exists()).toBe(false)
    await w.setProps({ item: item({ status: 'success', outcome: 'success' }) })  // terminal
    await w.setProps({ item: item({ id: 'e2' }), liveStep: null })               // a new run
    await w.vm.$nextTick()
    expect(w.find(NONE).exists()).toBe(true)
  })
})
