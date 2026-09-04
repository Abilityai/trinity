/**
 * Workspace / client portal store (epic #78 / #79; OSS core since ent#356).
 *
 * Domain store for the client-facing portal surface. First slice: the
 * "My Agents" roster (agents shared with the signed-in email) + the operator
 * exposure config. Backed by the gated `/api/enterprise/client-portal/*`
 * endpoints — 404 in OSS/unentitled builds, but the route guard
 * ent#356 moved the module into OSS core, so it ships in every build.
 */
import { defineStore } from 'pinia'
import { normalizeRoomRow, WORKSPACE_ROOT } from '@/components/portal/portalUtils'
import {
  applyBriefings,
  briefingHydrationPlan,
  mergeRosterBriefings,
  shouldRequestBriefing,
} from '@/components/portal/portalBriefingState'
import axios from 'axios'
import { useAuthStore } from './auth'
// #2162: the page size for a windowed report read. A dependency-free leaf
// shared with the operator reports store — never re-typed here, since the
// backend already owns REPORT_ROWS_PAGE_DEFAULT and a third hand-written copy
// is the shape that drifts while each side's tests pin its own version.
import { REPORT_ROWS_PAGE as ROWS_PAGE } from '@/utils/reportPaging'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'
// #2261 — per-TAB, so an operator working in another tab is untouched by a
// client's idle timeout (that is the whole reason expiry may not end the
// platform session). sessionStorage, not localStorage: it must survive a
// refresh of THIS tab and nothing wider.
const FALLBACK_SUPPRESSED_KEY = 'trinity.workspaceFallbackSuppressed'

function readSuppressed() {
  try {
    return sessionStorage.getItem(FALLBACK_SUPPRESSED_KEY) === '1'
  } catch {
    // Private mode / storage disabled: fail to NOT-suppressed, which is the
    // pre-#2261 behaviour rather than a workspace nobody can enter.
    return false
  }
}

function writeSuppressed(on) {
  try {
    if (on) sessionStorage.setItem(FALLBACK_SUPPRESSED_KEY, '1')
    else sessionStorage.removeItem(FALLBACK_SUPPRESSED_KEY)
  } catch {
    /* state still holds for this page-life; the refresh case degrades, loudly enough */
  }
}

// #2261 — every workspace request goes through THIS instance, and its
// interceptor is the single place a workspace credential is decided.
//
// Why an instance at all: `auth.js` installs the platform JWT as
// `axios.defaults.headers.common.Authorization`, and per-request headers MERGE
// over defaults. So on the bare `axios` export, a workspace call that passes no
// Authorization still sends the operator's — which is why #2258 rejected a
// "suppress the fallback" flag as dishonest: it hid the operator's roster on
// screen while the wire kept carrying the operator's credential.
//
// The interceptor DELETES whatever was inherited and then sets exactly what the
// store decided, so "the workspace is signed out" is a statement about the wire
// and not only about the screen. That is what makes the suppression above
// honest, and it is asserted directly (a request built with a platform JWT in
// `axios.defaults` and a suppressed session must carry no Authorization).
export const portalHttp = axios.create()

// #2261 — what to do when a workspace request 401s while the workspace session
// IS the platform session (ent#357's operator case).
//
// The global `axios` 401 interceptor in `main.js` used to catch these, and it
// decided with `onWorkspace && localStorage['token']`. Two things changed. Moving
// workspace calls onto `portalHttp` took them out of that interceptor's reach, so
// the operator bounce had to be re-established here or an operator whose JWT
// expired would sit on "Failed to load your agents" forever. And that predicate
// was already the wrong one: on the browser this issue is about, a CLIENT is
// signed in while `localStorage['token']` is an operator's — so one mistyped
// digit on the OTP form would 401 and throw the client onto /login while
// destroying the operator's session (the hazard `workspace-session-signout.md`
// lists as objection 3 to a suppression flag). `isPlatformSession` is the
// question actually being asked, and it answers false in exactly that case.
//
// A callback rather than a router import: the store is imported BY the views the
// router loads, so importing the router here is a cycle.
let _onPlatformSessionLost = null

export function setPlatformSessionLostHandler(fn) {
  _onPlatformSessionLost = fn
}

portalHttp.interceptors.request.use((config) => {
  // The store is the ONLY source of a workspace credential. Whatever arrived on
  // the config — a caller's `headers: this.authHeader`, or anything axios merged
  // in from defaults — is discarded and replaced by the current decision.
  //
  // Rebuilding from the store rather than preserving what was there is the whole
  // point, and the first version of this got it wrong: it kept any `Authorization`
  // it found, which cannot tell "the store decided this" from "axios inherited
  // this", so it would have PRESERVED an inherited platform JWT rather than
  // stripping it. That mattered less than it looked (verified: axios 1.19.0 does
  // not propagate later `axios.defaults.headers.common` mutations into an instance
  // created earlier, so nothing is inherited today) — but the property this
  // interceptor exists to guarantee cannot rest on a merge behaviour we do not
  // control and do not test. Now it holds by construction.
  //
  // Fail-closed if the store is unreachable (Pinia not active): send no credential
  // rather than a stale one. Every workspace call originates from a component or
  // action with Pinia active; the two that do not (`requestCode`/`verifyCode`)
  // need no credential anyway.
  const headers = config.headers || {}
  delete headers.Authorization
  delete headers.authorization
  try {
    const decided = useClientPortalStore().authHeader?.Authorization
    if (decided) headers.Authorization = decided
  } catch {
    /* no store, no credential */
  }
  config.headers = headers
  return config
})
// #2258: where a PLATFORM principal lands after signing out of the Workspace —
// the platform login, because the session they just ended was the platform
// one. Exported so the test pins the destination the view actually pushes.
export const PLATFORM_LOGIN_ROUTE = '/login'
// Mirrors `SESSION_ROTATION_HEADER` in client_portal/portal_auth.py (ent#375).
// Lower-case: axios normalises response header names.
const SESSION_ROTATION_HEADER = 'x-trinity-session-token'

// #2128 — the one sentence shown when a chat with several agents cannot be
// started on this instance. Exported from the STORE, not from portalUtils: this
// is the message the store's own guard throws, and no store in this codebase
// imports from `@/components` — adding that edge would invert the dependency
// direction for one string.
//
// It states the capability and stops. Never "not licensed", never an edition
// name, never an upgrade prompt: the audience for this surface is the
// operator's customer, who can neither buy the missing module nor act on
// knowing it exists.
export const MULTI_AGENT_UNAVAILABLE =
  'Chats with more than one agent are not available on this instance.'

// ent#375 — adopt a rotated session token wherever it arrives.
//
// ONE interceptor rather than touching all 13 request sites: a new portal call
// added later cannot forget to opt in, and forgetting would be invisible (the
// session just stops sliding and the user is back to re-authenticating, which
// is the bug this issue fixes). Scoped to portal URLs and to responses that
// actually carry the header, so it is inert for every other request in the app.
//
// Registered at module scope, guarded so hot-reload cannot stack duplicates.
let _rotationInterceptorInstalled = false

function installRotationInterceptor() {
  if (_rotationInterceptorInstalled) return
  _rotationInterceptorInstalled = true
  // #2261: registered on `portalHttp`, not the bare `axios` export — every
  // workspace call moved onto the instance, and a response interceptor on the
  // global would no longer see any of them. Missing that is exactly the silent
  // failure this interceptor's own comment warns about: the session simply stops
  // sliding and the client is back to re-authenticating.
  portalHttp.interceptors.response.use(
    (response) => {
    const fresh = response?.headers?.[SESSION_ROTATION_HEADER]
    if (fresh) {
      const url = response?.config?.url || ''
      if (url.includes('/client-portal/')) {
        const current = localStorage.getItem(PORTAL_TOKEN_KEY)
        // Only a CLIENT session rotates. An operator previewing on a platform
        // JWT holds no portal token and must not acquire one from a header.
        if (current && current !== fresh) {
          localStorage.setItem(PORTAL_TOKEN_KEY, fresh)
          try {
            useClientPortalStore().portalToken = fresh
          } catch {
            // Pinia not active yet — localStorage is the source of truth on the
            // next store construction, so the rotation is not lost.
          }
        }
      }
    }
    return response
    },
    (error) => {
      // ent#357's operator bounce, re-homed onto the instance the workspace now
      // uses — and asked with the right discriminator (see the note beside
      // `setPlatformSessionLostHandler`). A CLIENT's 401 (wrong code, expired
      // token) must never reach it: their tab may well hold an operator's JWT,
      // and bouncing would destroy a session that did nothing wrong.
      if (error?.response?.status === 401) {
        try {
          if (useClientPortalStore().isPlatformSession) _onPlatformSessionLost?.()
        } catch {
          // Pinia not active (module-scope request, or teardown): no session to
          // reason about, so there is nothing to bounce.
        }
      }
      return Promise.reject(error)
    },
  )
}

// Live from import, so a session restored from localStorage rotates too — not
// only one created by a fresh sign-in in this tab.
installRotationInterceptor()

// #2163 — in-flight and per-session attempt bookkeeping for briefing
// hydration. Module-level rather than store state on purpose: nothing renders
// it, and reactive state that nothing renders is a re-render budget spent on
// bookkeeping (the store's own precedent). `briefingAttempts` implements the
// one-retry-per-agent-per-session rule from `shouldRequestBriefing`, so a
// wedged agent costs one extra bounded call rather than one per chat open.
const briefingsInFlight = new Set()
const briefingAttempts = new Map()
let briefingsBatchInFlight = false

export const useClientPortalStore = defineStore('clientPortal', {
  state: () => ({
    clientEmail: null,
    agents: [],
    loading: false,
    error: null,
    // ent#357: set when the backend says the workspace module is absent /
    // unentitled (404), so the view can say so instead of showing an empty
    // roster that looks like "nobody shared anything with you".
    unavailable: false,
    // A portal session token (verified email, no platform account). When set,
    // it authenticates the workspace endpoints; else we fall back to the
    // platform session. Persisted so an external client stays signed in.
    portalToken: localStorage.getItem(PORTAL_TOKEN_KEY) || null,
    // ent#375 — set when a session ENDED rather than never existing. The
    // sign-in form reads it to say "your session expired" instead of silently
    // re-appearing, which is indistinguishable from "you were never signed in"
    // and reads as the app having lost the thread.
    sessionExpired: false,
    // #2261 — while true, a platform session does NOT re-derive a workspace
    // session in this tab. Set when a CLIENT session expires here, so the
    // browser that was a client's does not silently become the operator's; the
    // operator's own session is untouched (see `continueAsPlatform`).
    platformFallbackSuppressed: readSuppressed(),
    // ent#364 — agent-initiated asks addressed to this user. ONE list, read by all
    // three surfaces (sidebar count, agent page, inline in chat), because the
    // underlying row is one row: answering anywhere clears it everywhere with no
    // sync step, and three separate queries is how that stops being true.
    asks: [],
    asksAvailable: false,   // false when the backend does not serve /asks (404/403)
    // Where the user was when it expired, so re-authenticating returns them
    // there instead of the roster root.
    resumePath: null,
    // #2128 — may a chat hold MORE THAN ONE agent on this instance? Carried on
    // the roster payload, because the platform feature-flag endpoint is
    // JWT-gated and an external client on a portal session cannot read it.
    //
    // Default false is the live policy for everything unconfigured, so it has
    // to be the safe value: the picker renders single-select until a roster
    // actually says otherwise, and the component never sees an undefined
    // tri-state.
    multiAgentChatAvailable: false,
    // Set once a roster attempt REACHED A VERDICT for this session. The room
    // route needs to tell "still loading" from "loaded, and the answer is no" —
    // without it a hard-loaded /workspace/r/:id would flash a refusal it then
    // takes back on an entitled instance.
    rosterLoaded: false,

    // #2198 — the last successfully-loaded thread list, and whether the most
    // recent attempt failed.
    //
    // Both exist because the batch inverted a failure mode. The old per-agent
    // fan-out could not reject: each call was individually caught
    // (`catch { return [] }`) so "one down agent never blanks the whole list".
    // One request cannot degrade per agent, so without a remembered list a
    // single 500 would blank a populated sidebar — which
    // design-system-contract:43/55 forbid ("no skeleton re-flash", "nothing
    // shifts on arrival") and which `Portal.vue::bootstrap()` cannot survive:
    // it awaits refreshThreads() before resolveAgentQuery(), so a transient
    // blip would break Workspace deep-link landing entirely.
    lastSessions: [],
    sessionsFailed: false,

    // --- Reports tab (#2162) ---
    // Which agent the report state below belongs to, and a monotonic counter
    // every report request captures before its first await. A reset bumps it,
    // so a response that arrives after an agent switch is discarded instead of
    // landing under the new agent's name.
    reportsAgent: null,
    _reportsGeneration: 0,
    reports: [],
    // Set ONLY by a fetch that succeeded — the empty state reads it, and
    // "loaded" must never mean "failed and returned nothing" (contract #15).
    reportsLoaded: false,
    reportsError: null,
    reportPayloads: {},
    // id -> {total, loaded}; present only for a payload the server actually
    // windowed, so a bounded document never renders a paging footer.
    reportRowMeta: {},
    // id -> message. Deliberately NOT stored inside reportPayloads: an error
    // object there would be handed to the renderer and presented as a report.
    reportErrors: {},
    _reportInFlight: {},
  }),

  getters: {
    // Portal-session token wins; otherwise the platform session.
    authHeader() {
      if (this.portalToken) return { Authorization: `Bearer ${this.portalToken}` }
      // #2261 — the suppression has to reach the WIRE, not just the screen. This
      // getter is the explicit half (it stops handing the platform header to a
      // tab whose client session expired); `portalHttp`'s interceptor is the
      // implicit half (it strips the same header when axios merges it in from
      // `axios.defaults`). Either alone leaves the operator's credential going
      // out under a signed-out UI — which is the exact objection that sank the
      // suppression flag in #2258.
      if (this.platformFallbackSuppressed) return {}
      const authStore = useAuthStore()
      return authStore.authHeader
    },

    // ent#357: TWO ways to be signed in to the workspace.
    //
    // This getter used to be `!!state.portalToken`, which is why an
    // already-signed-in platform user was shown the email-OTP form on a surface
    // they were entitled to see: the transport layer below already fell back to
    // the platform header, and the backend's `get_portal_identity` already
    // accepts a platform JWT and resolves it to that user's email — only this
    // one predicate disagreed, so the round-trip it forced was pure ceremony.
    //
    // Deriving the internal case from the platform session is also what makes
    // "signing out of the platform ends it" true by construction: there is no
    // second credential to revoke, so `authStore.logout()` ends the workspace
    // session in the same act.
    //
    // #2258: the SAME derivation runs the other way, and that is the trap.
    // Signing out of the WORKSPACE has to end the platform session too when
    // one exists — clearing only the portal token is precisely what activates
    // this fallback, so a "Sign out" that stopped there re-entered the
    // Workspace as the operator on the next refresh. See `signOutEverywhere`.
    // #2261: `platformFallbackSuppressed` is the third term, and it is the whole
    // fix. Expiry cannot end the platform credential (that would log out an
    // operator in another tab over a client's idle timeout), so the only place
    // left to break the derivation is the derivation itself — for this tab, until
    // someone says otherwise.
    isPlatformSession() {
      if (this.platformFallbackSuppressed) return false
      return !this.portalToken && !!useAuthStore().isAuthenticated
    },
    isClientSignedIn() {
      return !!this.portalToken || this.isPlatformSession
    },
    // ent#364: only PENDING asks are actionable. An expired one still renders (see
    // `asks`), but it is not something the badge should nag about — the person did
    // not fail to answer something they can still answer.
    openAsks: (state) => state.asks.filter((a) => a.status === 'pending'),
    askCount() {
      return this.openAsks.length
    },
    asksForAgent() {
      return (agentName) => this.asks.filter((a) => a.agent_name === agentName)
    },
  },

  actions: {
    // Step 1 — request a 6-digit code. Always resolves (generic response); the
    // backend reveals nothing about whether the email has access.
    async requestCode(email) {
      await portalHttp.post('/api/enterprise/client-portal/auth/request', { email })
    },

    // Step 2 — verify the code → portal session token (persisted).
    async verifyCode(email, code) {
      const { data } = await portalHttp.post('/api/enterprise/client-portal/auth/verify', { email, code })
      this.portalToken = data.token
      this.clientEmail = data.email
      // #2261 — a successful client sign-in is a new session in this tab, so the
      // expiry marker is spent. Leaving it would cost an operator who later
      // works in this tab a needless "Continue as" click, and a marker that
      // outlives what it described is how the next reader stops trusting it.
      this.platformFallbackSuppressed = false
      writeSuppressed(false)
      this.sessionExpired = false
      localStorage.setItem(PORTAL_TOKEN_KEY, data.token)
      return data
    },

    // ent#375 — the backend slides the session and hands back a rotated token
    // in a response header. Swapping it in is the whole client side of renewal.
    // A response without the header is the normal case (rotation only happens
    // once a session is halfway through its idle window), so this is a no-op
    // almost always.
    adoptRotatedToken(response) {
      const fresh = response?.headers?.[SESSION_ROTATION_HEADER]
      if (!fresh || fresh === this.portalToken) return
      // Only a CLIENT session rotates. An operator previewing with a platform
      // JWT has no portal token, and must not acquire one from a header.
      if (!this.portalToken) return
      this.portalToken = fresh
      localStorage.setItem(PORTAL_TOKEN_KEY, fresh)
    },

    // An ended session, told honestly. `expired` distinguishes "your session
    // ran out" from "you signed out" — the user sees the same form either way,
    // so the reason has to be carried explicitly.
    endSession({ expired = false, resumePath = null } = {}) {
      this.signOut()
      this.sessionExpired = expired
      this.resumePath = expired ? resumePath : null
      // #2261 — ONLY on expiry, and only after `signOut()` (which clears it).
      // A user-initiated sign-out destroys the platform credential outright
      // (`signOutEverywhere`), so suppressing there would leave a stale marker
      // that greets the next legitimate platform login in this tab with a
      // needless "Continue as" step.
      if (expired) {
        this.platformFallbackSuppressed = true
        writeSuppressed(true)
      }
    },

    signOut() {
      this.portalToken = null
      this.clientEmail = null
      this.agents = []
      this.sessionExpired = false
      this.resumePath = null
      // #2128: both are per-session facts. A different client signing in on the
      // same browser must not inherit the previous one's capability verdict,
      // and `rosterLoaded` must go back to "no verdict yet" or the room route
      // would read a stale one as authoritative.
      this.multiAgentChatAvailable = false
      this.rosterLoaded = false
      // #2261: the primitive clears the suppression; `endSession({expired})`
      // re-arms it immediately afterwards. Keeping the clear HERE is what stops
      // a marker from outliving the session it was about.
      this.platformFallbackSuppressed = false
      writeSuppressed(false)
      localStorage.removeItem(PORTAL_TOKEN_KEY)
    },

    // #2261 — the operator's escape from the suppression above.
    //
    // The suppression has to fail CLOSED (a tab that was a client's shows the
    // sign-in form), which necessarily catches the case where the operator is
    // the person sitting there. This is their one explicit click back in, and it
    // is explicit on purpose: re-deriving the platform identity automatically is
    // the bug. Nothing here mints or reads a credential — the platform JWT was
    // always live; what changes is whether this tab treats it as a workspace
    // session.
    continueAsPlatform() {
      this.platformFallbackSuppressed = false
      writeSuppressed(false)
      this.sessionExpired = false
      this.resumePath = null
    },

    // #2258 — the whole user-initiated sign-out, in one place, so the sequence
    // is pinned by a unit test instead of trapped in a component this suite
    // cannot mount (`vitest.config.js` is node-only, no plugin-vue).
    //
    // A platform session IS a workspace session (ent#357), so signing out of
    // the Workspace while one exists has to end THAT. There is no portal
    // credential to clear for a platform user; and for a client on a browser
    // that also holds a platform login, clearing only the portal token is what
    // ACTIVATES the platform fallback — the reported bug.
    //
    // A persisted "suppress the platform fallback" flag was the issue's own
    // suggestion and was rejected on evidence: `auth.js` installs the platform
    // JWT as an axios DEFAULT header, and per-request headers MERGE over
    // defaults, so a flag hides the operator's roster on screen while every
    // portal request still carries the operator's credential and the backend
    // still answers as them (`get_portal_principal` scopes by whatever it is
    // handed). Destroying the credential is the one design where the wire and
    // the screen agree, and it needs no flag, no persistence, and leaves the
    // 401-bounce predicate (`localStorage['token']`) correct by construction.
    //
    // ORDER is load-bearing. The platform credential goes FIRST, so there is
    // no window in which `portalToken` is already gone while
    // `isAuthenticated` is still true — that is exactly the state in which
    // `authHeader` would hand an in-flight portal poll the operator's
    // identity. `signOut()` stays the plain state-clearing primitive:
    // `endSession({expired})` calls it, and an EXPIRED portal session must
    // never end a platform session (expiry is not a user act, and would take
    // an operator working in another tab with it).
    //
    // Known residual, stated rather than hidden: a CLIENT session that merely
    // EXPIRES on a browser which later gained a platform login still falls
    // back to that platform identity through `endSession` → `signOut`. It is
    // the same class by a route the UI does not produce (the OTP form never
    // renders while a platform JWT exists, so that ordering needs the platform
    // login to arrive AFTER the client signed in) and is tracked as #2261.
    //
    // Returns where the caller should navigate: an operator to the platform
    // login (they signed out of Trinity), a client to the workspace root (the
    // OTP form). The `wasPlatform` read must happen before either clear.
    async signOutEverywhere() {
      const wasPlatform = this.isPlatformSession
      const authStore = useAuthStore()
      if (authStore.isAuthenticated) await authStore.logout()
      this.signOut()
      return wasPlatform ? PLATFORM_LOGIN_ROUTE : WORKSPACE_ROOT
    },

    // Chat one turn with a rostered agent over the portal session (the gated,
    // roster-scoped endpoint — the OSS chat endpoint fences the portal token).
    // Returns `{response, cost, session_id}` — the echoed session_id lets the
    // caller adopt the thread a first (session-less) turn landed in.
    // ent#451: `newThread` says a null `sessionId` means "start a fresh one",
    // not "I don't know which". The backend cannot tell those apart from the
    // absence alone — which is why New chat used to land in the existing
    // conversation — and it ignores the flag when a session IS named.
    async sendPortalChat(agentName, message, sessionId = null, { newThread = false } = {}) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/chat`,
        { message, session_id: sessionId, new_thread: newThread },
        { headers: this.authHeader }
      )
      return data
    },

    // ent#286: begin a turn and get its id back immediately (202), so the UI can
    // show live tool activity while the agent works. The synchronous
    // `sendPortalChat` above is untouched — it stays the documented API surface
    // for headless clients (ent#83), and is still the fallback when streaming
    // is unavailable.
    async startPortalChat(agentName, message, sessionId = null, { newThread = false } = {}) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/chat/stream`,
        { message, session_id: sessionId, new_thread: newThread },
        { headers: this.authHeader }
      )
      return data   // {execution_id, session_id}
    },

    // ent#155: stop one of my own in-flight turns. Roster-scoped and
    // started-by-this-caller-scoped server-side; a turn that already ended
    // answers `already_terminal` rather than an error, because losing that
    // race is not something the person can act on.
    async cancelPortalTurn(agentName, executionId) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/executions/${executionId}/terminate`,
        {},
        { headers: this.authHeader }
      )
      return data
    },

    // Read one turn's live log. `fetch` + ReadableStream rather than
    // EventSource: EventSource cannot send an Authorization header, which would
    // force the portal token into the query string — the same credential-in-URL
    // leak #550 removed from WebSockets. `onEvent` is called per parsed SSE
    // payload; the promise resolves when the stream ends.
    async streamPortalExecution(agentName, executionId, onEvent, { signal } = {}) {
      const res = await fetch(
        `/api/enterprise/client-portal/agents/${agentName}/executions/${executionId}/stream`,
        { headers: { ...this.authHeader, Accept: 'text/event-stream' }, signal }
      )
      if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      // SSE frames are separated by a blank line and can be split across
      // chunks, so parse on the boundary rather than per chunk.
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          let payload
          try { payload = JSON.parse(line.slice(5).trim()) } catch { continue }
          onEvent(payload)
          if (payload?.type === 'stream_end') return
        }
      }
    },

    // --- Multi-agent chats, backed by rooms (ent#361) ------------------------
    //
    // A chat with ONE agent stays a portal thread: that path resumes, streams
    // and reattaches (ent#358/#286). A chat with two or more is a room, because
    // rooms are the only substrate that models several agents, @mention-waking
    // and per-participant budgets. The sidebar merges both.

    // #2128 — ONE chokepoint in front of every room call. A build with no rooms
    // substrate must never issue one: the request is a guaranteed 4xx whose
    // generic failure copy is the dead end this issue is about.
    //
    // Applied to all FIVE room actions, not just the two reachable today. Three
    // of them have exactly one caller each, inside a component the render gate
    // stops mounting — so gating them is redundant *for the current call graph*,
    // which is precisely the claim `learnings.md` 2026-07-01 records failing: a
    // kill-switch is only as airtight as its least-gated entry point. Cost:
    // three lines. Deliberate, not oversight.
    _requireRooms() {
      if (this.multiAgentChatAvailable) return
      const err = new Error(MULTI_AGENT_UNAVAILABLE)
      // A typed code, never message-sniffing: the view has to tell this apart
      // from a transport failure, which needs different copy.
      err.code = 'rooms_unavailable'
      throw err
    },

    // The capability can vanish BETWEEN the roster load and the confirm (an
    // entitlement lapsing, an instance restarted into lockdown). A definitive
    // refusal from the rooms endpoint itself is evidence the substrate is gone,
    // so lower the flag and let the picker collapse on the same tick. Without
    // this the gate is correct at load and still dead-ends mid-session.
    //
    // 404/403 only. A network error or a 5xx is "could not ask", not "is
    // absent", and lowering on those would unmount a live room over a blip.
    //
    // But the STATUS ALONE IS NOT THE SIGNAL, and reading it as one is a live
    // bug: on a fully entitled instance the rooms module answers "you cannot
    // reach that agent" with a 403 and "you are not in that room" with a
    // uniform 404. Lowering on those turns one denied request into a
    // session-long false claim about the operator's build — and overwrites the
    // only message that tells the user what to do.
    //
    // The two are cleanly separable by the BODY, because absence and denial are
    // authored by different layers: a module that is SERVING answers with its
    // own structured `detail: {code, message}`, while absence is a plain string
    // — FastAPI's own "Not Found" when the route was never mounted, and the
    // entitlement gate's one-sentence 403 when it is mounted but unlicensed. A
    // coded detail therefore PROVES the substrate is present; treat it as an
    // ordinary refusal and let the caller surface the server's own words.
    //
    // Runs on all FIVE room calls, for the same reason `_requireRooms` does —
    // and here it is not redundant. `refreshThreads()` is event-driven, not
    // periodic, so a capability that lapses while a room is OPEN is only ever
    // observed by the room's own calls: without this the 3s poll swallows its
    // 404 (`load()` only reports on a full load), sending shows the generic
    // "That message was not delivered.", and the state never converges — the
    // gate is correct at load and the room is a dead end for the rest of the
    // session. With it, the poll lowers the flag, `<PortalRoom>` unmounts, and
    // the room route's honest refusal takes the stage. That is AC #4 holding
    // THROUGH a transition, and it is only safe because of the discriminator
    // above: `/api/rooms/:id` answers a uniform coded 404 for a room the caller
    // is not in, and reading THAT as absence would let a stale room link switch
    // an entitled workspace to single-select.
    _noteRoomsRefusal(err) {
      const status = err?.response?.status
      if (status !== 404 && status !== 403) return err
      const detail = err?.response?.data?.detail
      if (detail && typeof detail === 'object' && detail.code) return err
      this.multiAgentChatAvailable = false
      err.code = 'rooms_unavailable'
      err.message = MULTI_AGENT_UNAVAILABLE
      return err
    },

    async createRoom(agentNames, name) {
      this._requireRooms()
      try {
        const { data } = await portalHttp.post(
          '/api/rooms',
          { name: name || 'New chat', agents: agentNames },
          { headers: this.authHeader }
        )
        return data
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    async fetchRooms() {
      // Returns [] rather than throwing: this one is called on every sidebar
      // refresh and its caller already treats "no rooms" as normal. Returning
      // early removes a guaranteed-4xx round-trip per refresh.
      if (!this.multiAgentChatAvailable) return []
      try {
        const { data } = await portalHttp.get('/api/rooms', { headers: this.authHeader })
        return (data.rooms || []).map((r) => ({ ...r, is_room: true }))
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    // ent#360 — the agent page. One call for the whole page: it is one screen,
    // and fetching header/stats/asks/work separately renders it in pieces.
    async fetchAgentPage(agentName, window = '7d') {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/page`,
        { headers: this.authHeader, params: { window } },
      )
      return data
    },

    async fetchAgentReports(agentName) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/reports`,
        { headers: this.authHeader },
      )
      return data.reports || []
    },

    // ent#366 — one click on a message or a deliverable. Deliberately NOT
    // fail-soft like the deliverables read: a rating that silently did not
    // record would leave the person believing they were heard, so the caller
    // gets the error and shows it next to the control.
    async submitRating(agentName, body) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/ratings`,
        body,
        { headers: this.authHeader },
      )
      return data
    },

    // ent#365 — deliverables produced in ONE chat, for the inline cards. Same
    // endpoint, narrowed server-side: the audience condition is applied
    // regardless, so a session id belonging to someone else returns nothing
    // rather than their deliverables. Fail-soft to [] — a chat that cannot list
    // its deliverables must still be a working chat.
    async fetchSessionDeliverables(agentName, sessionId) {
      if (!agentName || !sessionId) return []
      try {
        const { data } = await portalHttp.get(
          `/api/enterprise/client-portal/agents/${agentName}/reports`,
          { headers: this.authHeader, params: { session_id: sessionId } },
        )
        return data.reports || []
      } catch {
        return []
      }
    },

    // #2162: `rowsLimit` windows a TABULAR payload server-side. Sent on every
    // expand — the server decides whether the payload actually has a row axis,
    // so the client never predicts the shape from an agent-authored
    // `display_hint` that can disagree with what was filed. A non-tabular
    // payload simply comes back whole with no `row_meta`.
    async fetchAgentReport(agentName, reportId, { rowsOffset, rowsLimit } = {}) {
      const params = {}
      if (rowsLimit !== undefined && rowsLimit !== null) {
        params.rows_limit = rowsLimit
        params.rows_offset = rowsOffset || 0
      }
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/reports/${encodeURIComponent(reportId)}`,
        { headers: this.authHeader, params },
      )
      return data
    },

    // --- Reports tab orchestration (#2162) --------------------------------
    //
    // Lives in the store, not the component (design contract #21: loading flags
    // belong to stores). That is not only tidiness — it is what makes the three
    // riskiest paths here testable at all, since this project has no
    // component-mount harness but does unit-test store fetchers against a
    // mocked axios.
    //
    // EVERY await is generation-guarded. Clearing refs on agent switch cannot
    // cancel a promise already in flight, so without this:
    //
    //   on agent A, open Reports   → fetch starts
    //   click agent B              → state cleared
    //   A's fetch resolves         → writes A's reports into the cleared state
    //   open Reports on B          → "already loaded" → no refetch
    //                              → A's reports render under B's name
    //
    // …which is the ent#359 class of bug, and adding a `reportsLoaded` flag
    // makes it PERMANENT rather than self-correcting: B stays marked
    // loaded-with-A's-data for the life of the mount. The flag and the guard
    // have to ship together.

    /** Drop all report state and invalidate every in-flight report request. */
    resetAgentReports(agentName = null) {
      this._reportsGeneration += 1
      this.reportsAgent = agentName
      this.reports = []
      this.reportsLoaded = false
      this.reportsError = null
      this.reportPayloads = {}
      this.reportRowMeta = {}
      this.reportErrors = {}
      this._reportInFlight = {}
    },

    async loadAgentReports(agentName) {
      // Self-correcting: a caller that loads for a different agent without
      // resetting first gets the reset anyway, rather than merging two agents.
      if (this.reportsAgent !== agentName) this.resetAgentReports(agentName)
      const gen = this._reportsGeneration
      this.reportsError = null
      try {
        const rows = await this.fetchAgentReports(agentName)
        if (gen !== this._reportsGeneration) return
        this.reports = rows
        // Only a SUCCEEDED fetch may set this: the empty state gates on it, and
        // an empty list after a failure is the wrong sentence (contract #15).
        this.reportsLoaded = true
      } catch {
        if (gen !== this._reportsGeneration) return
        // Paired with `LoadFailed`'s title ("Couldn't load reports"), so this
        // says what to DO rather than restating what happened (contract #25).
        this.reportsError = 'The request failed. Check your connection and try again.'
      }
    },

    async loadAgentReport(agentName, reportId) {
      if (this.reportPayloads[reportId] || this._reportInFlight[reportId]) return
      const gen = this._reportsGeneration
      this._reportInFlight = { ...this._reportInFlight, [reportId]: true }
      // Clear a previous failure up front, so a retry that succeeds does not
      // leave the error banner sitting under a rendered report.
      const { [reportId]: _dropped, ...remainingErrors } = this.reportErrors
      this.reportErrors = remainingErrors
      try {
        const data = await this.fetchAgentReport(agentName, reportId, {
          rowsOffset: 0, rowsLimit: ROWS_PAGE,
        })
        if (gen !== this._reportsGeneration) return
        const payload = data?.payload ?? {}
        this.reportPayloads = { ...this.reportPayloads, [reportId]: payload }
        // `row_meta` present ⇒ the server windowed a real table. Absent ⇒ a
        // bounded document, and no paging footer must render for it.
        const meta = data?.row_meta
        if (meta && Array.isArray(payload.rows)) {
          this.reportRowMeta = {
            ...this.reportRowMeta,
            [reportId]: { total: meta.total, loaded: payload.rows.length },
          }
        }
      } catch {
        if (gen !== this._reportsGeneration) return
        // Deliberately NOT written into reportPayloads: a `{error: …}` object
        // there would be handed to the renderer and presented AS a report.
        //
        // Retryable regardless of status: the read swallows a DB fault into the
        // same 404 a missing report gets (invariant #8), so the client cannot
        // tell a transient failure from a gone report — and stranding someone on
        // a transient one is the worse of the two mistakes.
        this.reportErrors = {
          ...this.reportErrors, [reportId]: 'Could not load this report.',
        }
      } finally {
        // Generation-guarded like every other write: a reset already emptied
        // this map, so clearing "our" key afterwards would clear a NEW request's
        // marker instead and let a duplicate through — the guard leaking through
        // its own bookkeeping.
        if (gen === this._reportsGeneration) {
          const { [reportId]: _done, ...stillInFlight } = this._reportInFlight
          this._reportInFlight = stillInFlight
        }
      }
    },

    /** Dismiss one report's error banner without retrying (contract #18). */
    clearReportError(reportId) {
      if (!this.reportErrors[reportId]) return
      const { [reportId]: _dropped, ...rest } = this.reportErrors
      this.reportErrors = rest
    },

    async loadMoreReportRows(agentName, reportId) {
      const meta = this.reportRowMeta[reportId]
      const current = this.reportPayloads[reportId]
      // Terminal guard. Without it a click at loaded === total appends [],
      // `loaded` never moves, and ReportTable's `meta.total > rows.length`
      // never goes false — a permanently visible, permanently inert button.
      if (!meta || !current || meta.loaded >= meta.total) return
      if (this._reportInFlight[reportId]) return
      const gen = this._reportsGeneration
      this._reportInFlight = { ...this._reportInFlight, [reportId]: true }
      try {
        const data = await this.fetchAgentReport(agentName, reportId, {
          rowsOffset: meta.loaded, rowsLimit: ROWS_PAGE,
        })
        if (gen !== this._reportsGeneration) return
        const next = data?.payload?.rows
        const nextMeta = data?.row_meta
        if (!Array.isArray(next) || !nextMeta) return
        // Re-read through the store rather than trusting the `current` captured
        // before the await — the same reason the operator store captures it.
        const held = this.reportPayloads[reportId]
        if (!held || !Array.isArray(held.rows)) return
        const merged = [...held.rows, ...next]
        this.reportPayloads = {
          ...this.reportPayloads, [reportId]: { ...held, rows: merged },
        }
        this.reportRowMeta = {
          ...this.reportRowMeta,
          [reportId]: { total: nextMeta.total, loaded: merged.length },
        }
      } catch {
        if (gen !== this._reportsGeneration) return
        this.reportErrors = {
          ...this.reportErrors, [reportId]: 'Could not load more rows.',
        }
      } finally {
        // Generation-guarded like every other write: a reset already emptied
        // this map, so clearing "our" key afterwards would clear a NEW request's
        // marker instead and let a duplicate through — the guard leaking through
        // its own bookkeeping.
        if (gen === this._reportsGeneration) {
          const { [reportId]: _done, ...stillInFlight } = this._reportInFlight
          this._reportInFlight = stillInFlight
        }
      }
    },

    // ent#359 — per-viewer star + unread state, for BOTH chat kinds in one call.
    // Threads and rooms come from different endpoints (and different repos) but
    // sort into a single sidebar list, so their view state has to arrive
    // together or the list would reshuffle as the second response landed.
    //
    // Keyed `${kind}:${id}`: the two id spaces are independent, so an id alone
    // is not a key.
    async fetchChatState() {
      const { data } = await portalHttp.get('/api/enterprise/client-portal/chat-state', {
        headers: this.authHeader,
      })
      const out = {}
      for (const c of data.chats || []) {
        if (c && c.kind && c.id) out[`${c.kind}:${c.id}`] = c
      }
      return out
    },

    async setChatStar(kind, chatId, starred) {
      const url = `/api/enterprise/client-portal/chat-state/${kind}/${encodeURIComponent(chatId)}/star`
      const cfg = { headers: this.authHeader }
      if (starred) await portalHttp.put(url, null, cfg)
      else await portalHttp.delete(url, cfg)
    },

    // Fire-and-forget by design: a failed read marker leaves a stale badge,
    // which is not worth interrupting navigation over, and the next state fetch
    // corrects it.
    async markChatRead(kind, chatId) {
      try {
        await portalHttp.post(
          `/api/enterprise/client-portal/chat-state/${kind}/${encodeURIComponent(chatId)}/read`,
          null,
          { headers: this.authHeader },
        )
      } catch { /* stale badge only */ }
    },

    // `since` is the seq cursor: 0 loads the whole transcript, a later value
    // fetches only what the client has not seen.
    async fetchRoom(roomId, since = 0) {
      this._requireRooms()
      try {
        const { data } = await portalHttp.get(`/api/rooms/${roomId}`, {
          headers: this.authHeader, params: { since },
        })
        return data
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    async postRoomMessage(roomId, content) {
      this._requireRooms()
      try {
        const { data } = await portalHttp.post(
          `/api/rooms/${roomId}/messages`, { content },
          { headers: this.authHeader }
        )
        return data   // {room_id, seq, mentions, woke}
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    async addRoomParticipant(roomId, agentName) {
      this._requireRooms()
      try {
        const { data } = await portalHttp.post(
          `/api/rooms/${roomId}/participants`, { agent_name: agentName, role: 'member' },
          { headers: this.authHeader }
        )
        return data
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    // The client's conversation threads with an agent (most-recent first) — the
    // chat-history list backing the session switcher.
    async fetchSessions(agentName) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/sessions`,
        { headers: this.authHeader }
      )
      return data.sessions || []
    },

    // Open a fresh conversation thread ("New chat"). Returns the empty session.
    async createSession(agentName) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/sessions`,
        {},
        { headers: this.authHeader }
      )
      return data
    },

    // Voice mode (#78): synthesize a reply to speech via the agent's ElevenLabs
    // voice. Returns a playable object URL for an <audio> src, or null when voice
    // is unavailable / synthesis failed / over the cost cap (caller stays text).
    async synthesizeTts(agentName, text) {
      try {
        const { data } = await portalHttp.post(
          `/api/enterprise/client-portal/agents/${agentName}/tts`,
          { text },
          // ent#440 review: `portalHttp` is created with no `timeout`, and this
          // call is awaited in SPEAKING — the one live state with no timer of
          // its own (barge-in and Stop are its only exits). A hung /tts
          // therefore holds the mic stream open indefinitely with nothing
          // counting, which is the same hot-mic outcome FR-9 forbids that the
          // /stt timeout was added to close. Same 60s bound, same reason.
          { headers: this.authHeader, responseType: 'blob', timeout: 60000 }
        )
        return URL.createObjectURL(data)
      } catch {
        return null
      }
    },

    // Speech-to-text for voice input on browsers without the Web Speech API
    // (Firefox): a recorded audio Blob → transcript via ElevenLabs Scribe.
    async transcribeStt(agentName, blob) {
      const form = new FormData()
      const ext = blob.type.includes('ogg') ? 'ogg' : blob.type.includes('webm') ? 'webm' : blob.type.includes('mp4') ? 'mp4' : 'dat'
      form.append('file', blob, `voice.${ext}`)
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/stt`,
        form,
        // ent#440 review (NEW-1): `portalHttp` is created with no `timeout`,
        // i.e. axios's `timeout: 0` — a hung /stt never settles. That is the
        // root cause behind the hot mic: the hands-free loop sat in
        // TRANSCRIBING with the microphone tracks live until the user pressed
        // Stop. The caller's watchdog now spans this await too, so this is a
        // second layer — but a promise that can never settle is the wrong
        // primitive to hand a UI regardless of who is watching it. Comfortably
        // above a real transcription of a bounded utterance (MAX_UTTERANCE_MS
        // is 30s).
        { headers: this.authHeader, timeout: 60000 }
      )
      return data.text || ''
    },

    // Cross-chat search over the client's conversations (all rostered agents), by
    // thread title or message content. Returns [{agent_name, session_id, title,
    // snippet, last_message_at}] newest-active first.
    async searchChats(query) {
      const { data } = await portalHttp.get('/api/enterprise/client-portal/search', {
        headers: this.authHeader,
        params: { q: query },
      })
      return data.results || []
    },

    // Files a rostered agent has shared (FILES-001), each with a download URL
    // (`?sig=` token is the credential — the download route is public).
    // ent#438 — the canvases this agent published to the people it works
    // with. Metadata only; blocks come per canvas on open, the same split the
    // reports pair uses and for the same reason (a canvas is capped at 512 KiB
    // and a list of them is not a list view).
    async fetchAgentCanvases(agentName) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/canvas`,
        { headers: this.authHeader }
      )
      return data.canvases || []
    },

    async fetchAgentCanvas(agentName, canvasId) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/canvas/${encodeURIComponent(canvasId)}`,
        { headers: this.authHeader }
      )
      return data
    },

    async fetchDocuments(agentName) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/documents`,
        { headers: this.authHeader }
      )
      return data.documents || []
    },

    // The client's persisted conversation with an agent (oldest-first) — so the
    // chat survives a refresh / re-sign-in. With `sessionId` loads that thread;
    // without, the most-recent. Returns `{ session_id, messages }` so the caller
    // can adopt the resolved thread when it didn't specify one.
    async fetchHistory(agentName, sessionId = null) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/history`,
        { headers: this.authHeader, params: sessionId ? { session_id: sessionId } : {} }
      )
      return {
        sessionId: data.session_id || null,
        messages: data.messages || [],
        // ent#286: non-null when a turn is running on this thread right now —
        // what a client that reloaded mid-turn resubscribes to.
        inFlightExecutionId: data.in_flight_execution_id || null,
        // #2214: how long that turn may honestly be waited for — the server
        // marker's remaining TTL in seconds. Null/absent (old backend, TTL
        // unreadable) → the component falls back via resolveWaitBudgetMs.
        inFlightWaitBudgetSeconds: data.in_flight_wait_budget_seconds ?? null,
        // #2320: why the last turn ended, when it ended badly. Absent on an
        // older backend and on a thread whose last turn answered — both mean
        // "no verdict", and the caller degrades to its pre-#2320 handling.
        lastTurnOutcome: data.last_turn_outcome || null,
      }
    },

    // Files the client has sent to an agent (their inbox) — lets them review
    // what they uploaded.
    async fetchUploads(agentName) {
      const { data } = await portalHttp.get(
        `/api/enterprise/client-portal/agents/${agentName}/uploads`,
        { headers: this.authHeader }
      )
      return data.uploads || []
    },

    // Send a file TO a rostered agent (lands in its inbox). Multipart; let the
    // browser set the boundary — only add the portal auth header.
    async uploadDocument(agentName, file) {
      const form = new FormData()
      form.append('file', file)
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/agents/${agentName}/documents`,
        form,
        { headers: this.authHeader }
      )
      return data
    },

    // #138 / #2198: unified history across ALL rostered agents for the sidebar.
    //
    // ONE viewer-scoped call (`GET /client-portal/sessions`), not one per
    // rostered agent. The fan-out this replaces was N+1 in HTTP and 2N-3N in DB
    // queries — `list_sessions` re-resolves the roster per agent before reading
    // the session table — and it ran on all SIX `refreshThreads()` call sites,
    // including every thread open and every completed turn.
    //
    // Sorted most-recently-active first; each thread carries its agent so the
    // sidebar can show the colour dot and route to the right conversation.
    // `agent_name` now comes from the DB row rather than being stamped on
    // client-side from `this.agents`.
    async fetchAllSessions() {
      const agents = this.agents || []
      let lists = []
      if (agents.length) {
        try {
          lists = [await this.fetchSessionsBatch()]
          this.sessionsFailed = false
        } catch (err) {
          // TRANSITIONAL (#2198) — delete one release after the batch ships.
          //
          // Deploy skew: a cached or partially-rolled-out bundle can reach a
          // backend that does not have `/client-portal/sessions` yet. That 404s,
          // and the sidebar — the most client-visible surface in the product —
          // would simply empty. Fall back to the per-agent fan-out this replaced.
          //
          // 404 ONLY, deliberately. A 5xx or a network error already degrades
          // correctly above (keep the last good list), and fanning out there
          // would turn one failed request into N.
          if (err?.response?.status === 404) {
            try {
              lists = [await this._fetchSessionsFanout()]
              this.sessionsFailed = false
              return this._mergeThreadList(lists, await this._fetchRoomsSafe())
            } catch { /* fall through to the last-good-list path below */ }
          }
          // NEVER blank an already-populated sidebar. Return what we last had
          // and flag the failure so the UI can say so honestly.
          this.sessionsFailed = true
          console.warn('[portal] session list refresh failed:', err?.message || err)
          return this.lastSessions
        }
      }
      // ent#361: multi-agent chats are rooms, and they belong in the same list —
      // to the user these are all just conversations. Room fetch failure
      // degrades to threads-only rather than emptying the sidebar (an
      // unentitled or OSS build has no rooms at all, and that is not an error).
      const rooms = await this._fetchRoomsSafe()
      return this._mergeThreadList(lists, rooms)
    },

    // TRANSITIONAL (#2198) — the pre-batch per-agent fan-out, kept only for the
    // deploy-skew window above. Delete with its caller.
    async _fetchSessionsFanout() {
      const agents = this.agents || []
      const lists = await Promise.all(agents.map(async (a) => {
        try {
          const sessions = await this.fetchSessions(a.name)
          return sessions.map((s) => ({ ...s, agent_name: a.name }))
        } catch { return [] }   // one down agent never blanks the whole list
      }))
      return lists.flat()
    },

    async _fetchRoomsSafe() {
      try { return await this.fetchRooms() } catch { return [] }
    },

    // Merge threads + rooms into the single recency-sorted list the sidebar
    // renders, and remember it (see `lastSessions`).
    _mergeThreadList(lists, rooms) {
      const merged = lists.flat().concat((rooms || []).map(normalizeRoomRow))
      merged.sort((x, y) => {
        const tx = x.last_message_at || x.created_at || ''
        const ty = y.last_message_at || y.created_at || ''
        return ty.localeCompare(tx)
      })
      this.lastSessions = merged
      return merged
    },

    // The batch read behind `fetchAllSessions`, scoped client-side to the
    // DISPLAYED roster.
    //
    // The backend scopes by the caller's roster — that is the access boundary
    // and it is not negotiable here. This second, narrower filter is a
    // rendering rule: `this.agents` is what the sidebar actually shows, and a
    // thread whose agent is not in it would route nowhere (a dead end,
    // design-system-contract principle 16). Today the two sets are identical,
    // so this is a no-op that preserves current rendering exactly — and it
    // keeps the sidebar correct whatever #2196 decides about hiding
    // container-less agents from the displayed roster.
    async fetchSessionsBatch() {
      const { data } = await portalHttp.get(
        '/api/enterprise/client-portal/sessions',
        { headers: this.authHeader }
      )
      const shown = new Set((this.agents || []).map((a) => a.name))
      const rows = data.sessions || []
      const kept = rows.filter((s) => shown.has(s.agent_name))
      if (import.meta.env.DEV && kept.length !== rows.length) {
        // Not a normal condition: it means the displayed roster and the
        // backend's roster have diverged. Worth knowing about.
        console.warn(
          `[portal] ${rows.length - kept.length} thread(s) dropped — their agent is not on the displayed roster`
        )
      }
      return kept
    },

    // ent#364 — the ONE read all three surfaces use.
    //
    // Runs on `portalHttp` like every other workspace call: #2261 moved the
    // store onto a dedicated instance so a workspace request can never inherit
    // the platform JWT from `axios.defaults`.
    //
    // Degrades to silence. Asks are OSS core since ent#428, so a CURRENT backend
    // always serves this; against an OLDER one — which either predates the
    // surface or still gates it behind the entitlement it used to carry — the
    // 404/403 is not an error worth showing a client. `asksAvailable` stays
    // false and every surface renders nothing.
    async fetchAsks(agentName = null) {
      if (!this.isClientSignedIn) return []
      try {
        const { data } = await portalHttp.get('/api/enterprise/client-portal/asks', {
          headers: this.authHeader,
          params: agentName ? { agent_name: agentName } : {},
        })
        this.asks = Array.isArray(data) ? data : []
        this.asksAvailable = true
        return this.asks
      } catch (err) {
        this.asksAvailable = false
        if (![403, 404].includes(err.response?.status)) {
          console.warn('[workspace] asks unavailable:', err?.message || err)
        }
        this.asks = []
        return []
      }
    },

    // Answer one ask. The row is removed from local state on success rather than
    // patched: the server's answer is authoritative, and a client that keeps a
    // stale "pending" copy would offer to answer it twice.
    async answerAsk(askId, { response = null, responseText = null } = {}) {
      const { data } = await portalHttp.post(
        `/api/enterprise/client-portal/asks/${askId}/answer`,
        { response, response_text: responseText },
        { headers: this.authHeader },
      )
      this.asks = this.asks.filter((a) => a.id !== askId)
      return data
    },

    async fetchRoster() {
      this.loading = true
      this.error = null
      // #2163: re-enter loading ONLY when there is no data on screen. A retry
      // after a failed first load must show the stage's scanline rather than
      // the "No agents shared with you yet" copy it flashed before (p15:
      // loading is not empty); a refetch WITH a roster rendered keeps its
      // verdict, so the standard motion stays invisible on a background
      // refresh (p13). `loading` above is in-flight and is never the key.
      if (!this.agents.length) this.rosterLoaded = false
      // Reset with `error`, not just alongside it: a 404 followed by a
      // successful retry would otherwise keep rendering "not available on this
      // instance" over a roster that loaded fine — and the retry button added
      // for that state makes the stale case one click away.
      this.unavailable = false
      try {
        const { data } = await portalHttp.get('/api/enterprise/client-portal/my-agents', {
          headers: this.authHeader,
        })
        this.clientEmail = data.client_email || null
        // #2163: the roster no longer CARRIES the briefing — every card
        // arrives `briefing_state: "pending"` and the description + hint
        // cards (#138 / ent#380) are hydrated by `GET /briefings` below. That
        // fan-out is what made the first paint wait for the slowest agent in
        // the fleet, for every user, on every sign-in.
        //
        // `mergeRosterBriefings` is what keeps a REFETCH invisible: a card
        // that is already hydrated keeps its fields instead of dropping back
        // to `pending` and re-entering the loading phase (p13).
        this.agents = mergeRosterBriefings(this.agents, data.agents || [])
        // #2128 — a SUCCESSFUL roster is the only thing that may RAISE this
        // flag, and strict `=== true` is what makes an older backend that omits
        // the field (or a proxy that returns the string "false", or an HTML
        // error body) read as absent rather than truthy.
        //
        // Deliberately NOT pre-reset at the top of this action, unlike the
        // sibling `unavailable` above. That one resets to the OPTIMISTIC value
        // so a stale 404 cannot paint over a roster that loaded fine; copying
        // its shape here would invert its meaning — pessimistic mid-flight, so
        // every background refetch would unmount a live room and flash a
        // refusal at an entitled client before taking it back.
        this.multiAgentChatAvailable = data.multi_agent_chat_available === true
        this.rosterLoaded = true
        // #2163: fired HERE and not from `Portal.vue::bootstrap()`, because
        // both "Try again" buttons call this action directly — a
        // failed-then-retried first load would otherwise leave every
        // non-active card pending for the whole session (design contract
        // principle 21: loading behaviour lives in the store). Not awaited:
        // the roster must not wait for it, which is the entire point.
        if (briefingHydrationPlan(this.agents).batch) void this.hydrateBriefings()
      } catch (err) {
        // Two DIFFERENT failures, kept distinct — neither may swallow the other.
        //
        // 401 (ent#375): the session ended. Say so and remember the page, so
        // re-authenticating returns the user to their thread. A bare signOut()
        // drops them at an empty sign-in form with no explanation, which is
        // indistinguishable from "you were never signed in".
        if (err.response?.status === 401 && this.portalToken) {
          this.endSession({
            expired: true,
            resumePath: typeof window !== 'undefined' ? window.location.pathname : null,
          })
        }
        // 404 (ent#357): NOT "you have no agents" — the module is absent (OSS
        // build) or unentitled. The empty roster used to swallow both, so an
        // operator whose entitlement had lapsed saw "No agents shared with you
        // yet" with no way to tell that from an actually-empty share list.
        this.unavailable = err.response?.status === 404
        this.error = this.unavailable
          ? 'The workspace is not available on this instance.'
          : (err.response?.data?.detail || 'Failed to load your agents.')
        this.agents = []
        // #2128: the attempt reached a verdict — the room route may stop
        // showing its neutral placeholder and render the honest failure copy.
        // `multiAgentChatAvailable` is deliberately left ALONE: a failed
        // request is not evidence the capability went away.
        //
        // Assigned here rather than in `finally`, which runs AFTER this whole
        // block — and CONDITIONALLY, because the 401 branch above already
        // signed the session out. A bare `= true` in either place re-sets the
        // flag the sign-out just cleared, and the field stops meaning "a
        // verdict was reached for this session" and starts meaning "some
        // attempt finished at some point".
        this.rosterLoaded = this.isClientSignedIn
      } finally {
        this.loading = false
      }
    },

    /**
     * Hydrate briefings (#2163). `names === null` briefs the whole roster (the
     * background batch that fills the picker and the composer's `/` typeahead);
     * a list briefs exactly those agents.
     *
     * Never throws and never leaves a card pending: a failure (network, 429, a
     * backend with no such route) marks the requested names `unavailable`,
     * which the zone renders as an honest "couldn't load" line rather than an
     * agent that looks like it has nothing to offer.
     */
    async hydrateBriefings(names = null) {
      // An EMPTY list is "brief nobody", never "brief everybody". Only an
      // omitted/null argument is the whole-roster batch. The two are opposite
      // answers to one call and the server separates them the same way
      // (`agents=` intersects the roster to nothing; no `agents=` fans out to
      // all of it), so letting `[]` fall through to the batch would make a
      // future caller that filtered its list down to nothing silently fan out
      // across the whole fleet — on the one path in this store that costs a
      // bounded agent request per rostered agent.
      if (Array.isArray(names) && names.length === 0) return
      const requested = Array.isArray(names) && names.length ? names : null
      // One batch at a time. Two "Try again" clicks in a row would otherwise
      // fire two whole-roster hydrations, each costing one bounded agent call
      // per rostered agent — and the unfiltered form's limiter budget is
      // deliberately small (10/min). A SINGLE is never coalesced into it: the
      // active agent's hints must not inherit the batch's floor.
      if (!requested) {
        if (briefingsBatchInFlight) return
        briefingsBatchInFlight = true
      }
      const url = '/api/enterprise/client-portal/briefings'
      try {
        const { data } = await portalHttp.get(url, {
          headers: this.authHeader,
          params: requested ? { agents: requested.join(',') } : undefined,
        })
        this.agents = applyBriefings(this.agents, data && data.briefings, requested)
      } catch {
        // Deliberately swallowed: a hydration failure is a degraded briefing,
        // never a failed Workspace. `store.error` belongs to the roster.
        this.agents = applyBriefings(this.agents, null, requested, { failed: true })
      } finally {
        if (requested) requested.forEach((n) => briefingsInFlight.delete(n))
        else briefingsBatchInFlight = false
      }
    },

    /**
     * Hydrate ONE agent's briefing — the active chat's, so its hints arrive at
     * its own speed instead of the background batch's slowest member.
     *
     * A single is NEVER coalesced into an in-flight batch: that would hand the
     * active agent exactly the floor this issue removed. The duplicate bounded
     * call for that one agent is the accepted cost.
     */
    async ensureBriefing(name) {
      if (!name || briefingsInFlight.has(name)) return
      const card = this.agents.find((a) => a && a.name === name)
      if (!card) return
      const attempts = briefingAttempts.get(name) || 0
      if (!shouldRequestBriefing(card, attempts)) return
      briefingsInFlight.add(name)
      briefingAttempts.set(name, attempts + 1)
      await this.hydrateBriefings([name])
    },
  },
})
