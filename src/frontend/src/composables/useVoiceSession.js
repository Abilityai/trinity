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
 *    → voice_session_id + websocket_url
 * 2. Open WebSocket → stream audio bidirectionally
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

  const isActive = computed(() => active.value)
  const isConnecting = computed(() => status.value === 'connecting')
  const isSpeaking = computed(() => status.value === 'speaking')
  const isListening = computed(() => status.value === 'listening')
  const isToolCalling = computed(() => status.value === 'tool_calling')
  const hasBackgroundTasks = computed(() => backgroundTasks.value.length > 0)

  /**
   * Start a voice session with a caller-supplied start request (ent#534: the
   * Workspace's portal-principal route). `requestFn` resolves to the start
   * payload `{voice_session_id, websocket_url, chat_session_id?, portal_session_id?}`.
   * Resolves `true` when the socket is open and the mic is live, `false` with
   * `error` set otherwise — the caller always gets words, never a silent no-op.
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

    try {
      const data = await requestFn()

      voiceSessionId.value = data.voice_session_id
      chatSessionId.value = data.chat_session_id || null
      portalSessionId.value = data.portal_session_id || null
      const wsPath = data.websocket_url

      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = `${wsProtocol}//${window.location.host}${wsPath}?token=${authStore.token}`
      ws = new WebSocket(wsUrl)

      const opened = new Promise((resolve) => {
        ws.onopen = async () => {
          try {
            audioPlayer = createAudioPlayer()
            micCapture = await startMicCapture((base64Audio) => {
              if (ws && ws.readyState === WebSocket.OPEN && !muted.value) {
                ws.send(JSON.stringify({ type: 'audio', data: base64Audio }))
              }
            })
            status.value = 'listening'
            _startAmplitudePolling()
            resolve(true)
          } catch (micError) {
            error.value = 'Microphone access denied. Please allow microphone access and try again.'
            await stop()
            resolve(false)
          }
        }
        ws.onerror = () => {
          if (!error.value) error.value = 'Voice connection error'
          resolve(false)
        }
      })

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)

          if (msg.type === 'audio' && msg.data) {
            if (audioPlayer) audioPlayer.play(msg.data)

          } else if (msg.type === 'transcript') {
            transcriptEntries.value.push({ role: msg.role, text: msg.text })

          } else if (msg.type === 'status') {
            if (msg.state === 'ended') {
              _onEnded(msg)
            } else {
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

      ws.onclose = () => { _resolveSaved(null); _cleanup() }

      return await opened
    } catch (err) {
      console.error('Voice start error:', err)
      error.value = startFailureReason({ status: err?.response?.status, detail: err?.response?.data?.detail })
      _resolveSaved(null)
      _cleanup()
      return false
    }
  }

  /**
   * End the call. Resolves once the bridge confirmed the transcript is saved
   * (or the wait timed out / the socket closed) — safe to reload the thread after.
   */
  async function stop() {
    if (!active.value) return saved.value

    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch (_) {}
    }
    _stopMedia()
    status.value = 'ended'

    if (restStopOnEnd && voiceSessionId.value) {
      try {
        await axios.post(
          `/api/agents/${agentName}/voice/stop`,
          { voice_session_id: voiceSessionId.value },
          { headers: authStore.authHeader }
        )
      } catch (e) {
        console.warn('Voice stop API error (transcript may already be saved):', e)
      }
    }

    const result = await awaitSaved()
    _cleanup()
    return result
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
