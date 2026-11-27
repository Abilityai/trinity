/**
 * #4813 — the fleet Executions list refreshes when a run starts and ends.
 *
 * The store refreshed only on `schedule_start` / `schedule_end` activities. The
 * backend writes neither for a run: every dispatch — chat, task or scheduled —
 * opens a `chat_start` activity and closes that same activity when the run
 * ends. So the socket trigger never fired and the list waited for its 30 s
 * poll. These specs drive the real store with the events the backend sends.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }
  return { default: inst }
})
import axios from 'axios'
import { useExecutionsStore } from '@/stores/executions'

const flush = () => new Promise((r) => setTimeout(r, 0))

/** `GET /api/executions` and its stats; returns how many list reads were made. */
function serveExecutions() {
  let reads = 0
  axios.get.mockImplementation((url) => {
    if (url === '/api/executions/stats') return Promise.resolve({ data: { total: 1, running_count: 0 } })
    if (url !== '/api/executions') return Promise.reject(new Error(`unexpected GET ${url}`))
    reads += 1
    return Promise.resolve({ data: [{ id: 'exec-1', agent_name: 'scout', status: 'running', started_at: 't1' }] })
  })
  return () => reads
}

// What `activity_service._broadcast_activity_event` sends for a scheduled run.
const runStarted = {
  type: 'agent_activity',
  agent_name: 'scout',
  activity_id: 'act-1',
  activity_type: 'chat_start',
  activity_state: 'started',
  details: { execution_id: 'exec-1', triggered_by: 'schedule' },
}
const runEnded = (state) => ({
  type: 'agent_activity',
  agent_name: 'scout',
  activity_id: 'act-1',
  activity_type: 'chat_start',
  activity_state: state,
  details: {},
})

describe('executions store — a run refreshes the live list (#4813)', () => {
  let store
  let reads
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    reads = serveExecutions()
    store = useExecutionsStore()
  })
  afterEach(() => {
    store.stopPolling()
  })

  async function openPanel() {
    store.startPolling(30000)
    await flush()
    return reads()
  }

  it('a scheduled run that starts refreshes the list', async () => {
    const before = await openPanel()
    store.handleWebSocketEvent(runStarted)
    await flush()
    expect(reads()).toBe(before + 1)
  })

  it.each(['completed', 'failed', 'cancelled'])('a run that ends as %s refreshes the list', async (state) => {
    const before = await openPanel()
    store.handleWebSocketEvent(runEnded(state))
    await flush()
    expect(reads()).toBe(before + 1)
  })

  it('a tool call inside a run does not refresh the list', async () => {
    const before = await openPanel()
    store.handleWebSocketEvent({ ...runStarted, activity_type: 'tool_call' })
    await flush()
    expect(reads()).toBe(before)
  })

  it('a run reads nothing while no panel shows the list', async () => {
    store.handleWebSocketEvent(runStarted)
    store.handleWebSocketEvent(runEnded('completed'))
    await flush()
    expect(reads()).toBe(0)
  })

  it('an event of another type that names chat_start does not refresh the list', async () => {
    const before = await openPanel()
    store.handleWebSocketEvent({ ...runStarted, type: 'agent_collaboration' })
    await flush()
    expect(reads()).toBe(before)
  })
})
