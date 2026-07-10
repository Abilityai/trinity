/**
 * Client Portal store (enterprise `client_portal`, epic #78 / #79).
 *
 * Domain store for the client-facing portal surface. First slice: the
 * "My Agents" roster (agents shared with the signed-in email) + the operator
 * exposure config. Backed by the gated `/api/enterprise/client-portal/*`
 * endpoints — 404 in OSS/unentitled builds, but the route guard
 * (`requiresEntitlement: 'client_portal'`) keeps this store off those builds.
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

const PORTAL_TOKEN_KEY = 'trinity.portalToken'

export const useClientPortalStore = defineStore('clientPortal', {
  state: () => ({
    clientEmail: null,
    agents: [],
    loading: false,
    error: null,
    // A portal session token (verified email, no platform account). When set,
    // it authenticates the portal endpoints; else we fall back to the platform
    // login (operator preview). Persisted so a client stays signed in.
    portalToken: localStorage.getItem(PORTAL_TOKEN_KEY) || null,
  }),

  getters: {
    // Portal-session token wins; otherwise the operator's platform auth header.
    authHeader() {
      if (this.portalToken) return { Authorization: `Bearer ${this.portalToken}` }
      const authStore = useAuthStore()
      return authStore.authHeader
    },
    isClientSignedIn: (state) => !!state.portalToken,
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

    signOut() {
      this.portalToken = null
      this.clientEmail = null
      this.agents = []
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
      return { sessionId: data.session_id || null, messages: data.messages || [] }
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

    async fetchRoster() {
      this.loading = true
      this.error = null
      try {
        const { data } = await axios.get('/api/enterprise/client-portal/my-agents', {
          headers: this.authHeader,
        })
        this.clientEmail = data.client_email || null
        this.agents = data.agents || []
      } catch (err) {
        // An expired/invalid portal token → drop it so the sign-in form returns.
        if (err.response?.status === 401 && this.portalToken) this.signOut()
        this.error = err.response?.data?.detail || 'Failed to load your agents.'
        this.agents = []
      } finally {
        this.loading = false
      }
    },
  },
})
