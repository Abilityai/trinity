// @vitest-environment jsdom
/**
 * ent#1175 AC 3 — when every step is done the card collapses to one line
 * (it does not disappear), and the five rows are gone.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalProgressCard from '../../src/components/portal/PortalProgressCard.vue'

const KEYS = ['assistant_ready', 'trinity_being_prepared', 'trinity_ready', 'first_agent_built', 'first_run']
const steps = (state) => KEYS.map((key) => ({ key, label: key, state, note: null }))

describe('PortalProgressCard — collapsed when all done', () => {
  it('renders one line, not the five rows, once every step is done', () => {
    const w = mount(PortalProgressCard, { props: { steps: steps('done'), active: true, allDone: true } })
    const line = w.find('[data-testid="portal-progress-card-collapsed"]')
    expect(line.exists()).toBe(true)
    expect(line.text()).toContain("You're all set up")
    expect(line.text()).toContain('5 of 5')
    expect(w.find('[data-testid="portal-progress-card"]').exists()).toBe(false)
    expect(w.findAll('li')).toHaveLength(0)
  })

  it('shows the full card while a step remains', () => {
    const s = steps('done'); s[4].state = 'in_progress'
    const w = mount(PortalProgressCard, { props: { steps: s, active: true, allDone: false } })
    expect(w.find('[data-testid="portal-progress-card"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-progress-card-collapsed"]').exists()).toBe(false)
    expect(w.findAll('li')).toHaveLength(5)
  })

  it('renders nothing on an OSS build (inactive)', () => {
    const w = mount(PortalProgressCard, { props: { steps: [], active: false, allDone: false } })
    expect(w.html()).not.toContain('data-testid')
  })
})
