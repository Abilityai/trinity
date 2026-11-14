// @vitest-environment jsdom
/**
 * trinity-enterprise#1082 — a Workspace whose roster the terms gate refused
 * shows a named state with a link to /terms: not a skeleton, not the gate's
 * detail object printed as text, not a "Try again" that can only be refused
 * again. The redirect itself stays the HTTP interceptor's job.
 *
 * `Portal.vue` is mounted for real (shallow — the harness of
 * `portalUnavailableTargets.mount.spec.js`) and the REAL `fetchRoster` runs
 * against a refused `portalHttp` call, so the store's catch and the stage
 * chain under test are the shell's own.
 *
 * trinity-enterprise#1107 ruling A(1): the link is the PLATFORM session's way
 * on. A PORTAL session holds no platform credential and cannot open /terms,
 * so the shell renders the terms screen (`PortalTerms`) in place of the
 * roster; accepting re-bootstraps, declining signs out.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
}))

import { portalHttp, useClientPortalStore } from '@/stores/clientPortal'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const Blank = { template: '<div />' }
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: Blank },
      { path: '/workspace/inbox', component: Blank },
      { path: '/workspace/c/:sessionId', component: Blank },
      { path: '/workspace/r/:roomId', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
      { path: '/terms', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

function refuse(detail) {
  portalHttp.defaults.adapter = async (config) => {
    const error = new Error('HTTP 403')
    error.config = config
    error.response = { status: 403, data: { detail }, headers: {}, config }
    throw error
  }
}

let store
function arm({ portal = false } = {}) {
  store = useClientPortalStore()
  // A platform session (the mocked auth store is signed in) unless `portal`.
  store.portalToken = portal ? 'tok' : null
  store.fetchAllSessions = vi.fn(async () => [])
  store.fetchChatState = vi.fn(async () => ({}))
  store.fetchSessions = vi.fn(async () => [])
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
}

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router], stubs: { RouterLink: false } } })
  await flushPromises(); await flushPromises()
  return { w, router }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  arm()
})
afterEach(() => { delete portalHttp.defaults.adapter })

const TERMS = { code: 'terms_required', version: '2026-11', message: 'Accept the current terms to use the Workspace.' }

describe.each([
  ['the Workspace landing', '/workspace'],
  ['a room link', '/workspace/r/room-1'],
])('ruling A(1): a terms-refused roster on %s, on a portal session', (_label, path) => {
  beforeEach(() => arm({ portal: true }))

  it('renders the terms screen in place of the roster — no link to /terms, no retry', async () => {
    refuse(TERMS)
    const { w } = await boot(path)
    const screen = w.findComponent({ name: 'PortalTerms' })
    expect(screen.exists()).toBe(true)
    expect(w.find('[data-testid="portal-terms-required"]').exists()).toBe(false)
    expect(w.findAll('button').filter((b) => /Try again/.test(b.text()))).toHaveLength(0)
    expect(w.findComponent({ name: 'PortalSkeleton' }).exists()).toBe(false)
  })

  it('accepting re-bootstraps into the Workspace; declining signs the session out', async () => {
    refuse(TERMS)
    const { w } = await boot(path)
    const screen = w.findComponent({ name: 'PortalTerms' })
    // The roster now answers. The refusal is NOT cleared by hand: it is what
    // keeps the screen mounted for its emit (#4515), and the shell's reload
    // is what ends it.
    portalHttp.defaults.adapter = async (config) => ({ status: 200, data: { agents: [{ name: 'scout' }] }, headers: {}, config, statusText: 'OK' })
    screen.vm.$emit('accepted')
    await flushPromises(); await flushPromises()
    expect(w.findComponent({ name: 'PortalTerms' }).exists()).toBe(false)
    expect(store.termsRefusal).toBe(null)
    expect(store.agents.map((a) => a.name)).toEqual(['scout'])

    refuse(TERMS)
    await store.fetchRoster()
    await flushPromises()
    const again = w.findComponent({ name: 'PortalTerms' })
    expect(again.exists()).toBe(true)
    const signOut = vi.spyOn(store, 'signOutEverywhere')
    again.vm.$emit('decline')
    await flushPromises(); await flushPromises()
    expect(signOut).toHaveBeenCalledTimes(1)
    expect(store.portalToken).toBe(null)
    expect(w.findComponent({ name: 'PortalTerms' }).exists()).toBe(false)
  })

  it('any other refusal keeps the generic failure with its retry', async () => {
    refuse('Forbidden')
    const { w } = await boot(path)
    expect(w.findComponent({ name: 'PortalTerms' }).exists()).toBe(false)
    expect(w.findAll('button').filter((b) => /Try again/.test(b.text()))).toHaveLength(1)
  })
})

describe.each([
  ['the Workspace landing', '/workspace'],
  ['a room link', '/workspace/r/room-1'],
])('#1082 a terms-refused roster on %s, on a platform session', (_label, path) => {
  it('names the state and links to /terms, carrying the page back', async () => {
    refuse(TERMS)
    const { w, router } = await boot(path)
    const link = w.get('[data-testid="portal-terms-required"]')
    const back = router.currentRoute.value.fullPath
    expect(link.attributes('href')).toBe(router.resolve({ path: '/terms', query: { redirect: back } }).href)
    const text = w.text()
    expect(text).toContain('Accept the terms to continue')
    expect(text).toContain(TERMS.message)
    expect(text).not.toContain('[object Object]')
    expect(text).not.toContain('{"code"')
    expect(w.findAll('button').filter((b) => /Try again/.test(b.text()))).toHaveLength(0)
    expect(w.findComponent({ name: 'PortalSkeleton' }).exists()).toBe(false)
    expect(w.findComponent({ name: 'PortalTerms' }).exists()).toBe(false)
  })

  it('a closed invitation says so, and the link carries the reason', async () => {
    refuse({ code: 'invite_expired', message: 'Expired.' })
    const { w } = await boot(path)
    expect(w.text()).toContain('This invitation has expired.')
    expect(w.get('[data-testid="portal-terms-required"]').attributes('href')).toContain('reason=invite_expired')
  })

  it('any other refusal keeps the generic failure with its retry', async () => {
    refuse('Forbidden')
    const { w } = await boot(path)
    expect(w.find('[data-testid="portal-terms-required"]').exists()).toBe(false)
    expect(w.findAll('button').filter((b) => /Try again/.test(b.text()))).toHaveLength(1)
  })
})
