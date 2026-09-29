// @vitest-environment jsdom
/**
 * trinity-enterprise#527 rider (PR #3038 review, item 3) — the 30 s agent poll
 * carries a readiness flip to an already-open dashboard.
 *
 * The poll replaces `agents` only when the SET of names changes, and a
 * readiness flip emits no WS event, so without an in-place patch an open tab
 * showed `calibrating` until a reload. Pinned over the real poll body: the
 * rows already present get `readiness` / `brief_held` patched in place, and
 * nothing is rebuilt (same row objects, same `nodes`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const get = vi.fn()
vi.mock('axios', () => {
  const inst = {
    get: (...a) => get(...a), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import { useNetworkStore } from '../../src/stores/network.js'

const CAL = { status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' }
const READY = { status: 'ready', changed_at: '2026-09-29T09:00:00Z', source: 'owner' }

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  get.mockReset()
})
afterEach(() => vi.useRealTimers())

describe('agent poll — readiness patched in place (ent#527 rider)', () => {
  it('a flip reaches the row without rebuilding it or the nodes', async () => {
    const store = useNetworkStore()
    store.agents = [
      { name: 'companion', readiness: CAL, brief_held: true },
      { name: 'plain', readiness: null, brief_held: false },
    ]
    const rowBefore = store.agents[0]
    const nodesBefore = store.nodes
    get.mockResolvedValue({ data: [
      { name: 'companion', readiness: READY, brief_held: false },
      { name: 'plain', readiness: null, brief_held: false },
    ] })

    store.startAgentRefresh()
    await vi.advanceTimersByTimeAsync(30000)
    store.stopAgentRefresh()

    expect(get).toHaveBeenCalledWith('/api/agents', expect.anything())
    expect(store.agents[0]).toBe(rowBefore)
    expect(store.agents[0].readiness).toEqual(READY)
    expect(store.agents[0].brief_held).toBe(false)
    expect(store.nodes).toBe(nodesBefore)
  })

  it('a newly stamped agent gains its badge, a cleared stamp loses it', async () => {
    const store = useNetworkStore()
    store.agents = [
      { name: 'a', readiness: null, brief_held: false },
      { name: 'b', readiness: READY, brief_held: false },
    ]
    get.mockResolvedValue({ data: [
      { name: 'a', readiness: CAL, brief_held: true },
      { name: 'b', readiness: null, brief_held: false },
    ] })

    store.startAgentRefresh()
    await vi.advanceTimersByTimeAsync(30000)
    store.stopAgentRefresh()

    expect(store.agents.find(x => x.name === 'a').readiness).toEqual(CAL)
    expect(store.agents.find(x => x.name === 'a').brief_held).toBe(true)
    expect(store.agents.find(x => x.name === 'b').readiness).toBeNull()
  })
})
