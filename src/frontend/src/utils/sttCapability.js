// #2695 — what the Voice settings panel says about speech-to-text.
//
// `key_configured` is presence; `stt_capability` is what the provider answered
// when asked. ElevenLabs permissions are per endpoint, so a key that speaks may
// still not transcribe — and before this the panel said "configured" while every
// Workspace voice message failed. Pure, so the wording rule is testable without
// mounting the panel (vitest runs `environment: 'node'`).

export const STT_TONE = Object.freeze({ ok: 'ok', bad: 'bad', unverified: 'unverified', none: 'none' })

/**
 * @param {{ key_configured?: boolean, stt_capability?: string, stt_detail?: string|null }} state
 * @returns {{ tone: string, label: string, hint: string }}
 */
export function describeSttCapability(state) {
  if (!state || !state.key_configured || state.stt_capability === 'unconfigured') {
    return { tone: STT_TONE.none, label: '', hint: '' }
  }
  switch (state.stt_capability) {
    case 'capable':
      return {
        tone: STT_TONE.ok,
        label: 'can transcribe',
        hint: 'Workspace voice input (dictation) is available.',
      }
    case 'refused':
      return {
        tone: STT_TONE.bad,
        label: state.stt_detail ? `cannot transcribe — ${state.stt_detail}` : 'cannot transcribe',
        hint: 'This key is not permitted to call speech-to-text, so the Workspace mic is hidden. '
          + 'Grant the Speech to Text permission on the key at ElevenLabs, then save it again.',
      }
    default:
      return {
        tone: STT_TONE.unverified,
        label: 'transcription not verified',
        hint: 'ElevenLabs could not be reached to check the key; the Workspace mic stays available '
          + 'until the check completes.',
      }
  }
}
