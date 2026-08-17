/**
 * #2258 — the wiring half of "Sign out signs out".
 *
 * `workspaceSession.spec.js` pins the DECISION (`signOutEverywhere()`: which
 * credential ends, in what order, where to land). This file pins that the view
 * and the sidebar actually route through it, and that the platform logout is
 * ordered so the global 401 interceptors cannot re-enter it.
 *
 * `.vue` files cannot be imported here (`vitest.config.js` is node-only, no
 * plugin-vue), so the component half is asserted the way this suite already
 * does for `Portal.vue` (`portalLeaveSpecificRoute.spec.js`): by scanning the
 * source for the delegation. It is a spelling check by necessity — which is
 * exactly why the decision itself was moved somewhere it can be CALLED.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

// Module-level, as vitest requires of anything hoisted: `auth.js` reads
// localStorage at import time (node has none), and imports axios (mocked so
// the revoke never leaves the process). Both apply only to the last describe.
vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: () => instance },
      { interceptors: instance.interceptors, defaults: instance.defaults },
    ),
  }
})

/** Body of `function <name>(...) { ... }` by brace matching (async or not). */
function fnBody(source, name) {
  const start = source.indexOf(`function ${name}(`)
  expect(start, `${name}() not found`).toBeGreaterThan(-1)
  const open = source.indexOf('{', source.indexOf(')', start))
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1)
  }
  throw new Error(`unterminated ${name}()`)
}

describe('Portal.vue routes sign-out through the store decision', () => {
  const source = read('../../src/views/Portal.vue')
  const body = fnBody(source, 'onSignOut')

  it('delegates to signOutEverywhere(), not the bare state-clearing primitive', () => {
    expect(body).toMatch(/await store\.signOutEverywhere\(\)/)
    // The bare `store.signOut()` is the pre-fix handler — the one that cleared
    // the portal token and thereby ACTIVATED the platform fallback.
    expect(body).not.toMatch(/store\.signOut\(\)/)
  })

  it('a platform principal is pushed to the platform login', () => {
    expect(body).toMatch(/router\.push\(PLATFORM_LOGIN_ROUTE\)/)
  })

  it('a client still escapes the stage (query included) instead of navigating away', () => {
    expect(body).toMatch(/escapeStage\(\)/)
  })

  it('holds the frame while the credential is being revoked, so no principal sees the other kind\'s screen', () => {
    // The `signingOut` branch is the FIRST v-if in the template chain — above
    // the OTP form — or a platform user watches "enter the email an operator
    // shared agents with" for the beat between logout and the /login push.
    const signingOutIdx = source.indexOf('v-if="signingOut"')
    const signInIdx = source.indexOf('!store.isClientSignedIn')
    expect(signingOutIdx).toBeGreaterThan(-1)
    expect(signingOutIdx).toBeLessThan(signInIdx)
    expect(body).toMatch(/signingOut\.value = true/)
    // …and it is a guard, not just a flag: a second click mid-flight is a no-op.
    expect(body).toMatch(/if \(signingOut\.value\) return/)
  })
})

describe('PortalSidebar.vue labels the button for the principal it acts on (AC #5)', () => {
  const source = read('../../src/components/portal/PortalSidebar.vue')

  it('the accessible name comes from the shared, tested helper', () => {
    expect(source).toMatch(/signOutLabelFor\(props\.isPlatformSession\)/)
    expect(source).toMatch(/:aria-label="signOutLabel"/)
    expect(source).toMatch(/:title="signOutLabel"/)
  })

  it('there is ONE button, not two v-if-alternated ones (the #2159 focus lesson)', () => {
    const emits = source.match(/@click="\$emit\('sign-out'\)"/g) || []
    expect(emits).toHaveLength(1)
  })
})

// The platform `logout()` used to revoke on the network FIRST and clear the
// local record after. Two readers key on that record: the global 401
// interceptors (which bounce to /login while `localStorage['token']` exists)
// and the router guard (which sends /login → / while `isAuthenticated`). With
// an already-expired JWT the revoke answers 401, the interceptor saw the token
// still present, and re-entered logout + pushed the operator /login — for a
// client on the Workspace, the wrong login. Ordering the local clear first
// closes both.
describe('auth.logout() clears the local session before the network revoke', () => {
  beforeEach(async () => {
    localStorage.clear()
    const { setActivePinia, createPinia } = await import('pinia')
    setActivePinia(createPinia())
  })

  it('at the moment the revoke is issued, the token is already gone from storage and state', async () => {
    const axios = (await import('axios')).default
    const { useAuthStore } = await import('@/stores/auth')
    const auth = useAuthStore()
    auth.token = 'jwt'
    auth.isAuthenticated = true
    localStorage.setItem('token', 'jwt')
    axios.defaults.headers.common['Authorization'] = 'Bearer jwt'

    let seen = null
    axios.post.mockImplementationOnce(async () => {
      seen = {
        stored: localStorage.getItem('token'),
        authed: auth.isAuthenticated,
        // The revoke itself must still be able to carry the credential: it
        // rides the axios DEFAULT header, deleted only after the call.
        header: axios.defaults.headers.common['Authorization'],
      }
      const err = new Error('401'); err.response = { status: 401 }; throw err
    })

    // logout() clears a legacy cookie last; node has no `document`. Shimmed
    // HERE, after Vue has loaded — a hoisted shim would make `runtime-dom`
    // probe `document.createElement` at import and throw.
    globalThis.document = { cookie: '' }
    try {
      await auth.logout()
    } finally {
      delete globalThis.document
    }

    expect(seen).toEqual({ stored: null, authed: false, header: 'Bearer jwt' })
    // …and a failed revoke still leaves the browser signed out.
    expect(auth.isAuthenticated).toBe(false)
    expect(localStorage.getItem('token')).toBeNull()
    expect(axios.defaults.headers.common['Authorization']).toBeUndefined()
  })
})
