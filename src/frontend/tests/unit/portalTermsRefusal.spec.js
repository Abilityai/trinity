// @vitest-environment jsdom
/**
 * trinity-enterprise#1082 — the Workspace store records a terms refusal of the
 * roster as a named state, and keeps the server's message as TEXT.
 *
 * The real `fetchRoster` runs against the real `portalHttp` client with a stub
 * adapter, so its interceptor and its catch are what run. Before this, the
 * gate's `detail` object became `store.error` and the stage printed it.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { portalHttp, useClientPortalStore } from '../../src/stores/clientPortal'
import { setTermsRefusalHandler } from '../../src/utils/termsGate'

const TERMS = { code: 'terms_required', version: '2026-11', message: 'Accept the current terms to use the Workspace.' }

function answer(status, data) {
  return async (config) => {
    if (status < 400) return { status, data, headers: {}, config, statusText: 'OK' }
    const error = new Error(`HTTP ${status}`)
    error.config = config
    error.response = { status, data, headers: {}, config }
    throw error
  }
}

let store
beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok' // a client session: the interceptor does not report, the store still names the state
})
afterEach(() => {
  delete portalHttp.defaults.adapter
  setTermsRefusalHandler(null)
})

async function roster(status, data) {
  portalHttp.defaults.adapter = answer(status, data)
  await store.fetchRoster()
}

describe('fetchRoster on a terms refusal', () => {
  it('names the refusal, keeps the message as a string and reaches a verdict', async () => {
    await roster(403, { detail: TERMS })
    expect(store.termsRefusal).toBe('terms_required')
    expect(typeof store.error).toBe('string')
    expect(store.error).toBe(TERMS.message)
    expect(store.rosterLoaded).toBe(true)
    expect(store.agents).toEqual([])
    expect(store.unavailable).toBe(false)
  })

  it('falls back to a sentence when the refusal carries no message', async () => {
    await roster(403, { detail: { code: 'terms_required' } })
    expect(store.error).toBe('Accept the current terms to continue.')
  })

  it('carries a closed invitation by its code', async () => {
    await roster(403, { detail: { code: 'invite_revoked', message: 'Withdrawn.' } })
    expect(store.termsRefusal).toBe('invite_revoked')
  })

  it('a later successful roster clears it', async () => {
    await roster(403, { detail: TERMS })
    await roster(200, { agents: [{ name: 'scout' }] })
    expect(store.termsRefusal).toBe(null)
    expect(store.error).toBe(null)
    expect(store.agents.map((a) => a.name)).toEqual(['scout'])
  })

  it('a 404 and a plain 403 are not terms refusals', async () => {
    await roster(404, { detail: 'Not found' })
    expect(store.termsRefusal).toBe(null)
    expect(store.unavailable).toBe(true)
    await roster(403, { detail: 'Forbidden' })
    expect(store.termsRefusal).toBe(null)
    expect(store.error).toBe('Forbidden')
  })

  it('a refusal followed by any other failure drops the terms state', async () => {
    await roster(403, { detail: TERMS })
    await roster(500, { detail: 'boom' })
    expect(store.termsRefusal).toBe(null)
  })

  it('sign-out clears it', async () => {
    await roster(403, { detail: TERMS })
    store.signOut()
    expect(store.termsRefusal).toBe(null)
  })
})
