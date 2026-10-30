// @vitest-environment jsdom
/**
 * #3466 — an operator's Workspace tab left open when the platform session dies.
 *
 * The Workspace polls on `portalHttp`, whose request interceptor sends the
 * platform JWT from the auth store and whose response interceptor
 * (`clientPortal.js`, the 401 arm) reports a 401 to the one handler only while
 * the tab is on the platform session. Run here through the REAL transport: real
 * axios instances (`portalHttp`, `@/api`, the global) with a fake adapter in
 * place of the network, the interceptors the modules register, the handler
 * composed exactly as `main.js` composes it, the app's real router, and
 * `Portal.vue` mounted on it.
 *
 *   1. The token dies on the server (expiry, revoke, a password changed
 *      elsewhere): the next poll 401s on the stored token, the verdict is
 *      `logout`, and the tab goes to `/login?redirect=<the Workspace page>`.
 *   2. The session ends in ANOTHER tab (its logout clears storage): this tab
 *      hears it through `storage` and, by #2791's rule, does not navigate. Its
 *      next poll goes out with no credential, so the 401 is not the platform
 *      session's and is not reported. The card offers the platform login
 *      instead (#3466's link) — that is the way back on this path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { START_LOCATION, RouterLink } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})
const Blank = { default: { render: () => null } }
vi.mock('../../src/views/Login.vue', () => Blank)
vi.mock('../../src/views/Dashboard.vue', () => Blank)

import axios from 'axios'
import api from '../../src/api'
import router from '../../src/router'
import Portal from '../../src/views/Portal.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useClientPortalStore, portalHttp } from '../../src/stores/clientPortal'
import {
  applyRequestCredential, fullPathForVerdict, notifyPlatformUnauthorized, pathForVerdict,
  reactToPlatformUnauthorized, reactToStorageEvent, setPlatformUnauthorizedHandler,
} from '../../src/utils/platformSession'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

// The server: a token it no longer accepts answers 401 with the platform's
// Bearer challenge, and so does a request that carries none.
const server = { dead: new Set(), seen: [] }
function adapter(config) {
  const h = config.headers || {}
  const auth = (typeof h.get === 'function' ? h.get('Authorization') : h.Authorization) || ''
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : null
  server.seen.push({ url: config.url, token })
  if (!token || server.dead.has(token)) {
    const err = new Error('Request failed with status code 401')
    err.config = config
    err.response = { status: 401, headers: { 'www-authenticate': 'Bearer' }, data: {}, config }
    return Promise.reject(err)
  }
  return Promise.resolve({ data: {}, status: 200, statusText: 'OK', headers: {}, config })
}

// main.js's wiring, as written there (its lines are pinned in
// platformSessionSync.spec.js; main.js mounts the app on import).
const reported = vi.fn()
axios.defaults.adapter = adapter
portalHttp.defaults.adapter = adapter
api.defaults.adapter = adapter
axios.interceptors.request.use((config) => applyRequestCredential(config))
axios.interceptors.response.use((r) => r, (error) => {
  if (error.response?.status === 401) notifyPlatformUnauthorized(error)
  return Promise.reject(error)
})
setPlatformUnauthorizedHandler((error) => {
  reported(error.config?.url)
  const authStore = useAuthStore()
  let portalTokenPresent = false
  try {
    const portalStore = useClientPortalStore()
    portalTokenPresent = !!portalStore.portalToken || !!portalStore.platformFallbackSuppressed
  } catch { /* no store */ }
  const { navigation } = reactToPlatformUnauthorized(error, {
    path: pathForVerdict(router, START_LOCATION, window.location.pathname),
    returnPath: fullPathForVerdict(router, START_LOCATION, window.location),
    portalTokenPresent,
    adoptStoredSession: () => authStore.adoptStoredSession(),
    logout: () => authStore.logout(),
    goToLogin: (location) => router.push(location),
  })
  return navigation
})
const onStorage = (event) => reactToStorageEvent(event, {
  storage: localStorage,
  adoptStoredSession: () => useAuthStore().adoptStoredSession(),
  applySessionEndedElsewhere: () => useAuthStore().applySessionEndedElsewhere(),
})

let wrapper
async function operatorInWorkspace(path) {
  localStorage.setItem('token', 'OP')
  localStorage.setItem('auth0_user', JSON.stringify({ email: 'op@example.com', role: 'admin' }))
  const auth = useAuthStore()
  Object.assign(auth, {
    isLoading: false, isAuthenticated: true, token: 'OP', profileVerified: true,
    user: { email: 'op@example.com', role: 'admin' },
  })
  await router.push(path)
  await router.isReady()
  wrapper = shallowMount(Portal, { global: { plugins: [router], stubs: { RouterLink } } })
  await flushPromises()
  expect(useClientPortalStore().isPlatformSession).toBe(true)
  server.seen.length = 0
  reported.mockClear()
}
const poll = () => useClientPortalStore().fetchSessionsBatch().catch((e) => e)

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  server.dead.clear()
  globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
  window.addEventListener('storage', onStorage)
})
afterEach(() => {
  window.removeEventListener('storage', onStorage)
  wrapper?.unmount()      // after the assertions: it resets the router to START_LOCATION
  wrapper = null
})

describe('#3466 — the token dies on the server while the Workspace tab is open', () => {
  it('the next poll 401s on the stored token and the tab goes to /login with the page', async () => {
    await operatorInWorkspace('/workspace/inbox')
    server.dead.add('OP')

    const err = await poll()
    await flushPromises()

    expect(err.response.status).toBe(401)
    expect(server.seen[0]).toEqual({ url: '/api/enterprise/client-portal/sessions', token: 'OP' })
    expect(reported).toHaveBeenCalledWith('/api/enterprise/client-portal/sessions')
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query).toEqual({ redirect: '/workspace/inbox' })
    expect(localStorage.getItem('token')).toBeNull()
  })

  it('the same from a chat', async () => {
    await operatorInWorkspace('/workspace/c/s_42')
    server.dead.add('OP')
    await poll()
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/workspace/c/s_42')
  })
})

describe('#3466 — the session ends in another tab (#2791: this tab does not navigate)', () => {
  it('the card offers the platform login with this page, and the no-credential poll is not reported', async () => {
    await operatorInWorkspace('/workspace/inbox')

    // The sibling tab's logout clears both keys; this tab hears `storage`.
    localStorage.removeItem('token')
    localStorage.removeItem('auth0_user')
    window.dispatchEvent(new StorageEvent('storage', { key: 'token', newValue: null, storageArea: localStorage }))
    await flushPromises()

    const err = await poll()
    await flushPromises()
    expect(err.response.status).toBe(401)
    expect(server.seen[0]).toEqual({ url: '/api/enterprise/client-portal/sessions', token: null })
    expect(reported).not.toHaveBeenCalled()
    expect(router.currentRoute.value.fullPath).toBe('/workspace/inbox')

    expect(wrapper.text()).toContain('Sign in to your agents')
    await wrapper.get('[data-testid="workspace-platform-login"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/login?redirect=/workspace/inbox')
  })
})
