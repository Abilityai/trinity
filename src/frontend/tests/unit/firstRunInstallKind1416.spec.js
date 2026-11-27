/**
 * trinity-enterprise#1416 — a known install kind survives a re-read that does
 * not know it.
 *
 * `fresh_install` is null whenever the backend cannot say (verdict not
 * recorded yet, or unreadable). Once the store holds true or false, a later
 * forced re-read (`fetchState(true)`, used by other first-run consumers) that
 * answers null must not reset it to "not known": the Grid would fall back to
 * the unsaved old placement and start asking again for an answer it had.
 *
 * Driven through the store's own `fetchState` against a mocked API.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }))
vi.mock('@/api', () => ({ default: apiMock }))
vi.mock('../src/api', () => ({ default: apiMock }))

import { useFirstRunStore } from '@/stores/firstRun'

const answer = (fresh) => ({ data: { first_run: false, seeded_agents: [], core_agents: [], fresh_install: fresh } })

beforeEach(() => {
  setActivePinia(createPinia())
  apiMock.get.mockReset()
})

describe('ent#1416 — fetchState keeps a known install kind', () => {
  it.each([true, false])('a forced re-read answering null keeps %s', async (known) => {
    const store = useFirstRunStore()
    apiMock.get.mockResolvedValueOnce(answer(known))
    await store.fetchState()
    expect(store.freshInstall).toBe(known)

    apiMock.get.mockResolvedValueOnce(answer(null))
    await store.fetchState(true)
    expect(store.freshInstall).toBe(known)
  })

  it('a forced re-read that does know the kind still updates it', async () => {
    const store = useFirstRunStore()
    apiMock.get.mockResolvedValueOnce(answer(null))
    await store.fetchState()
    expect(store.freshInstall).toBe(null)
    apiMock.get.mockResolvedValueOnce(answer(true))
    await store.fetchState(true)
    expect(store.freshInstall).toBe(true)
  })
})
