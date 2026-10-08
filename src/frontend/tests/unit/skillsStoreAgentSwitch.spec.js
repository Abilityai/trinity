/**
 * trinity-enterprise#754 — the skills store survives an agent switch.
 *
 * AgentDetail is KeepAlive'd and the merged Skills tab stays open from one
 * agent to the next, so a read or a write started for one agent can answer
 * after the page has moved on. That answer is dropped: it is never shown as
 * the next agent's state, and a write's follow-up read never re-reads the next
 * agent in its place (plan §3; the 10-07 write-then-reload learning, which
 * `skillGatesStore.spec.js` pins for the gate store).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))

import { useSkillsStore } from '../../src/stores/skills'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const ROWS = {
  fin: [{ skill_name: 'shared-one', individual: true }],
  ops: [{ skill_name: 'shared-two', individual: true }],
}

/** GET answers; `slow` names a URL whose answer the test releases by hand. */
function serve(slow = {}) {
  api.get.mockImplementation((url) => {
    if (slow[url]) return slow[url].promise
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true } })
    if (url === '/api/skills/library') return Promise.resolve({ data: [{ name: 'shared-one' }, { name: 'shared-two' }] })
    if (url === '/api/skills/library/sets') return Promise.resolve({ data: [] })
    const m = url.match(/^\/api\/agents\/([^/]+)\/(skills|skill-sets)$/)
    if (m) return Promise.resolve({ data: m[2] === 'skills' ? ROWS[m[1]] : [] })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

const readsOf = (url) => api.get.mock.calls.filter(([u]) => u === url).length

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of Object.values(api)) f.mockReset()
})

describe('an answer for the previous agent is dropped', () => {
  it('a load', async () => {
    const finRows = deferred()
    serve({ '/api/agents/fin/skills': finRows })
    const s = useSkillsStore()

    const first = s.load('fin')
    await s.load('ops')
    finRows.resolve({ data: ROWS.fin })
    await first

    expect(s.agentName).toBe('ops')
    expect(s.assigned).toEqual(ROWS.ops)
    expect(s.loading).toBe(false)
  })

  it('a failed load', async () => {
    const finRows = deferred()
    serve({ '/api/agents/fin/skills': finRows })
    const s = useSkillsStore()

    const first = s.load('fin')
    await s.load('ops')
    finRows.reject({ response: { status: 500, data: { detail: 'boom' } } })
    await first

    expect(s.error).toBeNull()
    expect(s.assigned).toEqual(ROWS.ops)
  })

  it('a sync: its results are not shown on the next agent', async () => {
    serve()
    const sync = deferred()
    api.post.mockImplementation(() => sync.promise)
    const s = useSkillsStore()
    await s.load('fin')

    const pending = s.inject()
    await s.load('ops')
    sync.resolve({ data: { results: { 'shared-one': { success: true, status: 'injected' } } } })
    await pending

    expect(api.post).toHaveBeenCalledWith('/api/agents/fin/skills/inject')
    expect(s.injectionResults).toEqual({})
    expect(s.lastInjectionAt).toBeNull()
    expect(s.injecting).toBe(false)
  })

  it('a save: nothing is applied to, or re-read for, the next agent', async () => {
    serve()
    const put = deferred()
    api.put.mockImplementation(() => put.promise)
    const s = useSkillsStore()
    await s.load('fin')

    const save = s.saveAssignments(['shared-one', 'shared-two'])
    await s.load('ops')
    const opsReads = readsOf('/api/agents/ops/skills')
    put.resolve({ data: { delivery: { status: 'delivered' } } })
    const out = await save

    expect(api.put.mock.calls[0][0]).toBe('/api/agents/fin/skills')
    expect(out).toBeNull()                               // superseded, not "saved here"
    expect(readsOf('/api/agents/ops/skills')).toBe(opsReads)
    expect(s.assigned).toEqual(ROWS.ops)
    expect(s.lastDelivery).toBeNull()
    expect(s.saving).toBe(false)
  })

  it('a set assignment', async () => {
    serve()
    const post = deferred()
    api.post.mockImplementation(() => post.promise)
    const s = useSkillsStore()
    await s.load('fin')

    const assign = s.assignSet('finance-pack')
    await s.load('ops')
    post.resolve({ data: { set_name: 'finance-pack', members_added: ['shared-one'], delivery: { status: 'delivered' } } })
    await assign

    expect(api.post.mock.calls[0][0]).toBe('/api/agents/fin/skill-sets/finance-pack')
    expect(s.lastSetResult).toBeNull()
    expect(s.lastDelivery).toBeNull()
    expect(s.setBusy).toBeNull()
    expect(s.assigned).toEqual(ROWS.ops)
  })
})
