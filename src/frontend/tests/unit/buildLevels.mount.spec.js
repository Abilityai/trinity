// @vitest-environment jsdom
/**
 * The levels card on the Company page: seven levels, the next one explained with one
 * button that asks the guide for help, Level 3 (keys) shown as not needed when no agent
 * needs keys, and a short celebration when one is reached.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalBuildLevels from '@/components/portal/builder/PortalBuildLevels.vue'
import { LEVELS } from '@/components/portal/builder/levels'

const withState = (reached, skipped = []) => LEVELS.map((l) => ({ ...l, reached: reached.includes(l.id), skipped: skipped.includes(l.id), evidence: [] }))
const card = (props = {}) => mount(PortalBuildLevels, { props: { levels: withState([1]), celebrate: null, guideLabel: 'Atlas', ...props } })

describe('the levels card', () => {
  it('shows the seven levels and how many are done', () => {
    const w = card()
    expect(w.get('[data-testid="build-levels"]').text()).toContain('Your levels')
    expect(w.get('[data-testid="build-levels-count"]').text()).toBe('1 of 7')
    expect(w.get('[data-testid="build-level-1"]').attributes('data-reached')).toBe('true')
    expect(w.get('[data-testid="build-level-1"]').text()).toContain('Reached')
    expect(w.get('[data-testid="build-level-2"]').attributes('data-reached')).toBe('false')
    for (const level of LEVELS) expect(w.get(`[data-testid="build-level-${level.id}"]`).text()).toContain(level.name)
  })

  it('explains the next level, with one button that asks the guide', async () => {
    const w = card()
    const next = w.get('[data-testid="build-level-next"]')
    expect(next.text()).toContain(LEVELS[1].means)
    expect(next.text()).toContain(LEVELS[1].why)
    expect(w.findAll('[data-testid="build-level-ask"]')).toHaveLength(1)
    const ask = w.get('[data-testid="build-level-ask"]')
    expect(ask.text()).toBe('Do it with Atlas')
    await ask.trigger('click')
    expect(w.emitted('ask')).toEqual([[LEVELS[1].prompt]])
  })

  it('shows Level 3 as not needed when no agent needs keys, counts it, and moves on', () => {
    const w = card({ levels: withState([1, 2], [3]) })
    const row = w.get('[data-testid="build-level-3"]')
    expect(row.attributes('data-skipped')).toBe('true')
    expect(row.text()).toContain('Not needed')
    expect(row.text()).not.toContain('Reached')
    expect(w.get('[data-testid="build-levels-count"]').text()).toBe('3 of 7')
    expect(w.get('[data-testid="build-level-next"]').text()).toContain(LEVELS[3].means)
  })

  it('with every level done, has no next level', () => {
    const w = card({ levels: withState([1, 2, 4, 5, 6, 7], [3]) })
    expect(w.get('[data-testid="build-levels-count"]').text()).toBe('7 of 7')
    expect(w.find('[data-testid="build-level-next"]').exists()).toBe(false)
    expect(w.find('[data-testid="build-level-ask"]').exists()).toBe(false)
  })

  it('celebrates a level once it is reached, and can be closed', async () => {
    const w = card({ levels: withState([1, 4]), celebrate: { ...LEVELS[3], reached: true } })
    const banner = w.get('[data-testid="build-level-celebrate"]')
    expect(banner.attributes('role')).toBe('status')
    expect(banner.text()).toContain('Level 4 reached: In control')
    expect(banner.text()).toContain(LEVELS[3].means)
    await w.get('[data-testid="build-level-celebrate-dismiss"]').trigger('click')
    expect(w.emitted('dismiss')).toHaveLength(1)
  })

  it('names the person\'s own guide on the button', () => {
    const w = card({ guideLabel: 'Build Guide' })
    expect(w.get('[data-testid="build-level-ask"]').text()).toBe('Do it with Build Guide')
  })
})
