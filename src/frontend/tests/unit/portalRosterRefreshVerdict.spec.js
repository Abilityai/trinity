/**
 * #4502 — `refreshRoster()` says whether it READ the roster.
 *
 * A quiet refresh that fails "changes nothing on screen": it swallows the error
 * and leaves the last roster in place. That is right for a background poll and
 * wrong for a caller that is about to make a claim from the roster — the ask
 * card's "<agent> is picking this up." needs a roster it just read, not the one
 * from page load. So the action resolves `true` only for a read that succeeded.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {} }),
}))

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(() => Promise.resolve({ data: {} })), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'

let store
beforeEach(() => {
  axios.get.mockReset()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.agents = [{ name: 'scout', availability: 'ready' }]
})

describe('refreshRoster reports whether the roster was read (#4502)', () => {
  it('resolves true for a read that succeeded, with the new roster in place', async () => {
    axios.get.mockResolvedValue({ data: { agents: [{ name: 'scout', availability: 'stopped' }] } })
    await expect(store.refreshRoster()).resolves.toBe(true)
    expect(store.agents.find((a) => a.name === 'scout').availability).toBe('stopped')
  })

  it('does not resolve true for a read that failed, and keeps the last roster', async () => {
    axios.get.mockRejectedValue({ response: { status: 503 } })
    await expect(store.refreshRoster()).resolves.not.toBe(true)
    expect(store.agents.find((a) => a.name === 'scout').availability).toBe('ready')
    axios.get.mockRejectedValue(new Error('network'))
    await expect(store.refreshRoster()).resolves.not.toBe(true)
  })
})
