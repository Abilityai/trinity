// @vitest-environment jsdom
/**
 * #3439 — the open conversation picks up what the agent wrote after the turn ended.
 *
 * A completion report, an approval outcome (#3709), a recovered lost turn
 * (#2319) or a background task's later reply all land in the thread the person
 * is looking at — and the transcript did not change until a reload, while the
 * sidebar badge did. The shell's 20 s tick re-stamps every row of `threads`
 * (the prop this component already receives); the conversation now watches its
 * OWN row's `message_count|last_message_at`, re-reads the tail, appends what it
 * has not seen, and tells the shell to mark the thread read.
 *
 * Mounted (#2918): the real PortalConversation (shallow), store stubbed at the
 * store seam — the harness of `portalThreadMissing.mount.spec.js`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const T0 = '2026-10-26T10:00:00Z'
const T1 = '2026-10-26T10:05:00Z'
const u1 = { id: 'u1', role: 'user', content: 'Draft the plan', created_at: T0 }
const a1 = { id: 'a1', role: 'assistant', content: 'On it — I will report back.', created_at: T0 }
const a2 = { id: 'a2', role: 'assistant', content: 'Done: the plan is in your inbox.', source: 'completion:done', created_at: T1 }

const threadRow = (over = {}) => ({
  id: 's1', agent_name: 'scout', title: 'Plan', message_count: 2, last_message_at: T0, unread: 0, ...over,
})

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

function history(messages) {
  return { sessionId: 's1', messages, truncated: false, inFlightExecutionId: null, inFlightWaitBudgetSeconds: null, lastTurnOutcome: null }
}

async function open({ threads = [threadRow()], initial = [u1, a1] } = {}) {
  store.fetchHistory = vi.fn(async () => history(initial))
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1', threads },
    attachTo: document.body,
  })
  await flushPromises()
  expect(store.fetchHistory).toHaveBeenCalledTimes(1)   // the mount's own load
  return wrapper
}

const shown = (w) => w.vm.messages.map((m) => m.id)

describe('#3439 — the open thread catches up when its row moves', () => {
  it('re-reads the tail when the row gains a message and appends the new row once', async () => {
    const w = await open()
    store.fetchHistory = vi.fn(async () => history([u1, a1, a2]))
    await w.setProps({ threads: [threadRow({ message_count: 3, last_message_at: T1, unread: 1 })] })
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 's1', { limit: 9 })   // unread 1 + REPLY_POLL_ROWS 8
    expect(shown(w)).toEqual(['u1', 'a1', 'a2'])
    expect(w.emitted('thread-caught-up')).toEqual([['s1']])
  })

  it('does not re-read when the tick re-stamps the row with the same values (new object, same thread)', async () => {
    const w = await open()
    store.fetchHistory = vi.fn(async () => history([u1, a1]))
    await w.setProps({ threads: [threadRow({ starred: true, hasDraft: true })] })
    await flushPromises()
    await w.setProps({ threads: [threadRow({ unread: 2 })] })   // an ask-only arrival: badge moves, no row
    await flushPromises()
    expect(store.fetchHistory).not.toHaveBeenCalled()
    expect(w.emitted('thread-caught-up')).toBeUndefined()
  })

  it('a read that brings nothing new appends nothing and does not ask the shell to mark read', async () => {
    const w = await open()
    store.fetchHistory = vi.fn(async () => history([u1, a1]))
    await w.setProps({ threads: [threadRow({ last_message_at: T1 })] })   // a deliverable touch: `added=0`
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledTimes(1)
    expect(shown(w)).toEqual(['u1', 'a1'])
    expect(w.emitted('thread-caught-up')).toBeUndefined()
  })

  it('the person\'s own just-sent question is not rendered twice — the local row takes the server id', async () => {
    const w = await open()
    // What `send()` pushes: the user's row with no id yet.
    w.vm.messages.push({ role: 'user', content: 'Also check the budget', local: true, at: null, failed: false, error: null })
    const u2 = { id: 'u2', role: 'user', content: 'Also check the budget', created_at: T1 }
    store.fetchHistory = vi.fn(async () => history([u1, a1, u2]))
    await w.setProps({ threads: [threadRow({ message_count: 3, last_message_at: T1 })] })
    await flushPromises()
    const users = w.vm.messages.filter((m) => m.role === 'user' && m.content === 'Also check the budget')
    expect(users).toHaveLength(1)
    expect(users[0].id).toBe('u2')
  })

  it('a read that is entirely unseen at the cap falls back to the window read and merges it', async () => {
    const w = await open({ threads: [threadRow()] })
    const burst = Array.from({ length: 50 }, (_, i) => ({ id: `b${i}`, role: 'assistant', content: `step ${i}`, created_at: T1 }))
    store.fetchHistory = vi.fn(async (_agent, _sid, opts) => (
      opts?.limit ? history(burst) : history([u1, a1, ...burst, { id: 'z', role: 'assistant', content: 'last', created_at: T1 }])
    ))
    await w.setProps({ threads: [threadRow({ message_count: 53, last_message_at: T1, unread: 51 })] })
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 's1', { limit: 50 })
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 's1')
    expect(shown(w)).toEqual(['u1', 'a1', ...burst.map((m) => m.id), 'z'])
    expect(w.emitted('thread-caught-up')).toEqual([['s1']])
  })

  it('a row that moves for a different thread is not this conversation\'s business', async () => {
    const w = await open()
    store.fetchHistory = vi.fn()
    await w.setProps({ threads: [threadRow(), threadRow({ id: 's2', message_count: 9, last_message_at: T1, unread: 4 })] })
    await flushPromises()
    expect(store.fetchHistory).not.toHaveBeenCalled()
  })

  it('a failed tail read is swallowed — never `thread-missing`, and the next move tries again', async () => {
    const w = await open()
    store.fetchHistory = vi.fn(async () => { throw { response: { status: 404 } } })
    await w.setProps({ threads: [threadRow({ message_count: 3, last_message_at: T1 })] })
    await flushPromises()
    expect(w.emitted('thread-missing')).toBeUndefined()
    expect(shown(w)).toEqual(['u1', 'a1'])
  })
})
