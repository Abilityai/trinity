/**
 * #2212 — Workspace dictation: pick the path we control, and never fail silently.
 *
 * Reported symptom: clicking voice mode "kind of turns on the mic and dies
 * instantly", with nothing shown. Two causes, both pinned here.
 *
 * 1. The branch. `micMode` preferred the browser Web Speech API whenever the
 *    object merely EXISTED. Measured in Playwright's Chromium (secure context,
 *    microphone permission granted, fake audio device): `start()` does not
 *    throw and the engine then emits NOTHING for 15s — no `start`, no
 *    `audiostart`, no `result`, no `error`, no `end`. `continuous` also defaults
 *    to false, so even where the service answers, recognition ends at the first
 *    pause. The MediaRecorder + `POST /stt` path, by contrast, is ours end to
 *    end: real status codes, real messages, a real transcript.
 *
 * 2. The silence. Every failure path discarded its own signal — `onerror` threw
 *    the error code away, `getUserMedia` was wrapped in `catch { return }`, a
 *    short blob was dropped with no feedback, and the transcription call ended
 *    in `catch { }`.
 *
 * There is no component-mount harness in this project (no @vue/test-utils), so
 * the decisions live in pure helpers that ARE unit-tested, plus source-structure
 * guards for the wiring — the shape of `portalVoiceModePersistence.spec.js`.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import {
  MIN_RECORDING_BYTES,
  SPEECH_NO_RESULT_MESSAGE,
  SPEECH_UNRESPONSIVE_MESSAGE,
  recorderErrorMessage,
  resolveMicMode,
  resolveRecordingMimeType,
  speechAttemptOutcome,
  speechErrorMessage,
  transcriptionErrorMessage,
} from '../../src/components/portal/portalUtils'

const SRC = readFileSync(
  fileURLToPath(new URL('../../src/components/portal/PortalConversation.vue', import.meta.url)),
  'utf8'
)
// Strip comments so the guards scan code, not prose about the code.
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const VOICE = CODE.slice(CODE.indexOf('const SpeechRec'))

describe('#2212 which mic path', () => {
  it('prefers the server path when the platform can transcribe — the regression', () => {
    // Chrome has BOTH. It used to take `speech`, the branch we cannot fix.
    expect(resolveMicMode({ speechApi: true, canRecord: true, serverStt: true })).toBe('record')
  })

  it('falls back to the browser API when the instance has no provider', () => {
    // Free and often fine in official Chrome — a fallback, not the default.
    expect(resolveMicMode({ speechApi: true, canRecord: true, serverStt: false })).toBe('speech')
    expect(resolveMicMode({ speechApi: true, canRecord: false, serverStt: false })).toBe('speech')
  })

  it('uses the server path on browsers with no Web Speech API (Firefox)', () => {
    expect(resolveMicMode({ speechApi: false, canRecord: true, serverStt: true })).toBe('record')
  })

  it('offers no mic at all rather than a dead control', () => {
    // No provider and no browser API: recording could only 404 at /stt.
    expect(resolveMicMode({ speechApi: false, canRecord: true, serverStt: false })).toBeNull()
    expect(resolveMicMode({ speechApi: false, canRecord: false, serverStt: true })).toBeNull()
    expect(resolveMicMode({})).toBeNull()
    expect(resolveMicMode()).toBeNull()
  })
})

describe('#2212 what a finished dictation attempt says', () => {
  it('says nothing when the user got their words', () => {
    expect(speechAttemptOutcome({ gotText: true })).toBeNull()
  })

  it('speaks up when it ended with no words and no reason — the reported bug', () => {
    expect(speechAttemptOutcome({ gotText: false })).toBe(SPEECH_NO_RESULT_MESSAGE)
  })

  it('stays quiet when the user pressed stop', () => {
    // `aborted` is what a deliberate stop raises; an error notice for obeying
    // teaches people the feature is broken.
    expect(speechAttemptOutcome({ errorCode: 'aborted' })).toBeNull()
    expect(speechAttemptOutcome({ gotText: true, errorCode: 'aborted' })).toBeNull()
  })

  it('ranks an unresponsive engine above everything else', () => {
    // The one state waiting cannot resolve, so it must win even over a code.
    expect(speechAttemptOutcome({ timedOut: true, errorCode: 'no-speech' }))
      .toBe(SPEECH_UNRESPONSIVE_MESSAGE)
    expect(speechAttemptOutcome({ timedOut: true, gotText: true }))
      .toBe(SPEECH_UNRESPONSIVE_MESSAGE)
  })

  it('prefers a real error code over the generic no-result line', () => {
    expect(speechAttemptOutcome({ errorCode: 'network' })).toBe(speechErrorMessage('network'))
    expect(speechAttemptOutcome({ errorCode: 'network' })).not.toBe(SPEECH_NO_RESULT_MESSAGE)
  })
})

describe('#2212 error codes become sentences', () => {
  it('distinguishes a blocked permission from an unreachable service', () => {
    const denied = speechErrorMessage('not-allowed')
    const network = speechErrorMessage('network')
    expect(denied).toMatch(/blocked/i)
    expect(network).toMatch(/unreachable/i)
    expect(denied).not.toBe(network)   // the distinction the discarded code held
  })

  it('names an unknown code instead of shrugging', () => {
    expect(speechErrorMessage('brand-new-code')).toContain('brand-new-code')
  })

  it('still says something when there is no code at all', () => {
    expect(speechErrorMessage('')).toBeTruthy()
    expect(speechErrorMessage(undefined)).toBeTruthy()
  })

  it('maps every getUserMedia rejection name to its own actionable line', () => {
    const denied = recorderErrorMessage({ name: 'NotAllowedError' })
    const missing = recorderErrorMessage({ name: 'NotFoundError' })
    const busy = recorderErrorMessage({ name: 'NotReadableError' })
    expect(denied).toMatch(/blocked/i)
    expect(missing).toMatch(/no microphone/i)
    expect(busy).toMatch(/another app/i)
    expect(new Set([denied, missing, busy]).size).toBe(3)
    expect(recorderErrorMessage(undefined)).toBeTruthy()
  })
})

describe('#2212 transcription failures keep the backend’s own words', () => {
  it('renders the endpoint detail verbatim when it sent one', () => {
    // /stt answers with user-facing strings; they used to be swallowed.
    const err = { response: { status: 422, data: { detail: "Didn't catch that — please try again" } } }
    expect(transcriptionErrorMessage(err)).toBe("Didn't catch that — please try again")
  })

  it('does not blame the connection when the request reached us', () => {
    expect(transcriptionErrorMessage({ response: { status: 500, data: {} } })).toContain('500')
    expect(transcriptionErrorMessage({ response: { status: 500, data: {} } })).not.toMatch(/reach Trinity/i)
  })

  it('has a distinct line for rate limit, size, and not-configured', () => {
    const r429 = transcriptionErrorMessage({ response: { status: 429, data: {} } })
    const r413 = transcriptionErrorMessage({ response: { status: 413, data: {} } })
    const r404 = transcriptionErrorMessage({ response: { status: 404, data: {} } })
    expect(r429).toMatch(/too many/i)
    expect(r413).toMatch(/too long/i)
    expect(r404).toMatch(/not set up/i)
    expect(new Set([r429, r413, r404]).size).toBe(3)
  })

  it('names the transport when there was no response', () => {
    expect(transcriptionErrorMessage(new Error('Network Error'))).toMatch(/reach Trinity/i)
  })

  it('ignores a non-string detail rather than rendering [object Object]', () => {
    const err = { response: { status: 422, data: { detail: [{ msg: 'nope' }] } } }
    expect(transcriptionErrorMessage(err)).not.toContain('object')
  })
})

describe('#2212 the recorded clip is labelled with what it actually is', () => {
  // Measured (Playwright, fake mic): Chromium reports
  // "audio/webm;codecs=opus" on the recorder; Firefox reports "" and puts
  // "audio/ogg; codecs=opus" on the chunks. Firefox has no Web Speech API, so it
  // ALWAYS records — every one of its clips was uploaded as WebM containing Ogg.
  it('trusts the recorder when it says something (Chromium)', () => {
    expect(resolveRecordingMimeType('audio/webm;codecs=opus', [{ type: 'audio/webm;codecs=opus' }]))
      .toBe('audio/webm;codecs=opus')
  })

  it('falls back to the chunk type when the recorder is silent (Firefox)', () => {
    expect(resolveRecordingMimeType('', [{ type: 'audio/ogg; codecs=opus' }]))
      .toBe('audio/ogg; codecs=opus')
  })

  it('skips typeless leading chunks rather than reporting an empty type', () => {
    expect(resolveRecordingMimeType(undefined, [{ type: '' }, { type: 'audio/ogg' }])).toBe('audio/ogg')
  })

  it('keeps a usable default when nothing declares a type', () => {
    expect(resolveRecordingMimeType('', [])).toBe('audio/webm')
    expect(resolveRecordingMimeType()).toBe('audio/webm')
  })
})

describe('#2212 the component wiring', () => {
  it('labels the upload from the resolver, not from the recorder alone', () => {
    expect(VOICE).toContain('resolveRecordingMimeType(mediaRec?.mimeType, recChunks)')
    expect(VOICE).not.toContain("type: mediaRec?.mimeType || 'audio/webm'")
  })

  it('chooses the mode through the tested helper, not an inline ternary', () => {
    expect(VOICE).toContain('resolveMicMode({')
    expect(VOICE).not.toContain("const micMode = SpeechRec ? 'speech'")
  })

  it('gates the mic on the platform bit, not only on browser capability', () => {
    expect(VOICE).toContain('serverStt: !!props.agent.stt_available')
  })

  it('asks for continuous recognition so it does not end at the first pause', () => {
    expect(VOICE).toContain('recog.continuous = true')
  })

  it('arms a watchdog for the engine that reports nothing at all', () => {
    // Without it, `listening` stays true forever — measured in Chromium.
    expect(VOICE).toMatch(/speechWatchdog = setTimeout\([\s\S]{0,200}SPEECH_START_TIMEOUT_MS\)/)
    expect(VOICE).toMatch(/timedOut = true/)
    expect(VOICE).toContain('clearSpeechWatchdog()')
  })

  it('keeps the SpeechRecognition error code instead of discarding it', () => {
    expect(VOICE).toMatch(/recog\.onerror = \(e\) => \{ errorCode = e\?\.error/)
    expect(VOICE).not.toMatch(/recog\.onerror = \(\) =>/)
  })

  it('reports a denied microphone instead of returning from a bare catch', () => {
    expect(VOICE).toMatch(/catch \(e\) \{ voiceError\.value = recorderErrorMessage\(e\); return \}/)
    expect(VOICE).not.toMatch(/getUserMedia\([^)]*\) \} catch \{ return \}/)
  })

  it('tells the user when a recording was too short to send', () => {
    expect(VOICE).toMatch(new RegExp(
      `blob\\.size < MIN_RECORDING_BYTES\\) \\{ voiceError\\.value`
    ))
    expect(MIN_RECORDING_BYTES).toBeGreaterThan(0)
  })

  it('surfaces a transcription failure rather than keeping text mode quietly', () => {
    expect(VOICE).toContain('voiceError.value = transcriptionErrorMessage(e)')
    expect(VOICE).not.toContain('catch { /* keep text mode */ }')
  })

  it('surfaces a narration failure too — the speaker is a separate path', () => {
    expect(VOICE).toMatch(/voiceError\.value = TTS_FAILED_MESSAGE/)
    expect(VOICE).toContain('const ttsEnabled = computed(() => !!props.agent.voice_available)')
  })

  it('renders the failure where the composer is, and clears it on the next try', () => {
    expect(CODE).toMatch(/v-if="voiceError"/)
    expect(CODE).toMatch(/aria-live="polite"/)
    expect(VOICE).toMatch(/function toggleMic\(\) \{[\s\S]{0,200}voiceError\.value = ''/)
  })

  it('hides the mic entirely when neither path can work', () => {
    expect(CODE).toMatch(/v-if="sttSupported"/)
    expect(VOICE).toContain('const sttSupported = computed(() => micMode.value !== null)')
  })
})
