/**
 * The activation-checklist store's contract (ent#238).
 *
 * Two behaviours here are load-bearing and invisible to any structural check:
 *
 *   1. **Silence on OSS/unentitled.** The component ships in the OSS bundle, so
 *      the checklist endpoint 404s (router never mounted) or 403s (mounted but
 *      unentitled) for a large share of installs. Either must leave `available`
 *      false and log NOTHING — an ambient nudge that shouts about its own
 *      absence is worse than no nudge.
 *
 *   2. **It hides when there is nothing left to ask for.** `visible` must go
 *      false on completion and on dismissal independently: the reward for
 *      finishing is that it stops asking, and a dismissal must survive a
 *      still-incomplete list (ent#54: never a mandatory tour).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const mem = new Map()
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/' } }
})

// The store talks to the shared `api` client, not to axios directly, so THAT is
// the seam to mock. Mocking axios instead would hand back the PERF-269 dedupe
// wrapper `api.js` installs over `get` — a real function, not a spy.
vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import api from '@/api'
import { useOnboardingStore } from '@/stores/onboarding'

const item = (key, done) => ({
  key, title: key, description: key, done,
  action_label: 'Go', action_route: '/',
})

const payload = (doneCount, dismissed = false) => ({
  items: [
    item('first_agent_created', doneCount > 0),
    item('first_chat', doneCount > 1),
    item('first_schedule_created', doneCount > 2),
    item('first_channel_connected', doneCount > 3),
  ],
  completed_count: doneCount,
  total_count: 4,
  complete: doneCount === 4,
  dismissed,
})

let store

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useOnboardingStore()
})

describe('availability', () => {
  it('renders nothing and stays quiet on an OSS build (404)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockRejectedValueOnce({ response: { status: 404 } })

    await store.fetchChecklist()

    expect(store.available).toBe(false)
    expect(store.visible).toBe(false)
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('renders nothing and stays quiet when mounted but unentitled (403)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockRejectedValueOnce({ response: { status: 403 } })

    await store.fetchChecklist()

    expect(store.available).toBe(false)
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('reports a real failure once, without becoming available', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockRejectedValueOnce({ response: { status: 500 } })

    await store.fetchChecklist()

    expect(store.available).toBe(false)
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('fetches once unless forced', async () => {
    api.get.mockResolvedValue({ data: payload(1) })

    await store.fetchChecklist()
    await store.fetchChecklist()
    expect(api.get).toHaveBeenCalledTimes(1)

    await store.fetchChecklist(true)
    expect(api.get).toHaveBeenCalledTimes(2)
  })
})

describe('visibility', () => {
  it('shows while work remains', async () => {
    api.get.mockResolvedValueOnce({ data: payload(1) })
    await store.fetchChecklist()
    expect(store.visible).toBe(true)
    expect(store.completedCount).toBe(1)
  })

  it('hides itself once every milestone is reached', async () => {
    api.get.mockResolvedValueOnce({ data: payload(4) })
    await store.fetchChecklist()
    expect(store.complete).toBe(true)
    expect(store.visible).toBe(false)
  })

  it('hides on a persisted dismissal even with work remaining', async () => {
    api.get.mockResolvedValueOnce({ data: payload(1, true) })
    await store.fetchChecklist()
    expect(store.visible).toBe(false)
  })
})

describe('dismissal', () => {
  it('hides immediately, before the write lands', async () => {
    api.get.mockResolvedValueOnce({ data: payload(1) })
    await store.fetchChecklist()

    let resolve
    api.post.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const pending = store.dismiss()

    expect(store.visible).toBe(false)   // optimistic — no wait on the network

    resolve({ data: payload(1, true) })
    await pending
    expect(store.dismissed).toBe(true)
  })

  it('stays hidden when the write fails — the user asked once', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockResolvedValueOnce({ data: payload(1) })
    await store.fetchChecklist()

    api.post.mockRejectedValueOnce(new Error('offline'))
    await store.dismiss()

    expect(store.dismissed).toBe(true)
    warn.mockRestore()
  })

  it('restores through the DELETE seam', async () => {
    api.get.mockResolvedValueOnce({ data: payload(1, true) })
    await store.fetchChecklist()
    expect(store.visible).toBe(false)

    api.delete.mockResolvedValueOnce({ data: payload(1, false) })
    await store.restore()

    expect(store.dismissed).toBe(false)
    expect(store.visible).toBe(true)
  })
})
