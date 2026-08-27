/**
 * ent#434 — the weekly-limit alert settings control.
 *
 * These rules were extracted out of `SubscriptionsPanel.vue` precisely so they
 * could be tested: vitest runs `environment: 'node'` with no component-mount
 * harness, so a decision left inside the SFC is one no test can reach (the
 * ent#392 precedent).
 *
 * The finding behind this file: the PR shipped the backend for AC #1
 * ("Settings-surfaced value") and AC #4 ("reports itself inactive on the
 * Settings surface") with NO client rendering either — the honest-status block
 * existed in the payload and reached nobody, which is exactly the #2217 failure
 * the design cites throughout.
 */
import { describe, it, expect } from 'vitest'
import {
  thresholdValidationError,
  thresholdChanged,
  alertStatusLine,
  INACTIVE_COPY,
  FALLBACK_MIN_PCT,
  FALLBACK_MAX_PCT,
} from '../../src/utils/headroomAlertSettings'

describe('thresholdValidationError', () => {
  it('accepts a value inside the server-supplied bounds', () => {
    expect(thresholdValidationError('75', { min: 50, max: 99 })).toBe('')
  })

  it('accepts 0 — the documented off value, not a range violation', () => {
    // A bare "must be between 50 and 99" message would be actively wrong here:
    // 0 is how the operator switches the alerts off.
    expect(thresholdValidationError('0', { min: 50, max: 99 })).toBe('')
  })

  it('names the problem, the fix, and an example for a non-number', () => {
    const msg = thresholdValidationError('eighty', { min: 50, max: 99 })
    expect(msg).toContain('Whole numbers only')
    expect(msg).toContain('eighty')
    expect(msg).toMatch(/Example: 75/)
  })

  it('rejects an out-of-range value and states the legal set including 0', () => {
    const msg = thresholdValidationError('20', { min: 50, max: 99 })
    expect(msg).toContain('0 to switch the alerts off')
    expect(msg).toContain('between 50 and 99')
    expect(msg).toContain('Example: 75')
  })

  it('rejects a negative or decimal value rather than silently truncating', () => {
    expect(thresholdValidationError('-40', { min: 50, max: 99 })).not.toBe('')
    expect(thresholdValidationError('75.5', { min: 50, max: 99 })).not.toBe('')
  })

  it('says nothing about an empty field — that is not an error yet', () => {
    expect(thresholdValidationError('', { min: 50, max: 99 })).toBe('')
    expect(thresholdValidationError(null, { min: 50, max: 99 })).toBe('')
  })

  it('falls back to the mirrored bounds when the server has not answered', () => {
    const msg = thresholdValidationError('10', {})
    expect(msg).toContain(`between ${FALLBACK_MIN_PCT} and ${FALLBACK_MAX_PCT}`)
  })
})

describe('thresholdChanged', () => {
  it('is false before the server value is known, so Save cannot arm on nothing', () => {
    expect(thresholdChanged('75', undefined)).toBe(false)
    expect(thresholdChanged('75', null)).toBe(false)
  })

  it('ignores surrounding whitespace and numeric/string type differences', () => {
    expect(thresholdChanged(' 75 ', 75)).toBe(false)
    expect(thresholdChanged('75', '75')).toBe(false)
  })

  it('is true for a real edit', () => {
    expect(thresholdChanged('80', 75)).toBe(true)
  })
})

describe('alertStatusLine', () => {
  it('does not claim a state before the first successful read', () => {
    // Principle 15: loading is not empty and not failed. Asserting either
    // "active" or a specific inactive reason here would be inventing one.
    expect(alertStatusLine({ active: true }, false)).toEqual({
      text: 'Checking…',
      tone: 'muted',
    })
  })

  it('reports active with both tiers when they differ', () => {
    const { text, tone } = alertStatusLine(
      { active: true, threshold_pct: 75, escalation_pct: 90 }, true
    )
    expect(text).toBe('Active — warning at 75%, escalating at 90%.')
    expect(tone).toBe('active')
  })

  it('omits the escalation clause when the tiers collapse', () => {
    // At a threshold of 90+ the derived escalation equals it and the warning
    // tier is unreachable — saying "escalating at 90%" beside "warning at 90%"
    // would imply two tiers that do not exist.
    const { text } = alertStatusLine(
      { active: true, threshold_pct: 90, escalation_pct: 90 }, true
    )
    expect(text).toBe('Active — warning at 90%.')
  })

  it.each(Object.keys(INACTIVE_COPY))('names the reason: %s', (reason) => {
    const { text, tone } = alertStatusLine({ active: false, inactive_reason: reason }, true)
    expect(text).toBe(INACTIVE_COPY[reason])
    expect(tone).toBe('muted')
  })

  it('distinguishes "off by choice" from "not checking"', () => {
    // The whole point of AC #4 — these must not read the same.
    const off = alertStatusLine({ active: false, inactive_reason: 'threshold_disabled' }, true)
    const blind = alertStatusLine({ active: false, inactive_reason: 'auto_refresh_off' }, true)
    expect(off.text).not.toBe(blind.text)
  })

  it('is honest about an unrecognised reason rather than silent or invented', () => {
    const { text } = alertStatusLine({ active: false, inactive_reason: 'from_the_future' }, true)
    expect(text).toContain('did not report a reason')
  })

  it('never throws on a missing payload', () => {
    expect(() => alertStatusLine(undefined, true)).not.toThrow()
    expect(alertStatusLine(undefined, true).tone).toBe('muted')
  })
})
