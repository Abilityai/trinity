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
import { createAudioPlayer, startMicCapture } from '../utils/audio'
import { createEndpointer, rmsOf, samplesToPcm16Base64, VAD } from '../utils/voiceEndpointing'
import { startFailureReason, VOICE_INSECURE_REASON, VOICE_NO_MIC_REASON } from '../components/portal/portalVoiceMode'
import {
  VOICE_BAD_START_REASON, VOICE_CONNECT_TIMEOUT_MS, VOICE_NOT_VERIFIED_REASON, VOICE_TIMEOUT_REASON,
  VOICE_SESSION_ID_SHAPE as SESSION_ID_SHAPE, VOICE_TICKET_SHAPE as TICKET_SHAPE,
} from './useVoiceSession'
import {
  answersAsk, heldWord, HOLD_LOST_MS, holdDelay, isEcho, isOwnSentence, isStopUtterance, joinHeld,
} from '../components/portal/portalDirectCall'

const SAVED_TIMEOUT_MS = 5000
const RECENT_SPOKEN = 12
// #4456: how long sent words wait for their turn to start before they stop counting.
const SENT_PENDING_MS = 15000
export const STOPPED_NOTICE = 'Stopped.'
export const STOPPED_REPLY_IN_CHAT = 'Stopped. The full reply is in the chat.'

export function useDirectVoiceSession({ onTranscript, onBargeIn, onStop, onAnswer, onTurnIdle, isBusy, now = () => Date.now() } = {}) {

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
  // Refs, because whether there is anything to stop is shown (ent#1087).
  const turnRunning = ref(false)
  const pausedForSpeech = ref(false)
  // ent#1087: the run the bridge is speaking, so Stop reaches an approved run too, which is
  // not the composer's turn.
  let speakingExecutionId = null
  // Transcripts at or below this seq were on their way when the person stopped.
  let stopSeq = 0
  // Between sending `interrupt` and its `audio_flush`, audio already on the wire is stale.
  let dropAudio = false
  let wasPlaying = false
  let promptEndedAt = null
  let promptArmed = false
  // #4456: what the person has said and not yet sent, as `{seq, text}` so it is joined in
  // spoken order. It goes out as ONE turn once they have been quiet (`armHold`).
  let heldParts = []
  let holdTimer = null
  // The person is mid-utterance: between the endpointer's onset and its utterance/discard.
  let personSpeaking = false
  // When their last utterance ended: the quiet period is measured from here.
  let lastEndAt = null
  // "Hold on" was said: the sentence waits the longer hold.
  let holdLong = false
  // The turn the last flush started, until it is heard or over. A continuation that
  // arrives before the agent said anything supersedes it WITH its words, not without.
  let lastSent = null
  // The person's own level, so the orb moves while they talk, not only while it does.
  // Speech is a few hundredths RMS; scaled so a normal voice reads as most of the range.
  let micLevel = 0
  const utterances = new Map()
  const recentSpoken = []

  const isActive = computed(() => active.value)
  const canStop = computed(() => turnRunning.value || pausedForSpeech.value || status.value === 'speaking' || !!isBusy?.())

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

  function dropHeld() {
    clearTimeout(holdTimer); holdTimer = null
    heldParts = []
    holdLong = false
  }

  function flushHeld() {
    const text = joinHeld(heldParts)
    dropHeld()
    if (!text) return
    lastSent = { text, answered: false, started: false, at: now() }
    // Parked behind a turn that is still settling: the conversation's own queue joins
    // whatever follows, so these words are not a turn to supersede.
    if (onTranscript?.(text) === 'queued') lastSent = null
  }

  // The words last sent, while they can still be the first half of a sentence. A send
  // that never became a turn (the dispatch failed: no `thinking`, no `idle`) stops
  // counting after `SENT_PENDING_MS`, or it would ride along with some later turn.
  function sentAndPending() {
    if (lastSent && !lastSent.started && now() - lastSent.at > SENT_PENDING_MS) lastSent = null
    return lastSent
  }
  function turnStarted() {
    if (sentAndPending()) lastSent.started = true
  }

  // The one place the send is scheduled, called from every path that could leave words
  // collected. Nothing is sent while the person is speaking or a transcript is still on
  // its way; otherwise the words go once the quiet period, counted from the end of the
  // last utterance, is over.
  function armHold() {
    clearTimeout(holdTimer); holdTimer = null
    if (!heldParts.length || personSpeaking || status.value === 'ended') return
    const since = lastEndAt ?? now()
    if (utterances.size > 0) {
      // Still being transcribed. Only a lost frame ends this wait.
      holdTimer = setTimeout(() => { utterances.clear(); flushHeld() }, Math.max(0, since + HOLD_LOST_MS - now()))
      return
    }
    const due = since + holdDelay(joinHeld(heldParts), { long: holdLong })
    holdTimer = setTimeout(flushHeld, Math.max(0, due - now()))
  }

  function speechStarted({ duringPlayback } = {}) {
    personSpeaking = true
    // More is coming: whatever is collected waits for it.
    clearTimeout(holdTimer); holdTimer = null
    if (duringPlayback) { pausedForSpeech.value = true; player?.pause() }
    // "Got it" while they are talking again reads as if the mic had stopped.
    else if (status.value === 'transcribing') resting()
  }

  function speechDiscarded() {
    personSpeaking = false
    resumeIfPaused()
    armHold()
  }

  function resumeIfPaused() {
    if (pausedForSpeech.value) { pausedForSpeech.value = false; player?.resume() }
    if (!turnRunning.value && !player?.isPlaying()) resting()
  }

  function onFrames(samples) {
    if (muted.value || !endpointer) { micLevel = 0; return }
    micLevel = Math.min(1, rmsOf(samples) * 6)
    endpointer.push(samples, {
      playing: !!player?.isPlaying(),
      playbackLevel: typeof player?.getLevel === 'function' ? player.getLevel() : undefined,
    })
  }

  function handleUtterance({ samples, duringPlayback }) {
    personSpeaking = false
    lastEndAt = now()
    seq += 1
    // When the person began: the audio's length back from now, less the preroll that
    // was kept from before they spoke.
    const spokenMs = (samples.length / 16000) * 1000 - VAD.prerollMs
    utterances.set(seq, { duringPlayback, startedAt: now() - Math.max(0, spokenMs) })
    status.value = 'transcribing'
    sendFrame({ type: 'utterance', seq, data: samplesToPcm16Base64(samples) })
    armHold()
  }

  function somethingToStop(meta) {
    return meta.duringPlayback || turnRunning.value || pausedForSpeech.value || !!player?.isPlaying() || !!isBusy?.()
  }

  function handleTranscript({ seq: id, text }) {
    // The call is over: a transcript still on the wire starts nothing.
    if (status.value === 'ended') return
    const known = utterances.has(id)
    const meta = utterances.get(id) || { duringPlayback: false, startedAt: now() }
    utterances.delete(id)
    if (!known) lastEndAt = now()
    // ent#1087: said before the person stopped the agent. It must not start a turn.
    if (typeof id === 'number' && id <= stopSeq) { armHold(); return }
    if (notice.value === STOPPED_NOTICE || notice.value === STOPPED_REPLY_IN_CHAT) notice.value = ''
    const waiting = waitingAsk.value
    // A stop word, unless it is the agent's own sentence heard back word for word.
    const stopWord = isStopUtterance(text) && !(meta.duringPlayback && isOwnSentence(text, recentSpoken))

    // Echo is judged while something was playing, except when a yes is awaited: the
    // prompt contains the very words the person answers with.
    if (meta.duringPlayback && !waiting && !stopWord && isEcho(text, recentSpoken)) { resumeIfPaused(); armHold(); return }

    liveText.value = text

    const answer = answersAsk({ waiting, text, duringPlayback: meta.duringPlayback,
                                startedAt: meta.startedAt, promptEndedAt })
    if (answer) {
      pausedForSpeech.value = false
      waitingAsk.value = null
      resting()
      onAnswer?.(waiting, answer)
      armHold()
      return
    }

    // ent#1087: "stop" on its own stops the agent and is never sent as a turn. With nothing
    // to stop it is just words, and goes to the agent like any other.
    if (stopWord && somethingToStop(meta)) {
      interrupt()
      return
    }

    // #4456: with words collected and nothing to stop, "never mind" takes them back and
    // "hold on" asks for a moment. Neither is sent.
    if (heldParts.length) {
      const word = heldWord(text)
      if (word === 'drop') { dropHeld(); liveText.value = ''; resting(); return }
      if (word === 'wait') { holdLong = true; liveText.value = joinHeld(heldParts); armHold(); resting(); return }
    }

    // Real speech over the agent: stop it, and stop the turn it was speaking. A turn the
    // last flush started and that is still being dispatched counts: it is the same turn.
    const dispatching = !turnRunning.value && !!sentAndPending() && !lastSent.started && !!isBusy?.()
    if (meta.duringPlayback || turnRunning.value || pausedForSpeech.value || dispatching) {
      // #4456: the person carried on before the agent said anything. Their first words go
      // again with the rest, or the continuation would arrive without what it continues.
      const carry = sentAndPending() && !lastSent.answered && !meta.duringPlayback && !pausedForSpeech.value ? lastSent.text : ''
      lastSent = null
      pausedForSpeech.value = false
      player?.stop()
      if (sendFrame({ type: 'interrupt' })) dropAudio = true
      if (turnRunning.value || dispatching) { turnRunning.value = false; onBargeIn?.({ executionId: speakingExecutionId }) }
      if (carry) heldParts.push({ seq: 0, text: carry })
    }

    heldParts.push({ seq: typeof id === 'number' ? id : seq, text })
    liveText.value = joinHeld(heldParts)
    armHold()
    if (personSpeaking || utterances.size === 0) resting()
  }

  // ent#1087: what talking over the agent does, without a new turn. The Stop button, a
  // stop word and Escape all come here. `cancelTurn: false` silences and leaves the run
  // going (Escape, the composer's own Stop which cancels it itself).
  function interrupt({ cancelTurn = true } = {}) {
    const hadTurn = turnRunning.value
    const audible = !!player?.isPlaying() || pausedForSpeech.value
    pausedForSpeech.value = false
    player?.stop()
    if (sendFrame({ type: 'interrupt' })) dropAudio = true
    stopSeq = seq
    dropHeld()
    lastSent = null
    // A prompt cut off is answered on its card, never by a yes to words not heard.
    promptArmed = false
    promptEndedAt = null
    // The bridge cancels the task that was speaking, so no `speak_state idle` follows.
    turnRunning.value = false
    if (cancelTurn && (hadTurn || isBusy?.())) onBargeIn?.({ executionId: speakingExecutionId })
    speakingExecutionId = null
    onStop?.({ cancelTurn })
    notice.value = audible && !(cancelTurn && hadTurn) ? STOPPED_REPLY_IN_CHAT : STOPPED_NOTICE
    resting()
  }

  function handleFrame(msg) {
    switch (msg.type) {
      case 'audio':
        if (dropAudio) break
        // #4456: the agent has started to answer what was sent. From here a new phrase
        // is the person talking over it, not the rest of their sentence.
        if (msg.data && lastSent) lastSent.answered = true
        if (msg.data && player) {
          player.play(msg.data)
          if (!pausedForSpeech.value) status.value = 'speaking'
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
        armHold()
        break
      case 'speak_state':
        if (msg.state === 'thinking') {
          speakingExecutionId = msg.execution_id || speakingExecutionId
          turnRunning.value = true
          tool.value = ''
          status.value = 'thinking'
        } else {
          if (msg.execution_id === speakingExecutionId) speakingExecutionId = null
          turnRunning.value = false
          lastSent = null
          tool.value = ''
          if (!player?.isPlaying()) resting()
          onTurnIdle?.(msg.execution_id)
        }
        break
      case 'activity':
        tool.value = msg.tool || ''
        // #4456: it is already acting on what was sent. Sending those words again with
        // the rest would run the same action twice.
        if (msg.tool && lastSent) lastSent.answered = true
        break
      case 'audio_flush':
        dropAudio = false
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

  function onPlaybackEnded() {
    if (promptArmed) promptEndedAt = now()
    if (!turnRunning.value && status.value === 'speaking') resting()
  }

  function startPolling() {
    pollTimer = setInterval(() => {
      if (!player) return
      const playing = player.isPlaying()
      amplitude.value = playing ? player.getAmplitude() : micLevel
      if (wasPlaying && !playing && !pausedForSpeech.value) onPlaybackEnded()
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
    recentSpoken.length = 0; dropHeld(); promptEndedAt = null; promptArmed = false
    personSpeaking = false; lastEndAt = null; lastSent = null
    turnRunning.value = false; pausedForSpeech.value = false
    speakingExecutionId = null; stopSeq = 0; dropAudio = false
    active.value = true
    status.value = 'connecting'
    try {
      const data = await requestFn()
      voiceSessionId.value = data.voice_session_id
      portalSessionId.value = data.portal_session_id || null
      runtime.value = data.runtime || ''
      provider.value = data.provider || ''
      earlySpeech.value = data.early_speech !== false
      // trinity-enterprise#595: the socket URL carries no credential. The start
      // response's single-use ticket is the first frame, and the microphone
      // starts only after the server's `auth_ok`.
      const sid = data?.voice_session_id
      const ticket = data?.websocket_ticket
      const wsPath = typeof sid === 'string' && SESSION_ID_SHAPE.test(sid) ? `/ws/voice/${sid}` : null
      if (!wsPath || data.websocket_url !== wsPath || typeof ticket !== 'string' || !TICKET_SHAPE.test(ticket)) {
        error.value = VOICE_BAD_START_REASON
        cleanup()
        return false
      }
      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const sock = new WebSocket(`${scheme}//${window.location.host}${wsPath}`)
      ws = sock
      let authed = false
      let settle = null
      const opened = new Promise((resolve) => { settle = resolve })
      let settled = false
      const finishOpen = (ok, reason = null) => {
        if (settled) return
        settled = true
        clearTimeout(authTimer)
        if (!ok && reason && !error.value) error.value = reason
        settle(ok)
      }
      const authTimer = setTimeout(() => finishOpen(false, VOICE_TIMEOUT_REASON), VOICE_CONNECT_TIMEOUT_MS)
      // After `auth_ok`: player and mic, then listening only if this start is still the
      // live one. A mic grant that lands after the start failed (the socket closed while
      // the permission prompt was open, or the call was cleaned up) is stopped at once.
      const startMedia = async () => {
        const stale = () => settled || ws !== sock
        let ownPlayer = null
        try {
          ownPlayer = createAudioPlayer()
          player = ownPlayer
          endpointer = createEndpointer({
            onSpeechStart: speechStarted,
            onUtterance: handleUtterance,
            onDiscard: speechDiscarded,
          })
          const ownMic = await startMicCapture((_b64, samples) => { if (samples) onFrames(samples) })
          if (stale()) {
            try { ownMic.stop() } catch { /* already stopped */ }
            try { ownPlayer.stop() } catch { /* already stopped */ }
            finishOpen(false)
            return
          }
          mic = ownMic
          startPolling()
          resting()
          finishOpen(true)
        } catch {
          if (stale()) {
            try { ownPlayer?.stop() } catch { /* already stopped */ }
            finishOpen(false)
            return
          }
          finishOpen(false, 'Microphone access denied. Please allow microphone access and try again.')
          await stop()
        }
      }
      sock.onopen = () => {
        if (ws !== sock) return
        try {
          sock.send(JSON.stringify({ type: 'auth', ticket }))
        } catch {
          finishOpen(false, VOICE_NOT_VERIFIED_REASON)
        }
      }
      sock.onerror = () => {
        if (!error.value) error.value = 'Voice connection error'
        finishOpen(false)
      }
      sock.onmessage = (event) => {
        if (ws !== sock) return
        let frame
        try { frame = JSON.parse(event.data) } catch (e) { console.error('Direct call frame error:', e); return }
        // Before admission only the acknowledgement and a refusal with its words count.
        if (!authed) {
          // The timer covers admission only: time on the mic-permission prompt is the
          // person's, not the connection's.
          if (frame?.type === 'auth_ok') { authed = true; clearTimeout(authTimer); void startMedia() }
          else if (frame?.type === 'status' && frame.state === 'ended') finishOpen(false, frame.message || VOICE_NOT_VERIFIED_REASON)
          return
        }
        try { handleFrame(frame) } catch (e) { console.error('Direct call frame error:', e) }
      }
      sock.onclose = () => {
        if (ws !== sock) return
        finishOpen(false, authed ? null : VOICE_NOT_VERIFIED_REASON)
        finishSaved(null); cleanup()
      }
      const ok = await opened
      // Only this start's own socket: a later call may already own `ws`.
      if (!ok && ws === sock) cleanup()
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

  // `flush: false` when the call ends because the person left for another agent or
  // thread: words still collected were meant for the one they left, and go nowhere.
  async function stop({ flush = true } = {}) {
    if (!active.value) return null
    if (flush) flushHeld()
    else dropHeld()
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
    dropHeld()
    personSpeaking = false
    lastSent = null
    if (mic) { mic.stop(); mic = null }
    if (player) { player.stop(); player = null }
    endpointer = null
  }

  function cleanup() {
    active.value = false
    status.value = 'idle'
    tool.value = ''
    turnRunning.value = false
    pausedForSpeech.value = false
    waitingAsk.value = null
    utterances.clear()
    stopMedia()
    if (ws) {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close()
      ws = null
    }
  }

  if (getCurrentInstance()) onBeforeUnmount(() => { if (active.value) void stop({ flush: false }) })

  return {
    active, isActive, canStop, status, muted, error, voiceSessionId, portalSessionId,
    runtime, provider, earlySpeech, liveText, tool, notice, amplitude, timing,
    endReason, endMessage, waitingAsk,
    startWith, stop, interrupt,
    // Whether the agent's voice is coming out (or held mid-reply) right now: Escape asks.
    isSpeaking() { return !!player?.isPlaying() || pausedForSpeech.value },
    toggleMute() {
      muted.value = !muted.value
      if (!muted.value) return
      // `reset()` reports neither an utterance nor a discard: say the speech is over here.
      endpointer?.reset()
      personSpeaking = false
      armHold()
    },
    // The words last sent did not become a turn (the dispatch failed).
    forgetSent() { lastSent = null },
    speak(executionId) {
      if (!active.value) return
      if (notice.value === STOPPED_NOTICE || notice.value === STOPPED_REPLY_IN_CHAT) notice.value = ''
      speakingExecutionId = executionId
      turnStarted()
      turnRunning.value = true
      status.value = 'thinking'
      sendFrame({ type: 'speak', execution_id: executionId })
    },
    promptAsk(ask) {
      // Only an approval can be decided by a yes. A question is read, and answered in words.
      waitingAsk.value = ask.kind === 'approval' ? ask : null
      promptEndedAt = null
      promptArmed = false
      sendFrame({ type: 'speak_ask', ask_id: ask.id })
      if (waitingAsk.value && !turnRunning.value && !player?.isPlaying()) status.value = 'awaiting_approval'
    },
    clearAsk() { waitingAsk.value = null; resting() },
    askAnswered(askId, outcome) { sendFrame({ type: 'ask_followup', ask_id: askId, outcome }) },
    // Exposed for specs: the frame handlers are the protocol.
    _handleFrame: handleFrame,
    _handleUtterance: handleUtterance,
    _speechStart: speechStarted,
    _discard: speechDiscarded,
    _promptPlayed: onPlaybackEnded,
  }
}
