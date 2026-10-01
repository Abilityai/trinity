// @vitest-environment jsdom
/**
 * ent#661 v3 — the hub, mounted (#2918: each predicate gates a write).
 *   - "Update" (health) exists only when the server says `can.set_health`;
 *     saving calls the store with the picked state and the note;
 *   - metric add/remove exist only for a contributor on a live project;
 *     remove calls the store with the agent and metric;
 *   - "Needs you" renders only the asks whose ids the project read returned,
 *     and counts the rest;
 *   - "I steward" lists what needs the steward and opens the project.
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
import ProjectHub from '@/components/portal/projects/ProjectHub.vue'
import ProjectsStewarding from '@/components/portal/projects/ProjectsStewarding.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const byTestId = (id) => document.querySelector(`[data-testid="${id}"]`)
let portal
let projects

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  portal = useClientPortalStore()
  projects = useProjectsStore()
  portal.fetchAsks = vi.fn()
})

const METRIC = {
  agent_name: 'scout', name: 'signups', label: 'Signups', type: 'gauge', unit: null, declared: true,
  latest: { value: 42, ts: '2026-09-30T10:00:00Z' }, stale: true, freshness: 'stale', trend: [1, 5, 9],
  last_point_at: '2026-09-30T10:00:00Z',
}

function project(over = {}) {
  return {
    id: 'prj_1', archived_at: null,
    can: { set_health: false, contribute: true },
    rollup: {
      by_status: { active: 2, 'pending-verification': 1 },
      awaiting_verification: [{ id: 'T-003', title: 'x', status: 'pending-verification' }],
      stuck: [], stale: [], health: null, health_due: true, last_activity_at: '2026-09-30T10:00:00Z',
    },
    metrics: [METRIC],
    asks: { mine: [], others_open: 0 },
    ...over,
  }
}

describe('health', () => {
  it('no Update without can.set_health', async () => {
    mount(ProjectHub, { props: { project: project() }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-health-update')).toBeNull()
    expect(byTestId('project-health').textContent).toContain('The steward posts one.')
  })

  it('the steward saves a state and a note', async () => {
    projects.setHealth = vi.fn().mockResolvedValue({})
    mount(ProjectHub, { props: { project: project({ can: { set_health: true, contribute: true } }) }, attachTo: document.body })
    await flushPromises()
    byTestId('project-health-update').click()
    await flushPromises()
    byTestId('project-health-at-risk').click()
    const note = document.getElementById('project-health-note')
    note.value = 'Vendor late.'
    note.dispatchEvent(new Event('input'))
    await flushPromises()
    byTestId('project-health-form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(projects.setHealth).toHaveBeenCalledWith('prj_1', 'at-risk', 'Vendor late.')
  })

  it('shows the current health and a due warning only when one exists', async () => {
    const p = project()
    p.rollup = { ...p.rollup, health: { state: 'off-track', note: 'Blocked on legal.', at: '2026-09-01T00:00:00Z', by: 'agent:scout' } }
    mount(ProjectHub, { props: { project: p }, attachTo: document.body })
    await flushPromises()
    const text = byTestId('project-health').textContent
    expect(text).toContain('Off track')
    expect(text).toContain('scout (agent)')
    expect(byTestId('project-health-due')).not.toBeNull()
  })
})

describe('roll-up', () => {
  it('names what needs attention and opens Tasks', async () => {
    const w = mount(ProjectHub, { props: { project: project() }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-rollup').textContent).toContain('2 active · 1 awaiting verification')
    const line = byTestId('project-attention-verify')
    expect(line.textContent).toContain('1 task to verify')
    expect(line.textContent).toContain('T-003')
    line.click()
    expect(w.emitted('open-tasks')).toHaveLength(1)
  })
})

describe('metrics', () => {
  it('a contributor can add and remove; remove calls the store', async () => {
    projects.unlinkMetric = vi.fn().mockResolvedValue({})
    mount(ProjectHub, { props: { project: project() }, attachTo: document.body })
    await flushPromises()
    const tile = byTestId('project-metric-scout-signups')
    expect(tile.textContent).toContain('42')
    expect(tile.textContent).toContain('Stale')
    expect(tile.querySelector('polyline')).not.toBeNull()
    expect(byTestId('project-metric-add')).not.toBeNull()
    tile.querySelector('button[aria-label="Remove Signups"]').click()
    await flushPromises()
    expect(projects.unlinkMetric).toHaveBeenCalledWith('prj_1', 'scout', 'signups')
  })

  it.each([
    ['a viewer', { can: { set_health: false, contribute: false } }],
    ['an archived project', { archived_at: '2026-09-30T00:00:00Z' }],
  ])('%s gets no add or remove', async (_label, over) => {
    mount(ProjectHub, { props: { project: project(over) }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-metric-add')).toBeNull()
    expect(byTestId('project-metric-scout-signups').querySelector('button')).toBeNull()
  })

  it('adding offers only what the server listed, and sends the pick', async () => {
    projects.metricOptions = vi.fn().mockResolvedValue([
      { agent_name: 'scout', metrics: [{ name: 'mrr', label: 'MRR', unit: 'usd' }] },
      { agent_name: 'quiet', metrics: [] },
    ])
    projects.linkMetric = vi.fn().mockResolvedValue({})
    mount(ProjectHub, { props: { project: project() }, attachTo: document.body })
    await flushPromises()
    byTestId('project-metric-add').click()
    await flushPromises()
    expect([...document.getElementById('project-metric-agent').options].map((o) => o.value)).toEqual(['scout'])
    byTestId('project-metric-form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(projects.linkMetric).toHaveBeenCalledWith('prj_1', 'scout', 'mrr')
  })
})

describe('needs you', () => {
  it('renders only my asks on this project and counts the others', async () => {
    portal.asksAvailable = true
    portal.asksLoaded = true
    portal.asks = [
      { id: 'q1', agent_name: 'scout', kind: 'question', title: 'Mine here', question: '?', status: 'pending', created_at: '2026-09-30T10:00:00Z' },
      { id: 'q9', agent_name: 'scout', kind: 'question', title: 'Mine elsewhere', question: '?', status: 'pending', created_at: '2026-09-30T10:00:00Z' },
    ]
    mount(ProjectHub, { props: { project: project({ asks: { mine: ['q1'], others_open: 2 } }) }, attachTo: document.body })
    await flushPromises()
    const card = byTestId('project-needs-you').textContent
    expect(card).toContain('Mine here')
    expect(card).not.toContain('Mine elsewhere')
    expect(byTestId('project-asks-others').textContent).toContain('2 asks are waiting on other people')
  })

  it('nothing waiting says so', async () => {
    mount(ProjectHub, { props: { project: project() }, attachTo: document.body })
    await flushPromises()
    expect(byTestId('project-needs-you').textContent).toContain('Nothing on this project is waiting on you.')
  })
})

describe('I steward', () => {
  it('lists what needs the steward and opens the project', async () => {
    projects.fetchStewarding = vi.fn()
    projects.stewarding = {
      loaded: true, loading: false, error: null,
      rows: [{
        project: { id: 'prj_1', name: 'Q4' }, health: { state: 'at-risk' }, health_due: false,
        awaiting_verification: [{ id: 'T-1' }], stuck: [], stale: [], open_asks: 2, quiet: false,
      }],
    }
    const w = mount(ProjectsStewarding, { attachTo: document.body })
    await flushPromises()
    const row = byTestId('stewarding-row-prj_1')
    expect(row.textContent).toContain('At risk')
    expect(row.textContent).toContain('1 task to verify')
    expect(row.textContent).toContain('2 open asks')
    row.click()
    expect(w.emitted('open')[0]).toEqual(['prj_1'])
  })

  it('an empty read explains what a steward does', async () => {
    projects.fetchStewarding = vi.fn()
    projects.stewarding = { loaded: true, loading: false, error: null, rows: [] }
    mount(ProjectsStewarding, { attachTo: document.body })
    await flushPromises()
    expect(byTestId('stewarding-empty').textContent).toContain("You don't steward any projects")
  })
})
