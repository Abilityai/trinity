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
    // Last error per agent for list-load failures (string or null)
    errorByAgent: {},

    // -- Per-session turn state (Issue #759) --------------------------------
    // SessionPanel lives inside an AgentDetail view that is wrapped in
    // <KeepAlive>, so the same component instance services all `/agents/*`
    // routes. Local refs would survive navigation and bleed across agents.
    // Keying turn state by sessionId scopes it to the actual conversation.
    inFlightBySession: {},       // sessionId -> bool (a turn is running)
    errorBySession: {},          // sessionId -> string|null (last turn error)
    noticeBySession: {},         // sessionId -> string|null (neutral async status, #1376)
    fallbackNoticeBySession: {}, // sessionId -> bool (resume fallback fired)
    pollTimerBySession: {},      // sessionId -> setTimeout handle (private)
    pollWatchersBySession: {},   // sessionId -> int (ref-count, private)

    // Feature-flag cache (resolved once per page load)
    featureFlagsLoaded: false,
    sessionTabEnabled: false,
    voiceAvailable: false,
    workspaceAvailable: false,
    voipAvailable: false,
    brainOrbAvailable: false,      // trinity-enterprise#58 — Brain Orb platform flag
    brainOrbVoiceAvailable: false, // trinity-enterprise#60 — Brain Orb voice tile (Phase 3)
    brainOrbWriteAvailable: false, // trinity-enterprise#61 — Brain Orb KB-write surface (Phase 4a)
    claudeAuthConfigured: false,   // trinity-enterprise#52 — onboarding hard gate
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
    isInFlight: (state) => (sessionId) => !!state.inFlightBySession[sessionId],
    errorForSession: (state) => (sessionId) => state.errorBySession[sessionId] || null,
    noticeForSession: (state) => (sessionId) => state.noticeBySession[sessionId] || null,
    fallbackNoticeForSession: (state) => (sessionId) => !!state.fallbackNoticeBySession[sessionId],
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
        this.voiceAvailable = !!r.data?.voice_available
        this.workspaceAvailable = !!r.data?.workspace_available
        this.voipAvailable = !!r.data?.voip_available
        this.brainOrbAvailable = !!r.data?.brain_orb_available
        this.brainOrbVoiceAvailable = !!r.data?.brain_orb_voice_available
        this.brainOrbWriteAvailable = !!r.data?.brain_orb_write_available
        this.claudeAuthConfigured = !!r.data?.claude_auth_configured
      } catch {
        this.sessionTabEnabled = false
        this.voiceAvailable = false
        this.workspaceAvailable = false
        this.voipAvailable = false
        this.brainOrbAvailable = false
        this.brainOrbVoiceAvailable = false
        this.brainOrbWriteAvailable = false
        this.claudeAuthConfigured = false
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
      // Drop any in-flight turn state (#759). If a poll was running it
      // will exit on its next tick once `pollWatchersBySession[sid]` is
      // gone; the timer is also cleared eagerly.
      this.clearSessionTurnState(sessionId)
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
     * Issue #759 — in-flight state lives in the store keyed by sessionId
     * (`inFlightBySession`, `errorBySession`, `fallbackNoticeBySession`)
     * so it survives KeepAlive deactivation and doesn't bleed across
     * agents (one SessionPanel instance services all `/agents/*` routes).
     *
     * Optimistic insert without rollback: the user's message is appended
     * to `messagesBySession` synchronously so the UI doesn't sit on a
     * blank spinner for the full turn duration (long Bash/sleep prompts
     * can run for minutes). The backend persists this exact row at turn
     * start (routers/sessions.py step 2), so on success `loadSession`
     * replaces our optimistic row with the canonical row — same content,
     * no flicker. On error the optimistic row is intentionally kept:
     * server-side failures occur after the user row is persisted, and
     * for the rare network failure where the POST never reaches the
     * server, keeping the row in view is still better than the message
     * silently disappearing. (The original PR #782 dropped optimistic
     * insert entirely on the assumption that turns are fast — for 30s+
     * turns that left the user staring at a bare spinner.)
     */
    async sendMessage(agentName, sessionId, text, { model, timeoutSeconds, files } = {}) {
      const authStore = useAuthStore()

      this.inFlightBySession[sessionId] = true
      this.errorBySession[sessionId] = null
      this.noticeBySession[sessionId] = null
      this.fallbackNoticeBySession[sessionId] = false

      // Baseline for reattach success-detection. A pre-persistence failure
      // (e.g. a file-upload 502 raised at sessions.py:205, BEFORE the user
      // row is persisted at :217, or a request dropped before it landed)
      // leaves the session ending in the PREVIOUS turn's assistant reply —
      // so "last message is assistant" is NOT a safe success signal. Only a
      // NEW assistant beyond this baseline proves THIS turn landed.
      const baselineAssistantCount = (this.messagesBySession[sessionId] || []).filter(
        (m) => m.role === 'assistant',
      ).length
      // User-row baseline: lets the genuine-failure path tell a pre-persistence
      // drop (server has no row for this turn) from a post-persistence failure
      // (server has the row) so the optimistic row is only restored when the
      // reconcile GET actually wiped it (#1376).
      const baselineUserCount = (this.messagesBySession[sessionId] || []).filter(
        (m) => m.role === 'user',
      ).length
      // Set in the catch when we hand the turn off to the poller; guards the
      // `finally` so the spinner isn't blanked between catch and the first tick.
      let handedOff = false

      const existing = this.messagesBySession[sessionId] || []
      this.messagesBySession[sessionId] = [
        ...existing,
        {
          id: `optimistic-${Date.now()}`,
          role: 'user',
          content: text,
          timestamp: new Date().toISOString(),
        },
      ]
      // Snapshot the optimistic list (incl. the user row just appended). The
      // catch-path reconcile calls loadSession, which REPLACES messagesBySession
      // with server truth — on a pre-persistence failure that truth lacks the
      // user's row, so we restore this snapshot to keep the message visible.
      const optimisticSnapshot = this.messagesBySession[sessionId]

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

        // Reload canonical messages: the backend persisted both the user
        // and assistant rows; `loadSession` replaces `messagesBySession`
        // with the server's authoritative ordering.
        await this.loadSession(agentName, sessionId)

        // Update session row in the list (turn endpoint already returned it).
        if (r.data?.session) {
          const list = this.sessionsByAgent[agentName] || []
          const idx = list.findIndex((s) => s.id === sessionId)
          if (idx >= 0) list[idx] = r.data.session
        }

        if (r.data?.fallback_fired) {
          this.fallbackNoticeBySession[sessionId] = true
        }

        return r.data
      } catch (e) {
        // A 429 means another turn already holds the session lock — THIS message
        // was rejected, not severed mid-flight. `turn_in_progress` would reflect
        // the OTHER turn, so reconciling here would wrongly attach to it and
        // silently drop this send. Skip reconcile; surface the real error (#1376).
        const isConcurrencyReject = e.response?.status === 429

        // The turn endpoint is synchronous; an intermediary (e.g. Cloudflare's
        // ~100s edge timeout) can sever the socket while the backend keeps
        // running. Reconcile against authoritative server state before
        // declaring failure — a backend 502, a Cloudflare 524, and a browser
        // ECONNABORTED are all resolved the same way: by server truth (#1376).
        if (!isConcurrencyReject) {
          try {
            const data = await this.loadSession(agentName, sessionId)
            const session = data?.session
            const msgs = data?.messages || []
            const newAssistant =
              msgs.filter((m) => m.role === 'assistant').length > baselineAssistantCount

            // Check success BEFORE the in-flight handoff. The assistant row is
            // persisted INSIDE the inflight sentinel, so a reconcile GET can see
            // assistant-present AND sentinel-still-set. Handing off here would let
            // startPolling recompute its baseline from the just-loaded assistant
            // (loadSession already merged it into messagesBySession) and then
            // false-fail with "No reply came back" when the sentinel clears.
            // Mirrors the poller's own assistantLanded-before-!inProgress order.
            if (newAssistant) {
              return data // a NEW reply already landed — genuine success
            }
            if (session?.turn_in_progress) {
              handedOff = true
              this.inFlightBySession[sessionId] = true // keep spinner; no flicker
              this.startPolling(agentName, sessionId)
              return data // still running — no error
            }
            // turn not in flight AND no new reply → real failure. If the server
            // never persisted this turn's user row (pre-persistence drop), the
            // reconcile GET above overwrote our optimistic row — restore it so
            // the typed message stays visible alongside the error (#1376).
            const reconciledUsers = (this.messagesBySession[sessionId] || []).filter(
              (m) => m.role === 'user',
            ).length
            if (reconciledUsers <= baselineUserCount) {
              this.messagesBySession[sessionId] = optimisticSnapshot
            }
          } catch {
            // GET also failed (network down) — fall through to surface the
            // original error. The optimistic row is untouched (loadSession only
            // overwrites messagesBySession on a successful GET).
          }
        }

        const detail = e.response?.data?.detail
        let msg
        if (typeof detail === 'string') {
          msg = detail
        } else if (detail?.error) {
          msg = detail.error
        } else {
          msg = 'Failed to send message'
        }
        this.errorBySession[sessionId] = msg
        throw e
      } finally {
        if (!handedOff) this.inFlightBySession[sessionId] = false
      }
    },

    // ----- polling (reattach to in-flight turn on KeepAlive activation) --
    // The flow: on `onActivated` in SessionPanel, call loadSession; if the
    // returned session row has `turn_in_progress=true` AND no assistant
    // reply has landed since the last user message, call startPolling.
    // Ref-counted so two activations on the same session don't double-fire.

    async startPolling(agentName, sessionId) {
      this.pollWatchersBySession[sessionId] =
        (this.pollWatchersBySession[sessionId] || 0) + 1
      if (this.pollTimerBySession[sessionId]) return // already polling

      // Only a NEW assistant beyond this baseline proves the in-flight turn
      // landed (mirrors the catch-path baseline; a trailing assistant from a
      // prior turn is not a success signal).
      const baselineAssistantCount = (this.messagesBySession[sessionId] || []).filter(
        (m) => m.role === 'assistant',
      ).length
      const startedAt = Date.now()
      let attempts = 0
      const BACKOFF_MS = [2000, 2000, 2000, 5000, 5000, 5000, 15000, 15000, 15000]
      // Wall-clock deadline, not an attempt count: background-tab timer
      // throttling makes "N attempts × 15s" a bad clock. Covers the sentinel
      // TTL (_LOCK_TTL_FALLBACK=7230s, sessions.py:176) + slack. The high
      // attempt floor stays only as a pure leak guard. The window override is
      // a test seam only (e2e drives the deadline branch deterministically);
      // production always uses the 7260000 default.
      const deadlineOverride =
        typeof window !== 'undefined' && Number.isFinite(window.__TRINITY_SESSION_POLL_DEADLINE_MS__)
          ? window.__TRINITY_SESSION_POLL_DEADLINE_MS__
          : null
      const DEADLINE_MS = deadlineOverride !== null ? deadlineOverride : 7260000 // ~2h1m
      const MAX_ATTEMPTS = 2000 // leak guard only; the deadline is the real bound

      const tick = async () => {
        if (!this.pollWatchersBySession[sessionId]) return // stopped externally

        try {
          this.inFlightBySession[sessionId] = true
          const data = await this.loadSession(agentName, sessionId)
          const session = data?.session
          const messages = data?.messages || []
          const inProgress = !!session?.turn_in_progress
          const assistantLanded =
            messages.filter((m) => m.role === 'assistant').length > baselineAssistantCount

          if (assistantLanded) {
            // success — a new reply landed (covers slow lock release: the
            // backend INSERT races the DEL of the in-flight sentinel, so a
            // newly-arrived assistant reply is terminal even if sentinel still
            // reads true).
            this._stopPollingTimer(sessionId)
            this.inFlightBySession[sessionId] = false
            delete this.pollWatchersBySession[sessionId]
            return
          }
          if (!inProgress) {
            // Sentinel cleared with no new reply → the turn failed or produced
            // nothing. Never stop silently — a severed-then-failed turn must
            // surface an error (Finding 1).
            this._stopPollingTimer(sessionId)
            this.inFlightBySession[sessionId] = false
            this.errorBySession[sessionId] =
              "No reply came back — your message was sent but the agent didn't respond."
            delete this.pollWatchersBySession[sessionId]
            return
          }
        } catch (e) {
          // Transient — keep polling unless we've hit the deadline/leak guard.
          // eslint-disable-next-line no-console
          console.warn('[sessions] poll tick failed:', e)
        }

        attempts++
        if (Date.now() - startedAt > DEADLINE_MS || attempts >= MAX_ATTEMPTS) {
          // At the deadline the app only knows polling gave up — NOT that the
          // turn failed. Route through the neutral notice channel, not
          // errorBySession: a red error box for a maybe-still-running turn
          // would reintroduce the very false-failure #1376 kills (D6).
          this._stopPollingTimer(sessionId)
          this.inFlightBySession[sessionId] = false
          this.noticeBySession[sessionId] =
            'Still checking in the background. Refresh to see the latest status.'
          delete this.pollWatchersBySession[sessionId]
          return
        }

        const delay = BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)]
        this.pollTimerBySession[sessionId] = setTimeout(tick, delay)
      }

      this.pollTimerBySession[sessionId] = setTimeout(tick, BACKOFF_MS[0])
    },

    stopPolling(sessionId) {
      if (!this.pollWatchersBySession[sessionId]) return
      this.pollWatchersBySession[sessionId]--
      if (this.pollWatchersBySession[sessionId] <= 0) {
        this._stopPollingTimer(sessionId)
        delete this.pollWatchersBySession[sessionId]
      }
    },

    // Private: clears the setTimeout handle without touching the watcher
    // count. Used both by `stopPolling` (when refcount → 0) and by `tick`
    // itself (when it hits a terminal condition).
    _stopPollingTimer(sessionId) {
      const t = this.pollTimerBySession[sessionId]
      if (t) {
        clearTimeout(t)
        delete this.pollTimerBySession[sessionId]
      }
    },

    // Clear all per-session turn state. Called when a session is deleted
    // or when the agent changes (to defuse the cross-agent bleed risk
    // before it materialises).
    clearSessionTurnState(sessionId) {
      this._stopPollingTimer(sessionId)
      delete this.pollWatchersBySession[sessionId]
      delete this.inFlightBySession[sessionId]
      delete this.errorBySession[sessionId]
      delete this.noticeBySession[sessionId]
      delete this.fallbackNoticeBySession[sessionId]
    },
  },
})
