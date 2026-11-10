// @vitest-environment jsdom
/**
 * #4395 review (webmixgamer, 2026-11-09) — the UI items it left open:
 *
 * - Remove on a brief asks first, and says what it ends (design contract:
 *   destructive actions confirm and restate the consequence).
 * - The create/edit answer's `steward_skills` outcome is shown: the Steward
 *   card says WHY the set wasn't assigned, not only that it isn't held.
 * - "Pick an agent as steward in Edit project" is offered only to a viewer who
 *   can edit.
 * - Material's empty state doesn't say "Nothing added yet" above listed briefs.
 * - The store's create/update keep the outcome by project id (run, not mocked).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => {
  const mk = () => ({
    ...http,
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useProjectsStore } from '@/stores/projects'
import ProjectContext from '@/components/portal/projects/ProjectContext.vue'
import ProjectMaterial from '@/components/portal/projects/ProjectMaterial.vue'
import ProjectStewardCard from '@/components/portal/projects/ProjectStewardCard.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const BOB = 'bob@example.com'
const BRIEF = { id: 'ctx_1', kind: 'project-file', task_id: null, filename: 'brief.md', mime: 'text/markdown',
  size_bytes: 120, for_agents: true, created_by: BOB, created_at: '2026-11-09T10:00:00Z', updated_at: '2026-11-09T10:00:00Z' }
const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
// The dialog's button is the LAST match: the row's own Remove button comes first.
const buttonByText = (text) => [...document.querySelectorAll('button')].filter((b) => b.textContent.trim() === text).pop()

let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useProjectsStore()
})

describe('#4395 review — Remove on a brief confirms first', () => {
  function ctx() {
    return mount(ProjectContext, {
      props: { projectId: 'prj_1', items: [BRIEF], canContribute: true, myEmail: BOB, isCreator: false },
      attachTo: document.body,
    })
  }

  it('one click deletes nothing; the dialog names the brief and that agents stop reading it', async () => {
    store.deleteContext = vi.fn().mockResolvedValue({})
    ctx()
    byTestId('context-delete-ctx_1').click()
    await flushPromises()
    expect(store.deleteContext).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('Remove this brief?')
    expect(document.body.textContent).toContain('brief.md is deleted from the project, and agents stop reading it')
  })

  it('Keep it deletes nothing; Remove deletes that brief', async () => {
    store.deleteContext = vi.fn().mockResolvedValue({})
    ctx()
    byTestId('context-delete-ctx_1').click()
    await flushPromises()
    buttonByText('Keep it').click()
    await flushPromises()
    expect(store.deleteContext).not.toHaveBeenCalled()
    byTestId('context-delete-ctx_1').click()
    await flushPromises()
    buttonByText('Remove').click()
    await flushPromises()
    expect(store.deleteContext).toHaveBeenCalledWith('prj_1', 'ctx_1')
  })
})

describe('#4395 review — the Steward card says why the set was not assigned', () => {
  const AGENT = { kind: 'agent', ref: 'pm-steward', last_sweep_at: null, schedule: { state: 'none' },
    skill_set: { name: 'project-management', held: false, available: true } }
  const project = (steward, extra = {}) => ({
    id: 'prj_1', created_by: BOB, steward, rollup: { by_status: {}, health: null, health_due: false }, proposal: null, ...extra,
  })
  const card = (p, stewardOutcome = null) => mount(ProjectStewardCard, {
    props: { project: p, myEmail: BOB, stewardOutcome }, global: { stubs: { RouterLink: RouterLinkStub } },
  })
  const line = (w) => w.find('[data-testid="project-steward-skills"]').text()

  it.each(['awaiting_owner', 'not_permitted', 'failed', 'unavailable'])('%s: the server\'s message replaces the generic line', (state) => {
    const w = card(project(AGENT), { set: 'project-management', agent: 'pm-steward', state, message: `why: ${state}` })
    expect(line(w)).toBe(`why: ${state}`)
  })

  it('an outcome for another agent, or once the set is held, is not shown', () => {
    expect(line(card(project(AGENT), { agent: 'other', state: 'failed', message: 'stale' }))).not.toBe('stale')
    const held = { ...AGENT, skill_set: { name: 'project-management', held: true, available: true } }
    expect(line(card(project(held), { agent: 'pm-steward', state: 'failed', message: 'stale' }))).toBe('Has the project-management skills')
  })

  it('a person steward card offers "Edit project" only to a viewer who can edit', () => {
    const person = { kind: 'person', ref: BOB }
    const hint = (w) => w.find('[data-testid="project-steward-no-agent"]').text()
    expect(hint(card(project(person, { can: { edit: true } })))).toContain('Pick an agent as steward in Edit project')
    expect(hint(card(project(person, { can: { edit: false } })))).not.toContain('Edit project')
  })
})

describe('#4395 review — Material empty state with briefs listed', () => {
  it('does not say "Nothing added yet" above a listed brief', async () => {
    const PROJECT = { id: 'prj_1', created_by: BOB, files: [], reports: [], decisions: [], context: [BRIEF], can: { contribute: true } }
    mount(ProjectMaterial, { props: { project: PROJECT, myEmail: BOB }, attachTo: document.body })
    await flushPromises()
    const empty = byTestId('project-material-empty')
    if (empty) expect(empty.textContent).not.toContain('Nothing added yet')
  })
})

describe('#4395 review — the store keeps the steward outcome (run, not mocked)', () => {
  const OUTCOME = { set: 'project-management', agent: 'pm-steward', state: 'not_permitted', message: 'm' }

  it('create keeps it under the new project id', async () => {
    http.post.mockResolvedValue({ data: { id: 'prj_9', name: 'Q', steward_skills: OUTCOME } })
    http.get.mockResolvedValue({ data: { projects: [] } })
    await store.create({ name: 'Q', goal: 'G' })
    expect(http.post.mock.calls[0][0]).toBe('/api/enterprise/projects')
    expect(store.stewardOutcomes.prj_9).toEqual(OUTCOME)
  })

  it('update keeps it under the edited project id', async () => {
    http.patch.mockResolvedValue({ data: { id: 'prj_1', steward_skills: OUTCOME } })
    http.get.mockResolvedValue({ data: {} })
    await store.update('prj_1', { steward: { kind: 'agent', ref: 'pm-steward' } })
    expect(http.patch.mock.calls[0][0]).toBe('/api/enterprise/projects/prj_1')
    expect(store.stewardOutcomes.prj_1).toEqual(OUTCOME)
  })
})
