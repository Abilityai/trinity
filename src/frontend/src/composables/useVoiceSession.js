/**
 * Voice session composable for Trinity (VOICE-001).
 *
 * Manages the full lifecycle of a voice conversation:
 * 1. POST /voice/start → get voice_session_id + websocket_url
 * 2. Open WebSocket → stream audio bidirectionally
 * 3. Handle tool_call / tool_result events → update orb state
 * 4. POST /voice/stop or WebSocket close → save transcript
 */

import { ref, computed } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { startMicCapture, createAudioPlayer } from '../utils/audio'

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
  const transcriptEntries = ref([])
  const toolName = ref(null)       // name of currently executing tool
  const amplitude = ref(0)         // 0–1 output amplitude for orb animation

  // Internal
  let ws = null
  let micCapture = null
  let audioPlayer = null
  let amplitudeTimer = null

  const isActive = computed(() => active.value)
  const isConnecting = computed(() => status.value === 'connecting')
  const isSpeaking = computed(() => status.value === 'speaking')
  const isListening = computed(() => status.value === 'listening')
  const isToolCalling = computed(() => status.value === 'tool_calling')

  /**
   * Start a voice session.
   * @param {string|null} sessionId - Existing chat session to continue
   * @param {string|null} voiceName - Gemini voice name override
   * @param {boolean} workspaceMode - Enable canvas panel tools
   */
  async function start(sessionId = null, voiceName = null, workspaceMode = false) {
    if (active.value) return
    error.value = null
    transcriptEntries.value = []
    toolName.value = null
    status.value = 'connecting'
    active.value = true

    try {
      const response = await axios.post(
        `/api/agents/${agentName}/voice/start`,
        { session_id: sessionId, voice_name: voiceName, workspace_mode: workspaceMode },
        { headers: authStore.authHeader }
      )

      voiceSessionId.value = response.data.voice_session_id
      chatSessionId.value = response.data.chat_session_id
      const wsPath = response.data.websocket_url

      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsUrl = `${wsProtocol}//${window.location.host}${wsPath}?token=${authStore.token}`
      ws = new WebSocket(wsUrl)

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
        } catch (micError) {
          error.value = 'Microphone access denied. Please allow microphone access and try again.'
          await stop()
        }
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)

          if (msg.type === 'audio' && msg.data) {
            if (audioPlayer) audioPlayer.play(msg.data)

          } else if (msg.type === 'transcript') {
            transcriptEntries.value.push({ role: msg.role, text: msg.text })

          } else if (msg.type === 'status') {
            if (msg.state === 'ended') {
              _cleanup()
            } else {
              status.value = msg.state
            }

          } else if (msg.type === 'tool_call') {
            status.value = 'tool_calling'
            toolName.value = msg.tool || null

          } else if (msg.type === 'tool_result') {
            // Tool finished — Gemini will continue speaking
            toolName.value = null
            status.value = 'listening'
          }
        } catch (e) {
          console.error('Voice WS message parse error:', e)
        }
      }

      ws.onerror = () => {
        error.value = 'Voice connection error'
        _cleanup()
      }

      ws.onclose = () => { _cleanup() }

    } catch (err) {
      console.error('Voice start error:', err)
      error.value = err.response?.data?.detail || 'Failed to start voice session'
      _cleanup()
    }
  }

  async function stop() {
    if (!active.value) return

    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch (_) {}
    }

    if (voiceSessionId.value) {
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

    _cleanup()
  }

  function toggleMute() {
    muted.value = !muted.value
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

  function _cleanup() {
    active.value = false
    status.value = 'idle'
    toolName.value = null

    _stopAmplitudePolling()

    if (micCapture) { micCapture.stop(); micCapture = null }
    if (audioPlayer) { audioPlayer.stop(); audioPlayer = null }
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
    voiceSessionId, chatSessionId,
    transcriptEntries,
    toolName,
    amplitude,

    // Actions
    start, stop, toggleMute,
  }
}
