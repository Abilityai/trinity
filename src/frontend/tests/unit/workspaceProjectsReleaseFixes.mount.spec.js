// @vitest-environment jsdom
/**
 * Workspace Projects — 1.0 release-testing findings (row B7), mounted.
 *
 *   - #3562: the empty Projects list carries exactly ONE primary action, and the
 *     empty state follows the empty-state recipe (dashed border, icon, purpose);
 *   - #3565: the "Who can see it" help line reserves the taller text's box, so
 *     switching the visibility does not change the dialog's height;
 *   - the project page's Agents card says so when there are none;
 *   - after a successful add, the agent picker shows its placeholder again and
 *     Add is disabled.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
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

import { useClientPortalStore } from '@/stores/clientPortal'
import { useProjectsStore } from '@/stores/projects'
import BaseButton from '@/components/base/BaseButton.vue'
import ProjectsList from '@/components/portal/projects/ProjectsList.vue'
import ProjectCreateModal from '@/components/portal/projects/ProjectCreateModal.vue'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'
import ProjectMembersModal from '@/components/portal/projects/ProjectMembersModal.vue'

const PROJECT = {
  id: 'prj_1', name: 'Q4 launch', goal: 'Ship it.', status: 'active', visibility: 'members',
  created_by: 'eva@example.com', updated_at: '2026-09-29T10:00:00Z', archived_at: null, steward: null,
  members: [{ email: 'eva@example.com', role: 'creator' }],
  agents: [{ agent_name: 'scout', state: 'active' }],
  my_chats: [], rooms: [], others_chat_count: 0,
  can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true },
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let portal
let projects

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  projects = useProjectsStore()
})

describe('#3562 — the empty Projects list', () => {
  function emptyList() {
    portal.portalToken = null
    portal.platformFallbackSuppressed = false
    projects.fetchList = vi.fn()
    projects.list = []
    projects.listLoaded = true
    projects.listError = null
    return mount(ProjectsList, { attachTo: document.body })
  }

  it('has exactly one primary action on the view', async () => {
    const w = emptyList()
    await flushPromises()
    expect(w.find('[data-testid="projects-empty"]').exists()).toBe(true)
    const primaries = w.findAllComponents(BaseButton).filter((b) => (b.props('variant') || 'primary') === 'primary')
    expect(primaries.map((b) => b.text())).toEqual(['New project'])
    // The header's New project is the one that stays primary — it is there
    // whatever the list holds; the empty state's own button still opens the form.
    // (BaseButton's root is a comment + <button> fragment, so read the button.)
    expect(primaries[0].find('button').attributes('data-testid')).toBe('projects-new')
    await w.find('[data-testid="projects-empty-create"]').trigger('click')
    expect(w.findComponent(ProjectCreateModal).props('modelValue')).toBe(true)
  })

  it('follows the empty-state recipe: dashed border, an icon, one line of purpose', async () => {
    const w = emptyList()
    await flushPromises()
    const box = w.find('[data-testid="projects-empty"]')
    expect(box.classes()).toEqual(expect.arrayContaining(['border', 'border-dashed', 'rounded-lg']))
    expect(box.find('svg').exists()).toBe(true)
    expect(box.text()).toContain('A project gathers the chats and rooms about one piece of work')
  })
})

describe('#3565 — the visibility help line keeps its height', () => {
  it('both help texts share one reserved box; only the current one is visible', async () => {
    mount(ProjectCreateModal, { props: { modelValue: true }, attachTo: document.body })
    await flushPromises()
    const COMPANY = 'Everyone signed in at your company can see it.'
    const MEMBERS = 'Only people you add can see it.'
    const box = byTestId('project-visibility-help')
    expect(box).not.toBe(null)
    // Both texts are laid out in the same grid cell, so the box is always as
    // tall as the longer one, whichever is showing.
    const texts = () => [...box.children].map((el) => ({
      text: el.textContent.trim(), hidden: el.classList.contains('invisible'),
    }))
    expect(box.classList.contains('grid')).toBe(true)
    expect(texts().map((t) => t.text.slice(0, MEMBERS.length))).toEqual(
      expect.arrayContaining([COMPANY.slice(0, MEMBERS.length), MEMBERS]),
    )
    expect(texts().find((t) => t.text.startsWith(MEMBERS)).hidden).toBe(false)
    expect(texts().find((t) => t.text.startsWith(COMPANY)).hidden).toBe(true)

    const select = document.getElementById('project-visibility')
    select.value = 'company'
    select.dispatchEvent(new Event('change'))
    await flushPromises()
    expect(byTestId('project-visibility-help')).toBe(box)
    expect(texts().find((t) => t.text.startsWith(MEMBERS)).hidden).toBe(true)
    expect(texts().find((t) => t.text.startsWith(COMPANY)).hidden).toBe(false)
  })
})

describe('project page — Agents card', () => {
  it('says so when the project has no agents, like Rooms does', async () => {
    projects.fetchProject = vi.fn()
    projects.pageState = vi.fn(() => ({
      data: { ...PROJECT, agents: [] }, loaded: true, loading: false, error: null, notFound: false,
    }))
    const w = mount(ProjectPage, { props: { projectId: 'prj_1', myEmail: 'eva@example.com' }, attachTo: document.body })
    await flushPromises()
    const empty = w.find('[data-testid="project-agents-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toBe('No agents yet.')
  })

  it('shows no empty line when there are agents', async () => {
    projects.fetchProject = vi.fn()
    projects.pageState = vi.fn(() => ({ data: PROJECT, loaded: true, loading: false, error: null, notFound: false }))
    const w = mount(ProjectPage, { props: { projectId: 'prj_1', myEmail: 'eva@example.com' }, attachTo: document.body })
    await flushPromises()
    expect(w.find('[data-testid="project-agents-empty"]').exists()).toBe(false)
  })
})

describe('Members & access — adding an agent', () => {
  it('after a successful add the picker shows its placeholder again and Add is disabled', async () => {
    const w = mount(ProjectMembersModal, {
      props: {
        modelValue: true, project: { ...PROJECT, agents: [] }, myEmail: 'eva@example.com',
        rosterAgents: ['scout', 'mine', 'third'], ownedAgents: ['scout', 'mine', 'third'],
      },
      attachTo: document.body,
    })
    // The real store refreshes the project before addAgent resolves; the page
    // hands the refreshed project back down as the prop.
    projects.addAgent = vi.fn(async () => {
      await w.setProps({ project: { ...PROJECT, agents: [{ agent_name: 'mine', state: 'active' }] } })
    })
    await flushPromises()
    const select = () => document.getElementById('project-add-agent')
    select().value = 'mine'
    select().dispatchEvent(new Event('change'))
    await flushPromises()
    // The agent form's own Add (the People and Guests forms carry one too).
    const add = () => select().closest('form').querySelector('button[type="submit"]')
    expect(add().disabled).toBe(false)

    select().closest('form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(projects.addAgent).toHaveBeenCalledWith('prj_1', 'mine')
    expect([...select().options].map((o) => o.value)).toEqual(['', 'scout', 'third'])
    expect(select().selectedIndex).toBe(0)
    expect(select().selectedOptions[0].textContent).toBe('Add an agent you can use…')
    expect(add().disabled).toBe(true)
  })
})
