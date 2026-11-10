// @vitest-environment jsdom
/**
 * trinity-enterprise#1169 — the steward agent carries the library's
 * project-management skills (R5 on ent#1137).
 *
 * The create/edit dialog says, under Steward, what picking that agent does:
 * it gets the skills, it already has them, only its owner or an admin can give
 * them, or the skills are not available on this Trinity yet. A person steward
 * reads what an agent steward would add. The Steward card says whether the
 * agent steward holds the skills, and a person steward's card what having no
 * steward agent means. A failed or absent read adds no line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import ProjectCreateModal from '@/components/portal/projects/ProjectCreateModal.vue'
import ProjectStewardCard from '@/components/portal/projects/ProjectStewardCard.vue'
import { stewardSkillHint } from '@/components/portal/projects/projectsUtils'
import { useProjectsStore } from '@/stores/projects'

const ME = 'alice@example.com'
const OPTIONS = {
  set: { name: 'project-management', available: true, message: null },
  agents: [
    { agent_name: 'pm-steward', holds_set: false, can_assign: true },
    { agent_name: 'old-hand', holds_set: true, can_assign: true },
    { agent_name: 'bobs-agent', holds_set: false, can_assign: false },
  ],
}
const UNAVAILABLE = { set: { name: 'project-management', available: false, message: "The project-management skills aren't available on this Trinity yet, so a steward agent can be named but won't get them." }, agents: OPTIONS.agents.map((a) => ({ ...a, holds_set: false, can_assign: false })) }

let projects
const hint = () => document.querySelector('[data-testid="project-steward-skills-hint"]')

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  projects = useProjectsStore()
})

async function openDialog(options, agents = ['pm-steward', 'old-hand', 'bobs-agent']) {
  projects.fetchStewardOptions = typeof options === 'function' ? options : vi.fn().mockResolvedValue(options)
  mount(ProjectCreateModal, { props: { modelValue: true, myEmail: ME, agents }, attachTo: document.body })
  await flushPromises()
}

async function pickSteward(value) {
  const el = document.getElementById('project-steward')
  el.value = value
  el.dispatchEvent(new Event('change'))
  await flushPromises()
}

describe('ent#1169 — the dialog says what picking a steward agent does', () => {
  it('reads the options once on opening', async () => {
    await openDialog(OPTIONS)
    expect(projects.fetchStewardOptions).toHaveBeenCalledTimes(1)
  })

  it('a person steward reads what an agent steward would add', async () => {
    await openDialog(OPTIONS)
    expect(hint().textContent).toContain('An agent steward with the project-management skills does it on its own schedule.')
  })

  it('an agent I may equip gets the skills', async () => {
    await openDialog(OPTIONS)
    await pickSteward('agent:pm-steward')
    expect(hint().textContent).toBe('pm-steward gets the project-management skills, so it can verify finished tasks, record health and write the log.')
  })

  it('an agent that already has them says so', async () => {
    await openDialog(OPTIONS)
    await pickSteward('agent:old-hand')
    expect(hint().textContent).toContain('old-hand already has the project-management skills')
  })

  it("someone else's agent: only its owner or an admin can give them", async () => {
    await openDialog(OPTIONS)
    await pickSteward('agent:bobs-agent')
    expect(hint().textContent).toContain("Only bobs-agent's owner or an admin can give it the project-management skills.")
  })

  it('the skills not available on this Trinity are said plainly, and creating is not blocked', async () => {
    await openDialog(UNAVAILABLE)
    await pickSteward('agent:pm-steward')
    expect(hint().textContent).toBe(UNAVAILABLE.set.message)
    expect(document.querySelector('[data-testid="project-create-submit"]').disabled).toBe(false)
  })

  it('a failed options read adds no agent line and never blocks', async () => {
    await openDialog(vi.fn().mockRejectedValue(new Error('down')))
    await pickSteward('agent:pm-steward')
    expect(hint().textContent).toBe('')
    expect(document.querySelector('[data-testid="project-create-submit"]').disabled).toBe(false)
  })

  it('an agent whose skills could not be read is said so, not guessed', async () => {
    await openDialog({ ...OPTIONS, agents: [{ agent_name: 'pm-steward', holds_set: null, can_assign: false }] })
    await pickSteward('agent:pm-steward')
    expect(hint().textContent).toContain("couldn't check pm-steward's skills")
  })

  it('in Edit, an unchanged steward is promised nothing', async () => {
    projects.fetchStewardOptions = vi.fn().mockResolvedValue(OPTIONS)
    const p = { id: 'prj_1', name: 'Q4', goal: 'Ship.', visibility: 'members', steward: { kind: 'agent', ref: 'pm-steward' }, members: [] }
    mount(ProjectCreateModal, { props: { modelValue: true, myEmail: ME, agents: ['pm-steward', 'old-hand'], project: p }, attachTo: document.body })
    await flushPromises()
    expect(hint().textContent).toBe('')
    await pickSteward('agent:old-hand')
    expect(hint().textContent).toContain('old-hand already has')
  })

  it('the hint line keeps its height while the read loads', async () => {
    await openDialog(() => new Promise(() => {}))
    await pickSteward('agent:pm-steward')
    expect(hint().textContent).toBe('Checking the project-management skills…')
    expect(hint().className).toContain('min-h-')
  })

  it('with no agents to offer, nothing is fetched', async () => {
    await openDialog(OPTIONS, [])
    expect(projects.fetchStewardOptions).not.toHaveBeenCalled()
    expect(hint().textContent).toContain('A person steward')
  })
})

describe('ent#1169 — stewardSkillHint', () => {
  it('says it is checking while the read loads, and nothing when it failed', () => {
    expect(stewardSkillHint('agent:pm-steward', null)).toBe('Checking the project-management skills…')
    expect(stewardSkillHint('agent:pm-steward', false)).toBe('')
  })
})

function project(steward) {
  return {
    id: 'prj_1', created_by: ME, steward,
    rollup: { by_status: {}, health: null, health_due: false }, proposal: null,
  }
}
const card = (p) => mount(ProjectStewardCard, { props: { project: p, myEmail: ME }, global: { stubs: { RouterLink: RouterLinkStub } } })
const AGENT = { kind: 'agent', ref: 'pm-steward', last_sweep_at: null, schedule: { state: 'none' } }

describe('ent#1169 — the Steward card', () => {
  it('an agent steward that holds the skills says so', () => {
    const w = card(project({ ...AGENT, skill_set: { name: 'project-management', held: true, available: true } }))
    expect(w.find('[data-testid="project-steward-skills"]').text()).toBe('Has the project-management skills')
  })

  it('one that lacks them says who can give them', () => {
    const w = card(project({ ...AGENT, skill_set: { name: 'project-management', held: false, available: true } }))
    expect(w.find('[data-testid="project-steward-skills"]').text()).toContain("Its owner or an admin can give them on the agent's Skills tab.")
  })

  it('skills not available on this Trinity are said', () => {
    const w = card(project({ ...AGENT, skill_set: { name: 'project-management', held: false, available: false } }))
    expect(w.find('[data-testid="project-steward-skills"]').text()).toBe("The project-management skills aren't available on this Trinity yet")
  })

  it('an unknown read, or an older backend, adds no line', () => {
    expect(card(project({ ...AGENT, skill_set: { name: 'project-management', held: null, available: null } }))
      .find('[data-testid="project-steward-skills"]').exists()).toBe(false)
    expect(card(project(AGENT)).find('[data-testid="project-steward-skills"]').exists()).toBe(false)
  })

  it('a person steward reads what having no steward agent means', () => {
    const w = card(project({ kind: 'person', ref: ME }))
    expect(w.find('[data-testid="project-steward-no-agent"]').text()).toContain('No steward agent')
    expect(card(project(AGENT)).find('[data-testid="project-steward-no-agent"]').exists()).toBe(false)
  })
})
