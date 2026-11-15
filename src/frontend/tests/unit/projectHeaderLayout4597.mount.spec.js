// @vitest-environment jsdom
/**
 * #4597 — the project page header must not squeeze the goal into a narrow
 * column beside the action buttons.
 *
 * The header used to be `sm:flex-row`: the content column was `flex-1`
 * (basis 0) and the action group kept its default `flex: 0 1 auto`, so its
 * basis was its max-content width (every button on one line). With the full
 * owner action set the buttons claimed most of the row and the title, goal
 * and role line wrapped into a ~450px column. jsdom has no layout engine, so
 * this pins the structural contract that prevents it: the actions never sit
 * in the same flex row as the content, and they wrap.
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

import { useProjectsStore } from '@/stores/projects'
import ProjectPage from '@/components/portal/projects/ProjectPage.vue'

const PROJECT = {
  id: 'prj_1', name: 'Q4 launch', goal: 'A long goal. '.repeat(40), status: 'active', visibility: 'members',
  created_by: 'eva@example.com', updated_at: '2026-09-29T10:00:00Z', archived_at: null, steward: null,
  members: [{ email: 'eva@example.com', role: 'creator' }],
  agents: [{ agent_name: 'scout', state: 'active' }],
  my_chats: [], rooms: [], others_chat_count: 0,
  can: { manage_members: true, manage_agents: true, edit: true, archive: true, work: true },
}

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

async function mountPage() {
  const projects = useProjectsStore()
  projects.fetchProject = vi.fn()
  projects.pageState = vi.fn(() => ({ data: PROJECT, loaded: true, loading: false, error: null, notFound: false }))
  const w = mount(ProjectPage, { props: { projectId: 'prj_1', myEmail: 'eva@example.com' }, attachTo: document.body })
  await flushPromises()
  return w
}

const ROW = /(^|\s)(sm|md|lg|xl|2xl):flex-row(\s|$)/

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('#4597 — project header layout', () => {
  it('never lays the action buttons in the same row as the title and goal', async () => {
    const w = await mountPage()
    const actions = w.find('[data-testid="project-actions"]')
    expect(actions.exists()).toBe(true)
    const goal = w.find('[data-testid="project-goal"]')
    // Walk up from the goal: no ancestor below the page may be a horizontal
    // flex row that also contains the actions.
    let el = goal.element.parentElement
    while (el && el.dataset?.testid !== 'project-page') {
      if (el.contains(actions.element)) {
        expect(el.className).not.toMatch(ROW)
      }
      el = el.parentElement
    }
  })

  it('lets the actions wrap on their own row', async () => {
    const w = await mountPage()
    const cls = w.find('[data-testid="project-actions"]').classes()
    expect(cls).toContain('flex')
    expect(cls).toContain('flex-wrap')
    expect(w.find('[data-testid="project-actions"]').find('[data-testid="project-members-open"]').exists()).toBe(true)
  })
})
