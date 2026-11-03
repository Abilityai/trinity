/**
 * A Workspace Direct call in the browser (trinity-enterprise#1046).
 *
 * Mic → endpointer → one `utterance` frame → transcript → back to the conversation, which
 * sends it through the same `send()` a typed message uses. That is why this file starts
 * no turn and answers no ask: `onTranscript` and `onAnswer` belong to the conversation.
 *
 * Separate from `useVoiceSession` because the two share a shape and almost no logic: the
 * Live call streams audio both ways to a model, this one exchanges whole utterances.
 */
import { computed, getCurrentInstance, onBeforeUnmount, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { createAudioPlayer, startMicCapture } from '../utils/audio'
import { createEndpointer, rmsOf, samplesToPcm16Base64, VAD } from '../utils/voiceEndpointing'
import { startFailureReason, VOICE_INSECURE_REASON, VOICE_NO_MIC_REASON } from '../components/portal/portalVoiceMode'
import { answersAsk, HOLD_UNFINISHED_MS, isEcho, looksUnfinished } from '../components/portal/portalDirectCall'

const SAVED_TIMEOUT_MS = 5000
const RECENT_SPOKEN = 12

export function useDirectVoiceSession({ onTranscript, onBargeIn, onAnswer, onTurnIdle, now = () => Date.now() } = {}) {
  const authStore = useAuthStore()

  const active = ref(false)
  const status = ref('idle')
  const muted = ref(false)
  const error = ref(null)
  const voiceSessionId = ref(null)
  const portalSessionId = ref(null)
  const runtime = ref('')
  const provider = ref('')
  const earlySpeech = ref(true)
  const liveText = ref('')
  const tool = ref('')
  const notice = ref('')
  const amplitude = ref(0)
  const timing = ref(null)
  const endReason = ref(null)
  const endMessage = ref('')
  const waitingAsk = ref(null)

  let ws = null
  let mic = null
  let player = null
  let endpointer = null
  let pollTimer = null
  let savedResolve = null
  let savedTimer = null
  let seq = 0
  let turnRunning = false
  let pausedForSpeech = false
  let wasPlaying = false
  let promptEndedAt = null
  let promptArmed = false
  let held = ''
  let holdTimer = null
  // The person's own level, so the orb moves while they talk, not only while it does.
  // Speech is a few hundredths RMS; scaled so a normal voice reads as most of the range.
  let micLevel = 0
  const utterances = new Map()
  const recentSpoken = []

  const isActive = computed(() => active.value)

  function sendFrame(frame) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(frame)); return true } catch { return false }
    }
    return false
  }

  function resting() {
    if (!active.value || status.value === 'ended') return
    status.value = waitingAsk.value ? 'awaiting_approval' : 'listening'
  }

  function flushHeld() {
    clearTimeout(holdTimer); holdTimer = null
    const text = held.trim()
    held = ''
    if (text) onTranscript?.(text)
  }

  function resumeIfPaused() {
    if (pausedForSpeech) { pausedForSpeech = false; player?.resume() }
    if (!turnRunning && !player?.isPlaying()) resting()
  }

  function onFrames(samples) {
    if (muted.value || !endpointer) { micLevel = 0; return }
    micLevel = Math.min(1, rmsOf(samples) * 6)
    endpointer.push(samples, { playing: !!player?.isPlaying() })
  }

  function handleUtterance({ samples, duringPlayback }) {
    seq += 1
    // When the person began: the audio's length back from now, less the preroll that
    // was kept from before they spoke.
    const spokenMs = (samples.length / 16000) * 1000 - VAD.prerollMs
    utterances.set(seq, { duringPlayback, startedAt: now() - Math.max(0, spokenMs) })
    status.value = 'transcribing'
    sendFrame({ type: 'utterance', seq, data: samplesToPcm16Base64(samples) })
  }

  function handleTranscript({ seq: id, text }) {
    const meta = utterances.get(id) || { duringPlayback: false, startedAt: now() }
    utterances.delete(id)
    const waiting = waitingAsk.value

    // Echo is judged while something was playing, except when a yes is awaited: the
    // prompt contains the very words the person answers with.
    if (meta.duringPlayback && !waiting && isEcho(text, recentSpoken)) { resumeIfPaused(); return }

    liveText.value = text

    const answer = answersAsk({ waiting, text, duringPlayback: meta.duringPlayback,
                                startedAt: meta.startedAt, promptEndedAt })
    if (answer) {
      pausedForSpeech = false
      waitingAsk.value = null
      resting()
      onAnswer?.(waiting, answer)
      return
    }

    // Real speech over the agent: stop it, and stop the turn it was speaking.
    if (meta.duringPlayback || turnRunning || pausedForSpeech) {
      pausedForSpeech = false
      player?.stop()
      sendFrame({ type: 'interrupt' })
      if (turnRunning) { turnRunning = false; onBargeIn?.() }
    }

    held = held ? `${held} ${text}` : text
    clearTimeout(holdTimer); holdTimer = null
    if (looksUnfinished(held)) {
      holdTimer = setTimeout(flushHeld, HOLD_UNFINISHED_MS)
      resting()
    } else {
      flushHeld()
    }
  }

  function handleFrame(msg) {
    switch (msg.type) {
      case 'audio':
        if (msg.data && player) {
          player.play(msg.data)
          if (!pausedForSpeech) status.value = 'speaking'
        }
        break
      case 'spoken':
        recentSpoken.push(msg.text || '')
        // Only the prompt's own playback opens the answer window, not the reply before it.
        if (msg.ask) { promptArmed = true; promptEndedAt = null }
        if (recentSpoken.length > RECENT_SPOKEN) recentSpoken.shift()
        break
      case 'transcript_live':
        handleTranscript(msg)
        break
      case 'not_heard':
        utterances.delete(msg.seq)
        if (msg.reason === 'too_long') notice.value = 'That was too long to catch in one go. Try shorter pieces.'
        else if (msg.reason === 'rate_limited') notice.value = 'Too much speech in a minute. Give it a moment.'
        resumeIfPaused()
        break
      case 'speak_state':
        if (msg.state === 'thinking') {
          turnRunning = true
          tool.value = ''
          status.value = 'thinking'
        } else {
          turnRunning = false
          tool.value = ''
          if (!player?.isPlaying()) resting()
          onTurnIdle?.(msg.execution_id)
        }
        break
      case 'activity':
        tool.value = msg.tool || ''
        break
      case 'audio_flush':
        player?.stop()
        break
      case 'notice':
        notice.value = msg.text || ''
        break
      case 'timing':
        timing.value = msg
        break
      case 'status':
        if (msg.state === 'ended') {
          status.value = 'ended'
          endReason.value = msg.reason || null
          endMessage.value = msg.message || ''
          stopMedia()
        } else if (msg.state === 'listening') {
          resting()
        }
        break
      case 'saved':
        finishSaved(msg)
        cleanup()
        break
      default:
        break
    }
  }

  function startPolling() {
    pollTimer = setInterval(() => {
      if (!player) return
      const playing = player.isPlaying()
      amplitude.value = playing ? player.getAmplitude() : micLevel
      if (wasPlaying && !playing && !pausedForSpeech) {
        if (promptArmed) promptEndedAt = now()
        if (!turnRunning && status.value === 'speaking') resting()
      }
      wasPlaying = playing
    }, 50)
  }

  async function startWith(requestFn) {
    if (active.value) return false
    if (typeof window !== 'undefined' && window.isSecureContext === false) {
      error.value = VOICE_INSECURE_REASON; return false
    }
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      error.value = VOICE_NO_MIC_REASON; return false
    }
    error.value = null; notice.value = ''; liveText.value = ''; tool.value = ''; timing.value = null
    endReason.value = null; endMessage.value = ''; waitingAsk.value = null; muted.value = false
    recentSpoken.length = 0; held = ''; promptEndedAt = null; promptArmed = false
    active.value = true
    status.value = 'connecting'
    try {
      const data = await requestFn()
      voiceSessionId.value = data.voice_session_id
      portalSessionId.value = data.portal_session_id || null
      runtime.value = data.runtime || ''
      provider.value = data.provider || ''
      earlySpeech.value = data.early_speech !== false
      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${scheme}//${window.location.host}${data.websocket_url}?token=${authStore.token}`)
      const opened = new Promise((resolve) => {
        ws.onopen = async () => {
          try {
            player = createAudioPlayer()
            endpointer = createEndpointer({
              onSpeechStart: ({ duringPlayback }) => {
                if (duringPlayback) { pausedForSpeech = true; player?.pause() }
              },
              onUtterance: handleUtterance,
              onDiscard: () => resumeIfPaused(),
            })
            mic = await startMicCapture((_b64, samples) => { if (samples) onFrames(samples) })
            startPolling()
            resting()
            resolve(true)
          } catch {
            error.value = 'Microphone access denied. Please allow microphone access and try again.'
            resolve(false)
            await stop()
          }
        }
        ws.onerror = () => {
          if (!error.value) error.value = 'Voice connection error'
          resolve(false)
        }
      })
      ws.onmessage = (event) => {
        try { handleFrame(JSON.parse(event.data)) } catch (e) { console.error('Direct call frame error:', e) }
      }
      ws.onclose = () => { finishSaved(null); cleanup() }
      const ok = await opened
      if (!ok) cleanup()
      return ok
    } catch (err) {
      error.value = startFailureReason({ status: err?.response?.status, detail: err?.response?.data?.detail })
      cleanup()
      return false
    }
  }

  function finishSaved(value) {
    if (savedTimer) { clearTimeout(savedTimer); savedTimer = null }
    if (savedResolve) { const r = savedResolve; savedResolve = null; r(value) }
  }

  async function stop() {
    if (!active.value) return null
    flushHeld()
    const saved = new Promise((resolve) => { savedResolve = resolve })
    savedTimer = setTimeout(() => { finishSaved(null); cleanup() }, SAVED_TIMEOUT_MS)
    if (!sendFrame({ type: 'end' })) { finishSaved(null); cleanup() }
    stopMedia()
    status.value = 'ended'
    return saved
  }

  function stopMedia() {
    if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null }
    amplitude.value = 0
    micLevel = 0
    clearTimeout(holdTimer); holdTimer = null
    if (mic) { mic.stop(); mic = null }
    if (player) { player.stop(); player = null }
    endpointer = null
  }

  function cleanup() {
    active.value = false
    status.value = 'idle'
    tool.value = ''
    turnRunning = false
    pausedForSpeech = false
    waitingAsk.value = null
    utterances.clear()
    stopMedia()
    if (ws) {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close()
      ws = null
    }
  }

  if (getCurrentInstance()) onBeforeUnmount(() => { if (active.value) void stop() })

  return {
    active, isActive, status, muted, error, voiceSessionId, portalSessionId,
    runtime, provider, earlySpeech, liveText, tool, notice, amplitude, timing,
    endReason, endMessage, waitingAsk,
    startWith, stop,
    toggleMute() { muted.value = !muted.value; if (muted.value) endpointer?.reset() },
    speak(executionId) { turnRunning = true; status.value = 'thinking'; sendFrame({ type: 'speak', execution_id: executionId }) },
    promptAsk(ask) {
      // Only an approval can be decided by a yes. A question is read, and answered in words.
      waitingAsk.value = ask.kind === 'approval' ? ask : null
      promptEndedAt = null
      promptArmed = false
      sendFrame({ type: 'speak_ask', ask_id: ask.id })
      if (waitingAsk.value && !turnRunning && !player?.isPlaying()) status.value = 'awaiting_approval'
    },
    clearAsk() { waitingAsk.value = null; resting() },
    askAnswered(askId, outcome) { sendFrame({ type: 'ask_followup', ask_id: askId, outcome }) },
    // Exposed for specs: the frame handlers are the protocol.
    _handleFrame: handleFrame,
    _handleUtterance: handleUtterance,
  }
}
