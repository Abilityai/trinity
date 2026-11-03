// @vitest-environment jsdom
/**
 * trinity-enterprise#1082 — first sign-in of a person who has not accepted the
 * terms: the Workspace must open /terms, every time.
 *
 * The miss: a refusal answered while the SPA still read /login (the socket
 * ticket, minted the moment the token is set) is ignored by the handler — a
 * sign-in page never navigates — but it used to spend the 2 s burst window
 * anyway, so the Workspace's own first refusals were dropped and the page sat
 * on its failed stage.
 *
 * Driven through the app's REAL router (`router/index.js`, so its guard runs)
 * with the handler wired exactly as `main.js` wires it, and an injected clock
 * (`Date.now`), which the router's setup-status cache and the burst window
 * both read. Views are stubbed so a navigation does not mount the app.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { START_LOCATION } from 'vue-router'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('../../src/views/Portal.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Login.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Dashboard.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/TermsAcceptance.vue', () => ({ default: { render: () => null } }))

import router from '../../src/router'
import { useAuthStore } from '../../src/stores/auth'
import { fullPathForVerdict } from '../../src/utils/platformSession'
import { notifyTermsRefusal, reactToTermsRefusal, setTermsRefusalHandler } from '../../src/utils/termsGate'

const TERMS = { code: 'terms_required', version: '2026-11', message: 'Accept the terms.' }
const refusal = () => ({ response: { status: 403, data: { detail: TERMS } } })
const here = () => router.currentRoute.value

let clock = 50_000_000
function wireLikeMainJs() {
  setTermsRefusalHandler((error) => reactToTermsRefusal(error, {
    currentPath: () => fullPathForVerdict(router, START_LOCATION, window.location),
    push: (location) => router.push(location),
  }))
}

function signedOut() {
  const auth = useAuthStore()
  auth.isLoading = false
  auth.isAuthenticated = false
  auth.profileVerified = false
  auth.user = null
  return auth
}

// What `_finalizeLogin` leaves behind: a token, a verified `user` profile.
function verifyCode(auth) {
  auth.isAuthenticated = true
  auth.profileVerified = true
  auth.user = { email: 'x@example.com', role: 'user' }
}

/**
 * One first sign-in, as the reporter saw it. Returns the route it ends on.
 * `rewire: false` keeps the handler — and the gate state — of the sign-in
 * before it, as a sign-out and sign-in within one page session does.
 */
async function firstSignIn({ rewire = true } = {}) {
  if (rewire) wireLikeMainJs()
  const auth = signedOut()
  await router.replace('/login')
  expect(here().path).toBe('/login')
  // Typing the code takes longer than the setup-status cache's 5 s, so the
  // guard on the way to the Workspace refetches it.
  clock += 6_000
  verifyCode(auth)
  // The socket ticket refusal lands while the SPA still reads /login.
  notifyTermsRefusal(refusal(), clock)
  // afterLogin → the Workspace.
  await router.push('/workspace')
  expect(here().path).toBe('/workspace')
  // The roster refusal, 100 ms later — well inside the old 2 s window.
  clock += 100
  notifyTermsRefusal(refusal(), clock)
  await flushPromises()
  return here()
}

beforeEach(() => {
  setActivePinia(createPinia())
  clock += 60_000
  vi.spyOn(Date, 'now').mockImplementation(() => clock)
  globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
})
afterEach(() => {
  setTermsRefusalHandler(null)
  vi.restoreAllMocks()
})

describe('first sign-in reaches /terms', () => {
  it('a refusal received on /login does not suppress the redirect for the next refusal on /workspace', async () => {
    const landed = await firstSignIn()
    expect(landed.path).toBe('/terms')
    expect(landed.query.redirect).toBe('/workspace')
  })

  it('a refusal that lands while the /login → /workspace guard is still pending is ignored, and the Workspace still redirects', async () => {
    wireLikeMainJs()
    const auth = signedOut()
    await router.replace('/login')
    clock += 6_000
    verifyCode(auth)
    let release
    globalThis.fetch = vi.fn(() => new Promise((resolve) => {
      release = () => resolve({ json: async () => ({ setup_completed: true }) })
    }))
    const nav = router.push('/workspace')
    await flushPromises()
    expect(globalThis.fetch).toHaveBeenCalled() // the guard is waiting on setup status
    expect(here().path).toBe('/login')
    notifyTermsRefusal(refusal(), clock) // the ticket refusal, mid-navigation
    release()
    await nav
    expect(here().path).toBe('/workspace')
    clock += 100
    notifyTermsRefusal(refusal(), clock)
    await flushPromises()
    expect(here().path).toBe('/terms')
    expect(here().query.redirect).toBe('/workspace')
  })

  it('ten consecutive first sign-ins all reach /terms', async () => {
    const landed = []
    for (let i = 0; i < 10; i++) {
      landed.push((await firstSignIn()).path)
      clock += 100
    }
    expect(landed).toEqual(Array(10).fill('/terms'))
  })

  it('ten sign-ins in one page session, the handler registered once, all reach /terms', async () => {
    wireLikeMainJs()
    const landed = []
    for (let i = 0; i < 10; i++) {
      landed.push((await firstSignIn({ rewire: false })).path)
      clock += 100
    }
    expect(landed).toEqual(Array(10).fill('/terms'))
  })

  it('a burst on /workspace pushes once, and later responses on /terms never navigate', async () => {
    wireLikeMainJs()
    const auth = signedOut()
    await router.replace('/login')
    verifyCode(auth)
    clock += 6_000
    await router.push('/workspace?agent=scout')
    const push = vi.spyOn(router, 'push')
    notifyTermsRefusal(refusal(), clock)
    notifyTermsRefusal(refusal(), clock + 10)
    notifyTermsRefusal(refusal(), clock + 20)
    await flushPromises()
    expect(push).toHaveBeenCalledTimes(1)
    expect(here().path).toBe('/terms')
    expect(here().query.redirect).toBe('/workspace?agent=scout')
    // Past the window, a straggler refused while ON /terms: no loop.
    clock += 5_000
    notifyTermsRefusal(refusal(), clock)
    await flushPromises()
    expect(push).toHaveBeenCalledTimes(1)
    expect(here().path).toBe('/terms')
  })
})
