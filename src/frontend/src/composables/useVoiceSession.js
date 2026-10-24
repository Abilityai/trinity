/**
 * Voice session composable for Trinity (VOICE-001, ent#534, #2559).
 *
 * ONE consumer: the Workspace conversation (`portal/PortalConversation.vue`).
 * The Agent-Detail-shaped `start(sessionId, voiceName, workspaceMode)` was
 * removed with its only caller in #2559 — it hardcoded the OSS route, and
 * keeping it would have left a second start implementation to hold in step
 * behind a docstring naming a front door that no longer exists.
 *
 * Manages the full lifecycle of a real-time voice call:
 * 1. A start request supplied by the caller (the Workspace's portal route)
 *    → voice_session_id + websocket_url + websocket_ticket
 * 2. Open the credential-free WebSocket, send `{type:'auth', ticket}` as its
 *    first frame, and start the mic only after the server's `auth_ok`
 *    (trinity-enterprise#595: no session token in any URL) → stream audio
 * 3. Handle tool_call / tool_result events → update orb state (and bump `panelVersion`
 *    when a canvas verb finished, so a canvas column can refetch)
 * 4. End: send `end`, await the bridge's `saved` frame → the transcript is in the DB
 *
 * Provider-neutral: the frame protocol (audio / transcript / status / tool_call /
 * tool_result / saved) is the whole contract; nothing here names a provider.
 */

import { ref, computed } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { startMicCapture, createAudioPlayer } from '../utils/audio'
import {
  PANEL_TOOL_NAMES,
  VOICE_INSECURE_REASON,
  VOICE_NO_MIC_REASON,
  applyTaskFrame,
  startFailureReason,
} from '../components/portal/portalVoiceMode'

// How long to wait for the bridge's `saved` frame after the call ended before
// giving up on it — the caller then reloads the thread anyway.
export const SAVED_FRAME_TIMEOUT_MS = 5000

// trinity-enterprise#595: the socket must open AND authenticate within this,
// or the start fails with words. The mic permission prompt is not counted.
export const VOICE_CONNECT_TIMEOUT_MS = 10000

// The words a failed or cancelled start leaves on `error` — never a silent orb.
export const VOICE_NOT_VERIFIED_REASON = 'The voice connection expired or could not be verified. Start a new call.'
export const VOICE_CONNECTION_LOST_REASON = 'The voice connection closed before the call started. Start a new call.'
export const VOICE_TIMEOUT_REASON = 'The voice connection took too long to start. Start a new call.'
export const VOICE_BAD_START_REASON = 'The voice call could not start. Start a new call.'
export const VOICE_MIC_DENIED_REASON = 'Microphone access denied. Please allow microphone access and try again.'
export const VOICE_CANCELLED_REASON = 'The call was cancelled before it connected.'

// The start response's ticket is `secrets.token_urlsafe(32)`; the session id is
// `vs_` + `token_urlsafe(16)`. Anything else is not a start this client made.
const TICKET_SHAPE = /^[A-Za-z0-9_-]{43}$/
const SESSION_ID_SHAPE = /^[A-Za-z0-9_-]{1,128}$/

/**
 * @param {string} agentName - The agent to voice-chat with
 */
export function useVoiceSession(agentName) {
  const authStore = useAuthStore()

  // State
  const active = ref(false)
  const status = ref('idle')       // idle | connecting | listening | speaking | tool_calling | ended | error
  const muted = ref(false)
  const error = ref(null)
  const voiceSessionId = ref(null)
  const chatSessionId = ref(null)
  const portalSessionId = ref(null)
  const transcriptEntries = ref([])
  const toolName = ref(null)       // name of currently executing tool
  // ent#551: tasks the agent is running in the background, `{taskId, label}`.
  // Distinct from `toolName`: a tool call is over in a moment, a task outlives
  // it and the badge must persist across turns until the task lands.
  const backgroundTasks = ref([])
  const amplitude = ref(0)         // 0–1 output amplitude for orb animation
  // ent#534: why the call ended (null = the person ended it) and the server's
  // words for it; bumped once per finished canvas verb.
  const endReason = ref(null)
  const endMessage = ref('')
  const panelVersion = ref(0)
  const saved = ref(null)          // the bridge's `saved` frame, once it arrived

  // Internal
  let ws = null
  let micCapture = null
  let audioPlayer = null
  let amplitudeTimer = null
  let savedResolve = null
  let savedPromise = null
  let savedTimer = null
  let restStopOnEnd = true
  // trinity-enterprise#595: one identity per start attempt, so a late HTTP
  // answer, a late mic grant or a stale socket event can never touch a later
  // call; `pendingStart` is how `stop()` settles a start still in flight.
  let attempt = 0
  let pendingStart = null
  let connectTimer = null

  const isActive = computed(() => active.value)
  const isConnecting = computed(() => status.value === 'connecting')
  const isSpeaking = computed(() => status.value === 'speaking')
  const isListening = computed(() => status.value === 'listening')
  const isToolCalling = computed(() => status.value === 'tool_calling')
  const hasBackgroundTasks = computed(() => backgroundTasks.value.length > 0)

  /**
   * Start a voice session with a caller-supplied start request (ent#534: the
   * Workspace's portal-principal route). `requestFn` resolves to the start
   * payload `{voice_session_id, websocket_url, websocket_ticket, chat_session_id?, portal_session_id?}`.
   * Resolves `true` only once the server acknowledged the socket's credential
   * (`auth_ok`) and the mic is live; `false` with `error` set otherwise — the
   * caller always gets words, never a silent no-op (trinity-enterprise#595).
   * @param {() => Promise<object>} requestFn
   * @param {{ restStop?: boolean }} opts - `restStop: false` skips POST /stop on end
   *   (the Workspace bridge closes the call on the socket; a REST stop that lands
   *   on another worker would race it).
   */
  async function startWith(requestFn, { restStop = true } = {}) {
    if (active.value) return false
    // Pre-flight, before any request leaves the browser (ent#534 AC: every
    // failure says why). A mic is unreachable off a secure origin, and the
    // browser's own refusal arrives as a bare NotAllowedError that reads as
    // "you denied permission".
    if (typeof window !== 'undefined' && window.isSecureContext === false) {
      error.value = VOICE_INSECURE_REASON; status.value = 'error'; return false
    }
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      error.value = VOICE_NO_MIC_REASON; status.value = 'error'; return false
    }
    error.value = null
    endReason.value = null
    endMessage.value = ''
    saved.value = null
    transcriptEntries.value = []
    toolName.value = null
    backgroundTasks.value = []
    status.value = 'connecting'
    active.value = true
    restStopOnEnd = restStop
    _armSavedPromise()

    const my = ++attempt
    let sock = null
    let done = false
    let settle
    const settled = new Promise((resolve) => { settle = resolve })

    // The one settlement path for this attempt: every failure, timeout and
    // cancellation comes through here exactly once.
    // `keepSocket`: the call was already authorized and the server is closing
    // it out — the start fails, but the socket stays open for the `saved`
    // frame (or the `_onEnded` wait), which then cleans up as an ended call does.
    const finish = (ok, reason = null, { keepSocket = false } = {}) => {
      if (done) return
      done = true
      if (pendingStart && pendingStart.my === my) pendingStart = null
      _clearConnectTimer()
      if (!ok) {
        if (reason && !error.value) error.value = reason
        if (keepSocket) {
          // nothing to release here: `_onEnded` stops the media and arms the wait
        } else if (my === attempt) {
          _resolveSaved(null)
          _cleanup()
        } else if (sock) {
          try { sock.close() } catch (_) {}
        }
      }
      settle(ok)
    }
    pendingStart = { my, fail: (reason) => finish(false, reason) }

    // The request, the socket and the mic run on; the caller awaits `settled`,
    // which a cancellation (`stop()`) can resolve while the request is still
    // in flight.
    const connect = async () => {
      let data
      try {
        data = await requestFn()
      } catch (err) {
        // Fixed words only: the error object carries the request's headers.
        console.warn('Voice start request failed', err?.response?.status ?? 'no response')
        finish(false, startFailureReason({ status: err?.response?.status, detail: err?.response?.data?.detail }))
        return
      }
      // Cancelled while the request was in flight: it never opens a socket.
      if (done || my !== attempt) return

      const sid = data?.voice_session_id
      const ticket = data?.websocket_ticket
      const wsPath = typeof sid === 'string' && SESSION_ID_SHAPE.test(sid) ? `/ws/voice/${sid}` : null
      if (!wsPath || data.websocket_url !== wsPath || typeof ticket !== 'string' || !TICKET_SHAPE.test(ticket)) {
        console.warn('Voice start response was not usable')
        finish(false, VOICE_BAD_START_REASON)
        return
      }

      voiceSessionId.value = sid
      chatSessionId.value = data.chat_session_id || null
      portalSessionId.value = data.portal_session_id || null

      // Same origin, the exact session path, no query: the URL carries nothing.
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      sock = new WebSocket(`${wsProtocol}//${window.location.host}${wsPath}`)
      ws = sock
      let authed = false
      connectTimer = setTimeout(() => finish(false, VOICE_TIMEOUT_REASON), VOICE_CONNECT_TIMEOUT_MS)

      sock.onopen = () => {
        if (ws !== sock || done) return
        try {
          sock.send(JSON.stringify({ type: 'auth', ticket }))
        } catch (_) {
          finish(false, VOICE_CONNECTION_LOST_REASON)
        }
      }

      sock.onerror = () => {
        if (ws !== sock) return
        if (!done) finish(false, 'Voice connection error')
        else if (!error.value) error.value = 'Voice connection error'
      }

      sock.onmessage = (event) => {
        if (ws !== sock) return
        try {
          const msg = JSON.parse(event.data)

          // Before admission only two frames mean anything: the acknowledgement,
          // and a refusal with its words. A provider frame that arrives early is
          // ignored — nothing plays before the mic is live.
          if (!authed) {
            if (msg.type === 'auth_ok') {
              authed = true
              _clearConnectTimer()
              void _startMedia(my, sock, () => done, finish)
            } else if (msg.type === 'status' && msg.state === 'ended') {
              finish(false, msg.message || VOICE_NOT_VERIFIED_REASON)
            }
            return
          }

          if (msg.type === 'audio' && msg.data) {
            if (audioPlayer) audioPlayer.play(msg.data)

          } else if (msg.type === 'transcript') {
            transcriptEntries.value.push({ role: msg.role, text: msg.text })

          } else if (msg.type === 'status') {
            if (msg.state === 'ended') {
              // Authorized but still setting up the mic: the start fails with
              // words, and the call ends the way any call ends — media stopped,
              // the socket kept for the bridge's `saved` frame.
              if (!done) finish(false, msg.message || VOICE_CONNECTION_LOST_REASON, { keepSocket: true })
              _onEnded(msg)
            } else if (done) {
              status.value = msg.state
            }

          } else if (msg.type === 'tool_call') {
            status.value = 'tool_calling'
            toolName.value = msg.tool || null

          } else if (msg.type === 'tool_result') {
            // Tool finished — the model will continue speaking. A canvas verb
            // finishing is the moment the board changed (ent#534).
            if (PANEL_TOOL_NAMES.includes(msg.tool)) panelVersion.value += 1
            toolName.value = null
            if (status.value === 'tool_calling') status.value = 'listening'

          } else if (msg.type === 'task') {
            // ent#551: a background task started / finished / failed. A task
            // that landed may have written to the canvas — refetch in the same
            // moment the agent brings it up.
            backgroundTasks.value = applyTaskFrame(backgroundTasks.value, msg)
            if (msg.state !== 'started') panelVersion.value += 1

          } else if (msg.type === 'saved') {
            // ent#534: the transcript rows exist NOW. Reloading on `ended`
            // raced the write.
            saved.value = msg
            if (msg.reason && !endReason.value) { endReason.value = msg.reason; endMessage.value = msg.message || '' }
            _resolveSaved(msg)
            _cleanup()
          }
        } catch (e) {
          console.error('Voice WS message parse error:', e)
        }
      }

      sock.onclose = () => {
        if (ws !== sock) return
        if (!done) { finish(false, authed ? VOICE_CONNECTION_LOST_REASON : VOICE_NOT_VERIFIED_REASON); return }
        _resolveSaved(null)
        _cleanup()
      }
    }
    connect().catch(() => finish(false, VOICE_BAD_START_REASON))

    return settled
  }

  // After `auth_ok`: player and mic, then — only if this attempt is still the
  // live one — listening. A mic grant that lands after the attempt was settled
  // (cancelled, timed out, refused) is stopped at once.
  async function _startMedia(my, sock, isDone, finish) {
    let player = null
    let mic = null
    try {
      player = createAudioPlayer()
      mic = await startMicCapture((base64Audio) => {
        if (ws === sock && sock.readyState === WebSocket.OPEN && !muted.value) {
          sock.send(JSON.stringify({ type: 'audio', data: base64Audio }))
        }
      })
    } catch (micError) {
      if (player) { try { player.stop() } catch (_) {} }
      if (my !== attempt || isDone()) return
      if (sock.readyState === WebSocket.OPEN) {
        try { sock.send(JSON.stringify({ type: 'end' })) } catch (_) {}
      }
      finish(false, VOICE_MIC_DENIED_REASON)
      return
    }
    if (my !== attempt || isDone() || ws !== sock) {
      try { mic.stop() } catch (_) {}
      try { player.stop() } catch (_) {}
      return
    }
    audioPlayer = player
    micCapture = mic
    status.value = 'listening'
    _startAmplitudePolling()
    finish(true)
  }

  function _clearConnectTimer() {
    if (connectTimer) { clearTimeout(connectTimer); connectTimer = null }
  }

  /**
   * End the call. Resolves once the bridge confirmed the transcript is saved
   * (or the wait timed out / the socket closed) — safe to reload the thread after.
   */
  async function stop() {
    if (!active.value) return saved.value

    // A start still in flight (request, socket authentication or mic prompt)
    // is settled — `false`, with words — rather than left to resolve later.
    if (pendingStart) {
      const pending = pendingStart
      const sid = voiceSessionId.value
      pending.fail(VOICE_CANCELLED_REASON)
      if (restStopOnEnd && sid) await _restStop(sid)
      return null
    }

    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch (_) {}
    }
    _stopMedia()
    status.value = 'ended'

    if (restStopOnEnd && voiceSessionId.value) await _restStop(voiceSessionId.value)

    const result = await awaitSaved()
    _cleanup()
    return result
  }

  async function _restStop(sid) {
    try {
      await axios.post(
        `/api/agents/${agentName}/voice/stop`,
        { voice_session_id: sid },
        { headers: authStore.authHeader }
      )
    } catch (e) {
      // Fixed words only: the error object carries the request's headers.
      console.warn('Voice stop API error (transcript may already be saved):', e?.response?.status ?? 'no response')
    }
  }

  /** Resolves with the `saved` frame, or null after SAVED_FRAME_TIMEOUT_MS / close. */
  function awaitSaved(timeoutMs = SAVED_FRAME_TIMEOUT_MS) {
    if (saved.value) return Promise.resolve(saved.value)
    if (!savedPromise) return Promise.resolve(null)
    if (!savedTimer) savedTimer = setTimeout(() => _resolveSaved(null), timeoutMs)
    return savedPromise
  }

  function toggleMute() {
    muted.value = !muted.value
  }

  // The server ended the call (the person pressed End, the cap, an error). The
  // mic and speaker stop NOW; the socket stays open for the `saved` frame.
  function _onEnded(msg) {
    status.value = 'ended'
    endReason.value = msg.reason || null
    endMessage.value = msg.message || ''
    _stopMedia()
    if (!savedTimer) savedTimer = setTimeout(() => { _resolveSaved(null); _cleanup() }, SAVED_FRAME_TIMEOUT_MS)
  }

  function _armSavedPromise() {
    savedPromise = new Promise((resolve) => { savedResolve = resolve })
  }

  function _resolveSaved(value) {
    if (savedTimer) { clearTimeout(savedTimer); savedTimer = null }
    if (savedResolve) { const r = savedResolve; savedResolve = null; r(value) }
  }

  function _startAmplitudePolling() {
    _stopAmplitudePolling()
    amplitudeTimer = setInterval(() => {
      if (audioPlayer) {
        amplitude.value = audioPlayer.getAmplitude()
      }
    }, 30) // ~33fps polling
  }

  function _stopAmplitudePolling() {
    if (amplitudeTimer !== null) {
      clearInterval(amplitudeTimer)
      amplitudeTimer = null
    }
    amplitude.value = 0
  }

  function _stopMedia() {
    _stopAmplitudePolling()
    if (micCapture) { micCapture.stop(); micCapture = null }
    if (audioPlayer) { audioPlayer.stop(); audioPlayer = null }
  }

  function _cleanup() {
    _clearConnectTimer()
    active.value = false
    status.value = 'idle'
    toolName.value = null
    backgroundTasks.value = []

    _stopMedia()

    if (ws) {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close()
      }
      ws = null
    }
  }

  return {
    // State
    active, isActive,
    status, isConnecting, isSpeaking, isListening, isToolCalling,
    muted,
    error,
    voiceSessionId, chatSessionId, portalSessionId,
    transcriptEntries,
    toolName,
    backgroundTasks, hasBackgroundTasks,
    amplitude,
    endReason, endMessage, panelVersion, saved,

    // Actions
    startWith, stop, toggleMute, awaitSaved,
  }
}
