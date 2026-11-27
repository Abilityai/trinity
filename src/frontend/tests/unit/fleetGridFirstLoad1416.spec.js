/**
 * The Grid's automatic first-load board vs a person's saved layout — the store
 * contract (trinity-enterprise#1416).
 *
 * With nothing saved, `syncLayout(…, arrange)` shows the caller's arrangement
 * and persists NOTHING, so a reconcile can never pass for a person's layout and
 * a later reconcile can re-arrange as agents arrive. The first gesture saves
 * the board; from then on the saved layout wins and newcomers are placed beside
 * it, never re-arranged. A record already on the server is never re-arranged.
 * Drives the real `fleetGrid` + `userPreferences` stores over mocked axios.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { reactive, nextTick } from 'vue'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

// A REACTIVE auth stand-in: the identity-change tests flip `principalId`.
const auth = reactive({
  authHeader: { Authorization: 'Bearer jwt' },
  user: { role: 'user' },
  principalId: 'alice',
})
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
vi.mock('@/stores/executions', () => ({
  useExecutionsStore: () => ({ fetchAgentAnalytics: vi.fn(), analyticsCache: {} }),
}))
vi.mock('@/stores/subscriptions', () => ({
  useSubscriptionsStore: () => ({ fetchPressureData: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useFleetGridStore } from '@/stores/fleetGrid'
import { LAYOUT_KEY, userScopedKey } from '@/utils/gridStorageKeys'

const PREFS_URL = '/api/users/me/preferences'

/** Mock the GET with a server record set; `null` → network failure. */
function serverHas(preferences) {
  axios.get.mockImplementation((url) => {
    if (url !== PREFS_URL) return Promise.resolve({ data: {}, headers: {} })
    if (preferences === null) return Promise.reject(new Error('network'))
    if (preferences instanceof Error) return Promise.reject(preferences)
    return Promise.resolve({ data: { preferences }, headers: {} })
  })
}
function rec(value, updated_at = 't1') {
  return { value, updated_at }
}
/** Every PUT succeeds, echoing a fresh updated_at. */
function putsSucceed() {
  let n = 0
  axios.put.mockImplementation(() => Promise.resolve({ data: { updated_at: `srv${++n}` } }))
}
function putBodies() {
  return axios.put.mock.calls.map(([url, body]) => ({ key: url.split('/').pop(), ...body }))
}
async function settle() {
  await vi.runAllTimersAsync()
  await nextTick()
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.useFakeTimers()
  axios.get.mockReset()
  axios.put.mockReset()
  axios.delete.mockReset()
  axios.delete.mockResolvedValue({ data: { deleted: true } })
  auth.principalId = 'alice'
  putsSucceed()
})
afterEach(() => {
  vi.useRealTimers()
})


beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.useFakeTimers()
  axios.get.mockReset()
  axios.put.mockReset()
  axios.delete.mockReset()
  auth.principalId = 'alice'
  putsSucceed()
})
afterEach(() => {
  vi.useRealTimers()
})

/** A stand-in arrangement: each name in a row, in the order given. */
function rowOf(names) {
  return (widgetKeys) => {
    const out = {}
    names.forEach((n, i) => { out[n] = { c: i, r: 0 } })
    widgetKeys.forEach((k, i) => { out[k] = { c: i, r: -1 } })
    return out
  }
}

describe('the automatic board (ent#1416)', () => {
  it('with nothing saved shows the arrangement and writes nothing', async () => {
    serverHas({})
    const store = useFleetGridStore()
    await store.loadPreferences()
    await settle()

    store.syncLayout(['corbin', 'cornelius'], new Set(), null, null, rowOf(['corbin', 'cornelius']))
    await settle()

    expect(store.layoutIsAuto).toBe(true)
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 1, r: 0 })
    expect(axios.put).not.toHaveBeenCalled()
    expect(localStorage.getItem(userScopedKey(LAYOUT_KEY, 'alice'))).toBeNull()
  })

  it('re-arranges as agents arrive while nobody has touched it', async () => {
    serverHas({})
    const store = useFleetGridStore()
    await store.loadPreferences()
    await settle()
    store.syncLayout(['cornelius'], new Set(), null, null, rowOf(['cornelius']))

    store.syncLayout(['corbin', 'cornelius'], new Set(), null, null, rowOf(['corbin', 'cornelius']))

    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.cornelius).toEqual({ c: 1, r: 0 })
  })

  it('the first gesture saves the board, and the saved layout then wins', async () => {
    serverHas({})
    const store = useFleetGridStore()
    await store.loadPreferences()
    await settle()
    store.syncLayout(['corbin', 'cornelius'], new Set(), null, null, rowOf(['corbin', 'cornelius']))

    store.moveTile('cornelius', 4, 2)
    await settle()

    expect(store.layoutIsAuto).toBe(false)
    expect(putBodies().some((b) => b.key === 'grid_layout' && b.value.cornelius.c === 4)).toBe(true)

    // A newcomer no longer re-arranges anything.
    store.syncLayout(['corbin', 'cornelius', 'mine'], new Set(), null, null, rowOf(['mine', 'corbin', 'cornelius']))
    expect(store.layout.cornelius).toEqual({ c: 4, r: 2 })
    expect(store.layout.corbin).toEqual({ c: 0, r: 0 })
    expect(store.layout.mine).toBeDefined()
    expect(store.layout.mine).not.toEqual({ c: 0, r: 0 })
  })

  it('a layout already on the server is never re-arranged', async () => {
    serverHas({ grid_layout: rec({ corbin: { c: 7, r: 3 }, cornelius: { c: 2, r: 5 } }) })
    const store = useFleetGridStore()
    await store.loadPreferences()
    await settle()

    store.syncLayout(['corbin', 'cornelius'], new Set(), null, null, rowOf(['corbin', 'cornelius']))

    expect(store.layoutIsAuto).toBe(false)
    expect(store.layout.corbin).toEqual({ c: 7, r: 3 })
    expect(store.layout.cornelius).toEqual({ c: 2, r: 5 })
  })

  it('a layout kept in this browser is never re-arranged either', () => {
    localStorage.setItem(userScopedKey(LAYOUT_KEY, 'alice'), JSON.stringify({ corbin: { c: 3, r: 3 } }))
    const store = useFleetGridStore()

    store.syncLayout(['corbin'], new Set(), null, null, rowOf(['corbin']))

    expect(store.layoutIsAuto).toBe(false)
    expect(store.layout.corbin).toEqual({ c: 3, r: 3 })
  })

  it('without an arrangement the old default is kept (callers other than the Grid)', () => {
    const store = useFleetGridStore()
    store.syncLayout(['a1', 'a2'])
    expect(store.layoutIsAuto).toBe(false)
    expect(Object.keys(store.layout)).toEqual(expect.arrayContaining(['a1', 'a2']))
  })
})
