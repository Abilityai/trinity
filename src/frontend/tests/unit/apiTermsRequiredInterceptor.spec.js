// @vitest-environment jsdom
/**
 * trinity-enterprise#1054 — a 403 from the terms gate (`terms_required`, or
 * `invite_expired` / `invite_revoked`) sends the person to /terms, from every
 * HTTP client the SPA uses, and never into a loop.
 *
 * Driven through the REAL clients with stub adapters — `api.js`'s instance,
 * the Workspace's `portalHttp` (where a Workspace-only person's calls actually
 * run) — so the interceptors in those modules are what run. The reaction is
 * a function of its collaborators and is executed with fakes.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import api from '../../src/api'
import { portalHttp } from '../../src/stores/clientPortal'
import { useAuthStore } from '../../src/stores/auth'
import {
  isTermsRefusal, notifyTermsRefusal, reactToTermsRefusal, setTermsRefusalHandler,
} from '../../src/utils/termsGate'

function failWith(status, detail) {
  return async (config) => {
    const error = new Error(`HTTP ${status}`)
    error.config = config
    error.response = { status, data: { detail }, headers: {}, config }
    throw error
  }
}

const TERMS = { code: 'terms_required', version: '2026-11', message: 'Accept the terms.' }
const errorOf = (detail, status = 403) => ({ response: { status, data: { detail } } })

let clock = 1_000_000
beforeEach(() => {
  setActivePinia(createPinia())
  clock += 10_000 // past the burst window, so each test's first refusal reports
  vi.spyOn(Date, 'now').mockReturnValue(clock)
})
afterEach(() => {
  setTermsRefusalHandler(null)
  vi.restoreAllMocks()
})

describe('the HTTP clients report the terms refusal', () => {
  it('api.js reports it once and still rejects the original error', async () => {
    const handler = vi.fn()
    setTermsRefusalHandler(handler)
    await expect(
      api.get('/api/users/me/agents', { adapter: failWith(403, TERMS) }),
    ).rejects.toMatchObject({ response: { status: 403 } })
    expect(handler).toHaveBeenCalledTimes(1)
    expect(handler.mock.calls[0][0].response.data.detail.code).toBe('terms_required')
  })

  it("the Workspace's own client reports it on a platform session", async () => {
    const auth = useAuthStore()
    auth.isAuthenticated = true
    const handler = vi.fn()
    setTermsRefusalHandler(handler)
    await expect(
      portalHttp.get('/api/client-portal/roster', { adapter: failWith(403, TERMS) }),
    ).rejects.toBeTruthy()
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('leaves every other 403 to its caller', async () => {
    const handler = vi.fn()
    setTermsRefusalHandler(handler)
    await expect(api.get('/api/x', { adapter: failWith(403, { code: 'workspace_only' }) })).rejects.toBeTruthy()
    await expect(api.get('/api/x', { adapter: failWith(403, 'Admin access required') })).rejects.toBeTruthy()
    expect(handler).not.toHaveBeenCalled()
  })

  it('reports a burst of refusals once', async () => {
    // The handler navigated (vue-router resolves a completed push with nothing).
    const handler = vi.fn(() => Promise.resolve())
    setTermsRefusalHandler(handler)
    notifyTermsRefusal(errorOf(TERMS), clock)
    notifyTermsRefusal(errorOf(TERMS), clock + 100)
    await Promise.resolve()
    await Promise.resolve()
    notifyTermsRefusal(errorOf(TERMS), clock + 200)
    expect(handler).toHaveBeenCalledTimes(1)
  })
})

describe('isTermsRefusal', () => {
  it('knows the three codes on a 403 and nothing else', () => {
    expect(isTermsRefusal(errorOf(TERMS))).toBe(true)
    expect(isTermsRefusal(errorOf({ code: 'invite_expired' }))).toBe(true)
    expect(isTermsRefusal(errorOf({ code: 'invite_revoked' }))).toBe(true)
    expect(isTermsRefusal(errorOf({ code: 'version_stale' }, 409))).toBe(false)
    expect(isTermsRefusal(errorOf(TERMS, 401))).toBe(false)
    expect(isTermsRefusal(errorOf({ code: 'workspace_only' }))).toBe(false)
  })
})

describe('reactToTermsRefusal — where the person goes', () => {
  it('to /terms, carrying the page to come back to', () => {
    const push = vi.fn(() => Promise.resolve())
    reactToTermsRefusal(errorOf(TERMS), { currentPath: () => '/workspace?agent=scout', push })
    expect(push).toHaveBeenCalledWith({ path: '/terms', query: { redirect: '/workspace?agent=scout' } })
  })

  it('names a closed invitation so the page offers no accept', () => {
    const push = vi.fn(() => Promise.resolve())
    reactToTermsRefusal(errorOf({ code: 'invite_revoked' }), { currentPath: () => '/workspace', push })
    expect(push).toHaveBeenCalledWith({ path: '/terms', query: { redirect: '/workspace', reason: 'invite_revoked' } })
  })

  it('never from the terms page itself — no loop', () => {
    const push = vi.fn()
    reactToTermsRefusal(errorOf(TERMS), { currentPath: () => '/terms?redirect=%2Fworkspace', push })
    expect(push).not.toHaveBeenCalled()
  })

  it('never from a sign-in page — the person must be able to sign out and in', () => {
    const push = vi.fn()
    reactToTermsRefusal(errorOf(TERMS), { currentPath: () => '/login', push })
    expect(push).not.toHaveBeenCalled()
  })
})
