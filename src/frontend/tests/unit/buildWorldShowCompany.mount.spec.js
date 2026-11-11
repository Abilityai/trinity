// @vitest-environment jsdom
/**
 * While an agent is being built the right side shows You, where the levels are, so the
 * person sees where they are as they go. A level moment lights the Levels card there: the
 * page brings the right side to it from wherever they had gone, and puts it back after.
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

const mountWorld = (st = state) => mount(PortalBuildWorld, { props: { world: worldView(st, { done: true }), intake: { done: true }, levels } })
const openCompany = (w) => w.findAll('button').find((b) => b.text().startsWith('Your company')).trigger('click')

describe('the right side while an agent is being built', () => {
  it('shows You, with the levels, once the guide is working on an agent', async () => {
    const w = mountWorld()
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(true)
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
  })

  it('shows Your company before any agent is being built', async () => {
    const w = mountWorld(normalizeState({ ...state, agents: [], focus: {} }))
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(false)
  })

  it('comes to You when the guide starts on an agent, and Your company then opens on that agent', async () => {
    const w = mountWorld(normalizeState({ ...state, focus: {} }))
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(false)
    await w.setProps({ world: worldView(state, { done: true }) })
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(true)
    await openCompany(w)
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
    expect(w.text()).toContain('Game filler')
  })

  it('leaves someone reading Your company there when the page rereads the same agent', async () => {
    const w = mountWorld()
    await flushPromises()
    await openCompany(w)
    await w.setProps({ world: worldView(normalizeState({ ...state, stage: 'live' }), { done: true }) })
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(false)
  })
})

describe('showing You for a level moment', () => {
  it('leaves the agent they were looking at, shows the Levels card, then goes back', async () => {
    const w = mountWorld()
    await flushPromises()
    await openCompany(w)
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
    const restore = w.vm.showLevels()
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(true)
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    restore()
    await flushPromises()
    expect(w.find('[data-testid="build-you"]').exists()).toBe(false)
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
  })

  it('holds on You while the guide moves on, and Your company then opens where the guide went', async () => {
    const two = normalizeState({ ...state, agents: [...state.agents, { id: 'reporter', name: 'Match reporter', status: 'designing', skills: [], connections: [] }] })
    const w = mountWorld(two)
    await flushPromises()
    const restore = w.vm.showLevels()
    await w.setProps({ world: worldView(normalizeState({ ...two, focus: { agent: 'reporter' } }), { done: true }) })
    await flushPromises()
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    restore()
    await flushPromises()
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    await openCompany(w)
    expect(w.text()).toContain('Match reporter')
    expect(w.find('[data-testid="build-world-map"]').exists()).toBe(false)
  })

  it('from Findings too', async () => {
    const w = mountWorld()
    await w.findAll('button').find((b) => b.text().startsWith('Findings')).trigger('click')
    expect(w.find('[data-testid="build-findings"]').exists()).toBe(true)
    w.vm.showLevels()
    await flushPromises()
    expect(w.find('[data-tour="levels"]').exists()).toBe(true)
    expect(w.find('[data-testid="build-findings"]').exists()).toBe(false)
  })
})
