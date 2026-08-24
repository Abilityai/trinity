/**
 * ent#440 — voice in the agent Workspace: one conversation, two modalities.
 *
 * The feature's whole claim is that a spoken turn IS an ordinary Workspace turn,
 * so the rules worth guarding are the ones that decide when to listen, when to
 * send, when to speak, and when to stop. They live in `voiceConversation.js`
 * rather than inside the component for the reason this project's other portal
 * specs record: there is no component-mount harness (no @vue/test-utils, jsdom
 * or happy-dom), so a rule expressed in a `.vue` file can only be guarded by
 * regexing source — which catches deletion but never a wrong rule.
 *
 * The source-regex assertions at the bottom are therefore scoped to what only
 * source can answer: that the component dispatches through the machine instead
 * of hand-rolling transitions, and that a spoken turn goes through the SAME
 * submit path a typed one does.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import {
  ACT_CAPTURE,
  ACT_NARRATE,
  ACT_RELEASE,
  ACT_SEND,
  ACT_STOP_CAPTURE,
  ACT_STOP_NARRATION,
  BARGE_IN_RMS,
  MAX_SPOKEN_CHARS,
  SPEECH_RMS,
  SPOKEN_CODE_PLACEHOLDER,
  SPOKEN_TRUNCATION_NOTICE,
  VOICE_LISTENING,
  VOICE_NO_MIC_REASON,
  VOICE_NO_STT_REASON,
  VOICE_OFF,
  VOICE_SPEAKING,
  VOICE_TEXT_REPLIES_NOTICE,
  VOICE_THINKING,
  VOICE_TRANSCRIBING,
  isSpeech,
  isVoiceLive,
  nextVoiceState,
  spokenReply,
  utteranceVerdict,
  voiceConversationMode,
  voiceStateLabel,
} from '../../src/components/portal/voiceConversation'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const component = read('../../src/components/portal/PortalConversation.vue')

const FULL = { canRecord: true, secureContext: true, serverStt: true, voiceAvailable: true }

describe('availability — a named reason, never a dead control (AC 6)', () => {
  it('is available when the browser can record and the platform can transcribe', () => {
    expect(voiceConversationMode(FULL)).toEqual({ available: true, narrates: true, reason: '' })
  })

  it('runs with text replies when the agent has no voice — and says so', () => {
    const mode = voiceConversationMode({ ...FULL, voiceAvailable: false })
    expect(mode.available).toBe(true)
    expect(mode.narrates).toBe(false)
    expect(mode.reason).toBe(VOICE_TEXT_REPLIES_NOTICE)
  })

  it('refuses with the microphone reason off a secure origin', () => {
    expect(voiceConversationMode({ ...FULL, secureContext: false }))
      .toEqual({ available: false, narrates: false, reason: VOICE_NO_MIC_REASON })
    expect(voiceConversationMode({ ...FULL, canRecord: false }).reason).toBe(VOICE_NO_MIC_REASON)
  })

  it('refuses with the provider reason when the platform cannot transcribe', () => {
    expect(voiceConversationMode({ ...FULL, serverStt: false }))
      .toEqual({ available: false, narrates: false, reason: VOICE_NO_STT_REASON })
  })

  it('defaults to unavailable rather than promising a loop it cannot run', () => {
    expect(voiceConversationMode().available).toBe(false)
    expect(voiceConversationMode({}).reason).toBeTruthy()
  })

  it('every reason points at the surface that still works', () => {
    for (const reason of [VOICE_NO_MIC_REASON, VOICE_NO_STT_REASON]) {
      expect(reason.toLowerCase()).toContain('type')
    }
  })
})

describe('the loop', () => {
  it('starts by listening', () => {
    expect(nextVoiceState(VOICE_OFF, 'start')).toEqual({
      state: VOICE_LISTENING, actions: [ACT_CAPTURE],
    })
  })

  it('is idempotent on start — a second press opens no second recorder', () => {
    expect(nextVoiceState(VOICE_LISTENING, 'start')).toEqual({ state: VOICE_LISTENING, actions: [] })
    expect(nextVoiceState(VOICE_THINKING, 'start').actions).toEqual([])
  })

  it('runs listen → transcribe → send → speak → listen', () => {
    const a = nextVoiceState(VOICE_LISTENING, 'utterance')
    expect(a).toEqual({ state: VOICE_TRANSCRIBING, actions: [] })
    const b = nextVoiceState(a.state, 'transcript')
    expect(b).toEqual({ state: VOICE_THINKING, actions: [ACT_SEND] })
    const c = nextVoiceState(b.state, 'reply')
    expect(c).toEqual({ state: VOICE_SPEAKING, actions: [ACT_NARRATE] })
    const d = nextVoiceState(c.state, 'narration-ended')
    expect(d).toEqual({ state: VOICE_LISTENING, actions: [ACT_CAPTURE] })
  })

  it('skips narration and re-opens the mic when the agent has no voice', () => {
    expect(nextVoiceState(VOICE_THINKING, 'reply', { canNarrate: false }))
      .toEqual({ state: VOICE_LISTENING, actions: [ACT_CAPTURE] })
  })

  it('re-listens without spending a transcription on room tone', () => {
    // Raised from TRANSCRIBING in practice: the recorder has already stopped by
    // the time its clip is found to hold no speech. A LISTENING-only edge
    // wedged the loop in TRANSCRIBING with the microphone open and nothing
    // ever advancing it.
    expect(nextVoiceState(VOICE_TRANSCRIBING, 'silence'))
      .toEqual({ state: VOICE_LISTENING, actions: [ACT_CAPTURE] })
    expect(nextVoiceState(VOICE_LISTENING, 'silence'))
      .toEqual({ state: VOICE_LISTENING, actions: [ACT_CAPTURE] })
  })

  it('leaves no live state without an edge that can advance it', () => {
    // The wedge above, generalised: from every live state at least one event
    // this loop actually raises must move it somewhere.
    const raised = ['utterance', 'silence', 'transcript', 'transcript-empty',
      'reply', 'narration-ended', 'barge-in', 'stop', 'error']
    for (const state of [VOICE_LISTENING, VOICE_TRANSCRIBING, VOICE_THINKING, VOICE_SPEAKING]) {
      const moves = raised.filter((e) => nextVoiceState(state, e).state !== state)
      expect(moves.length).toBeGreaterThan(0)
    }
  })

  it('re-listens when the transcript comes back empty', () => {
    expect(nextVoiceState(VOICE_TRANSCRIBING, 'transcript-empty'))
      .toEqual({ state: VOICE_LISTENING, actions: [ACT_CAPTURE] })
  })

  it('interrupts the agent mid-utterance and hears the user out (AC 5)', () => {
    expect(nextVoiceState(VOICE_SPEAKING, 'barge-in'))
      .toEqual({ state: VOICE_LISTENING, actions: [ACT_STOP_NARRATION, ACT_CAPTURE] })
  })

  it('ignores a barge-in that is not over speech', () => {
    for (const state of [VOICE_LISTENING, VOICE_THINKING, VOICE_TRANSCRIBING]) {
      expect(nextVoiceState(state, 'barge-in')).toEqual({ state, actions: [] })
    }
  })

  it('always gives the microphone back when it stops', () => {
    for (const state of [VOICE_LISTENING, VOICE_TRANSCRIBING, VOICE_THINKING, VOICE_SPEAKING]) {
      for (const event of ['stop', 'error']) {
        const out = nextVoiceState(state, event)
        expect(out.state).toBe(VOICE_OFF)
        expect(out.actions).toContain(ACT_RELEASE)
      }
    }
    expect(nextVoiceState(VOICE_LISTENING, 'stop').actions).toContain(ACT_STOP_CAPTURE)
    expect(nextVoiceState(VOICE_SPEAKING, 'stop').actions).toContain(ACT_STOP_NARRATION)
  })

  it('ignores every late callback once the loop is off', () => {
    for (const event of ['utterance', 'silence', 'transcript', 'transcript-empty', 'reply',
      'narration-ended', 'barge-in', 'nonsense']) {
      expect(nextVoiceState(VOICE_OFF, event)).toEqual({ state: VOICE_OFF, actions: [] })
    }
  })

  it('never sends a transcript twice — the send edge exists once', () => {
    // A replayed `transcript` (a slow /stt answering after the state moved on)
    // must not dispatch a second turn against the same words.
    expect(nextVoiceState(VOICE_THINKING, 'transcript').actions).toEqual([])
    expect(nextVoiceState(VOICE_SPEAKING, 'transcript').actions).toEqual([])
  })

  it('knows which states hold the microphone', () => {
    expect(isVoiceLive(VOICE_OFF)).toBe(false)
    for (const s of [VOICE_LISTENING, VOICE_TRANSCRIBING, VOICE_THINKING, VOICE_SPEAKING]) {
      expect(isVoiceLive(s)).toBe(true)
    }
  })

  it('labels every state, and falls back rather than rendering undefined', () => {
    for (const s of [VOICE_OFF, VOICE_LISTENING, VOICE_TRANSCRIBING, VOICE_THINKING, VOICE_SPEAKING]) {
      expect(voiceStateLabel(s)).toBeTruthy()
    }
    expect(voiceStateLabel('nope')).toBe(voiceStateLabel(VOICE_OFF))
  })
})

describe('utterance detection', () => {
  it('treats sustained level as speech and room tone as silence', () => {
    expect(isSpeech(SPEECH_RMS)).toBe(true)
    expect(isSpeech(SPEECH_RMS - 0.001)).toBe(false)
    expect(isSpeech(NaN)).toBe(false)
    expect(isSpeech(undefined)).toBe(false)
  })

  it('asks for more level to interrupt than to hear', () => {
    // The mic hears the agent's own narration; a barge-in bar at the listening
    // threshold makes the agent interrupt itself.
    expect(BARGE_IN_RMS).toBeGreaterThan(SPEECH_RMS)
  })

  it('keeps listening through a mid-sentence pause', () => {
    expect(utteranceVerdict({ sawSpeech: true, msSinceSpeech: 400, elapsedMs: 3000 })).toBe('continue')
  })

  it('ends the utterance once the pause holds', () => {
    expect(utteranceVerdict({ sawSpeech: true, msSinceSpeech: 1200, elapsedMs: 3000 })).toBe('end')
  })

  it('ends a monologue at the cap rather than uploading forever', () => {
    expect(utteranceVerdict({ sawSpeech: true, msSinceSpeech: 0, elapsedMs: 30000 })).toBe('end')
  })

  it('gives the microphone back when nothing was ever said', () => {
    expect(utteranceVerdict({ sawSpeech: false, elapsedMs: 3000 })).toBe('continue')
    expect(utteranceVerdict({ sawSpeech: false, elapsedMs: 15000 })).toBe('idle')
  })

  it('never reports idle once speech has been heard — that clip is worth sending', () => {
    expect(utteranceVerdict({ sawSpeech: true, msSinceSpeech: 0, elapsedMs: 20000 })).toBe('continue')
  })
})

describe('what gets spoken', () => {
  it('reads a plain reply as-is', () => {
    expect(spokenReply('All three checks passed.')).toBe('All three checks passed.')
  })

  it('never pronounces a code block', () => {
    const out = spokenReply('Run this:\n\n```bash\nnpm run build\n```\n\nThen refresh.')
    expect(out).toContain(SPOKEN_CODE_PLACEHOLDER)
    expect(out).not.toContain('npm run build')
    expect(out).not.toContain('```')
  })

  it('swallows an unterminated fence rather than reading backticks aloud', () => {
    const out = spokenReply('Here:\n```python\nprint(1)')
    expect(out).not.toContain('```')
    expect(out).not.toContain('print(1)')
  })

  it('reads link text, never the URL', () => {
    expect(spokenReply('See [the report](https://example.com/a/b?x=1).'))
      .toBe('See the report.')
  })

  it('drops markdown furniture', () => {
    const out = spokenReply('## Summary\n\n- **one**\n- _two_\n\n> quoted')
    expect(out).not.toMatch(/[#*_>]/)
    expect(out).toContain('one')
    expect(out).toContain('two')
  })

  it('caps a long reply and says where the rest is', () => {
    const out = spokenReply('word '.repeat(1000))
    expect(out.length).toBeLessThanOrEqual(MAX_SPOKEN_CHARS + SPOKEN_TRUNCATION_NOTICE.length)
    expect(out.endsWith(SPOKEN_TRUNCATION_NOTICE)).toBe(true)
  })

  it('cuts a capped reply at a sentence end when there is one near the limit', () => {
    const body = `${'a'.repeat(MAX_SPOKEN_CHARS - 40)}. ${'b'.repeat(200)}`
    const out = spokenReply(body)
    expect(out).toBe(`${'a'.repeat(MAX_SPOKEN_CHARS - 40)}.${SPOKEN_TRUNCATION_NOTICE}`)
  })

  it('has nothing to say about nothing', () => {
    expect(spokenReply('')).toBe('')
    expect(spokenReply(null)).toBe('')
    expect(spokenReply(undefined)).toBe('')
    expect(spokenReply(42)).toBe('')
  })
})

describe('the component consumes the machine (what only source can answer)', () => {
  it('dispatches through nextVoiceState instead of hand-rolling transitions', () => {
    expect(component).toContain('nextVoiceState(voiceState.value, event')
    // Exactly one writer of the state, so no path can move the loop behind the
    // machine's back.
    // `[^=]` so a comparison (`voiceState.value === …`) is not counted as a write.
    expect(component.match(/voiceState\.value = [^=]/g) || []).toHaveLength(2) // dispatch + unmount reset
  })

  it('sends a spoken turn through the same submit path a typed one uses', () => {
    // The claim of AC 2/3 in one line: if this ever becomes its own call to
    // `deliver` or the store, voice has become a parallel conversation.
    expect(component).toContain('const res = await submitUserText(text)')
    expect(component.match(/await submitUserText\(/g) || []).toHaveLength(2) // send() + the voice turn
  })

  it('does not narrate a reply twice while the loop is running', () => {
    expect(component).toContain('!voiceConvLive.value) speak(data.response)')
  })

  it('only renders the control when the loop can actually run', () => {
    expect(component).toContain('v-if="conversationMode.available"')
  })

  it('asks for echo cancellation — the agent must not interrupt itself', () => {
    expect(component).toContain('echoCancellation: true')
  })

  it('cannot open two microphones while a permission prompt is open', () => {
    // The loop is not live until getUserMedia resolves, so a second press, a
    // press after Stop, or a nav-away would otherwise install a second stream
    // and a second timer and orphan the first pair — a hot mic with no control
    // pointing at it.
    expect(component).toContain('if (voiceConvLive.value || voiceStarting) return')
    expect(component).toContain('const token = ++voiceStartToken')
    // Teardown invalidates an in-flight start rather than trusting it to notice.
    expect(component).toMatch(/function releaseVoiceHardware\(\)[\s\S]{0,400}voiceStartToken\+\+/)
    // A superseded start stops the tracks it just acquired.
    expect(component).toMatch(/token !== voiceStartToken[\s\S]{0,120}getTracks\(\)\.forEach/)
  })

  it('releases the microphone on unmount', () => {
    expect(component).toMatch(/function cleanupVoice\(\)\s*\{\s*releaseVoiceHardware\(\)/)
  })
})
