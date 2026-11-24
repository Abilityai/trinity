// @vitest-environment jsdom
/**
 * #4671 — a turn end reads the thread's history once, not twice.
 *
 * The reply poll finds the reply IN the thread's newest rows. Then the shell's
 * list refresh moves the thread's row, and the tail watcher (#3439) used to
 * read those same rows again to learn what the turn had just been handed —
 * one extra `GET …/history` on every turn end, a second on a turn long enough
 * for a 20 s tick to land inside it.
 *
 * The conversation now folds the poll's rows in itself (`settleOwnTail`): the
 * person's own question takes its server id, anything that arrived on its own
 * while the turn ran is appended, and the watcher finds the row's newest
 * instant already on screen.
 *
 * Mounted (#2918): the real PortalConversation (shallow), a real send through
 * `deliver()`, the store stubbed at the store seam — the harness of
 * `portalOpenThreadRefresh.mount.spec.js`. The count is asserted on
 * `store.fetchHistory`, the one door every history read goes through.
 *
 * What must NOT change is as much the point as the count: a row that arrives
 * after the turn is still read, a turn that brought no rows (the synchronous
 * fallback) still reads, and a row that landed mid-turn is not lost.
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
import { joinFresh, resetInFlight } from '@/utils/inflight'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const T0 = '2026-11-20T10:00:00.000000Z'
const T1 = '2026-11-20T10:00:40.000000Z'   // the question, persisted at dispatch
const T2 = '2026-11-20T10:01:00.500000Z'   // the reply
const T3 = '2026-11-20T10:03:00.000000Z'   // something later
const QUESTION = 'Also check the budget'

const u1 = { id: 'u1', role: 'user', content: 'Draft the plan', created_at: T0, execution_id: 'e0' }
const a1 = { id: 'a1', role: 'assistant', content: 'On it.', created_at: T0, execution_id: 'e0' }
const u2 = { id: 'u2', role: 'user', content: QUESTION, created_at: T1, execution_id: 'e1' }
const a2 = { id: 'a2', role: 'assistant', content: 'The budget holds.', created_at: T2, execution_id: 'e1' }
// A background task's report that landed in this thread WHILE the turn ran.
const report = { id: 'r1', role: 'assistant', content: 'Done: the plan is in your inbox.',
  source: 'completion:done', created_at: '2026-11-20T10:00:50.000000Z', execution_id: null }
const later = { id: 'a3', role: 'assistant', content: 'One more thing.', source: 'completion:done',
  created_at: T3, execution_id: null }

const threadRow = (over = {}) => ({
  id: 's1', agent_name: 'scout', title: 'Plan', message_count: 2, last_message_at: T0, unread: 0, ...over,
})
const history = (messages) => ({
  sessionId: 's1', messages, truncated: false, inFlightExecutionId: null,
  inFlightWaitBudgetSeconds: null, lastTurnOutcome: null,
})

let wrapper
let store
/** What the server holds; every history read answers from it. */
let server

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  resetInFlight()
  store = useClientPortalStore()
  server = [u1, a1]
  store.fetchHistory = vi.fn(async (_agent, _sid, opts) => history(opts?.limit ? server.slice(-opts.limit) : server))
  store.fetchAsks = vi.fn(async () => [])
  store.streamPortalExecution = vi.fn(async () => {})
  // Dispatch persists the question; the stream ending is the reply landing.
  store.startPortalChat = vi.fn(async () => {
    server = [...server, u2]
    return { execution_id: 'e1', session_id: 's1' }
  })
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function open() {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1', threads: [threadRow()] },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
  expect(store.fetchHistory).toHaveBeenCalledTimes(1)   // the mount's own load
  return wrapper
}

async function settle() {
  for (let i = 0; i < 6; i++) await flushPromises()
}

/** Type the question and send it; resolves once the turn has fully ended. */
async function sendTurn(w, { during = async () => {} } = {}) {
  await w.find('textarea').setValue(QUESTION)
  await w.find('form').trigger('submit')
  await during()
  await settle()
  expect(w.emitted('sessions-changed')).toBeTruthy()
}

const shown = (w) => w.vm.messages.map((m) => m.id)
const reads = () => store.fetchHistory.mock.calls.length

describe('#4671 — the turn\'s own rows are not read a second time', () => {
  it('the shell\'s list refresh after a turn costs no history read', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    await sendTurn(w)
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2'])
    const afterTurn = reads()

    // The shell refreshed its list: the row now counts the question and the reply.
    await w.setProps({ threads: [threadRow({ message_count: 4, last_message_at: T2 })] })
    await settle()

    expect(reads()).toBe(afterTurn)
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2'])
  })

  it('the person\'s own question took its server id from the reply poll', async () => {
    // Before, the post-turn re-read is what adopted it; without the adoption
    // the next tail read would show the question twice.
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    await sendTurn(w)
    const mine = w.vm.messages.filter((m) => m.role === 'user' && m.content === QUESTION)
    expect(mine).toHaveLength(1)
    expect(mine[0].id).toBe('u2')
    expect(mine[0].at).toBe(T1)
  })

  it('a long turn — the row moves mid-turn AND at the end — still costs no read', async () => {
    const w = await open()
    let release
    store.streamPortalExecution = vi.fn(() => new Promise((resolve) => {
      release = () => { server = [...server, a2]; resolve() }
    }))
    await sendTurn(w, {
      during: async () => {
        await settle()
        // A 20 s tick lands inside the turn: the row counts the question.
        await w.setProps({ threads: [threadRow({ message_count: 3, last_message_at: T1 })] })
        await settle()
        release()
      },
    })
    const afterTurn = reads()

    await w.setProps({ threads: [threadRow({ message_count: 4, last_message_at: T2 })] })
    await settle()

    expect(reads()).toBe(afterTurn)
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2'])
  })

  it('a row that landed on its own DURING the turn is on screen, and still no extra read', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, report, a2] })
    await sendTurn(w)
    expect(shown(w).sort()).toEqual(['a1', 'a2', 'r1', 'u1', 'u2'])
    const afterTurn = reads()

    await w.setProps({ threads: [threadRow({ message_count: 5, last_message_at: T2 })] })
    await settle()

    expect(reads()).toBe(afterTurn)
    expect(w.vm.messages.filter((m) => m.id === 'r1')).toHaveLength(1)
  })
})

describe('#4671 — and everything that is NOT the turn\'s own is still read', () => {
  it('a row that arrives AFTER the turn is fetched and appended', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    await sendTurn(w)
    const afterTurn = reads()

    server = [...server, later]
    await w.setProps({ threads: [threadRow({ message_count: 5, last_message_at: T3, unread: 1 })] })
    await settle()

    expect(reads()).toBe(afterTurn + 1)
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2', 'a3'])
    expect(w.emitted('thread-caught-up')).toEqual([['s1', T3]])
  })

  it('the settled mark is spent by that read: the next arrival is read too', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    await sendTurn(w)
    await w.setProps({ threads: [threadRow({ message_count: 4, last_message_at: T2 })] })
    await settle()
    const quiet = reads()

    server = [...server, later]
    await w.setProps({ threads: [threadRow({ message_count: 5, last_message_at: T3 })] })
    await settle()
    const T4 = '2026-11-20T10:05:00.000000Z'
    server = [...server, { ...later, id: 'a4', created_at: T4 }]
    await w.setProps({ threads: [threadRow({ message_count: 6, last_message_at: T4 })] })
    await settle()

    expect(reads()).toBe(quiet + 2)
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2', 'a3', 'a4'])
  })

  it('a deliverable touching the row after the turn (time moves, no row) is still read', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    await sendTurn(w)
    const afterTurn = reads()

    await w.setProps({ threads: [threadRow({ message_count: 4, last_message_at: T3 })] })
    await settle()

    expect(reads()).toBe(afterTurn + 1)
  })

  it('the synchronous fallback brings no rows, so the row move is read as before', async () => {
    // No streaming route: `POST …/chat` answers with the reply and nothing
    // else — no tail, no time. Nothing is settled; the watcher reads.
    const w = await open()
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 404 } })
    store.sendPortalChat = vi.fn(async () => {
      server = [...server, u2, a2]
      return { response: a2.content, message_id: 'a2', session_id: 's1' }
    })
    await sendTurn(w)
    const afterTurn = reads()

    await w.setProps({ threads: [threadRow({ message_count: 4, last_message_at: T2 })] })
    await settle()

    expect(reads()).toBe(afterTurn + 1)
    const mine = w.vm.messages.filter((m) => m.role === 'user' && m.content === QUESTION)
    expect(mine).toHaveLength(1)          // adopted by that read, as it always was
    expect(shown(w)).toEqual(['u1', 'a1', 'u2', 'a2'])
  })
})

/**
 * Is a read that left BEFORE `act` still handed to a caller that asks AFTER it?
 * The real `joinFresh` and the real `store.noteWorkspaceChange` — the store's
 * transport is mocked whole here, so no write interceptor runs and the only
 * thing that can make the earlier read stale is the conversation saying so.
 */
async function readAcross(act) {
  let release
  const before = joinFresh('probe', () => new Promise((resolve) => { release = () => resolve('before') }))
  await Promise.resolve()
  await act()
  const after = joinFresh('probe', async () => 'after')
  const joined = after === before
  release()
  return { joined, value: await after }
}

describe('#4671 — the reply landing is a change every list read must follow', () => {
  it('control: with no turn in between, a later read joins the earlier one', async () => {
    await open()
    expect(await readAcross(() => settle())).toEqual({ joined: true, value: 'before' })
  })

  it('a sent turn: a list read that left before the reply is not handed to the refresh after it', async () => {
    const w = await open()
    store.streamPortalExecution = vi.fn(async () => { server = [...server, a2] })
    expect(await readAcross(() => sendTurn(w))).toEqual({ joined: false, value: 'after' })
  })

  it('the synchronous fallback marks it too', async () => {
    const w = await open()
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 404 } })
    store.sendPortalChat = vi.fn(async () => {
      server = [...server, u2, a2]
      return { response: a2.content, message_id: 'a2', session_id: 's1' }
    })
    expect(await readAcross(() => sendTurn(w))).toEqual({ joined: false, value: 'after' })
  })

  it('a turn rejoined after a reload marks it when its reply lands', async () => {
    // The page opened INTO a running turn: history names it, the stream ends,
    // the reply is read. No `deliver()` ran in this page.
    server = [u1, a1, u2]
    let running = true
    store.fetchHistory = vi.fn(async (_agent, _sid, opts) => ({
      ...history(opts?.limit ? server.slice(-opts.limit) : server),
      inFlightExecutionId: running ? 'e1' : null,
      inFlightWaitBudgetSeconds: running ? 600 : null,
    }))
    let release
    store.streamPortalExecution = vi.fn(() => new Promise((resolve) => {
      release = () => { server = [...server, a2]; running = false; resolve() }
    }))

    const seen = await readAcross(async () => {
      wrapper = shallowMount(PortalConversation, {
        props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1', threads: [threadRow()] },
        global: { renderStubDefaultSlot: true },
        attachTo: document.body,
      })
      await settle()
      expect(store.streamPortalExecution).toHaveBeenCalledTimes(1)
      release()
      await settle()
    })

    expect(shown(wrapper)).toEqual(['u1', 'a1', 'u2', 'a2'])
    expect(seen).toEqual({ joined: false, value: 'after' })
  })
})

describe('#4671 — one thread\'s rows are never folded into another\'s transcript', () => {
  const x1 = { id: 'x1', role: 'user', content: 'Unrelated', created_at: T0, execution_id: 'x0' }
  const x2 = { id: 'x2', role: 'assistant', content: 'Unrelated answer.', created_at: T0, execution_id: 'x0' }

  it('the person switched threads mid-turn: the turn\'s rows stay out of the thread on screen', async () => {
    const other = [x1, x2]
    store.fetchHistory = vi.fn(async (_agent, sid, opts) => {
      const rows = sid === 's2' ? other : server
      return { ...history(opts?.limit ? rows.slice(-opts.limit) : rows), sessionId: sid || 's1' }
    })
    const w = await open()
    let release
    store.streamPortalExecution = vi.fn(() => new Promise((resolve) => {
      // The background report lands in s1 while the turn runs, then the reply.
      release = () => { server = [...server, report, a2]; resolve() }
    }))

    await w.find('textarea').setValue(QUESTION)
    await w.find('form').trigger('submit')
    await settle()
    await w.setProps({
      sessionId: 's2',
      threads: [threadRow(), threadRow({ id: 's2', title: 'Other' })],
    })
    await settle()
    expect(shown(w)).toEqual(['x1', 'x2'])

    release()
    await settle()

    // s1's history — its earlier exchange, the report, the question's server
    // row — belongs to s1. None of it may appear under s2.
    const ids = shown(w)
    for (const foreign of ['u1', 'a1', 'u2', 'r1']) expect(ids).not.toContain(foreign)
    expect(ids.slice(0, 2)).toEqual(['x1', 'x2'])
  })
})
