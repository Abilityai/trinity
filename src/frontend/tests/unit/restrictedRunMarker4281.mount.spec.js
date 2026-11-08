// @vitest-environment jsdom
/**
 * #4281 slice F — a schedule run that did not run at owner tier says so,
 * MOUNTED: the marker itself, and its wiring into the Tasks list and the
 * execution page.
 *
 * The fact is slice C's `turn_integrity.restricted_turn` (codes only). A
 * `success` is still a `success`; the marker says it ran without the shell
 * and why, worded by cause. Each surface has its own test id, so two of them
 * can never collide on one screen.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

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
  useRoute: () => ({ params: { name: 'fin', executionId: 'exec-r' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
}))
vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div />' },
}))

import RestrictedRunMarker from '../../src/components/RestrictedRunMarker.vue'
import TasksPanel from '../../src/components/TasksPanel.vue'
import ExecutionDetail from '../../src/views/ExecutionDetail.vue'

const record = (cause = 'creator_missing') => ({
  restricted_turn: { tier: 'share', kind: 'person', cause, schedule_id: 's1', creator_id: 7, shell: false },
})

describe('RestrictedRunMarker', () => {
  it('renders nothing for a run with no record', () => {
    const w = mount(RestrictedRunMarker, { props: { turnIntegrity: null, testid: 'm' } })
    expect(w.find('[data-testid="m"]').exists()).toBe(false)
    const other = mount(RestrictedRunMarker, { props: { turnIntegrity: JSON.stringify({ killed_notice: 'x' }), testid: 'm' } })
    expect(other.find('[data-testid="m"]').exists()).toBe(false)
  })

  it('a list badge, with the cause sentence on hover', () => {
    const b = mount(RestrictedRunMarker, { props: { turnIntegrity: JSON.stringify(record()), testid: 'm' } }).find('[data-testid="m"]')
    expect(b.text()).toBe('ran without shell')
    expect(b.attributes('title')).toMatch(/no longer exists/)
  })

  it('a line for a detail view says the sentence', () => {
    const w = mount(RestrictedRunMarker, {
      props: { turnIntegrity: JSON.stringify(record('agent_has_no_owner')), variant: 'line', testid: 'm' },
    })
    expect(w.find('[data-testid="m"]').text()).toMatch(/no owner on record/)
  })
})

const row = (id, over = {}) => ({
  id, schedule_id: 's1', agent_name: 'fin', status: 'success', message: `/${id}`,
  triggered_by: 'schedule', started_at: '2026-11-08T10:00:00Z', completed_at: '2026-11-08T10:01:00Z',
  duration_ms: 60000, turn_integrity: null, ...over,
})

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

describe('wired into the panels', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
  })

  it('the Tasks row of a restricted run carries the badge, an ordinary one does not', async () => {
    routes.get = vi.fn(async (url) => (url.includes('/executions')
      ? { data: [row('exec-r', { turn_integrity: JSON.stringify(record('creator_not_owner_tier')) }), row('exec-plain')] }
      : { data: [] }))
    const w = mount(TasksPanel, { props: { agentName: 'fin', agentStatus: 'running' } })
    await flush()
    const badges = w.findAll('[data-testid="task-run-restricted"]')
    expect(badges).toHaveLength(1)
    expect(badges[0].attributes('title')).toMatch(/person who created the schedule/)
    w.unmount()
  })

  it('the execution page says it in a sentence', async () => {
    routes.get = vi.fn(async (url) => {
      if (url.endsWith('/executions/exec-r')) {
        return { data: { ...row('exec-r'), response: 'done', error: null, turn_integrity: JSON.stringify(record('schedule_unreadable')) } }
      }
      return { data: {} }
    })
    const w = mount(ExecutionDetail, { global: { stubs: { 'router-link': true } } })
    await flush()
    expect(w.find('[data-testid="execution-restricted-line"]').text()).toMatch(/could not be read/)
    w.unmount()
  })
})
