// @vitest-environment jsdom
/**
 * trinity-enterprise#1161 — a member who can't use the steward agent is told,
 * by name, and the agent's owner gives access on the project page.
 *
 * Project membership never carries agent access (decision 1 on ent#1137). The
 * read's `steward_access` drives two blocks on the Steward card: the member's
 * "You can't use … yet" with Ask for access (or its pending / declined state),
 * and the owner's list of members without it, each with Give access.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ProjectStewardCard from '@/components/portal/projects/ProjectStewardCard.vue'
import { useProjectsStore } from '@/stores/projects'

const ME = 'dave@example.com'

function project(access, over = {}) {
  return {
    id: 'prj_1', created_by: 'alice@example.com',
    steward: { kind: 'agent', ref: 'trinity-pm', last_sweep_at: null, schedule: { state: 'hidden' } },
    rollup: { by_status: {}, health: null, health_due: false },
    proposal: null,
    steward_access: access,
    ...over,
  }
}

const card = (p, myEmail = ME) => mount(ProjectStewardCard, {
  props: { project: p, myEmail },
  global: { stubs: { RouterLink: RouterLinkStub } },
})
const noAccess = (w) => w.find('[data-testid="project-steward-no-access"]')
const missing = (w) => w.find('[data-testid="project-steward-missing-access"]')

beforeEach(() => setActivePinia(createPinia()))

describe('ent#1161 — the member who lacks the steward agent', () => {
  const BASE = { agent: 'trinity-pm', has_access: false, request: null, can_grant: false }

  it('reads what is missing, named, and can ask for it', async () => {
    const store = useProjectsStore()
    const ask = vi.spyOn(store, 'requestStewardAccess').mockResolvedValue({})
    const w = card(project(BASE))
    expect(noAccess(w).text()).toContain("You can't use trinity-pm yet")
    expect(noAccess(w).text()).toContain("you can't chat with it and its questions don't reach you")
    await w.find('[data-testid="project-steward-access-request"]').trigger('click')
    expect(ask).toHaveBeenCalledWith('prj_1')
  })

  it('a pending request says it waits on the owner, with nothing to click', () => {
    const w = card(project({ ...BASE, request: 'pending' }))
    expect(w.find('[data-testid="project-steward-access-pending"]').text()).toBe("Access requested. Waiting on the agent's owner.")
    expect(w.find('[data-testid="project-steward-access-request"]').exists()).toBe(false)
  })

  it('a declined request is said, and the member may ask again', () => {
    const w = card(project({ ...BASE, request: 'denied' }))
    expect(w.find('[data-testid="project-steward-access-denied"]').text()).toBe("The agent's owner declined your last request.")
    expect(w.find('[data-testid="project-steward-access-request"]').text()).toBe('Ask again')
  })

  it("the server's refusal renders next to the button", async () => {
    const store = useProjectsStore()
    vi.spyOn(store, 'requestStewardAccess').mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'steward_owner_unreachable', message: 'This agent has no owner who can give access. Ask an admin.' } } },
    })
    const w = card(project(BASE))
    await w.find('[data-testid="project-steward-access-request"]').trigger('click')
    await flushPromises()
    expect(noAccess(w).find('[role="alert"]').text()).toBe('This agent has no owner who can give access. Ask an admin.')
  })

  it('a member who has the agent sees neither block', () => {
    const w = card(project({ ...BASE, has_access: true }))
    expect(noAccess(w).exists()).toBe(false)
    expect(missing(w).exists()).toBe(false)
  })

  it('an older backend with no steward_access renders the card as before', () => {
    const w = card(project(undefined))
    expect(noAccess(w).exists()).toBe(false)
    expect(missing(w).exists()).toBe(false)
  })
})

describe("ent#1161 — the steward agent's owner", () => {
  const OWNER = 'alice@example.com'
  const access = (rows) => ({ agent: 'trinity-pm', has_access: true, request: null, can_grant: true, members_without_access: rows })

  it('reads which members lack it, marks who asked, and gives access in place', async () => {
    const store = useProjectsStore()
    const give = vi.spyOn(store, 'grantStewardAccess').mockResolvedValue({})
    const w = card(project(access([
      { email: ME, label: 'Dave', request_id: 'ar_1' },
      { email: 'erin@example.com', label: 'Erin', request_id: null },
    ])), OWNER)
    expect(missing(w).text()).toContain("2 members can't use trinity-pm")
    const dave = w.find(`[data-testid="project-steward-missing-${ME}"]`)
    expect(dave.text()).toContain('Dave')
    expect(dave.text()).toContain('Asked')
    expect(w.find('[data-testid="project-steward-missing-erin@example.com"]').text()).not.toContain('Asked')
    await dave.find('button').trigger('click')
    expect(give).toHaveBeenCalledWith('prj_1', ME)
  })

  it('one member reads in the singular', () => {
    const w = card(project(access([{ email: ME, label: 'Dave', request_id: null }])), OWNER)
    expect(missing(w).text()).toContain("1 member can't use trinity-pm")
  })

  it('nobody missing, no block', () => {
    const w = card(project(access([])), OWNER)
    expect(missing(w).exists()).toBe(false)
  })

  it('a read that failed says so instead of claiming everyone has it', () => {
    const w = card(project({ agent: 'trinity-pm', has_access: true, request: null, can_grant: true, unknown: true }), OWNER)
    expect(w.find('[data-testid="project-steward-access-unknown"]').exists()).toBe(true)
    expect(missing(w).exists()).toBe(false)
  })
})
