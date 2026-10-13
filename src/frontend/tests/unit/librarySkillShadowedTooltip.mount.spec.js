// @vitest-environment jsdom
/**
 * #3135 — the `shadowed` badge names the sources whose copies it hides.
 *
 * `shadowed_by` is a list of `{source_id, source_name}` objects
 * (`db_models.SkillInfo.shadowed_by`), and the tooltip joined the objects
 * themselves: "Shadowed by: [object Object]". That name is the one fact the
 * badge exists to give (ent#237 AC#4, "never a silent overwrite").
 *
 * Mounted (#2918), on the harness of `skillDeprecation.spec.js`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true }),
}))

import LibrarySkillsSection from '../../src/components/LibrarySkillsSection.vue'

const LIBRARY = [
  {
    name: 'research', description: 'shadowed twice', source_name: 'Company skills',
    shadowed_by: [
      { source_id: 'src-team', source_name: 'Team catalog' },
      { source_id: 'src-vendor', source_name: 'Vendor pack' },
    ],
  },
  // A source row without a name still names something rather than "undefined".
  { name: 'nameless', description: 'x', shadowed_by: [{ source_id: 'src-raw' }] },
  { name: 'alone', description: 'not shadowed', shadowed_by: [] },
]

beforeEach(() => {
  setActivePinia(createPinia())
  api.get.mockReset()
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') {
      return Promise.resolve({ data: { configured: true, cloned: true, skill_count: LIBRARY.length } })
    }
    if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
    if (url === '/api/skills/assignments') {
      return Promise.resolve({ data: { assignments: {}, scope: 'all', assignable_agents: [] } })
    }
    return Promise.resolve({ data: [] })
  })
})

async function mountLibrary() {
  const wrapper = mount(LibrarySkillsSection, {
    attachTo: document.body,
    global: { stubs: { 'router-link': true, LibrarySkillSets: true } },
  })
  await flushPromises()
  return wrapper
}

const badge = (w, name) => w.find(`[data-testid="skill-shadowed-library-${name}"]`)

describe('#3135 — Library → Skills `shadowed` badge', () => {
  it('names every shadowing source by its name', async () => {
    const w = await mountLibrary()
    const b = badge(w, 'research')
    expect(b.text()).toBe('shadowed')
    expect(b.attributes('title')).toBe('Shadowed by: Team catalog, Vendor pack')
  })

  it('falls back to the source id, never "[object Object]" or "undefined"', async () => {
    const w = await mountLibrary()
    const title = badge(w, 'nameless').attributes('title')
    expect(title).toBe('Shadowed by: src-raw')
    expect(title).not.toMatch(/object Object|undefined/)
  })

  it('an unshadowed skill has no badge', async () => {
    const w = await mountLibrary()
    expect(badge(w, 'alone').exists()).toBe(false)
  })
})
