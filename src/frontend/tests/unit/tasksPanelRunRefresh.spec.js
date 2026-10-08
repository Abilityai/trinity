// @vitest-environment jsdom
/**
 * trinity-enterprise#754 — a skill's Run settles on the Tasks tab without a
 * Refresh, MOUNTED.
 *
 * Run on the Skills tab is accepted asynchronously and opens the Tasks tab on
 * the run while it is still going. The panel used to read its rows once, and
 * its 5 s poll refreshed only the queue chip, so the row said "running" until
 * Refresh (found on the localhost eyeball). Now the poll re-reads the list
 * while any loaded row is in flight and stops when none is; the highlighted
 * row is opened and scrolled to once, not on every read; a task typed into
 * the panel is never listed twice; only the latest read is applied; and a row
 * that settles shows its result, not details read while it ran.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

// The panel reads through raw axios (and the auth store through the api
// client, whose `axios.create` returns the same instance).
const server = vi.hoisted(() => ({ get: null, post: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => server.get(...a)),
    post: vi.fn((...a) => server.post(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import axios from 'axios'
import TasksPanel from '../../src/components/TasksPanel.vue'

const row = (id, status, over = {}) => ({
  id, schedule_id: '__manual__', agent_name: 'fin', status, message: `/${id}`, triggered_by: 'manual',
  started_at: '2026-10-08T10:00:00Z', completed_at: status === 'running' ? null : '2026-10-08T10:01:00Z',
  duration_ms: null, ...over,
})

function deferred() {
  let resolve
  const promise = new Promise((res) => { resolve = res })
  return { promise, resolve }
}

let lists     // agent → the rows the server lists now
let details   // execution id → its details
let slow      // agent → a deferred the next list read waits on
const LIST = /^\/api\/agents\/([^/]+)\/executions\?limit=100$/

function serve() {
  server.get = vi.fn(async (url) => {
    const list = url.match(LIST)
    if (list) {
      const hold = slow[list[1]]
      if (hold) { slow[list[1]] = null; return hold.promise }
      return { data: lists[list[1]].map(r => ({ ...r })) }
    }
    if (url.endsWith('/queue')) return { data: { is_busy: false, queue_length: 0 } }
    if (url.endsWith('/executions/running')) return { data: { executions: [] } }
    const one = url.match(/\/executions\/([^/?]+)$/)
    if (one) return { data: { ...details[one[1]] } }
    return { data: {} }
  })
  server.post = vi.fn(async () => ({ data: {} }))
}

const listReads = (agent) => axios.get.mock.calls.filter(([u]) => u === `/api/agents/${agent}/executions?limit=100`).length
const detailReads = (id) => axios.get.mock.calls.filter(([u]) => u.endsWith(`/executions/${id}`)).length

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}
async function tick() {         // one poll
  vi.advanceTimersByTime(5000)
  await flush()
}

let wrapper
function mountPanel(props = {}) {
  wrapper = mount(TasksPanel, {
    props: { agentName: 'fin', agentStatus: 'running', ...props },
    global: { stubs: { ModelSelector: true, SkeletonLoader: true, LoadFailed: true, 'router-link': true } },
  })
  return wrapper
}
const listText = (w) => w.find('[data-testid="task-list"]').text()
const message = (w, text) => w.findAll('p').find(p => p.text() === text)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  axios.get.mockClear()
  axios.post.mockClear()
  lists = {}
  details = {}
  slow = {}
  serve()
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
})

describe('the poll re-reads the list while a run is in flight', () => {
  it('a run that opened here running settles without a Refresh, and the re-reads stop', async () => {
    lists = { fin: [row('exec-1', 'running'), row('exec-0', 'success')] }
    const w = mountPanel({ highlightExecutionId: 'exec-1' })
    await flush()
    expect(listReads('fin')).toBe(1)

    await tick()                                   // still running: read again
    expect(listReads('fin')).toBe(2)
    expect(listText(w)).toContain('running')

    lists.fin = [row('exec-1', 'success'), row('exec-0', 'success')]
    await tick()
    expect(listReads('fin')).toBe(3)
    expect(listText(w)).not.toContain('running')

    await tick()
    await tick()
    expect(listReads('fin')).toBe(3)               // nothing in flight: queue only
  })

  it.each(['queued', 'running', 'pending_retry'])('a %s row is re-read', async (status) => {
    lists = { fin: [row('exec-1', status)] }
    mountPanel()
    await flush()
    await tick()
    expect(listReads('fin')).toBe(2)
  })

  it.each(['success', 'failed', 'cancelled', 'skipped'])('a list of %s rows is not', async (status) => {
    lists = { fin: [row('exec-1', status)] }
    mountPanel()
    await flush()
    await tick()
    expect(listReads('fin')).toBe(1)
  })

  it('never stacks a read on one still out', async () => {
    lists = { fin: [row('exec-1', 'running')] }
    mountPanel()
    await flush()
    slow.fin = deferred()
    const held = slow.fin
    await tick()                                   // this read hangs
    await tick()
    await tick()
    expect(listReads('fin')).toBe(2)
    held.resolve({ data: [row('exec-1', 'running')] })
    await flush()
    await tick()
    expect(listReads('fin')).toBe(3)
  })
})

describe('the highlighted run', () => {
  it('is opened and scrolled to once, not on every re-read', async () => {
    lists = { fin: [row('exec-1', 'running'), row('exec-0', 'success')] }
    const w = mountPanel({ highlightExecutionId: 'exec-1' })
    await flush()
    expect(w.vm.expandedTaskId).toBe('exec-1')
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1)

    await message(w, '/exec-1').trigger('click')   // the user closes it
    await flush()
    expect(w.vm.expandedTaskId).toBeNull()

    await tick()
    await tick()
    expect(listReads('fin')).toBe(3)
    expect(w.vm.expandedTaskId).toBeNull()
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1)
  })
})

describe('a task typed into the panel', () => {
  it('is not listed twice while its call is out', async () => {
    lists = { fin: [row('exec-other', 'running')] }   // another run is in flight
    const call = deferred()
    server.post = vi.fn(() => call.promise)
    const w = mountPanel()
    await flush()

    w.vm.newTaskMessage = 'say hello'
    const run = w.vm.runNewTask()
    await flush()
    // The server lists the typed task as soon as it starts it.
    lists.fin = [row('exec-typed', 'running', { message: 'say hello' }), row('exec-other', 'running')]
    await tick()
    expect(listReads('fin')).toBe(1)               // the poll held off
    expect(listText(w).split('say hello').length - 1).toBe(1)

    lists.fin = [row('exec-typed', 'success', { message: 'say hello' }), row('exec-other', 'running')]
    call.resolve({ data: { response: 'hello', execution_id: 'exec-typed' } })
    await run
    await flush()
    expect(listReads('fin')).toBe(2)               // the call re-read the list itself
    expect(listText(w).split('say hello').length - 1).toBe(1)
  })
})

describe('only the latest read is applied', () => {
  it("a slow read for the agent the page left is not shown as the next agent's", async () => {
    lists = {
      fin: [row('exec-1', 'running')],
      ops: [row('ops-1', 'success', { agent_name: 'ops' })],
    }
    const w = mountPanel()
    await flush()
    slow.fin = deferred()
    const held = slow.fin
    await tick()                                   // fin's re-read hangs
    await w.setProps({ agentName: 'ops' })
    await flush()
    expect(listText(w)).toContain('/ops-1')

    held.resolve({ data: [row('exec-1', 'success')] })
    await flush()
    expect(listText(w)).toContain('/ops-1')
    expect(listText(w)).not.toContain('/exec-1')
  })
})

describe('a row that settles', () => {
  it('while open, shows its result there', async () => {
    lists = { fin: [row('exec-1', 'running')] }
    details = { 'exec-1': { response: null, error: null } }
    const w = mountPanel()
    await flush()
    await message(w, '/exec-1').trigger('click')   // opened while it runs
    await flush()
    expect(listText(w)).toContain('No response or error recorded')

    lists.fin = [row('exec-1', 'success')]
    details['exec-1'] = { response: 'hello from the run', error: null }
    await tick()
    expect(listText(w)).toContain('hello from the run')
  })

  it('while closed, reads its details afresh when opened', async () => {
    lists = { fin: [row('exec-1', 'running')] }
    details = { 'exec-1': { response: null, error: null } }
    const w = mountPanel()
    await flush()
    await message(w, '/exec-1').trigger('click')   // opened while it runs
    await flush()
    await message(w, '/exec-1').trigger('click')   // and closed
    await flush()

    lists.fin = [row('exec-1', 'success')]
    details['exec-1'] = { response: 'hello from the run', error: null }
    await tick()
    expect(detailReads('exec-1')).toBe(1)          // nothing re-read while closed

    await message(w, '/exec-1').trigger('click')
    await flush()
    expect(detailReads('exec-1')).toBe(2)
    expect(listText(w)).toContain('hello from the run')
  })
})
