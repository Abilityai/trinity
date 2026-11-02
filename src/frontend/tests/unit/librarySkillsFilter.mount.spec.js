// @vitest-environment jsdom
/**
 * trinity-enterprise#660 + trinity#3448 — Library → Skills: find a skill in a
 * bounded grid, MOUNTED (#2918), on the harness of
 * `librarySkillShadowedTooltip.mount.spec.js`.
 *
 * The filter rule is executed in skillFilter.spec.js. This proves the wiring: it
 * narrows the card grid and nothing else, a filtered-out card keeps its state
 * (AssignedAgents holds its pick, busy flag and a failed assign's error locally,
 * so the card must hide, never unmount), and the grid sits in one region. jsdom
 * cannot measure layout: the geometry is the eyeball. `skillsBoundedChain.mount`
 * pins the page's half of the chain.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises, enableAutoUnmount } from '@vue/test-utils'
import { afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true }),
}))

import LibrarySkillsSection from '../../src/components/LibrarySkillsSection.vue'

enableAutoUnmount(afterEach)

const LIBRARY = [
  { name: 'backlog', description: 'What is next', source_id: 'src_a', source_name: 'Team', category: 'project-management' },
  { name: 'roadmap', description: 'Priorities', source_id: 'src_a', source_name: 'Team', category: null },
  { name: 'chart', description: 'Draw a diagram', source_id: 'src_b', source_name: 'Vendor', category: 'visual-communication' },
]

let state
beforeEach(() => {
  setActivePinia(createPinia())
  state = { library: LIBRARY, assignments: {}, assignable: [{ name: 'alpha', display_label: null }] }
  api.get.mockReset()
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') {
      return Promise.resolve({ data: { configured: true, cloned: true, skill_count: state.library.length } })
    }
    if (url === '/api/skills/library') return Promise.resolve({ data: state.library })
    if (url === '/api/skills/assignments') {
      return Promise.resolve({ data: { assignments: state.assignments, scope: 'all', assignable_agents: state.assignable } })
    }
    return Promise.resolve({ data: [] })
  })
})

async function mountLibrary() {
  const w = mount(LibrarySkillsSection, {
    attachTo: document.body,
    global: { stubs: { 'router-link': true, LibrarySkillSets: { template: '<section data-testid="library-skill-sets" />' } } },
  })
  await flushPromises()
  return w
}

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
const card = (w, name) => tid(w, `library-skill-${name}`)
const shown = (w) => LIBRARY.map((s) => s.name).filter((n) => card(w, n).isVisible())
const type = async (w, text) => { await tid(w, 'library-skills-filter').setValue(text); await flushPromises() }
const choose = async (w, id, value) => { await tid(w, id).setValue(value); await flushPromises() }

describe('the filter narrows the grid', () => {
  it('by name or description, and states the total and what is shown', async () => {
    const w = await mountLibrary()
    expect(shown(w)).toEqual(['backlog', 'roadmap', 'chart'])
    expect(tid(w, 'library-skills-total').text()).toBe('3 skills')

    await type(w, 'DIAGRAM')

    expect(shown(w)).toEqual(['chart'])
    expect(tid(w, 'library-skills-total').text()).toBe('3 skills · 1 shown')
  })

  it('by source and by category', async () => {
    const w = await mountLibrary()
    await choose(w, 'library-skills-source', 'src_a')
    expect(shown(w)).toEqual(['backlog', 'roadmap'])
    await choose(w, 'library-skills-category', 'project-management')
    expect(shown(w)).toEqual(['backlog'])
  })

  it('the source filter hides with one source, the category filter with none', async () => {
    state.library = [{ name: 'solo', description: 'x', source_id: 'src_a', source_name: 'Team', category: null }]
    const w = await mountLibrary()
    expect(tid(w, 'library-skills-filter').exists()).toBe(true)
    expect(tid(w, 'library-skills-source').exists()).toBe(false)
    expect(tid(w, 'library-skills-category').exists()).toBe(false)
  })

  it('a filter with no result says so', async () => {
    const w = await mountLibrary()
    await type(w, 'no such skill')
    expect(tid(w, 'library-skills-total').text()).toBe('3 skills · No skill matches the filter.')
    expect(shown(w)).toEqual([])
  })

  it('a card filtered out stays mounted, so it keeps a failed assign\'s error', async () => {
    const w = await mountLibrary()
    const node = card(w, 'chart').element
    await type(w, 'priorities')
    expect(card(w, 'chart').element).toBe(node)
    expect(card(w, 'chart').isVisible()).toBe(false)
    await type(w, '')
    expect(card(w, 'chart').element).toBe(node)
    expect(card(w, 'chart').isVisible()).toBe(true)
  })

  it('every card keeps its assign control', async () => {
    const w = await mountLibrary()
    await type(w, 'priorities')
    const control = card(w, 'roadmap').find('select')
    expect(control.exists()).toBe(true)
    expect(control.text()).toContain('alpha')
  })
})

describe('what the filter leaves alone', () => {
  it('skill sets above the grid and orphaned assignments below it stay', async () => {
    state.assignments = { gone: [{ name: 'alpha', display_label: null }] }
    const w = await mountLibrary()
    await type(w, 'no such skill')
    expect(tid(w, 'library-skill-sets').exists()).toBe(true)
    expect(w.text()).toContain('Assigned but no longer in the library')
    expect(w.text()).toContain('gone')
  })
})

describe('the bounded grid (trinity#3448)', () => {
  it('the cards scroll in one named region; the filter and the total stay outside it', async () => {
    const w = await mountLibrary()
    const region = tid(w, 'library-skills-region')
    expect(region.attributes('role')).toBe('region')
    expect(region.attributes('aria-label')).toBe('Skills in the library')
    // Each card's sr-only label is absolute: an unpositioned box lets them grow the
    // page in Chromium. Only e2e/skills-bounded-lists.spec.js can see the geometry.
    expect(region.classes()).toContain('relative')
    expect(region.find('[data-testid="library-skill-backlog"]').exists()).toBe(true)
    expect(region.find('[data-testid="library-skills-filter"]').exists()).toBe(false)
    expect(region.find('[data-testid="library-skills-total"]').exists()).toBe(false)
  })

  it('skill sets and orphaned assignments sit outside it', async () => {
    state.assignments = { gone: [{ name: 'alpha', display_label: null }] }
    const w = await mountLibrary()
    const region = tid(w, 'library-skills-region')
    expect(region.find('[data-testid="library-skill-sets"]').exists()).toBe(false)
    expect(region.text()).not.toContain('Assigned but no longer in the library')
  })

  it('every link from the region to the section root is a 0px-basis flex column, never flex-1', async () => {
    const w = await mountLibrary()
    const region = tid(w, 'library-skills-region').element
    expect([...region.classList]).toEqual(expect.arrayContaining(['grow', 'basis-0']))
    for (let el = region.parentElement; el !== w.element.parentElement; el = el.parentElement) {
      expect([...el.classList]).toEqual(expect.arrayContaining(['flex', 'flex-col', 'grow', 'basis-0']))
      expect([...el.classList]).not.toContain('flex-1')
    }
  })

  it('a new query starts at the first card', async () => {
    const w = await mountLibrary()
    const region = tid(w, 'library-skills-region').element
    region.scrollTop = 300
    await type(w, 'chart')
    expect(region.scrollTop).toBe(0)
  })
})
