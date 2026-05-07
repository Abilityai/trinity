import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

/**
 * Session tab store (SESSION_TAB_2026-04 Phase 3.2).
 *
 * Wraps the six /api/agents/{name}/sessions* endpoints. State is keyed
 * by agentName so tab switching between agents doesn't bleed sessions.
 *
 * The state shape mirrors the per-agent isolation in the chat store but
 * is intentionally separate — Session and Chat are distinct surfaces and
 * we don't want one's loading state to affect the other.
 */
export const useSessionsStore = defineStore('sessions', {
  state: () => ({
    // Map of agentName -> array of session rows (newest first per backend)
    sessionsByAgent: {},
    // Map of agentName -> currently-selected session id (or null)
    activeSessionByAgent: {},
    // Map of sessionId -> array of message rows (oldest first)
    messagesBySession: {},
    // Map of sessionId -> "loading" | "ready"
    sessionStatus: {},
    // Last error per agent (string or null)
    errorByAgent: {},
    // Feature-flag cache (resolved once per page load)
    featureFlagsLoaded: false,
    sessionTabEnabled: false,
  }),

  getters: {
    sessionsFor: (state) => (agentName) => state.sessionsByAgent[agentName] || [],
    activeSessionId: (state) => (agentName) => state.activeSessionByAgent[agentName] || null,
    activeSession: (state) => (agentName) => {
      const id = state.activeSessionByAgent[agentName]
      if (!id) return null
      return (state.sessionsByAgent[agentName] || []).find((s) => s.id === id) || null
    },
    messagesFor: (state) => (sessionId) => state.messagesBySession[sessionId] || [],
  },

  actions: {
    // ----- feature flag --------------------------------------------------
    async loadFeatureFlags(force = false) {
      if (this.featureFlagsLoaded && !force) return
      const authStore = useAuthStore()
      try {
        const r = await axios.get('/api/settings/feature-flags', {
          headers: authStore.authHeader,
        })
        this.sessionTabEnabled = !!r.data?.session_tab_enabled
      } catch {
        this.sessionTabEnabled = false
      } finally {
        this.featureFlagsLoaded = true
      }
    },

    // ----- session list / lifecycle -------------------------------------
    async listSessions(agentName) {
      const authStore = useAuthStore()
      try {
        const r = await axios.get(`/api/agents/${agentName}/sessions`, {
          headers: authStore.authHeader,
        })
        this.sessionsByAgent[agentName] = r.data || []
        this.errorByAgent[agentName] = null
        return r.data
      } catch (e) {
        this.errorByAgent[agentName] = e.response?.data?.detail || 'Failed to load sessions'
        throw e
      }
    },

    async createSession(agentName) {
      const authStore = useAuthStore()
      const r = await axios.post(
        `/api/agents/${agentName}/session`,
        {},
        { headers: authStore.authHeader },
      )
      const session = r.data
      // Prepend so newest is first (matches backend ORDER BY last_message_at DESC).
      const existing = this.sessionsByAgent[agentName] || []
      this.sessionsByAgent[agentName] = [session, ...existing]
      this.activeSessionByAgent[agentName] = session.id
      this.messagesBySession[session.id] = []
      return session
    },

    async loadSession(agentName, sessionId) {
      const authStore = useAuthStore()
      this.sessionStatus[sessionId] = 'loading'
      try {
        const r = await axios.get(
          `/api/agents/${agentName}/sessions/${sessionId}`,
          { headers: authStore.authHeader },
        )
        // Replace the row in the cached list so any updated stats land on screen.
        const list = this.sessionsByAgent[agentName] || []
        const idx = list.findIndex((s) => s.id === sessionId)
        if (idx >= 0) list[idx] = r.data.session
        this.messagesBySession[sessionId] = r.data.messages || []
        this.sessionStatus[sessionId] = 'ready'
        return r.data
      } catch (e) {
        this.sessionStatus[sessionId] = 'ready'
        throw e
      }
    },

    selectSession(agentName, sessionId) {
      this.activeSessionByAgent[agentName] = sessionId
    },

    async deleteSession(agentName, sessionId) {
      const authStore = useAuthStore()
      await axios.delete(
        `/api/agents/${agentName}/sessions/${sessionId}`,
        { headers: authStore.authHeader },
      )
      this.sessionsByAgent[agentName] = (this.sessionsByAgent[agentName] || []).filter(
        (s) => s.id !== sessionId,
      )
      delete this.messagesBySession[sessionId]
      if (this.activeSessionByAgent[agentName] === sessionId) {
        this.activeSessionByAgent[agentName] = null
      }
    },

    async resetSession(agentName, sessionId) {
      const authStore = useAuthStore()
      const r = await axios.post(
        `/api/agents/${agentName}/sessions/${sessionId}/reset`,
        {},
        { headers: authStore.authHeader },
      )
      // Replace row so the cleared cached_claude_session_id reflects in UI.
      const list = this.sessionsByAgent[agentName] || []
      const idx = list.findIndex((s) => s.id === sessionId)
      if (idx >= 0) list[idx] = r.data
      return r.data
    },

    // ----- the turn ------------------------------------------------------
    /**
     * Send a message in a session.
     *
     * Optimistic UX: append a synthetic user message immediately so the
     * caller can render it before the (sometimes long) HTTP round-trip.
     * On failure, that synthetic message is rolled back so the input area
     * can re-populate without confusing the conversation log.
     */
    async sendMessage(agentName, sessionId, text, { model, timeoutSeconds, files } = {}) {
      const authStore = useAuthStore()

      const optimistic = {
        id: `optimistic-${Date.now()}`,
        role: 'user',
        content: text,
        timestamp: new Date().toISOString(),
      }
      const before = this.messagesBySession[sessionId] || []
      this.messagesBySession[sessionId] = [...before, optimistic]

      try {
        const body = { message: text }
        if (model) body.model = model
        if (timeoutSeconds) body.timeout_seconds = timeoutSeconds
        // Phase 5.2 — file uploads. ChatInput emits an array of
        // {name, mimetype, size, data_base64}; backend's WebFileUpload
        // model accepts the same shape.
        if (files && files.length > 0) body.files = files

        const r = await axios.post(
          `/api/agents/${agentName}/sessions/${sessionId}/message`,
          body,
          {
            headers: authStore.authHeader,
            // The session turn endpoint is synchronous and may legitimately
            // run for the agent's full execution timeout. TIMEOUT-001 caps
            // that at 7200s (2h); we add a 60s slack for HTTP/proxy overhead.
            // Without this ceiling matching, the browser gives up well
            // before the backend completes the task — the response still
            // lands in the DB and shows up after a page refresh, but the
            // user sees a misleading "failed" toast in the meantime.
            timeout: 7260000,
          },
        )

        // Backend returns the persisted assistant message + the refreshed
        // session row. Reload the canonical message list so the optimistic
        // row gets its real id/timestamp from the server (and any cleanup
        // from a fallback path is reflected).
        await this.loadSession(agentName, sessionId)

        // Update session row in the list (turn endpoint already returned it).
        if (r.data?.session) {
          const list = this.sessionsByAgent[agentName] || []
          const idx = list.findIndex((s) => s.id === sessionId)
          if (idx >= 0) list[idx] = r.data.session
        }

        return r.data
      } catch (e) {
        // Roll back the optimistic insert.
        this.messagesBySession[sessionId] = before
        throw e
      }
    },
  },
})
