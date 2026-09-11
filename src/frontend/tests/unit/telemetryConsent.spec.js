/**
 * The Finish-setup card's usage-sharing section (ent#437).
 *
 * Four rules carry this surface, none visible to a structural check:
 *   1. Never before the flags arrive, never to a non-admin, never when consent
 *      is on, hard-disabled, or dismissed on the server.
 *   2. "Not now" is a per-browser snooze; the warm variant may break through a
 *      snooze exactly once per browser.
 *   3. The copy promises only what the payload earns: anonymous, aggregate,
 *      off by default, reversible — never "not traceable" or "secure".
 *   4. A 404 is described as what it is: a receiver that is not live, or, from
 *      an overridden URL, a receiver that answered 404.
 *   5. The receiver line names the origin the attempt RECORDED, never the URL
 *      configured now, and says plainly when the two differ (#2571).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  isTelemetryConsentVisible,
  consentVariant,
  CONSENT_COPY,
  receiverCopy,
  receiverLabel,
  isEmailNudgeVisible,
  readSnoozedUntil,
  persistSnooze,
  readWarmShown,
  persistWarmShown,
  TELEMETRY_SNOOZE_KEY,
  SNOOZE_DAYS,
} from '../../src/components/onboarding/telemetryConsent'

const ready = {
  flagsLoaded: true,
  profileVerified: true,
  isAdmin: true,
  enabled: false,
  hardDisabled: false,
  dismissed: false,
  firstValue: false,
  snoozed: false,
  warmShown: false,
}

describe('isTelemetryConsentVisible', () => {
  it('renders for a verified admin with no server marker and no snooze', () => {
    expect(isTelemetryConsentVisible(ready)).toBe(true)
  })

  it('hides by default: an empty call is the hidden state', () => {
    expect(isTelemetryConsentVisible()).toBe(false)
    expect(isTelemetryConsentVisible({})).toBe(false)
  })

  it.each([
    ['flags not loaded', { flagsLoaded: false }],
    ['profile not verified', { profileVerified: false }],
    ['not an admin', { isAdmin: false }],
    ['consent already on', { enabled: true }],
    ['hard-disabled by config', { hardDisabled: true }],
    ['dismissed on the server', { dismissed: true }],
    ['snoozed, no first value', { snoozed: true }],
    ['snoozed, first value, warm already shown', { snoozed: true, firstValue: true, warmShown: true }],
  ])('hides when %s', (_label, override) => {
    expect(isTelemetryConsentVisible({ ...ready, ...override })).toBe(false)
  })

  it('lets the warm variant break through a snooze exactly once', () => {
    expect(isTelemetryConsentVisible({ ...ready, snoozed: true, firstValue: true, warmShown: false })).toBe(true)
  })

  it('never renders over a server marker even with a first value', () => {
    expect(isTelemetryConsentVisible({ ...ready, dismissed: true, firstValue: true })).toBe(false)
    expect(isTelemetryConsentVisible({ ...ready, enabled: true, firstValue: true })).toBe(false)
  })
})

describe('consentVariant', () => {
  it('is cold until the first autonomous success', () => {
    expect(consentVariant({ firstValue: false })).toBe('cold')
  })
  it('is warm once per browser after the first value', () => {
    expect(consentVariant({ firstValue: true, warmShown: false })).toBe('warm')
    expect(consentVariant({ firstValue: true, warmShown: true })).toBe('cold')
  })
})

describe('CONSENT_COPY promises only what the payload earns', () => {
  const all = JSON.stringify(CONSENT_COPY).toLowerCase()
  it('says anonymous, aggregate, off by default, reversible', () => {
    expect(all).toContain('anonymous')
    expect(all).toContain('aggregate')
    expect(all).toContain('off by default')
    expect(all).toContain('reversible')
  })
  it('discloses the consent-time backfill window at the moment of consent (§45.1 FR-4)', () => {
    expect(CONSENT_COPY.shared.detail).toContain('last 30 days')
  })
  it('names the share id rule and where sends appear', () => {
    expect(all).toContain('share id')
    expect(all).toContain('recent sends')
  })
  it('never claims unlinkability or security the receiver has to provide', () => {
    for (const banned of ['not traceable', 'untraceable', 'cannot be linked', 'secure', 'encrypted', 'guarantee']) {
      expect(all).not.toContain(banned)
    }
  })
  it('never mentions the install id as the key', () => {
    expect(all).not.toContain('install id')
    expect(all).not.toContain('installation_id')
  })
})

describe('receiverCopy', () => {
  const sink = 'https://sink.example:8787'
  const hosted = 'https://intake.abilityai.dev'

  it('names the receiver that acknowledged, from the RECORD — never the address configured now', () => {
    expect(receiverCopy('ok', { host: sink })).toContain(sink)
    expect(receiverCopy('ok', { host: sink })).toMatch(/acknowledged/)
    expect(receiverCopy('ok', { host: sink, configuredHost: hosted })).not.toContain(hosted)
  })
  it('says when the record predates the destination instead of inventing one', () => {
    expect(receiverCopy('ok')).toMatch(/acknowledged/)
    expect(receiverCopy('ok')).toMatch(/not recorded/)
    expect(receiverCopy('receiver_404')).toMatch(/not recorded/)
    expect(receiverCopy('receiver_404')).toContain('TELEMETRY_SHARING_URL')
  })
  it('states a default-URL 404 as a 404, never as "not live yet" (the receiver has been live since 2026-09-04)', () => {
    expect(receiverCopy('receiver_not_live')).toMatch(/answered 404/)
    // #2618: a failed send is retried at the next wake, not "daily" — the copy must not name a cadence
    expect(receiverCopy('receiver_not_live')).toMatch(/retried automatically/)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/daily/)
    expect(receiverCopy('failed')).not.toMatch(/daily/)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/not live/i)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/your/i)
  })
  it('states an override 404 as that receiver, naming the RECORDED origin and the env var', () => {
    const c = receiverCopy('receiver_404', { host: 'https://example.test' })
    expect(c).toContain('https://example.test')
    expect(c).toContain('TELEMETRY_SHARING_URL')
  })
  it('a failed attempt names where it was sent, and never says a receiver "answered"', () => {
    const c = receiverCopy('failed', { host: sink })
    expect(c).toContain(`to ${sink}`)
    expect(c).not.toMatch(/answered/)
    expect(c).toMatch(/retried/)
  })
  it('says plainly when the newest attempt went elsewhere than the configured address, and what happens next', () => {
    const c = receiverCopy('ok', { host: sink, configuredHost: hosted, mismatch: true, enabled: true })
    expect(c).toContain(sink)
    expect(c).toContain(hosted)
    expect(c).toMatch(/has not seen it/)
    expect(c).toMatch(/next scheduled send/)
    const off = receiverCopy('ok', { host: sink, configuredHost: hosted, mismatch: true, enabled: false })
    expect(off).toMatch(/off/)
    expect(off).not.toMatch(/next scheduled send/)
    // No recorded origin ⇒ nothing to contrast, so no clause even if asked.
    expect(receiverCopy('ok', { mismatch: true, configuredHost: hosted })).not.toContain(hosted)
  })
  it('never fabricates a send that did not happen', () => {
    expect(receiverCopy(null)).toMatch(/nothing has been sent/i)
    expect(receiverCopy(undefined)).toMatch(/nothing has been sent/i)
    expect(receiverCopy('ok')).toMatch(/acknowledged/)
    expect(receiverCopy('failed')).toMatch(/retried/)
  })
  it('never prints "null" or "undefined" for any hint or option shape', () => {
    const hints = ['ok', 'receiver_not_live', 'receiver_404', 'failed', null, undefined, 'something-new']
    const opts = [undefined, {}, { host: null }, { host: undefined, mismatch: true }, { host: 123, mismatch: true },
      { host: sink, configuredHost: null, mismatch: true }, { host: sink, configuredHost: undefined, mismatch: true, enabled: false }]
    for (const h of hints) for (const o of opts) {
      const c = receiverCopy(h, o)
      expect(c, `${h} ${JSON.stringify(o)}`).not.toMatch(/\bnull\b|\bundefined\b/)
      expect(c.length).toBeGreaterThan(0)
    }
  })
})

describe('receiverLabel', () => {
  it('prints the recorded origin verbatim', () => {
    expect(receiverLabel('https://sink.example:8787')).toBe('https://sink.example:8787')
    expect(receiverLabel('  https://a.example ')).toBe('https://a.example')
  })
  it('reads as unknown for an attempt logged before the destination was recorded, or a corrupt value', () => {
    for (const v of [undefined, null, '', '   ', 123, {}, []]) {
      const l = receiverLabel(v)
      expect(l).toMatch(/unknown/)
      expect(l).not.toMatch(/\bnull\b|\bundefined\b/)
    }
  })
})

describe('isEmailNudgeVisible (moved verbatim from AdminEmailNudge, #2381)', () => {
  it('renders only for a verified admin with no email and no dismissal', () => {
    expect(isEmailNudgeVisible({ profileVerified: true, isAdmin: true, hasEmail: false, dismissed: false })).toBe(true)
    expect(isEmailNudgeVisible({ profileVerified: false, isAdmin: true, hasEmail: false })).toBe(false)
    expect(isEmailNudgeVisible({ profileVerified: true, isAdmin: false, hasEmail: false })).toBe(false)
    expect(isEmailNudgeVisible({ profileVerified: true, isAdmin: true, hasEmail: true })).toBe(false)
    expect(isEmailNudgeVisible({ profileVerified: true, isAdmin: true, hasEmail: false, dismissed: true })).toBe(false)
    expect(isEmailNudgeVisible()).toBe(false)
  })
})

describe('per-browser state', () => {
  let store
  beforeEach(() => {
    store = new Map()
    globalThis.localStorage = {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    }
  })

  it('snoozes for SNOOZE_DAYS from now and reads back as snoozed', () => {
    const now = Date.UTC(2026, 8, 3)
    expect(readSnoozedUntil(now)).toBe(false)
    expect(persistSnooze(SNOOZE_DAYS, now)).toBe(true)
    expect(readSnoozedUntil(now)).toBe(true)
    expect(readSnoozedUntil(now + (SNOOZE_DAYS + 1) * 86400000)).toBe(false)
  })

  it('treats garbage in storage as not snoozed', () => {
    store.set(TELEMETRY_SNOOZE_KEY, 'not a date')
    expect(readSnoozedUntil()).toBe(false)
  })

  it('records the warm variant as shown', () => {
    expect(readWarmShown()).toBe(false)
    expect(persistWarmShown()).toBe(true)
    expect(readWarmShown()).toBe(true)
  })

  it('reads as not snoozed and not shown when storage throws', () => {
    globalThis.localStorage = {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
    }
    expect(readSnoozedUntil()).toBe(false)
    expect(readWarmShown()).toBe(false)
    expect(persistSnooze()).toBe(false)
    expect(persistWarmShown()).toBe(false)
  })
})
