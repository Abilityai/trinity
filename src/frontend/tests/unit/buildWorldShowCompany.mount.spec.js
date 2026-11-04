// @vitest-environment jsdom
/**
 * A level moment lights the Levels card on Your company, so the page can bring the right
 * side back to Your company from an agent it had zoomed into, and put it back after.
 */
import { describe, it, expect } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PortalBuildWorld from '@/components/portal/builder/PortalBuildWorld.vue'
import { normalizeState, worldView } from '@/components/portal/builder/portalBuild'
import { LEVELS } from '@/components/portal/builder/levels'

const state = normalizeState({
  schema: 'trinity-build/v1', stage: 'build', company: { name: 'Acme Freight' },
  agents: [{ id: 'filler', name: 'Game filler', status: 'built', skills: [], connections: [] }],
  focus: { agent: 'filler' },
})
globalThis.ResizeObserver ||= class { observe() {} unobserve() {} disconnect() {} }
const levels = LEVELS.map((l) => ({ ...l, reached: l.id === 1, skipped: false }))

describe('showing Your company for a level moment', () => {
  it('leaves the agent it followed, shows the Levels card, then goes back', async () => {
    const w = mount(PortalBuildWorld, { props: { world: worldView(state, { done: true }), intake: { done: true }, levels } })
    await flushPromises()
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
    const restore = w.vm.showCompany()
    await flushPromises()
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(true)
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    restore()
    await flushPromises()
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
  })

  it('holds on Your company while the guide moves on, then goes where the guide went', async () => {
    const two = normalizeState({ ...state, agents: [...state.agents, { id: 'reporter', name: 'Match reporter', status: 'designing', skills: [], connections: [] }] })
    const w = mount(PortalBuildWorld, { props: { world: worldView(two, { done: true }), intake: { done: true }, levels } })
    await flushPromises()
    const restore = w.vm.showCompany()
    await w.setProps({ world: worldView(normalizeState({ ...two, focus: { agent: 'reporter' } }), { done: true }) })
    await flushPromises()
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    restore()
    await flushPromises()
    expect(w.text()).toContain('Match reporter')
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
  })

  it('from Findings too', async () => {
    const w = mount(PortalBuildWorld, { props: { world: worldView(state, { done: true }), intake: { done: true }, levels } })
    await w.findAll('button').find((b) => b.text().startsWith('Findings')).trigger('click')
    expect(w.find('[data-testid="build-findings"]').exists()).toBe(true)
    w.vm.showCompany()
    await flushPromises()
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(true)
  })
})
