import { describe, it, expect } from 'vitest'
import { describeSttCapability, STT_TONE } from '../../src/utils/sttCapability.js'

// #2695 — "key configured" and "key can transcribe" are different facts, and the
// panel must say which one it is asserting.
describe('describeSttCapability', () => {
  it('says nothing when there is no key — presence is the other badge', () => {
    expect(describeSttCapability({ key_configured: false }).tone).toBe(STT_TONE.none)
    expect(describeSttCapability({ key_configured: true, stt_capability: 'unconfigured' }).tone).toBe(STT_TONE.none)
    expect(describeSttCapability(null).tone).toBe(STT_TONE.none)
  })

  it('a capable key reads as such', () => {
    const d = describeSttCapability({ key_configured: true, stt_capability: 'capable' })
    expect(d.tone).toBe(STT_TONE.ok)
    expect(d.label).toBe('can transcribe')
  })

  it("a refused key names the provider's reason and the way out", () => {
    const d = describeSttCapability({ key_configured: true, stt_capability: 'refused', stt_detail: 'missing_permissions' })
    expect(d.tone).toBe(STT_TONE.bad)
    expect(d.label).toBe('cannot transcribe — missing_permissions')
    expect(d.hint).toMatch(/Speech to Text permission/)
  })

  it('a refusal with no detail is still a refusal', () => {
    const d = describeSttCapability({ key_configured: true, stt_capability: 'refused', stt_detail: null })
    expect(d.tone).toBe(STT_TONE.bad)
    expect(d.label).toBe('cannot transcribe')
  })

  it('unknown is unverified — never "configured", never "cannot"', () => {
    const d = describeSttCapability({ key_configured: true, stt_capability: 'unknown' })
    expect(d.tone).toBe(STT_TONE.unverified)
    expect(d.label).toBe('transcription not verified')
    expect(d.hint).toMatch(/stays available/)
  })

  it('an older backend with no capability field reads as unverified, not capable', () => {
    expect(describeSttCapability({ key_configured: true }).tone).toBe(STT_TONE.unverified)
  })
})

import { describeSttLastFailure } from '../../src/utils/sttCapability.js'

// #2696 — the operator half of a client's "voice input failed".
describe('describeSttLastFailure', () => {
  it('is silent with nothing to report', () => {
    expect(describeSttLastFailure(null)).toBeNull()
    expect(describeSttLastFailure({})).toBeNull()
  })

  it("names the category and the provider's own status word", () => {
    const d = describeSttLastFailure({ category: 'permission', provider_status: 401, detail: 'missing_permissions', at: 1700000000 })
    expect(d.text).toBe('Last voice-input failure: the key is missing the speech-to-text permission (HTTP 401 missing_permissions).')
    expect(d.at).toBe(1700000000)
  })

  it('distinguishes quota from auth from audio', () => {
    expect(describeSttLastFailure({ category: 'quota', provider_status: 401, detail: 'quota_exceeded' }).text).toMatch(/out of credits/)
    expect(describeSttLastFailure({ category: 'auth', provider_status: 401, detail: 'invalid_api_key' }).text).toMatch(/rejected/)
    expect(describeSttLastFailure({ category: 'audio', provider_status: 400, detail: 'invalid_audio' }).text).toMatch(/recording was rejected/)
  })

  it('an unknown category still says the provider failed, never nothing', () => {
    const d = describeSttLastFailure({ category: 'something_new', provider_status: 418 })
    expect(d.text).toMatch(/unrecognised error \(HTTP 418\)/)
    expect(d.at).toBeNull()
  })
})
