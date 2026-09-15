/**
 * #2791 — one platform credential, one 401 verdict.
 *
 * The reported symptom: log out and log back in on the main app with a
 * Workspace tab still open from the previous session, and the NEW session dies
 * within seconds. That tab holds the old JWT in a closure, its 20s asks poll
 * 401s, and the handler calls `authStore.logout()` — which removes
 * `localStorage['token']`, i.e. the token the re-login had just written. The
 * handler never asked whether the credential that failed was still the current
 * one.
 *
 * `sessionLostVerdict` is that question, asked once, in a pure function three
 * interceptors share. This file is the table.
 */
import { describe, it, expect } from 'vitest'
import {
  isAuthRoute,
  isWorkspacePath,
  sessionLostVerdict,
  tokenOfRequest,
} from '@/utils/platformSession'

const OLD = 'jwt-from-the-previous-session'
const NEW = 'jwt-from-the-re-login'

describe('the superseded-token arm (finding 1 — the reported bug)', () => {
  it('a stale tab whose token was replaced does NOT destroy the new session', () => {
    expect(sessionLostVerdict({
      failedToken: OLD,
      storedToken: NEW,
      path: '/workspace',
    })).toBe('stale')
  })

  it('is decided by the token, not by the surface — the main app is just as vulnerable', () => {
    // A dashboard tab left open across a re-login holds the old JWT in
    // `axios.defaults` exactly as the Workspace tab does.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: NEW, path: '/agents/scout',
    })).toBe('stale')
  })

  it('still logs out when the credential that failed IS the stored one', () => {
    // The ordinary expiry case must keep working — this is not a blanket
    // "never log out", which would leave a dead session on screen forever.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: '/agents/scout',
    })).toBe('logout')
  })

  it('logs out when the failed token is unknown, rather than assuming innocence', () => {
    // A request that carried no Authorization header (or a caller we cannot
    // read) must not buy immunity: absence is not evidence of supersession.
    expect(sessionLostVerdict({
      failedToken: null, storedToken: OLD, path: '/agents/scout',
    })).toBe('logout')
  })
})

describe('a client is never thrown onto the operator login (AC #5, #2261 preserved)', () => {
  it('a Workspace client whose browser holds a DEAD operator JWT is not bounced', () => {
    // `initializeAuth` calls `fetchUserProfile` through bare axios on every page
    // load. Before this, its 401 bounced the client to the operator /login, and
    // navigating back to /workspace signed them in again.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD,
      portalTokenPresent: true, path: '/workspace',
    })).toBe('ignore')
  })

  it('an anonymous visitor on the Workspace is not bounced either', () => {
    expect(sessionLostVerdict({ storedToken: null, path: '/workspace' })).toBe('ignore')
    expect(sessionLostVerdict({ storedToken: null, path: '/workspace/c/abc' })).toBe('ignore')
    expect(sessionLostVerdict({ storedToken: null, path: '/portal' })).toBe('ignore')
  })

  it('an OPERATOR on the Workspace whose session expired IS still bounced (ent#357)', () => {
    // Their workspace session IS the platform session, so there is no second
    // credential to fall back to. Dropping this would strand them.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD,
      portalTokenPresent: false, path: '/workspace',
    })).toBe('logout')
  })

  it('off the Workspace a stray portal token does not veto the bounce', () => {
    // The veto is about which session owns the SURFACE. `/agents/scout` is an
    // operator surface whatever else the browser is holding, and widening the
    // veto to every path would leave a dead operator session rendered.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD,
      portalTokenPresent: true, path: '/agents/scout',
    })).toBe('logout')
  })

  it('a signed-out browser off the Workspace is still sent to sign in', () => {
    expect(sessionLostVerdict({ storedToken: null, path: '/agents/scout' })).toBe('logout')
  })
})

describe('the pages that are already the way out', () => {
  it('never bounce off /login, /setup or /m', () => {
    for (const path of ['/login', '/setup', '/m']) {
      expect(sessionLostVerdict({ failedToken: OLD, storedToken: OLD, path })).toBe('ignore')
    }
  })

  it('and the auth-route test is exact, not a prefix', () => {
    // `/login` must not shield `/loginsomething`, and the Workspace prefixes
    // deliberately ARE prefixes because they carry sub-routes.
    expect(isAuthRoute('/login')).toBe(true)
    expect(isAuthRoute('/login/extra')).toBe(false)
    expect(isWorkspacePath('/workspace/r/room_1')).toBe(true)
    expect(isWorkspacePath('/worksp')).toBe(false)
  })
})

describe('reading the credential a request actually carried', () => {
  it('pulls the bearer out of either header casing', () => {
    expect(tokenOfRequest({ headers: { Authorization: `Bearer ${OLD}` } })).toBe(OLD)
    expect(tokenOfRequest({ headers: { authorization: `Bearer ${OLD}` } })).toBe(OLD)
  })

  it('answers null rather than guessing', () => {
    // Every one of these must read as "unknown", which the verdict table above
    // treats as NOT superseded — the conservative direction.
    expect(tokenOfRequest(undefined)).toBeNull()
    expect(tokenOfRequest({})).toBeNull()
    expect(tokenOfRequest({ headers: {} })).toBeNull()
    expect(tokenOfRequest({ headers: { Authorization: 'Basic abc' } })).toBeNull()
    expect(tokenOfRequest({ headers: { Authorization: 123 } })).toBeNull()
  })
})
