// @vitest-environment jsdom
/**
 * trinity-enterprise#530 — skill sets, MOUNTED.
 *
 * Agent Skills tab: the honest per-set status (#342), "via <set>" on members,
 * a set-only member locked in the checklist, and — the store write that
 * matters — the bulk PUT sends the INDIVIDUAL list only, so saving never
 * turns a set's member into an individual assignment or drops it.
 *
 * Library → Skills: sets listed beside skills, expandable to members with
 * versions; a partial set cannot be assigned; assign goes to the per-agent set
 * route (the ent#596 fence) and patches holder chips in place.
 *
 * Mount harness per #2918 (skillsPanelDraftSurvivesSync.spec.js).
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

import SkillsPanel from '../../src/components/SkillsPanel.vue'
import LibrarySkillsSection from '../../src/components/LibrarySkillsSection.vue'
import { useSkillsLibraryStore } from '../../src/stores/skillsLibrary'

const AGENT = 'agent-x'
const LIBRARY = [
  { name: 'alpha', description: 'first' },
  { name: 'beta', description: 'second' },
  { name: 'groom', description: 'set member' },
  { name: 'backlog', description: 'set member' },
]
const ROWS = [
  { skill_name: 'alpha', individual: true, via_sets: [] },
  { skill_name: 'groom', individual: false, via_sets: ['dev-backlog'] },
  { skill_name: 'backlog', individual: true, via_sets: ['dev-backlog'] },
]
const AGENT_SETS = [{
  name: 'dev-backlog', status: 'partial', source_id: 's1', drift: false,
  members: [
    { name: 'groom', state: 'assigned', version: 'abcdef1234', shadowed_source: null },
    { name: 'backlog', state: 'assigned', version: 'bbbbbbb999', shadowed_source: null },
    { name: 'claim', state: 'missing_upstream', version: null, shadowed_source: null },
  ],
  prerequisites: { state: 'missing', missing_env: ['GITHUB_TOKEN'] },
  suggested_schedules: [{ name: 'Weekly groom', cron: '0 9 * * 1', message: '/groom' }],
}]
const LIB_SETS = [
  {
    name: 'dev-backlog', source_id: 's1', source_name: 'Default', shadowed_by: [], status: 'ok', problems: [],
    members: [
      { name: 'groom', present: true, version: 'abcdef1234', shadowed_source: null },
      { name: 'backlog', present: true, version: 'bbbbbbb999', shadowed_source: null },
    ],
    requires: { env: ['GITHUB_TOKEN'] }, schedules: [{ name: 'Weekly groom', cron: '0 9 * * 1', message: '/groom' }],
  },
  {
    name: 'project-management', source_id: 's1', source_name: 'Default', shadowed_by: [], status: 'ok', problems: [],
    members: [{ name: 'beta', present: true, version: 'ccccccc111', shadowed_source: null }],
    requires: { env: [] }, schedules: [],
  },
  {
    name: 'broken', source_id: 's1', source_name: 'Default', shadowed_by: [], status: 'partial',
    problems: ['member_missing'],
    members: [{ name: 'ghost', present: false, version: null, shadowed_source: null }],
    requires: { env: [] }, schedules: [],
  },
]

function respond({ sets = AGENT_SETS, libSets = LIB_SETS, setsFail = false, rows = ROWS } = {}) {
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, cloned: true, skill_count: 4 } })
    if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
    if (url === `/api/agents/${AGENT}/skills`) return Promise.resolve({ data: rows })
    if (url === `/api/agents/${AGENT}/skill-sets`) {
      return setsFail ? Promise.reject({ response: { data: { detail: 'boom' } } }) : Promise.resolve({ data: sets })
    }
    if (url === '/api/skills/library/sets') {
      return setsFail ? Promise.reject({ response: { data: { detail: 'boom' } } }) : Promise.resolve({ data: libSets })
    }
    if (url === '/api/skills/assignments') {
      return Promise.resolve({ data: { assignments: {}, scope: 'all', assignable_agents: [{ name: AGENT, display_label: null }] } })
    }
    return Promise.resolve({ data: [] })
  })
}

async function mountPanel(canManage = true) {
  const wrapper = mount(SkillsPanel, {
    props: { agentName: AGENT, canManage, agentRunning: true },
    attachTo: document.body,
    global: { stubs: { SkillContractChips: true } },
  })
  await flushPromises()
  return wrapper
}

async function mountLibrary() {
  const wrapper = mount(LibrarySkillsSection, {
    attachTo: document.body,
    global: { stubs: { SkillContractChips: true, AssignedAgents: true, 'router-link': true } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of Object.values(api)) f.mockReset()
  respond()
})

describe('Agent Skills tab — sets (ent#530)', () => {
  it('renders the honest set status, the missing prerequisite and the suggested schedule', async () => {
    const w = await mountPanel()
    const row = w.find('[data-testid="set-row-dev-backlog"]')
    expect(row.exists()).toBe(true)
    expect(w.find('[data-testid="set-status-dev-backlog"]').text()).toBe('partial')
    expect(row.text()).toContain('2 of 3 members assigned')
    expect(row.text()).toContain('claim — no longer in the library source')
    expect(w.find('[data-testid="set-prereq-dev-backlog"]').text()).toContain('GITHUB_TOKEN')
    expect(row.text()).toContain('Weekly groom')
    expect(row.text()).toContain('not created')
  })

  it('says why a member is present and locks a set-only member in the checklist', async () => {
    const w = await mountPanel()
    expect(w.find('[data-testid="skill-via-groom"]').text()).toBe('via dev-backlog')
    expect(w.find('[data-testid="skill-via-alpha"]').exists()).toBe(false)
    const locked = w.find('[data-testid="skill-locked-groom"]')
    expect(locked.element.disabled).toBe(true)
    expect(locked.element.checked).toBe(true)
    // An individually-assigned member of a set stays a normal, editable box.
    expect(w.find('input[type="checkbox"][value="backlog"]').element.checked).toBe(true)
  })

  it('the bulk PUT sends the individual list only — never the set-only member', async () => {
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { delivery: null } })
    await w.find('input[type="checkbox"][value="beta"]').setValue(true)
    const save = w.findAll('button').find(b => b.text() === 'Save assignments')
    await save.trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledTimes(1)
    const [url, body] = api.put.mock.calls[0]
    expect(url).toBe(`/api/agents/${AGENT}/skills`)
    expect([...body.skills].sort()).toEqual(['alpha', 'backlog', 'beta'])
    expect(body.skills).not.toContain('groom')
  })

  it('unassigns a set through the set route', async () => {
    const w = await mountPanel()
    api.delete.mockResolvedValue({ data: { success: true } })
    await w.find('[data-testid="set-unassign-dev-backlog"]').trigger('click')
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith(`/api/agents/${AGENT}/skill-sets/dev-backlog`)
  })

  it('assigns an ok set through the set route; a partial one is not selectable', async () => {
    const w = await mountPanel()
    const opts = w.findAll('[data-testid="set-picker"] option')
    const broken = opts.find(o => o.element.value === 'broken')
    expect(broken.element.disabled).toBe(true)
    expect(opts.some(o => o.element.value === 'dev-backlog')).toBe(false)   // already held
    api.post.mockResolvedValue({ data: { set_name: 'project-management', members_added: ['beta'], delivery: null } })
    await w.find('[data-testid="set-picker"]').setValue('project-management')
    await w.find('[data-testid="set-assign"]').trigger('click')
    await flushPromises()
    expect(api.post.mock.calls[0][0]).toBe(`/api/agents/${AGENT}/skill-sets/project-management`)
    expect(w.find('[data-testid="set-assigned-note"]').text()).toContain('added beta')
  })

  it('shows a named refusal inline (the ent#530 {code, message} detail)', async () => {
    const w = await mountPanel()
    api.post.mockRejectedValue({ response: { status: 403, data: { detail: { code: 'skill_management_not_permitted', message: 'Not permitted to change skills.' } } } })
    await w.find('[data-testid="set-picker"]').setValue('project-management')
    await w.find('[data-testid="set-assign"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('Not permitted to change skills.')
  })

  it('a failed set read is named and the skills still render', async () => {
    respond({ setsFail: true })
    const w = await mountPanel()
    expect(w.find('[data-testid="sets-error"]').text()).toContain('boom')
    expect(w.find('[data-testid="sets-empty"]').exists()).toBe(false)
    expect(w.find('input[type="checkbox"][value="alpha"]').exists()).toBe(true)
  })

  it('offers no set writes to a viewer who cannot manage', async () => {
    const w = await mountPanel(false)
    expect(w.find('[data-testid="set-row-dev-backlog"]').exists()).toBe(true)
    expect(w.find('[data-testid="set-unassign-dev-backlog"]').exists()).toBe(false)
    expect(w.find('[data-testid="set-picker"]').exists()).toBe(false)
  })
})

describe('Library → Skills — sets (ent#530)', () => {
  it('lists sets, expandable to members with versions', async () => {
    const w = await mountLibrary()
    const card = w.find('[data-testid="library-set-dev-backlog"]')
    expect(card.exists()).toBe(true)
    const toggle = w.find('[data-testid="library-set-toggle-dev-backlog"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(card.find('#set-members-dev-backlog').isVisible()).toBe(false)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(card.find('#set-members-dev-backlog').isVisible()).toBe(true)
    expect(card.text()).toContain('abcdef1')
    expect(card.text()).toContain('Requires GITHUB_TOKEN')
  })

  it('a partial set is badged and offers no assign control', async () => {
    const w = await mountLibrary()
    expect(w.find('[data-testid="library-set-partial-broken"]').exists()).toBe(true)
    expect(w.find('[data-testid="library-set-assign-broken"]').exists()).toBe(false)
    expect(w.find('[data-testid="library-set-assign-dev-backlog"]').exists()).toBe(true)
  })

  it('assign posts to the per-agent set route, flags missing prerequisites, patches holders', async () => {
    const w = await mountLibrary()
    api.post.mockResolvedValue({ data: {
      set_name: 'dev-backlog', members_added: ['groom'],
      status: { prerequisites: { state: 'missing', missing_env: ['GITHUB_TOKEN'] } },
    } })
    await w.find('[data-testid="library-set-picker-dev-backlog"]').setValue(AGENT)
    await w.find('[data-testid="library-set-assign-dev-backlog"]').trigger('click')
    await flushPromises()
    expect(api.post.mock.calls[0][0]).toBe(`/api/agents/${AGENT}/skill-sets/dev-backlog`)
    const note = w.find('[data-testid="library-set-note-dev-backlog"]')
    expect(note.text()).toContain('missing credentials GITHUB_TOKEN')
    const store = useSkillsLibraryStore()
    expect(store.agentsFor('groom').map(a => a.name)).toEqual([AGENT])
    expect(store.agentsFor('backlog').map(a => a.name)).toEqual([AGENT])
  })

  it('a failed set read is named and the skills grid still renders', async () => {
    respond({ setsFail: true })
    const w = await mountLibrary()
    expect(w.find('[data-testid="library-sets-error"]').text()).toContain('boom')
    expect(w.text()).toContain('alpha')
  })
})

describe('review regressions (ent#530)', () => {
  it('an invalid library set says so and offers no assign', async () => {
    respond({ libSets: [...LIB_SETS, {
      name: 'typo', source_id: 's1', source_name: 'Default', shadowed_by: [], status: 'invalid',
      problems: ['invalid_set'], members: [], requires: { env: [] }, schedules: [],
    }] })
    const w = await mountLibrary()
    expect(w.find('[data-testid="library-set-partial-typo"]').text()).toBe('invalid')
    expect(w.find('[data-testid="library-set-invalid-typo"]').text()).toContain('invalid_set')
    expect(w.find('[data-testid="library-set-assign-typo"]').exists()).toBe(false)
  })

  it('a set load failure with a {code, message} detail shows the message, not [object Object]', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/skills/library/sets') {
        return Promise.reject({ response: { data: { detail: { code: 'x', message: 'Library busy' } } } })
      }
      if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, cloned: true } })
      if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
      return Promise.resolve({ data: { assignments: {}, assignable_agents: [] } })
    })
    const w = await mountLibrary()
    const err = w.find('[data-testid="library-sets-error"]').text()
    expect(err).toContain('Library busy')
    expect(err).not.toContain('[object Object]')
  })

  it('a set moved to another source reads as unresolved with the re-assign next step', async () => {
    respond({ sets: [{ name: 'dev-backlog', status: 'unresolved', reason: 'source_changed', drift: true,
      members: [], prerequisites: { state: 'unknown', missing_env: [] }, suggested_schedules: [] }] })
    const w = await mountPanel()
    const row = w.find('[data-testid="set-row-dev-backlog"]')
    expect(w.find('[data-testid="set-status-dev-backlog"]').text()).toBe('unresolved')
    expect(row.text()).toContain('re-assign it to follow the new source')
    expect(row.text()).toContain('kept until it resolves')
  })

  it('an unassign the server deferred is said, never shown as done', async () => {
    const w = await mountPanel()
    api.delete.mockResolvedValue({ data: { success: true, members_removed: [], removal_deferred: true } })
    await w.find('[data-testid="set-unassign-dev-backlog"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="set-removal-deferred"]').text()).toContain('nothing is removed until it can')
  })
})
