/**
 * The first-run store's contract (ent#319; consumer since ent#581: the
 * first-run overlay, whose `agent` step absorbed the front-desk card).
 *
 * One rule carries it, invisible to a structural check: **never over a fleet
 * that isn't fresh.** Every failure resolves to "not first run", and `loaded`
 * stays false until an answer arrives, so nothing flashes in during the fetch.
 * The overlay's own gating on these terms is pinned in firstRunSteps.spec.js.
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

// The store talks to the shared `api` client, so that is the seam to mock —
// mocking axios would hand back the PERF-269 dedupe wrapper `api.js` installs.
vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import api from '@/api'
import { useFirstRunStore } from '@/stores/firstRun'

const seededOnly = {
  first_run: true,
  seeded_agents: ['acme-sage', 'acme-scout', 'cornelius'],
  own_agent_count: 0,
  demo_agent: 'cornelius',
}

let store

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useFirstRunStore()
})

describe('state', () => {
  it('reads a seed-only install as first run, with its demonstrator', async () => {
    api.get.mockResolvedValueOnce({ data: seededOnly })
    await store.fetchState()

    expect(store.loaded).toBe(true)
    expect(store.firstRun).toBe(true)
    expect(store.demoAgent).toBe('cornelius')
    expect(store.seededAgents).toEqual(['acme-sage', 'acme-scout', 'cornelius'])
  })

  it('is not first run once the user has an agent of their own', async () => {
    api.get.mockResolvedValueOnce({
      data: { first_run: false, seeded_agents: ['cornelius'], own_agent_count: 1, demo_agent: 'cornelius' },
    })
    await store.fetchState()

    expect(store.firstRun).toBe(false)
    expect(store.ownAgentCount).toBe(1)
  })

  it('is first run on a genuinely empty install, with no demonstrator to show', async () => {
    // Seeding disabled: nothing to "Show me", but the agent step still applies.
    api.get.mockResolvedValueOnce({
      data: { first_run: true, seeded_agents: [], own_agent_count: 0, demo_agent: null },
    })
    await store.fetchState()

    expect(store.firstRun).toBe(true)
    expect(store.demoAgent).toBeNull()
  })

  it('has not loaded before the answer arrives', () => {
    expect(store.loaded).toBe(false)
    expect(store.firstRun).toBe(false)
  })

  it('fails toward "not first run" when the read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockRejectedValueOnce(new Error('boom'))
    await store.fetchState()

    expect(store.firstRun).toBe(false)
    expect(store.loaded).toBe(true) // resolved, just not to "first run"
    warn.mockRestore()
  })

  it('fetches once unless forced', async () => {
    api.get.mockResolvedValue({ data: seededOnly })
    await store.fetchState()
    await store.fetchState()
    expect(api.get).toHaveBeenCalledTimes(1)

    await store.fetchState(true)
    expect(api.get).toHaveBeenCalledTimes(2)
  })
})
