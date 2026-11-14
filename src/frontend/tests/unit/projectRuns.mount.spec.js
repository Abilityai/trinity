// @vitest-environment jsdom
/**
 * trinity-enterprise#1105 — the project's Runs tab, mounted.
 *
 * Drives ProjectRuns through the real projects store with the Workspace HTTP
 * client stubbed, so the request params are the ones the component asks for:
 *   - rows render (status chip, agent, model, cost, queue age "—" when null, task);
 *   - a fenced row is muted, says "A run you can't open" and carries no detail;
 *   - the empty state needs a succeeded fetch returning zero rows;
 *   - a failed fetch shows LoadFailed, and Retry refetches;
 *   - changing a filter refetches with the new params;
 *   - the cost tile reads "Cost of runs you can open" when any row is fenced.
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

import { portalHttp } from '@/stores/clientPortal'
import ProjectRuns from '@/components/portal/projects/ProjectRuns.vue'

const OPEN = {
  execution_id: 'exec_1', agent_name: 'scout', status: 'success', model_used: 'sonnet', cost: 0.25,
  queued_at: '2026-11-10T10:00:00Z', started_at: '2026-11-10T10:00:02Z', completed_at: '2026-11-10T10:01:00Z',
  queue_age_ms: null, duration_ms: 58000, triggered_by: 'chat', source: 'task',
  dispatched_by: { kind: 'person', name: 'eva@example.com' }, task: { id: 'T-003', title: 'Draft the brief' },
  parent_execution_id: null, fenced: false,
}
const FENCED = { agent_name: 'bard', status: 'failed', fenced: true }

function payload(rows, totals = {}) {
  return {
    project_id: 'prj_1', window: { since: 'a', until: 'b' }, retention_days: 90, rows,
    totals: { scope: 'openable', runs: rows.length, fenced_runs: 0, cost: 0.25, queue_age_p50_ms: null, by_agent: [], ...totals },
    next_cursor: null,
  }
}

const byTestId = (w, id) => w.find(`[data-testid="${id}"]`)

async function mountRuns() {
  const w = mount(ProjectRuns, { props: { projectId: 'prj_1', agents: [{ agent_name: 'scout' }, { agent_name: 'bard' }] } })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  portalHttp.get = vi.fn()
})

describe('ProjectRuns', () => {
  it('renders openable rows with their detail and asks the executions route', async () => {
    portalHttp.get.mockResolvedValue({ data: payload([OPEN]) })
    const w = await mountRuns()
    expect(portalHttp.get).toHaveBeenCalledTimes(1)
    expect(portalHttp.get.mock.calls[0][0]).toBe('/api/enterprise/projects/prj_1/executions')
    const row = byTestId(w, 'project-run')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Success')
    expect(row.text()).toContain('scout')
    expect(row.text()).toContain('sonnet')
    expect(row.text()).toContain('$0.250')
    expect(byTestId(w, 'project-run-queue').text()).toContain('—')
    expect(byTestId(w, 'project-run-task').text()).toContain('Draft the brief')
    expect(byTestId(w, 'project-runs-cost-label').text()).toBe('Cost')
  })

  it('mutes a fenced row and shows no detail fields', async () => {
    portalHttp.get.mockResolvedValue({ data: payload([OPEN, FENCED], { fenced_runs: 1 }) })
    const w = await mountRuns()
    const fenced = byTestId(w, 'project-run-fenced')
    expect(fenced.exists()).toBe(true)
    expect(fenced.classes()).toContain('opacity-70')
    expect(fenced.text()).toContain("A run you can't open")
    expect(fenced.text()).toContain('bard')
    expect(fenced.text()).toContain('Failed')
    expect(fenced.find('[data-testid="project-run-cost"]').exists()).toBe(false)
    expect(fenced.find('[data-testid="project-run-queue"]').exists()).toBe(false)
    expect(fenced.find('[data-testid="project-run-task"]').exists()).toBe(false)
  })

  it('labels the cost tile for openable runs only when any row is fenced', async () => {
    portalHttp.get.mockResolvedValue({ data: payload([FENCED], { fenced_runs: 1 }) })
    const w = await mountRuns()
    expect(byTestId(w, 'project-runs-cost-label').text()).toBe('Cost of runs you can open')
  })

  it('shows the empty state after a fetch that returned zero rows', async () => {
    portalHttp.get.mockResolvedValue({ data: payload([]) })
    const w = await mountRuns()
    expect(byTestId(w, 'project-runs-empty').text()).toBe(
      'No runs on this project yet. Runs started from its chats, its tasks or its steward show here.')
  })

  it('shows the failed state, never the empty copy, and Retry refetches', async () => {
    portalHttp.get.mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500, data: {} } }))
    const w = await mountRuns()
    expect(byTestId(w, 'project-runs-failed').exists()).toBe(true)
    expect(byTestId(w, 'project-runs-empty').exists()).toBe(false)
    portalHttp.get.mockResolvedValue({ data: payload([OPEN]) })
    const retry = w.findAll('button').find((b) => /retry|try again/i.test(b.text()))
    expect(retry).toBeTruthy()
    await retry.trigger('click')
    await flushPromises()
    expect(portalHttp.get).toHaveBeenCalledTimes(2)
    expect(byTestId(w, 'project-run').exists()).toBe(true)
  })

  it('a filter change refetches with the new params', async () => {
    portalHttp.get.mockResolvedValue({ data: payload([OPEN]) })
    const w = await mountRuns()
    const first = portalHttp.get.mock.calls[0][1].params
    expect(first.agent).toBeUndefined()
    expect(Date.now() - Date.parse(first.since)).toBeGreaterThan(29 * 86400000)

    await w.find('#runs-agent').setValue('bard')
    await flushPromises()
    expect(portalHttp.get).toHaveBeenCalledTimes(2)
    expect(portalHttp.get.mock.calls[1][1].params.agent).toBe('bard')

    await w.find('#runs-status').setValue('failed')
    await flushPromises()
    expect(portalHttp.get.mock.calls[2][1].params.status).toBe('failed')

    await w.find('#runs-window').setValue('7')
    await flushPromises()
    const since = Date.parse(portalHttp.get.mock.calls[3][1].params.since)
    expect(Date.now() - since).toBeLessThan(8 * 86400000)
    expect(Date.now() - since).toBeGreaterThan(6 * 86400000)
  })

  it('Load more sends next_cursor and appends the page', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: { ...payload([OPEN]), next_cursor: 'cur_2' } })
    const w = await mountRuns()
    portalHttp.get.mockResolvedValueOnce({ data: payload([{ ...OPEN, execution_id: 'exec_2' }]) })
    await byTestId(w, 'project-runs-more').trigger('click')
    await flushPromises()
    expect(portalHttp.get.mock.calls[1][1].params.cursor).toBe('cur_2')
    expect(w.findAll('[data-testid="project-run"]').length).toBe(2)
  })
})
