/**
 * The usage-sharing ask (ent#437) — the Finish-setup card's section until
 * ent#581 made it the `sharing` step of the first-run overlay
 * (`steps/StepSharing.vue`). The copy and the per-browser helpers below are
 * unchanged; the step's own rules are pinned at the bottom of this file.
 *
 * Visibility now comes from the registry (`firstRunSteps.spec.js`). Three rules
 * carry what is left here, none visible to a structural check:
 *   1. The warm variant speaks exactly once per browser.
 *   2. The copy promises only what the payload earns: anonymous, aggregate,
 *      off by default, reversible — never "not traceable" or "secure".
 *   3. A 404 is described as what it is: a receiver that is not live, or, from
 *      an overridden URL, a receiver that answered 404.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  consentVariant,
  CONSENT_COPY,
  receiverCopy,
  readSnoozedUntil,
  readWarmShown,
  persistWarmShown,
  TELEMETRY_SNOOZE_KEY,
} from '../../src/components/onboarding/telemetryConsent'

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
    expect(persistWarmShown()).toBe(false)
  })
})

describe('the sharing step (ent#581)', () => {
  const STEP = readFileSync(
    fileURLToPath(new URL('../../src/components/onboarding/steps/StepSharing.vue', import.meta.url)),
    'utf8'
  )

  it('speaks CONSENT_COPY, never a second copy of it', () => {
    expect(STEP).toMatch(/import \{[^}]*CONSENT_COPY[^}]*\} from '\.\.\/telemetryConsent'/)
    expect(STEP).toContain('CONSENT_COPY.shared.detail')
    expect(STEP).toContain('CONSENT_COPY.shared.share')
    expect(STEP).toContain('CONSENT_COPY.shared.dontAsk')
  })

  /*
   * The warm re-ask is one shot per browser, and it exists precisely because a
   * cold ask at first login converts badly. On the old card stack a hidden
   * card still MOUNTED, so it had to watch real visibility (#2380 regression).
   * The step mounts only while it is the one on screen, so mounting IS being
   * seen — but it must still spend the ask only for the warm variant, and
   * never for an ask that is already decided.
   */
  it('spends the warm ask only when the warm variant actually renders', () => {
    const mounted = STEP.slice(STEP.indexOf('onMounted(() => {'))
    const body = mounted.slice(0, mounted.indexOf('})'))
    expect(body).toContain('persistWarmShown()')
    expect(body).toContain("variant === 'warm'")
    expect(body).toContain('!decided.value')
  })

  it('writes the server marker for both answers and leaves "not now" to the chassis Skip', () => {
    expect(STEP).toContain('store.setConsent(true, 30)')
    expect(STEP).toContain('store.dismissAsk()')
    // A failed verb has a visible home (contract p.18).
    expect(STEP).toContain('<InlineError')
  })

  it('loads the payload preview on expand, never on mount', () => {
    expect(STEP).toMatch(/@toggle="onPreviewToggle"/)
    expect(STEP).not.toMatch(/onMounted\([^)]*store\.load/)
  })
})
