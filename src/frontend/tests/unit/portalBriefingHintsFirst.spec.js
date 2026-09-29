// @vitest-environment jsdom
/**
 * The empty chat shows the agent's static hints ("Things you can ask") ABOVE
 * the dynamic suggestions ("Suggested for you").
 *
 * The hints arrive with the briefing, under a skeleton that already holds
 * their footprint. Suggestions are their own fetch, 0–3 rows, dismissible —
 * placed above the hints, every arrival or dismissal moved the grid the reader
 * was looking at. The suggestions now come through `PortalBriefing`'s
 * `after-hints` slot, rendered after the hint grid. MOUNTED: this is a DOM
 * order, so only a render proves it.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { h } from 'vue'
import PortalBriefing from '@/components/portal/PortalBriefing.vue'

const agent = (over = {}) => ({
  name: 'corbin',
  description: 'Business management assistant.',
  playbooks: [{ title: 'Add backlog', description: 'Adds a backlog' }, { title: 'Add canon' }],
  ...over,
})

const suggestions = () => h('section', { 'data-testid': 'slotted-suggestions' }, 'Suggested for you')

function orderOf(w) {
  const html = w.html()
  return {
    hints: html.indexOf('Things you can ask'),
    suggestions: html.indexOf('data-testid="slotted-suggestions"'),
  }
}

describe('PortalBriefing — static hints before dynamic suggestions', () => {
  it('renders "Things you can ask" before the after-hints slot', () => {
    const w = mount(PortalBriefing, { props: { agent: agent() }, slots: { 'after-hints': suggestions } })
    const { hints, suggestions: sugg } = orderOf(w)
    expect(hints).toBeGreaterThan(-1)
    expect(sugg).toBeGreaterThan(hints)
  })

  it('still renders the suggestions when the agent has no hints', () => {
    const w = mount(PortalBriefing, { props: { agent: agent({ playbooks: [] }) }, slots: { 'after-hints': suggestions } })
    expect(w.find('[data-testid="slotted-suggestions"]').exists()).toBe(true)
  })

  it('renders neither while the briefing is pending (the skeleton holds the zone)', () => {
    const w = mount(PortalBriefing, {
      props: { agent: agent({ briefing_state: 'pending' }) },
      slots: { 'after-hints': suggestions },
    })
    expect(w.find('[data-testid="slotted-suggestions"]').exists()).toBe(false)
    expect(w.text()).not.toContain('Things you can ask')
  })

  it('the old before-hints slot is gone, so a host cannot put them back on top', () => {
    const w = mount(PortalBriefing, { props: { agent: agent() }, slots: { 'before-hints': suggestions } })
    expect(w.find('[data-testid="slotted-suggestions"]').exists()).toBe(false)
  })
})
