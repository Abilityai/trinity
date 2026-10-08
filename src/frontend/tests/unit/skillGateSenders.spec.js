// @vitest-environment jsdom
/**
 * trinity#3274 — the UI's run senders say "waiting for approval", not "error".
 *
 * A request naming a gated skill (trinity-enterprise#751) answers `/task` with
 * 202 `pending_approval`: nothing ran, so there is no execution to poll. Each
 * sender used to read `execution_id` off it — and threw "No execution_id
 * returned", polled `/executions/undefined`, showed the JSON body as a result,
 * or navigated to a run that does not exist. The public link never gets a 202
 * (its turn is held in the background) and polled its `skipped` row for 30
 * minutes before "Request timed out". Each now shows the server's own message
 * and stops.
 *
 * MOUNTED, each sender through its own send function, with `axios` stubbed by
 * URL. `/m` is mounted in `mobileAdminSkillGate.spec.js` (it uses `boundedHttp`).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routes = vi.hoisted(() => ({ get: null, post: null }))

vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => routes.get(...a)),
    post: vi.fn((...a) => routes.post(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { token: 'tok' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
// uPlot reads `matchMedia` at module load (declaredMetricsTiles.spec.js).
vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div />' },
}))

import axios from 'axios'
import ChatPanel from '../../src/components/ChatPanel.vue'
import DashboardPanel from '../../src/components/DashboardPanel.vue'
import PlaybooksPanel from '../../src/components/PlaybooksPanel.vue'
import TasksPanel from '../../src/components/TasksPanel.vue'
import PublicChat from '../../src/views/PublicChat.vue'
import { useAgentsStore } from '../../src/stores/agents'

const AGENT = 'fin'
const NOTICE = 'Not run: the skill pay-invoice on fin needs approval before it can run. ' +
  'Request gate-abc is waiting for a decision.'
const REFUSED = 'The skill pay-invoice is not installed on fin.'
const PENDING = { status: 202, data: { status: 'pending_approval', code: 'approval_pending',
  request_id: 'gate-abc', skills: ['pay-invoice'], outcome_delivery: 'inbox', message: NOTICE } }
const refusal = () => Object.assign(new Error('Request failed with status code 409'), {
  response: { status: 409, headers: { 'x-trinity-error-code': 'gated_skill_not_installed' },
    data: { detail: { status: 'refused', code: 'gated_skill_not_installed', message: REFUSED } } },
})

let wrapper
const polled = () => axios.get.mock.calls.map(c => c[0]).filter(u => /\/executions\//.test(u))

function serve(get = {}, post = {}) {
  routes.get = vi.fn(async (url) => {
    for (const [pattern, answer] of Object.entries(get)) {
      if (url.includes(pattern)) return typeof answer === 'function' ? answer(url) : answer
    }
    return { data: [] }
  })
  routes.post = vi.fn(async (url, body) => {
    for (const [pattern, answer] of Object.entries(post)) {
      if (url.includes(pattern)) return typeof answer === 'function' ? answer(url, body) : answer
    }
    return { data: {} }
  })
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

// ---------------------------------------------------------------------------
// ChatPanel — the agent page's Chat tab (async /task)
// ---------------------------------------------------------------------------

describe('ChatPanel', () => {
  async function chatPanel() {
    wrapper = mount(ChatPanel, {
      props: { agentName: AGENT, agentStatus: 'running' },
      global: { stubs: { ModelSelector: true } },
    })
    await flush()
    return wrapper
  }

  it('shows the notice as a platform line, keeps the message, and polls nothing', async () => {
    serve({ '/chat/sessions': { data: [] } }, { '/task': PENDING })
    const w = await chatPanel()
    await w.vm.sendMessage('/pay-invoice 100 EUR')
    await flush()

    expect(w.find('[data-testid="chat-system-line"]').text()).toBe(NOTICE)
    expect(w.text()).toContain('/pay-invoice 100 EUR')
    expect(w.text()).not.toContain('No execution_id')
    expect(polled()).toEqual([])
  })

  it('leaves the held request out of the next turn\'s history', async () => {
    serve({ '/chat/sessions': { data: [] } }, { '/task': PENDING })
    const w = await chatPanel()
    await w.vm.sendMessage('/pay-invoice 100 EUR')
    await flush()
    await w.vm.sendMessage('what is on today?')
    await flush()

    const second = axios.post.mock.calls.filter(c => c[0].endsWith('/task'))[1][1]
    expect(second.message).not.toContain('/pay-invoice')
    expect(second.message).not.toContain(NOTICE)
  })

  it('names a refusal instead of an object', async () => {
    serve({ '/chat/sessions': { data: [] } }, { '/task': () => Promise.reject(refusal()) })
    const w = await chatPanel()
    await w.vm.sendMessage('/pay-invoice 100 EUR')
    await flush()
    expect(w.text()).toContain(REFUSED)
    expect(w.text()).not.toContain('"code"')              // not the detail object as JSON
  })
})

// ---------------------------------------------------------------------------
// PublicChat — a public link (async; held in the background, row `skipped`)
// ---------------------------------------------------------------------------

describe('PublicChat', () => {
  it('stops at a skipped turn and shows its notice instead of polling to a timeout', async () => {
    serve({
      '/api/public/link/': { data: { valid: true, agent_available: true, require_email: false,
        agent_name: AGENT } },
      '/status': { data: { execution_id: 'e1', status: 'skipped', response: null, error: NOTICE,
        gate: 'held' } },
      '/api/public/history/': { data: { messages: [] } },
    }, { '/api/public/chat/': { data: { status: 'accepted', execution_id: 'e1', async_mode: true } } })
    wrapper = mount(PublicChat)
    await flush()
    vi.useFakeTimers()
    wrapper.vm.sendMessage('/pay-invoice 100 EUR')
    // Two poll intervals: a sender that does not stop at `skipped` polls again
    // and shows nothing — an assertion failure, not a test timeout.
    await vi.advanceTimersByTimeAsync(10_000)
    const pollsAfterSkip = polled().length
    const line = wrapper.find('[data-testid="chat-system-line"]')
    wrapper.vm.chatLoading = false          // ends a poll loop that did not stop on its own
    vi.useRealTimers()
    await flush()

    expect(pollsAfterSkip).toBe(1)
    expect(line.exists() && line.text()).toBe(NOTICE)
    expect(wrapper.text()).not.toContain('timed out')
  })
})

describe('PublicChat — a refusal', () => {
  it('shows a gate refusal as an error, not as a waiting notice', async () => {
    const REFUSAL_ROW = 'Refused by the skill gate (gated_skill_not_installed): ' + REFUSED
    serve({
      '/api/public/link/': { data: { valid: true, agent_available: true, require_email: false,
        agent_name: AGENT } },
      '/status': { data: { execution_id: 'e1', status: 'skipped', response: null,
        error: REFUSAL_ROW, gate: 'refused' } },
      '/api/public/history/': { data: { messages: [] } },
    }, { '/api/public/chat/': { data: { status: 'accepted', execution_id: 'e1', async_mode: true } } })
    wrapper = mount(PublicChat)
    await flush()
    vi.useFakeTimers()
    wrapper.vm.sendMessage('/pay-invoice 100 EUR')
    await vi.advanceTimersByTimeAsync(10_000)
    wrapper.vm.chatLoading = false
    vi.useRealTimers()
    await flush()

    expect(wrapper.find('[data-testid="chat-system-line"]').exists()).toBe(false)
    expect(wrapper.vm.chatError).toBe(REFUSAL_ROW)
  })
})

// ---------------------------------------------------------------------------
// TasksPanel — the Tasks tab (sync /task)
// ---------------------------------------------------------------------------

describe('TasksPanel', () => {
  async function tasksPanel(post) {
    serve({}, { '/task': post })
    wrapper = mount(TasksPanel, {
      props: { agentName: AGENT, agentStatus: 'running' },
      global: { stubs: { ModelSelector: true, SkeletonLoader: true, LoadFailed: true, RouterLink: true } },
    })
    await flush()
    wrapper.vm.newTaskMessage = '/pay-invoice 100 EUR'
    await wrapper.vm.runNewTask()
    await flush()
    return wrapper
  }

  it('keeps the request as waiting for approval with the notice, not a success', async () => {
    const w = await tasksPanel(PENDING)
    expect(w.text()).toContain('pending_approval')
    expect(w.text()).toContain(NOTICE)
    expect(w.text()).not.toContain('"status"')            // not the JSON body
  })

  it('keeps a refused request with its named reason (no server row exists)', async () => {
    const w = await tasksPanel(() => Promise.reject(refusal()))
    expect(w.text()).toContain(REFUSED)
    expect(w.text()).not.toContain('"code"')              // not the detail object as JSON
  })
})

// ---------------------------------------------------------------------------
// PlaybooksPanel — Run on a playbook (sync /task)
// ---------------------------------------------------------------------------

describe('PlaybooksPanel', () => {
  async function playbooks(post) {
    serve({ '/playbooks': { data: { skills: [] } } }, { '/task': post })
    const notify = vi.fn()
    wrapper = mount(PlaybooksPanel, { props: { agentName: AGENT, agentStatus: 'running', notify } })
    await flush()
    await wrapper.vm.runSkill({ name: 'pay-invoice', user_invocable: true })
    await flush()
    return { w: wrapper, notify }
  }

  it('tells the notice and opens no run', async () => {
    const { w, notify } = await playbooks(PENDING)
    expect(notify).toHaveBeenCalledWith(NOTICE, 'info', { timeout: 8000 })
    expect(w.emitted('run-with-instructions')).toBeUndefined()
    expect(w.text()).not.toContain('started')
  })

  it('names a refusal in the persistent error toast, not a 3-second one', async () => {
    const { w, notify } = await playbooks(() => Promise.reject(refusal()))
    expect(notify).toHaveBeenCalledWith(REFUSED, 'error')
    expect(w.text()).not.toContain('"code"')              // not the detail object as JSON
  })
})

// ---------------------------------------------------------------------------
// DashboardPanel — Update Dashboard (sync /task)
// ---------------------------------------------------------------------------

describe('DashboardPanel', () => {
  async function dashboard(post) {
    serve({ '/playbooks': { data: { skills: [{ name: 'update-dashboard' }] } } }, { '/task': post })
    useAgentsStore().getAgentDashboard = vi.fn(async () => ({
      has_dashboard: true, stale: false, config: { title: 'Ops', sections: [] },
      last_modified: '2026-10-07T09:00:00Z',
    }))
    const notify = vi.fn()
    wrapper = mount(DashboardPanel, { props: { agentName: AGENT, agentStatus: 'running', notify } })
    await flush()
    await wrapper.vm.triggerUpdateDashboard()
    await flush()
    return { w: wrapper, notify }
  }

  it('tells the notice and stops waiting', async () => {
    const { w, notify } = await dashboard(PENDING)
    expect(notify).toHaveBeenCalledWith(NOTICE, 'info', { timeout: 8000 })
    expect(w.vm.updatingDashboard).toBe(false)
  })

  it('tells a refusal as an error', async () => {
    const { notify } = await dashboard(() => Promise.reject(refusal()))
    expect(notify).toHaveBeenCalledWith(REFUSED, 'error')
  })
})
