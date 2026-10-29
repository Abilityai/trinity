// @vitest-environment jsdom
/**
 * trinity-enterprise#875 — below the owner tier a list is the viewer's own
 * executions, and the Tasks panel says so, MOUNTED.
 *
 * A share holder's list is shorter than the owner's (the server narrows it to
 * the turns they started), and a shorter list with no explanation reads as lost
 * data. The panel takes `ownRowsOnly` (`!agent.can_share`, from Agent Detail)
 * and shows one fixed line, in the list and in the empty state; an owner sees
 * neither. The webhook controls of the Schedules panel are gated on the
 * server's per-schedule `can_manage_webhook` (the client has no numeric user
 * id to compare with the creator's), and the status read, which returns the
 * live token URL, is not even requested for a viewer who may not use it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const server = vi.hoisted(() => ({ get: null, post: null, delete: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => server.get(...a)),
    post: vi.fn((...a) => server.post(...a)),
    delete: vi.fn((...a) => server.delete(...a)),
    put: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import axios from 'axios'
import TasksPanel from '../../src/components/TasksPanel.vue'
import SchedulesPanel from '../../src/components/SchedulesPanel.vue'

const task = (id) => ({
  id, schedule_id: '__manual__', agent_name: 'fin', status: 'success', message: `/${id}`,
  triggered_by: 'manual', started_at: '2026-10-08T10:00:00Z', completed_at: '2026-10-08T10:01:00Z',
})

const schedule = (id, canManage) => ({
  id, agent_name: 'fin', name: `sched ${id}`, cron_expression: '0 9 * * *', message: 'm', enabled: true,
  timezone: 'UTC', description: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  last_run_at: null, next_run_at: null, can_manage_webhook: canManage,
})

async function flush() {
  for (let i = 0; i < 5; i++) { await nextTick(); await flushPromises() }
}

beforeEach(() => {
  setActivePinia(createPinia())
  axios.get.mockClear()
})

describe('ent#875 Tasks panel says whose executions it shows', () => {
  function serve(rows) {
    server.get = vi.fn(async (url) => {
      if (/\/executions\?limit=/.test(url)) return { data: rows }
      if (url.endsWith('/queue')) return { data: { is_busy: false, queue_length: 0 } }
      if (url.endsWith('/executions/running')) return { data: { executions: [] } }
      return { data: {} }
    })
  }
  const mountTasks = (props) => mount(TasksPanel, {
    props: { agentName: 'fin', agentStatus: 'running', ...props },
    global: { stubs: { ModelSelector: true, SkeletonLoader: true, LoadFailed: true, 'router-link': true } },
  })

  it('a share holder with rows sees the one-line note above the list', async () => {
    serve([task('a'), task('b')])
    const w = mountTasks({ ownRowsOnly: true })
    await flush()
    const note = w.find('[data-testid="own-rows-note"]')
    expect(note.exists()).toBe(true)
    expect(note.text()).toBe('This agent is shared with you: you see the tasks you started.')
    expect(w.find('[data-testid="task-list"]').text()).toContain('/a')
  })

  it('a share holder with no rows is told why, not just "No tasks yet"', async () => {
    serve([])
    const w = mountTasks({ ownRowsOnly: true })
    await flush()
    expect(w.text()).toContain('No tasks yet')
    expect(w.find('[data-testid="own-rows-note"]').exists()).toBe(true)
  })

  it('the owner sees no note, with rows or without', async () => {
    serve([task('a')])
    const withRows = mountTasks({})
    await flush()
    expect(withRows.find('[data-testid="own-rows-note"]').exists()).toBe(false)
    serve([])
    const empty = mountTasks({ ownRowsOnly: false })
    await flush()
    expect(empty.find('[data-testid="own-rows-note"]').exists()).toBe(false)
  })
})

describe('ent#875 Schedules panel webhook controls follow the server flag', () => {
  function serve(schedules) {
    server.get = vi.fn(async (url) => {
      if (/\/schedules$/.test(url)) return { data: schedules }
      if (/\/webhook$/.test(url)) {
        return { data: { schedule_id: 's', has_token: false, webhook_enabled: false, webhook_url: null, auth_enabled: false, has_secret: false } }
      }
      return { data: {} }
    })
    server.post = vi.fn(async () => ({ data: {} }))
    server.delete = vi.fn(async () => ({ data: {} }))
  }
  const mountSchedules = () => mount(SchedulesPanel, {
    props: { agentName: 'fin', agentStatus: 'running' },
    global: { stubs: { 'router-link': true, ModelSelector: true, SkeletonLoader: true, LoadFailed: true } },
  })
  const webhookButtons = (w) => w.findAll('button').filter((b) => b.text().startsWith('Webhook'))
  const webhookReads = () => axios.get.mock.calls.filter(([u]) => /\/webhook$/.test(u)).length

  it('renders the Webhook control only on a schedule the viewer may manage', async () => {
    serve([schedule('mine', true), schedule('theirs', false)])
    const w = mountSchedules()
    await flush()
    expect(w.text()).toContain('sched mine')
    expect(w.text()).toContain('sched theirs')
    expect(webhookButtons(w)).toHaveLength(1)
  })

  it('a viewer who may not manage any webhook gets no control and no status request', async () => {
    serve([schedule('a', false), schedule('b', false)])
    const w = mountSchedules()
    await flush()
    expect(webhookButtons(w)).toHaveLength(0)
    expect(webhookReads()).toBe(0)
  })

  it('opening a webhook the viewer may manage reads its status once', async () => {
    serve([schedule('mine', true)])
    const w = mountSchedules()
    await flush()
    await webhookButtons(w)[0].trigger('click')
    await flush()
    expect(webhookReads()).toBe(1)
    expect(w.text()).toContain('Enable webhook')
  })

  it('an older backend that sends no flag keeps the controls (the server gate is the boundary)', async () => {
    const legacy = schedule('legacy', true)
    delete legacy.can_manage_webhook
    serve([legacy])
    const w = mountSchedules()
    await flush()
    expect(webhookButtons(w)).toHaveLength(1)
  })
})
