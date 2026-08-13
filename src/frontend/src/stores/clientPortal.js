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
import { normalizeRoomRow } from '@/components/portal/portalUtils'
import axios from 'axios'
import { useAuthStore } from './auth'
// #2162: the page size for a windowed report read. A dependency-free leaf
// shared with the operator reports store — never re-typed here, since the
// backend already owns REPORT_ROWS_PAGE_DEFAULT and a third hand-written copy
// is the shape that drifts while each side's tests pin its own version.
import { REPORT_ROWS_PAGE as ROWS_PAGE } from '@/utils/reportPaging'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'
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
  axios.interceptors.response.use((response) => {
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
  })
}

// Live from import, so a session restored from localStorage rotates too — not
// only one created by a fresh sign-in in this tab.
installRotationInterceptor()

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
    isPlatformSession() {
      return !this.portalToken && !!useAuthStore().isAuthenticated
    },
    isClientSignedIn() {
      return !!this.portalToken || this.isPlatformSession
    },
  },

  actions: {
    // Step 1 — request a 6-digit code. Always resolves (generic response); the
    // backend reveals nothing about whether the email has access.
    async requestCode(email) {
      await axios.post('/api/enterprise/client-portal/auth/request', { email })
    },

    // Step 2 — verify the code → portal session token (persisted).
    async verifyCode(email, code) {
      const { data } = await axios.post('/api/enterprise/client-portal/auth/verify', { email, code })
      this.portalToken = data.token
      this.clientEmail = data.email
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
      localStorage.removeItem(PORTAL_TOKEN_KEY)
    },

    // Chat one turn with a rostered agent over the portal session (the gated,
    // roster-scoped endpoint — the OSS chat endpoint fences the portal token).
    // Returns `{response, cost, session_id}` — the echoed session_id lets the
    // caller adopt the thread a first (session-less) turn landed in.
    async sendPortalChat(agentName, message, sessionId = null) {
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/chat`,
        { message, session_id: sessionId },
        { headers: this.authHeader }
      )
      return data
    },

    // ent#286: begin a turn and get its id back immediately (202), so the UI can
    // show live tool activity while the agent works. The synchronous
    // `sendPortalChat` above is untouched — it stays the documented API surface
    // for headless clients (ent#83), and is still the fallback when streaming
    // is unavailable.
    async startPortalChat(agentName, message, sessionId = null) {
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/chat/stream`,
        { message, session_id: sessionId },
        { headers: this.authHeader }
      )
      return data   // {execution_id, session_id}
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
        const { data } = await axios.post(
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
        const { data } = await axios.get('/api/rooms', { headers: this.authHeader })
        return (data.rooms || []).map((r) => ({ ...r, is_room: true }))
      } catch (err) {
        throw this._noteRoomsRefusal(err)
      }
    },

    // ent#360 — the agent page. One call for the whole page: it is one screen,
    // and fetching header/stats/asks/work separately renders it in pieces.
    async fetchAgentPage(agentName, window = '7d') {
      const { data } = await axios.get(
        `/api/enterprise/client-portal/agents/${agentName}/page`,
        { headers: this.authHeader, params: { window } },
      )
      return data
    },

    async fetchAgentReports(agentName) {
      const { data } = await axios.get(
        `/api/enterprise/client-portal/agents/${agentName}/reports`,
        { headers: this.authHeader },
      )
      return data.reports || []
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
      const { data } = await axios.get(
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
      const { data } = await axios.get('/api/enterprise/client-portal/chat-state', {
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
      if (starred) await axios.put(url, null, cfg)
      else await axios.delete(url, cfg)
    },

    // Fire-and-forget by design: a failed read marker leaves a stale badge,
    // which is not worth interrupting navigation over, and the next state fetch
    // corrects it.
    async markChatRead(kind, chatId) {
      try {
        await axios.post(
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
        const { data } = await axios.get(`/api/rooms/${roomId}`, {
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
        const { data } = await axios.post(
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
        const { data } = await axios.post(
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
      const { data } = await axios.get(
        `/api/enterprise/client-portal/agents/${agentName}/sessions`,
        { headers: this.authHeader }
      )
      return data.sessions || []
    },

    // Open a fresh conversation thread ("New chat"). Returns the empty session.
    async createSession(agentName) {
      const { data } = await axios.post(
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
        const { data } = await axios.post(
          `/api/enterprise/client-portal/agents/${agentName}/tts`,
          { text },
          { headers: this.authHeader, responseType: 'blob' }
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
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/stt`,
        form,
        { headers: this.authHeader }
      )
      return data.text || ''
    },

    // Cross-chat search over the client's conversations (all rostered agents), by
    // thread title or message content. Returns [{agent_name, session_id, title,
    // snippet, last_message_at}] newest-active first.
    async searchChats(query) {
      const { data } = await axios.get('/api/enterprise/client-portal/search', {
        headers: this.authHeader,
        params: { q: query },
      })
      return data.results || []
    },

    // Files a rostered agent has shared (FILES-001), each with a download URL
    // (`?sig=` token is the credential — the download route is public).
    async fetchDocuments(agentName) {
      const { data } = await axios.get(
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
      const { data } = await axios.get(
        `/api/enterprise/client-portal/agents/${agentName}/history`,
        { headers: this.authHeader, params: sessionId ? { session_id: sessionId } : {} }
      )
      return {
        sessionId: data.session_id || null,
        messages: data.messages || [],
        // ent#286: non-null when a turn is running on this thread right now —
        // what a client that reloaded mid-turn resubscribes to.
        inFlightExecutionId: data.in_flight_execution_id || null,
      }
    },

    // Files the client has sent to an agent (their inbox) — lets them review
    // what they uploaded.
    async fetchUploads(agentName) {
      const { data } = await axios.get(
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
      const { data } = await axios.post(
        `/api/enterprise/client-portal/agents/${agentName}/documents`,
        form,
        { headers: this.authHeader }
      )
      return data
    },

    // #138: unified history across ALL rostered agents for the sidebar. The
    // per-agent /sessions endpoint is the source; a small roster makes merging
    // N calls cheap (a single all-agents endpoint is a later optimization). Each
    // thread is tagged with its agent so the sidebar can show the color dot and
    // route to the right conversation. Best-effort per agent (one down agent
    // never blanks the whole list); sorted most-recently-active first.
    async fetchAllSessions() {
      const agents = this.agents || []
      const lists = await Promise.all(agents.map(async (a) => {
        try {
          const sessions = await this.fetchSessions(a.name)
          return sessions.map((s) => ({ ...s, agent_name: a.name }))
        } catch { return [] }
      }))
      // ent#361: multi-agent chats are rooms, and they belong in the same list —
      // to the user these are all just conversations. Room fetch failure
      // degrades to threads-only rather than emptying the sidebar (an
      // unentitled or OSS build has no rooms at all, and that is not an error).
      let rooms = []
      try { rooms = await this.fetchRooms() } catch { rooms = [] }

      const merged = lists.flat().concat(rooms.map(normalizeRoomRow))
      merged.sort((x, y) => {
        const tx = x.last_message_at || x.created_at || ''
        const ty = y.last_message_at || y.created_at || ''
        return ty.localeCompare(tx)
      })
      return merged
    },

    async fetchRoster() {
      this.loading = true
      this.error = null
      // Reset with `error`, not just alongside it: a 404 followed by a
      // successful retry would otherwise keep rendering "not available on this
      // instance" over a roster that loaded fine — and the retry button added
      // for that state makes the stale case one click away.
      this.unavailable = false
      try {
        const { data } = await axios.get('/api/enterprise/client-portal/my-agents', {
          headers: this.authHeader,
        })
        this.clientEmail = data.client_email || null
        // Roster carries per-agent briefing (#138): description + capability
        // hints as playbooks[]{title,description,starter_prompt} — exposed
        // playbooks, else the template's "What You Can Ask" use-cases
        // (ent#380) — shipped at sign-in so the new-chat screen renders with
        // zero extra fetches.
        this.agents = data.agents || []
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
  },
})
