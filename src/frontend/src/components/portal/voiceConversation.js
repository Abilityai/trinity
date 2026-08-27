// Workspace voice conversation (ent#440) — the hands-free loop that turns the
// Workspace's two existing half-measures (push-to-talk dictation #2212, spoken
// replies #2157) into one continuous conversation with the SAME agent, in the
// SAME thread.
//
// The load-bearing decision is what a voice turn IS. It is an ordinary portal
// turn: mic → /stt → the same `POST /chat/stream` the composer sends → the same
// portal session → the same resumed Claude session → /tts. Nothing about the
// conversation forks. That is why "the voice session shares context with the
// text conversation" and "turns from voice appear in history the same way text
// turns do" are true by construction rather than by synchronisation — there is
// no second transcript to reconcile, no second model answering, and no second
// permission surface to keep in step (ent#440 AC 2, 3, 7).
//
// The alternative — bridging the platform's Gemini Live session (`routers/voice.py`)
// into the Workspace — was rejected for exactly that reason: it answers with a
// different model holding a summarised copy of the thread, writes its transcript
// back afterwards, and authenticates over a JWT-only WebSocket that a portal
// client does not hold. It would have been a parallel conversation wearing the
// same page.
//
// Everything decidable lives here as pure functions: `vitest` runs
// `environment: 'node'` with no component-mount harness, so a rule that lives
// inside the component is a rule no test can reach.

// ---- Availability ----------------------------------------------------------

// Capture needs a real microphone; transcription needs the platform's own /stt
// (an ElevenLabs key resolving server-side — `stt_available` on the roster card,
// #2212). The browser Web Speech API is deliberately NOT a fallback here: it is
// a browser-hosted service that reports no event at all in Chromium (measured)
// and ends at the first pause, which is survivable for one dictated sentence and
// unusable as the ear of a continuous conversation.
export const VOICE_NO_MIC_REASON =
  'This browser can’t reach a microphone here — voice needs a secure (https) page. You can still type.'
export const VOICE_NO_STT_REASON =
  'Voice conversation isn’t available — this instance has no speech provider configured. You can still type.'
// Not a failure: the loop runs, the agent answers in text. Named rather than
// silent, because a conversation that stops talking back with no explanation
// reads as broken (ent#440 AC 6).
export const VOICE_TEXT_REPLIES_NOTICE =
  'Replies will appear as text — no voice is configured for this agent.'

// `{ available, narrates, reason }`. `available` gates the control; `narrates`
// decides whether a reply is spoken or only shown; `reason` is the words a user
// gets, never a silent no-op.
export function voiceConversationMode({
  canRecord = false,
  secureContext = true,
  serverStt = false,
  voiceAvailable = false,
} = {}) {
  if (!canRecord || !secureContext) {
    return { available: false, narrates: false, reason: VOICE_NO_MIC_REASON }
  }
  if (!serverStt) {
    return { available: false, narrates: false, reason: VOICE_NO_STT_REASON }
  }
  return {
    available: true,
    narrates: !!voiceAvailable,
    reason: voiceAvailable ? '' : VOICE_TEXT_REPLIES_NOTICE,
  }
}

// ---- The loop --------------------------------------------------------------

export const VOICE_OFF = 'off'
export const VOICE_LISTENING = 'listening'
export const VOICE_TRANSCRIBING = 'transcribing'
export const VOICE_THINKING = 'thinking'
export const VOICE_SPEAKING = 'speaking'

// Actions the component performs. Kept as data so the machine stays testable
// and the component stays a dispatcher over it.
export const ACT_CAPTURE = 'capture'          // open the mic for one utterance
export const ACT_STOP_CAPTURE = 'stop-capture'
export const ACT_SEND = 'send'                // dispatch the transcript as a turn
export const ACT_NARRATE = 'narrate'
export const ACT_STOP_NARRATION = 'stop-narration'
export const ACT_RELEASE = 'release'          // drop the mic stream entirely

const LIVE = new Set([VOICE_LISTENING, VOICE_TRANSCRIBING, VOICE_THINKING, VOICE_SPEAKING])

export function isVoiceLive(state) { return LIVE.has(state) }

// One transition table, so no caller invents an edge. `event`:
//
//   start            user turned conversation on
//   utterance        the mic captured speech and stopped
//   silence          the mic heard nothing worth sending
//   transcript       /stt returned text
//   transcript-empty /stt returned nothing usable
//   reply            the agent's turn finished
//   narration-ended  the spoken reply finished (or failed — same next step)
//   barge-in         the user started speaking over the agent
//   stop             user turned conversation off
//   error            an unrecoverable step failed
//
// `canNarrate` decides only whether a finished turn is spoken; every other edge
// is identical in both modes, which is what keeps the text-reply degrade a
// setting rather than a second code path.
export function nextVoiceState(state, event, { canNarrate = true } = {}) {
  if (event === 'stop' || event === 'error') {
    return {
      state: VOICE_OFF,
      actions: [
        ...(state === VOICE_LISTENING ? [ACT_STOP_CAPTURE] : []),
        ...(state === VOICE_SPEAKING ? [ACT_STOP_NARRATION] : []),
        ACT_RELEASE,
      ],
    }
  }
  if (event === 'start') {
    // Idempotent: a second start while live must not open a second recorder.
    if (isVoiceLive(state)) return { state, actions: [] }
    return { state: VOICE_LISTENING, actions: [ACT_CAPTURE] }
  }
  // Every remaining event is meaningless when the loop is off — a late callback
  // from a torn-down recorder must not restart it.
  if (state === VOICE_OFF) return { state: VOICE_OFF, actions: [] }

  switch (event) {
    case 'utterance':
      return state === VOICE_LISTENING
        ? { state: VOICE_TRANSCRIBING, actions: [] }
        : { state, actions: [] }
    case 'silence':
      // Nothing worth sending; go straight back to listening without spending a
      // transcription on a clip of room tone. Accepted from TRANSCRIBING as well
      // as LISTENING because that is where it is actually raised — the recorder
      // has already stopped by the time its clip is found to be empty, and a
      // LISTENING-only edge left the loop wedged in TRANSCRIBING for good.
      return state === VOICE_LISTENING || state === VOICE_TRANSCRIBING
        ? { state: VOICE_LISTENING, actions: [ACT_CAPTURE] }
        : { state, actions: [] }
    case 'transcript':
      return state === VOICE_TRANSCRIBING
        ? { state: VOICE_THINKING, actions: [ACT_SEND] }
        : { state, actions: [] }
    case 'transcript-empty':
      return state === VOICE_TRANSCRIBING
        ? { state: VOICE_LISTENING, actions: [ACT_CAPTURE] }
        : { state, actions: [] }
    case 'reply':
      if (state !== VOICE_THINKING) return { state, actions: [] }
      return canNarrate
        ? { state: VOICE_SPEAKING, actions: [ACT_NARRATE] }
        : { state: VOICE_LISTENING, actions: [ACT_CAPTURE] }
    case 'narration-ended':
      return state === VOICE_SPEAKING
        ? { state: VOICE_LISTENING, actions: [ACT_CAPTURE] }
        : { state, actions: [] }
    case 'barge-in':
      // AC 5. Cutting the agent off is the whole point of the control, so it
      // takes effect on the spot: narration stops and the mic is already open.
      return state === VOICE_SPEAKING
        ? { state: VOICE_LISTENING, actions: [ACT_STOP_NARRATION, ACT_CAPTURE] }
        : { state, actions: [] }
    default:
      return { state, actions: [] }
  }
}

// ---- Utterance detection ---------------------------------------------------

// Speech vs room tone, on RMS of the analyser's time-domain samples (0..1).
// Deliberately generous: a missed word costs a re-ask, while a threshold tuned
// so tight that breathing ends the turn makes the loop unusable.
export const SPEECH_RMS = 0.025
// Louder bar to interrupt: the mic hears the agent's own narration through the
// speakers. `echoCancellation` removes most of it; this removes the rest.
export const BARGE_IN_RMS = 0.06
// How long a pause ends an utterance. Long enough to think mid-sentence, short
// enough that the agent doesn't feel slow to answer.
export const SILENCE_HOLD_MS = 1200
// Sustained loudness before an interrupt counts — one cough must not cut the
// agent off.
export const BARGE_IN_HOLD_MS = 350
// A single utterance is bounded so a stuck-open mic can't upload forever.
export const MAX_UTTERANCE_MS = 30000
// Nothing said at all: release the mic rather than hold it open indefinitely.
export const NO_SPEECH_TIMEOUT_MS = 15000
// Review finding: `transcribing` had no ceiling at all. `stopUtterance()` is a
// no-op when the recorder is already `inactive`, so `onstop` never fires and
// `finishUtterance` never runs — reachable when the mic is unplugged or
// permission is revoked mid-conversation (the recorder auto-stops while the
// machine is still `listening`, then the next tick dispatches `utterance` into
// a recorder that cannot stop), and again when `/stt` hangs, since
// `transcribeStt` carries no timeout either. Both leave `convStream`'s tracks
// live — the browser mic indicator on — with no path back but Stop, which is
// the one failure this feature says it must not ship.
//
// Generous, because the honest cost of firing early is a lost utterance: it
// bounds the wedge without competing with a slow-but-working transcription.
export const TRANSCRIBE_TIMEOUT_MS = 45000
export const VOICE_IDLE_STOP_REASON =
  'Stopped listening — I didn’t hear anything. Tap the voice button to start again.'

export function isSpeech(rms, threshold = SPEECH_RMS) {
  return Number.isFinite(rms) && rms >= threshold
}

// `'continue' | 'end' | 'idle'` for the currently open microphone.
//   end  — speech was heard and has now stopped (or ran to the cap): send it
//   idle — the mic has been open this long having heard nothing: give it back
export function utteranceVerdict({
  sawSpeech = false,
  msSinceSpeech = 0,
  elapsedMs = 0,
  silenceHoldMs = SILENCE_HOLD_MS,
  maxUtteranceMs = MAX_UTTERANCE_MS,
  noSpeechTimeoutMs = NO_SPEECH_TIMEOUT_MS,
} = {}) {
  if (sawSpeech) {
    if (elapsedMs >= maxUtteranceMs) return 'end'
    return msSinceSpeech >= silenceHoldMs ? 'end' : 'continue'
  }
  return elapsedMs >= noSpeechTimeoutMs ? 'idle' : 'continue'
}

// A reply is spoken, not read. Markdown that carries meaning on the page is
// noise in the ear, and a code block read aloud is unlistenable — so fences are
// dropped with a spoken marker rather than pronounced character by character.
export const SPOKEN_CODE_PLACEHOLDER = 'I’ve put the code in the chat.'
export const MAX_SPOKEN_CHARS = 1200
export const SPOKEN_TRUNCATION_NOTICE = ' The rest is in the chat.'

export function spokenReply(markdown, { maxChars = MAX_SPOKEN_CHARS } = {}) {
  if (!markdown || typeof markdown !== 'string') return ''
  let text = markdown
    // Fenced code → one spoken sentence. Non-greedy, and an unterminated fence
    // takes the rest of the reply with it rather than leaking backticks.
    .replace(/```[\s\S]*?(?:```|$)/g, ` ${SPOKEN_CODE_PLACEHOLDER} `)
    .replace(/`([^`]*)`/g, '$1')
    // Links read as their text, never their URL.
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/(\*\*|__|\*|_|~~)/g, '')
    .replace(/^\s*\|.*\|\s*$/gm, '')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{2,}/g, '\n')
    .trim()
  if (text.length > maxChars) {
    // Cut on a sentence end where there is one nearby, so the spoken half does
    // not stop mid-word.
    const head = text.slice(0, maxChars)
    const stop = Math.max(head.lastIndexOf('. '), head.lastIndexOf('! '), head.lastIndexOf('? '))
    text = (stop > maxChars * 0.6 ? head.slice(0, stop + 1) : head.trimEnd()) + SPOKEN_TRUNCATION_NOTICE
  }
  return text
}

// What the control says it is doing. One place, so the button title, the
// aria-label and the status line cannot disagree.
export const VOICE_STATE_LABELS = {
  [VOICE_OFF]: 'Start a voice conversation',
  [VOICE_LISTENING]: 'Listening…',
  [VOICE_TRANSCRIBING]: 'Got it…',
  [VOICE_THINKING]: 'Thinking…',
  [VOICE_SPEAKING]: 'Speaking — say something to interrupt',
}

export function voiceStateLabel(state) {
  return VOICE_STATE_LABELS[state] || VOICE_STATE_LABELS[VOICE_OFF]
}
