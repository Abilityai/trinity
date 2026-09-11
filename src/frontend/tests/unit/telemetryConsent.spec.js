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
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  isTelemetryConsentVisible,
  consentVariant,
  CONSENT_COPY,
  receiverCopy,
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
  it('states a default-URL 404 as a 404, never as "not live yet" (the receiver has been live since 2026-09-04)', () => {
    expect(receiverCopy('receiver_not_live')).toMatch(/answered 404/)
    // #2618: a failed send is retried at the next wake, not "daily" — the copy must not name a cadence
    expect(receiverCopy('receiver_not_live')).toMatch(/retried automatically/)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/daily/)
    expect(receiverCopy('failed')).not.toMatch(/daily/)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/not live/i)
    expect(receiverCopy('receiver_not_live')).not.toMatch(/your/i)
  })
  it('states an override 404 as that receiver, naming the env var', () => {
    const c = receiverCopy('receiver_404', 'https://example.test/x')
    expect(c).toContain('https://example.test/x')
    expect(c).toContain('TELEMETRY_SHARING_URL')
  })
  it('never fabricates a send that did not happen', () => {
    expect(receiverCopy(null)).toMatch(/nothing has been sent/i)
    expect(receiverCopy(undefined)).toMatch(/nothing has been sent/i)
    expect(receiverCopy('ok')).toMatch(/acknowledged/)
    expect(receiverCopy('failed')).toMatch(/retried/)
  })

  // The send log records where each share went, so the sentence can name the
  // host that answered instead of the address configured now (#2571).
  it('names the host that answered when the log recorded one', () => {
    const c = receiverCopy('ok', 'https://intake.abilityai.dev/v1', { destination: 'http://localhost:8787' })
    expect(c).toContain('http://localhost:8787')
    expect(c).toMatch(/acknowledged/)
    expect(c).not.toMatch(/your/i)
  })

  it('says plainly when the newest send went somewhere other than the configured address', () => {
    const moved = receiverCopy('ok', 'https://intake.abilityai.dev/v1', {
      destination: 'http://localhost:8787',
      configured: 'https://intake.abilityai.dev',
      changed: true,
    })
    expect(moved).toContain('http://localhost:8787')
    expect(moved).toContain('https://intake.abilityai.dev')
    expect(moved).toContain('TELEMETRY_SHARING_URL')
    expect(moved).toMatch(/no send has gone there since/)
    // Unchanged: the configured address is not named at all — there is nothing to say.
    const same = receiverCopy('ok', 'https://intake.abilityai.dev/v1', {
      destination: 'https://intake.abilityai.dev',
      configured: 'https://intake.abilityai.dev',
      changed: false,
    })
    expect(same).not.toMatch(/no send has gone there since/)
  })

  it('never names a host it does not have, for a pre-2571 entry', () => {
    // The load-bearing one: a legacy entry recorded no destination, so the
    // sentence must not borrow today's URL as if it were the one that answered.
    const legacy = receiverCopy('ok', 'https://intake.abilityai.dev/v1', {})
    expect(legacy).not.toContain('intake.abilityai.dev')
    expect(legacy).toMatch(/acknowledged/)
  })

  it('carries the changed-address clause on the 404 and failed branches too', () => {
    const opts = { destination: 'http://localhost:8787', configured: 'https://intake.abilityai.dev', changed: true }
    for (const hint of ['receiver_not_live', 'receiver_404', 'failed']) {
      expect(receiverCopy(hint, 'https://example.test/x', opts)).toMatch(/no send has gone there since\.$/)
    }
    // A recorded destination is what answered; shareUrl is only the fallback.
    const c = receiverCopy('receiver_404', 'https://example.test/x', { destination: 'http://localhost:8787' })
    expect(c).toContain('http://localhost:8787')
    expect(c).not.toContain('https://example.test/x')
  })

  it('leaks no JS-isms whatever the wire carries', () => {
    for (const destination of [null, undefined, '']) {
      for (const hint of ['ok', 'receiver_not_live', 'receiver_404', 'failed', null]) {
        expect(receiverCopy(hint, 'https://example.test/x', { destination })).not.toMatch(/undefined|null|NaN|\[object/)
      }
    }
  })
})

describe('TelemetrySharingPanel wiring (source-structure guard)', () => {
  const panel = stripComments(
    readFileSync(
      fileURLToPath(new URL('../../src/components/settings/TelemetrySharingPanel.vue', import.meta.url)),
      'utf8'
    )
  )

  it('renders the destination per row with the unknown fallback', () => {
    expect(panel).toMatch(/<code v-if="send\.destination">\{\{ send\.destination \}\}<\/code>/)
    expect(panel).toContain('unknown host')
  })

  it('passes the destination triple into receiverCopy', () => {
    const call = panel.match(/receiverCopy\([\s\S]*?\n\s*\)/)
    expect(call).not.toBeNull()
    expect(call[0]).toMatch(/receiver_destination/)
    expect(call[0]).toMatch(/configured_destination/)
    expect(call[0]).toMatch(/destination_changed/)
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
