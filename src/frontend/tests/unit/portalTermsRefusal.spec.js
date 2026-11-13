// @vitest-environment jsdom
/**
 * trinity-enterprise#1082 — the Workspace store records a terms refusal of the
 * roster as a named state, and keeps the server's message as TEXT.
 *
 * The real `fetchRoster` runs against the real `portalHttp` client with a stub
 * adapter, so its interceptor and its catch are what run. Before this, the
 * gate's `detail` object became `store.error` and the stage printed it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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

describe('ruling A(1): the terms on a portal session', () => {
  it('a refused roster on a portal session names the state and never reports to the /terms handler', async () => {
    const handler = vi.fn()
    setTermsRefusalHandler(handler)
    await roster(403, { detail: TERMS })
    expect(store.termsRefusal).toBe('terms_required')
    expect(handler).not.toHaveBeenCalled()
  })

  it('loadTerms reads on the portal bearer; acceptTerms posts the version and leaves the refusal to the roster reload', async () => {
    const seen = []
    portalHttp.defaults.adapter = async (config) => {
      seen.push({ method: config.method, url: config.url, auth: config.headers?.Authorization, data: config.data })
      const data = config.url.endsWith('/accept')
        ? { accepted: true, created: true, version: '2026-11', accepted_at: 'x' }
        : config.url.endsWith('/my-agents')
          ? { agents: [{ name: 'scout' }] }
          : { required: true, version: '2026-11', title: 'T', text: 'body', accepted: false, is_default: true }
      return { status: 200, data, headers: {}, config, statusText: 'OK' }
    }
    store.termsRefusal = 'terms_required'
    const current = await store.loadTerms()
    expect(current.version).toBe('2026-11')
    expect(store.terms).toEqual(current)
    expect(seen[0]).toMatchObject({ method: 'get', url: '/api/terms/current', auth: 'Bearer tok' })
    const out = await store.acceptTerms('2026-11')
    expect(out.accepted).toBe(true)
    expect(seen[1]).toMatchObject({ method: 'post', url: '/api/terms/accept', auth: 'Bearer tok', data: JSON.stringify({ version: '2026-11' }) })
    // #4515: the refusal is what keeps `PortalTerms` mounted for its
    // `accepted` emit; clearing it here unmounted the screen first and the
    // emit was dropped. The re-bootstrap's roster read is what ends it.
    expect(store.termsRefusal).toBe('terms_required')
    expect(store.terms.accepted).toBe(true)
    await store.fetchRoster()
    expect(store.termsRefusal).toBe(null)
    expect(store.agents.map((a) => a.name)).toEqual(['scout'])
  })

  it('a refused accept rejects with the server error and keeps the refusal', async () => {
    portalHttp.defaults.adapter = answer(409, { detail: { code: 'version_stale', version: '2026-12' } })
    store.termsRefusal = 'terms_required'
    await expect(store.acceptTerms('2026-11')).rejects.toMatchObject({ response: { status: 409 } })
    expect(store.termsRefusal).toBe('terms_required')
  })

  it('sign-out drops the loaded terms with the session', async () => {
    store.terms = { version: 'v' }
    store.signOut()
    expect(store.terms).toBe(null)
  })
})
