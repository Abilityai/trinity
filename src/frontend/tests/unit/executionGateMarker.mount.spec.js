// @vitest-environment jsdom
/**
 * trinity-enterprise#754 — a self-approved run says so, MOUNTED.
 *
 * The marker renders only the server's two booleans (no email reaches the
 * page) and words them for the viewer: "you are the approver" only when the
 * server says the caller is the person who ran it. Its wiring into the Tasks
 * row and the execution page is mounted here too, against the real panels.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

// The panels read through raw axios and the shared api client; both resolve
// to this instance (`axios.create` returns it), as in skillGateSenders.spec.js.
const routes = vi.hoisted(() => ({ get: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => routes.get(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { name: 'fin', executionId: 'exec-self' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}))
vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div />' },
}))

import ExecutionGateMarker from '../../src/components/skills/ExecutionGateMarker.vue'
import TasksPanel from '../../src/components/TasksPanel.vue'
import ExecutionDetail from '../../src/views/ExecutionDetail.vue'

const marker = (props) => mount(ExecutionGateMarker, { props })

describe('ExecutionGateMarker', () => {
  it('renders nothing for an ordinary run', () => {
    expect(marker({ selfApproved: false }).find('[data-testid="execution-gate-marker"]').exists()).toBe(false)
  })

  it('a list row badge, with the sentence on hover', () => {
    const b = marker({ selfApproved: true, byViewer: true }).find('[data-testid="execution-gate-marker"]')
    expect(b.text()).toBe('ran without approval')
    expect(b.attributes('title')).toBe('Ran without approval: you are the approver.')
  })

  it('says "you" only to the person who ran it', () => {
    const line = marker({ selfApproved: true, byViewer: false, variant: 'line' })
    expect(line.text()).toBe('Ran without approval: its approver started it.')
    const mine = marker({ selfApproved: true, byViewer: true, variant: 'line' })
    expect(mine.text()).toBe('Ran without approval: you are the approver.')
  })
})

const row = (id, over = {}) => ({
  id, schedule_id: '__manual__', agent_name: 'fin', status: 'success', message: `/${id}`,
  triggered_by: 'manual', started_at: '2026-10-08T10:00:00Z', completed_at: '2026-10-08T10:01:00Z',
  duration_ms: 60000, gate_self_approved: false, gate_self_approved_by_viewer: false, ...over,
})

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

describe('wired into the panels', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
  })

  it('the Tasks row of a self-approved run carries the marker, an ordinary one does not', async () => {
    routes.get = vi.fn(async (url) => (url.includes('/executions')
      ? { data: [row('exec-self', { gate_self_approved: true, gate_self_approved_by_viewer: true }), row('exec-plain')] }
      : { data: [] }))
    const w = mount(TasksPanel, { props: { agentName: 'fin', agentStatus: 'running' } })
    await flush()
    const markers = w.findAll('[data-testid="execution-gate-marker"]')
    expect(markers).toHaveLength(1)
    expect(markers[0].attributes('title')).toBe('Ran without approval: you are the approver.')
    w.unmount()
  })

  it('the execution page says it, in the origin card', async () => {
    routes.get = vi.fn(async (url) => {
      if (url.endsWith('/executions/exec-self')) {
        return { data: { ...row('exec-self'), response: 'done', error: null,
          gate_self_approved: true, gate_self_approved_by_viewer: false } }
      }
      return { data: {} }
    })
    const w = mount(ExecutionDetail, { global: { stubs: { 'router-link': true } } })
    await flush()
    expect(w.find('[data-testid="execution-gate-marker"]').text())
      .toBe('Ran without approval: its approver started it.')
    w.unmount()
  })
})
