/**
 * The front-desk store's contract (ent#319).
 *
 * Two rules carry the whole surface, and both are invisible to a structural
 * check:
 *
 *   1. **Never over a fleet that isn't fresh.** Every failure resolves to
 *      hidden. A missed nudge is a non-event; a "Start here" card sitting over
 *      somebody's forty-agent install is noise they cannot turn off for
 *      everyone.
 *   2. **Never stacked with the wizard.** The ent#52 wizard still auto-opens on
 *      a genuinely empty install, so the card must stand down there — it exists
 *      for the case the wizard cannot see, where a seeded fleet is running and
 *      none of it is the user's.
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
import { useFirstRunStore, FRONT_DESK_DISMISSED_KEY } from '@/stores/firstRun'

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

describe('visibility', () => {
  it('shows on a seed-only install', async () => {
    api.get.mockResolvedValueOnce({ data: seededOnly })
    await store.fetchState()

    expect(store.visible).toBe(true)
    expect(store.demoAgent).toBe('cornelius')
  })

  it('stays hidden once the user has an agent of their own', async () => {
    api.get.mockResolvedValueOnce({
      data: { first_run: false, seeded_agents: ['cornelius'], own_agent_count: 1, demo_agent: 'cornelius' },
    })
    await store.fetchState()

    expect(store.visible).toBe(false)
  })

  it('stands down on a genuinely empty install, leaving the wizard alone', async () => {
    // first_run is true, but nothing was seeded — this is the case the ent#52
    // wizard auto-opens for. Two first-run surfaces at once is worse than one.
    api.get.mockResolvedValueOnce({
      data: { first_run: true, seeded_agents: [], own_agent_count: 0, demo_agent: null },
    })
    await store.fetchState()

    expect(store.visible).toBe(false)
  })

  it('renders nothing before the answer arrives', () => {
    expect(store.loaded).toBe(false)
    expect(store.visible).toBe(false)
  })

  it('fails toward hidden when the read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockRejectedValueOnce(new Error('boom'))
    await store.fetchState()

    expect(store.firstRun).toBe(false)
    expect(store.visible).toBe(false)
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

describe('dismissal', () => {
  it('hides and persists', async () => {
    api.get.mockResolvedValueOnce({ data: seededOnly })
    await store.fetchState()

    store.dismiss()

    expect(store.visible).toBe(false)
    expect(localStorage.getItem(FRONT_DESK_DISMISSED_KEY)).toBe('1')
  })

  it('is honoured on the next load, before any fetch', async () => {
    localStorage.setItem(FRONT_DESK_DISMISSED_KEY, '1')
    setActivePinia(createPinia())
    const fresh = useFirstRunStore()

    api.get.mockResolvedValueOnce({ data: seededOnly })
    await fresh.fetchState()

    expect(fresh.dismissed).toBe(true)
    expect(fresh.visible).toBe(false)
  })

  it('still hides for the session when storage refuses the write', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    api.get.mockResolvedValueOnce({ data: seededOnly })
    await store.fetchState()

    const setItem = localStorage.setItem
    localStorage.setItem = () => { throw new Error('quota') }
    store.dismiss()
    localStorage.setItem = setItem

    expect(store.visible).toBe(false)
    warn.mockRestore()
  })
})
