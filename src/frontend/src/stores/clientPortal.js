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
import axios from 'axios'
import { useAuthStore } from './auth'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'
// Mirrors `SESSION_ROTATION_HEADER` in client_portal/portal_auth.py (ent#375).
// Lower-case: axios normalises response header names.
const SESSION_ROTATION_HEADER = 'x-trinity-session-token'

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

    async createRoom(agentNames, name) {
      const { data } = await axios.post(
        '/api/rooms',
        { name: name || 'New chat', agents: agentNames },
        { headers: this.authHeader }
      )
      return data
    },

    async fetchRooms() {
      const { data } = await axios.get('/api/rooms', { headers: this.authHeader })
      return (data.rooms || []).map((r) => ({ ...r, is_room: true }))
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
      const { data } = await axios.get(`/api/rooms/${roomId}`, {
        headers: this.authHeader, params: { since },
      })
      return data
    },

    async postRoomMessage(roomId, content) {
      const { data } = await axios.post(
        `/api/rooms/${roomId}/messages`, { content },
        { headers: this.authHeader }
      )
      return data   // {room_id, seq, mentions, woke}
    },

    async addRoomParticipant(roomId, agentName) {
      const { data } = await axios.post(
        `/api/rooms/${roomId}/participants`, { agent_name: agentName, role: 'member' },
        { headers: this.authHeader }
      )
      return data
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

      const merged = lists.flat().concat(rooms.map((r) => ({
        ...r,
        // Normalised onto the thread shape the sidebar already renders, so one
        // list component handles both kinds.
        title: r.name,
        last_message_at: r.last_message_at || r.created_at,
        agent_names: (r.participants || [])
          .filter((p) => p.kind === 'agent')
          .map((p) => p.identity),
      })))
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
      } finally {
        this.loading = false
      }
    },
  },
})
