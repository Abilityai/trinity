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
import axios, { AxiosError, AxiosHeaders } from 'axios'
import { createRouter, createMemoryHistory, START_LOCATION } from 'vue-router'
import {
  isAuthRoute,
  isBearerChallenge,
  isPublicChatPath,
  isSharedCanvasPath,
  isWorkspacePath,
  pathForVerdict,
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

// ---------------------------------------------------------------------------
// #3406 — the public token pages
// ---------------------------------------------------------------------------
//
// `/chat/:token` and `/canvas/s/:token` carry their own access rule and their
// own sign-in, so a 401 there never navigates. Whether it ENDS the platform
// session is the server's to say: every rejection `get_current_user` makes
// carries `WWW-Authenticate: Bearer`, and a link's own session check answers a
// bare 401 (pinned server-side by tests/unit/test_3406_bearer_challenge_contract.py).

const CHAT = '/chat/abc123'
const CANVAS = '/canvas/s/tok_1'

describe('a public chat link is never sent to the operator /login (#3406)', () => {
  it('the reported case: a visitor with no operator session at all', () => {
    // Today: `logout`, then router.push('/login') — the visitor's history 401
    // after 24 h landed them on "Sign in to manage your agents".
    expect(sessionLostVerdict({ storedToken: null, path: CHAT })).toBe('ignore')
  })

  it("the second victim: an operator's LIVE token rides the request, and the 401 is the link's", () => {
    // The global interceptor attaches the stored JWT to every bare-axios call,
    // so `failedToken === storedToken` here although the JWT is fine — the
    // link's own session expired. AC2: they stay signed in.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: CHAT, bearerChallenged: false,
    })).toBe('ignore')
  })

  it('a dead operator token (a Bearer-challenged 401) ends that session IN PLACE', () => {
    // Without this the ws-ticket POST retries every 5 s for as long as the dead
    // token sits in storage — a 401 loop that never ends on this page.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: CHAT, bearerChallenged: true,
    })).toBe('logout-in-place')
  })

  it('acts only when the token that failed IS the stored one', () => {
    // A request that went out with no credential, answered after a sibling tab
    // wrote a fresh one, says nothing about that fresh session.
    expect(sessionLostVerdict({
      failedToken: null, storedToken: NEW, path: CHAT, bearerChallenged: true,
    })).toBe('ignore')
    // Nothing stored any more — the revoke that `logout()` sends on its way
    // out — leaves nothing to end, so it cannot loop.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: null, path: CHAT, bearerChallenged: true,
    })).toBe('ignore')
  })

  it('a superseded token still converges rather than destroying the new session', () => {
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: NEW, path: CHAT, bearerChallenged: true,
    })).toBe('stale')
  })
})

describe('a shared canvas shows its own sign-in card (#3406)', () => {
  it('a signed-out visitor is not bounced: the page offers "Sign in" itself', () => {
    expect(sessionLostVerdict({ storedToken: null, path: CANVAS })).toBe('ignore')
  })

  it('a dead stored token is ended IN PLACE, so the card\'s Sign in reaches the form', () => {
    // Every 401 here is about the platform credential: `get_optional_user`
    // reads a bad token as anonymous, so the share answers sign_in_required —
    // with no Bearer challenge. Ignoring it would leave `isAuthenticated` true
    // and the `/login -> /` guard would bounce the Sign in link to the dashboard.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: CANVAS, bearerChallenged: false,
    })).toBe('logout-in-place')
  })

  it('the revoke on the way out, and a credential-less request, end nothing', () => {
    expect(sessionLostVerdict({ failedToken: OLD, storedToken: null, path: CANVAS })).toBe('ignore')
    expect(sessionLostVerdict({ failedToken: null, storedToken: NEW, path: CANVAS })).toBe('ignore')
  })

  it('a superseded token still converges', () => {
    expect(sessionLostVerdict({ failedToken: OLD, storedToken: NEW, path: CANVAS })).toBe('stale')
  })
})

describe('the mobile admin signs in inline, so a dead token there ends in place (#3406)', () => {
  it('a Bearer-challenged 401 on the stored token: logout-in-place, not a bounce', () => {
    // The PWA opens at /m. Before, a first-load 401 was judged as "/" and
    // bounced to the desktop /login; judged as /m it would be ignored, leaving
    // a dead session behind a signed-in-looking page and the ws-ticket retry
    // 401ing every 5 s. MobileAdmin shows its own login once it is ended.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: '/m', bearerChallenged: true,
    })).toBe('logout-in-place')
  })

  it('anything short of that still never acts on /m', () => {
    expect(sessionLostVerdict({ failedToken: OLD, storedToken: OLD, path: '/m' })).toBe('ignore')
    expect(sessionLostVerdict({ failedToken: null, storedToken: NEW, path: '/m', bearerChallenged: true })).toBe('ignore')
    expect(sessionLostVerdict({ failedToken: OLD, storedToken: NEW, path: '/m', bearerChallenged: true })).toBe('ignore')
  })

  it('/login is NOT given the same arm: a wrong password is challenged too', () => {
    // A sibling tab's fresh token rides the login POST, and its 401 carries
    // `WWW-Authenticate: Bearer` — acting on it would sign that session out.
    expect(sessionLostVerdict({
      failedToken: OLD, storedToken: OLD, path: '/login', bearerChallenged: true,
    })).toBe('ignore')
  })
})

describe('the public path predicates', () => {
  it('match however the address is cased, as the router does', () => {
    // vue-router matches case-insensitively: /Chat/x opens the public chat.
    expect(isPublicChatPath('/Chat/abc')).toBe(true)
    expect(isSharedCanvasPath('/Canvas/S/tok')).toBe(true)
    expect(sessionLostVerdict({ storedToken: null, path: '/Chat/abc' })).toBe('ignore')
  })

  it('are prefixes of the token routes, not of look-alikes', () => {
    expect(isPublicChatPath('/chat/abc')).toBe(true)
    expect(isPublicChatPath('/chat')).toBe(false)
    expect(isPublicChatPath('/chats/abc')).toBe(false)
    expect(isPublicChatPath(undefined)).toBe(false)
    expect(isSharedCanvasPath('/canvas/s/tok')).toBe(true)
    expect(isSharedCanvasPath('/canvas/tok')).toBe(false)
    expect(isSharedCanvasPath(null)).toBe(false)
  })
})

describe('whose credential a 401 rejected — the Bearer challenge', () => {
  // A REAL axios rejection: the adapter answers 401 the way the XHR adapter's
  // `settle` does — the raw header string parsed into AxiosHeaders, wrapped in
  // an AxiosError — and goes back out through axios's own request chain.
  async function rejectionWith(rawHeaders) {
    const client = axios.create({
      adapter: async (config) => {
        const response = {
          data: { detail: 'x' }, status: 401, statusText: 'Unauthorized',
          headers: AxiosHeaders.from(rawHeaders), config, request: {},
        }
        throw new AxiosError('Request failed with status code 401',
          AxiosError.ERR_BAD_REQUEST, config, response.request, response)
      },
    })
    try {
      await client.get('/api/anything')
    } catch (error) {
      return error
    }
    throw new Error('expected a 401 rejection')
  }

  it('reads the challenge get_current_user sends', async () => {
    const error = await rejectionWith('www-authenticate: Bearer\r\ncontent-type: application/json')
    expect(error.response.status).toBe(401)
    expect(isBearerChallenge(error)).toBe(true)
  })

  it('a link-session 401 carries none', async () => {
    expect(isBearerChallenge(await rejectionWith('content-type: application/json'))).toBe(false)
  })

  it('is case-insensitive on the scheme and tolerant of parameters', async () => {
    expect(isBearerChallenge(await rejectionWith('WWW-Authenticate: bearer realm="trinity"'))).toBe(true)
    expect(isBearerChallenge(await rejectionWith('WWW-Authenticate: Basic realm="x"'))).toBe(false)
  })

  it('answers false rather than guessing on anything it cannot read', () => {
    expect(isBearerChallenge(undefined)).toBe(false)
    expect(isBearerChallenge({})).toBe(false)
    expect(isBearerChallenge({ response: {} })).toBe(false)
    expect(isBearerChallenge({ response: { headers: { 'www-authenticate': 42 } } })).toBe(false)
  })
})

describe('the path a 401 is judged against (#3406)', () => {
  const page = { template: '<div/>' }
  function routerFor() {
    return createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: page },
        { path: '/chat/:token', component: page },
        { path: '/canvas/s/:token', component: page },
        { path: '/workspace', component: page },
        { path: '/m', component: page },
      ],
    })
  }

  for (const target of [CHAT, CANVAS, '/workspace', '/m']) {
    it(`during the FIRST navigation it is the address being loaded (${target}), not "/"`, async () => {
      // `currentRoute` is START_LOCATION — path "/" — until the first
      // navigation completes, and the guard holds that navigation open across
      // `checkSetupStatus()`'s fetch. A dead token's `/api/users/me` or
      // ws-ticket 401 lands in that window, and was judged as the dashboard.
      const router = routerFor()
      let release
      const held = new Promise((resolve) => { release = resolve })
      router.beforeEach(async () => { await held; return true })
      const navigation = router.push(target)
      await new Promise((resolve) => setTimeout(resolve, 0))

      expect(router.currentRoute.value).toBe(START_LOCATION)
      expect(pathForVerdict(router, START_LOCATION, target)).toBe(target)

      release()
      await navigation
      // Once settled, the router is the source — it reflects in-app navigation.
      expect(pathForVerdict(router, START_LOCATION, '/somewhere-stale')).toBe(target)
    })
  }

  it('falls back to the location when there is no router to ask', () => {
    expect(pathForVerdict(undefined, START_LOCATION, CHAT)).toBe(CHAT)
    expect(pathForVerdict({ currentRoute: { value: { path: '' } } }, START_LOCATION, CHAT)).toBe(CHAT)
    expect(pathForVerdict(undefined, START_LOCATION, undefined)).toBe('')
  })
})
