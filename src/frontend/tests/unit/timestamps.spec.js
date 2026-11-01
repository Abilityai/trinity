import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { formatCompactAge, formatRelativeTime, serverSkewMs } from '@/utils/timestamps'
import { useFormatters } from '@/composables/useFormatters'

/**
 * ent#100 — the two time helpers the "Recent failures" tile renders through.
 *
 * `formatCompactAge` takes an explicit elapsed duration rather than reading the
 * clock itself, so a tile can drive it from the Grid's shared 1s tick. That is
 * also what makes it testable at all under the node-environment unit suite.
 */

const S = 1000
const M = 60 * S
const H = 60 * M
const D = 24 * H

describe('formatCompactAge', () => {
  it('crosses each unit boundary exactly once', () => {
    expect(formatCompactAge(0)).toBe('now')
    expect(formatCompactAge(999)).toBe('now')
    expect(formatCompactAge(1 * S)).toBe('1s')
    expect(formatCompactAge(59 * S)).toBe('59s')
    expect(formatCompactAge(60 * S)).toBe('1m')
    expect(formatCompactAge(59 * M)).toBe('59m')
    expect(formatCompactAge(60 * M)).toBe('1h')
    expect(formatCompactAge(23 * H)).toBe('23h')
    expect(formatCompactAge(24 * H)).toBe('1d')
    expect(formatCompactAge(9 * D)).toBe('9d')
  })

  it('truncates rather than rounds, so a value never reads older than it is', () => {
    expect(formatCompactAge(119 * S)).toBe('1m')
    expect(formatCompactAge(90 * M)).toBe('1h')
  })

  it('renders a future instant as "now", never as a negative age', () => {
    // Reachable with no platform bug at all: `started_at` comes from the server
    // and `now` from the browser, so a fast client clock puts a fresh row in
    // the future. This is the floor that holds even when the `Date`-header skew
    // correction is unavailable.
    expect(formatCompactAge(-1)).toBe('now')
    expect(formatCompactAge(-5 * M)).toBe('now')
  })

  it('degrades visibly on a non-number rather than printing NaN', () => {
    expect(formatCompactAge(NaN)).toBe('—')
    expect(formatCompactAge(undefined)).toBe('—')
    expect(formatCompactAge(null)).toBe('—')
    expect(formatCompactAge(Infinity)).toBe('—')
  })
})

describe('serverSkewMs', () => {
  const client = Date.UTC(2026, 7, 12, 12, 0, 0)

  it('measures the offset from the HTTP Date header', () => {
    // Positive = the server is ahead, i.e. the browser is slow.
    expect(serverSkewMs('Wed, 12 Aug 2026 12:03:00 GMT', client)).toBe(3 * M)
    expect(serverSkewMs('Wed, 12 Aug 2026 11:57:00 GMT', client)).toBe(-3 * M)
  })

  it('treats sub-2s offsets as zero — that is header granularity, not skew', () => {
    expect(serverSkewMs('Wed, 12 Aug 2026 12:00:01 GMT', client)).toBe(0)
    expect(serverSkewMs('Wed, 12 Aug 2026 11:59:59 GMT', client)).toBe(0)
  })

  it('does NOT clamp a large offset away — that is the case that needs it', () => {
    expect(serverSkewMs('Wed, 12 Aug 2026 15:00:00 GMT', client)).toBe(3 * H)
  })

  it('falls back to 0 (uncorrected) on a missing or unreadable header', () => {
    expect(serverSkewMs(undefined, client)).toBe(0)
    expect(serverSkewMs(null, client)).toBe(0)
    expect(serverSkewMs('', client)).toBe(0)
    expect(serverSkewMs('not a date', client)).toBe(0)
    expect(serverSkewMs('Wed, 12 Aug 2026 12:03:00 GMT', NaN)).toBe(0)
  })
})

// Release testing (B7, finding 8 / #3568): "1 minutes ago" / "1 hours ago" /
// "1 days ago". Two copies of the helper carry the same prose — the shared util
// and the composable AgentHeader reads — so both are held to the same rule.
describe.each([
  ['utils/timestamps', formatRelativeTime],
  ['composables/useFormatters', useFormatters().formatRelativeTime],
])('formatRelativeTime (%s)', (_name, fmt) => {
  const NOW = Date.parse('2026-10-11T12:00:00Z')
  const ago = (ms) => new Date(NOW - ms).toISOString()
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })
  afterEach(() => { vi.useRealTimers() })

  it('uses the singular for exactly one unit', () => {
    expect(fmt(ago(1 * M))).toBe('1 minute ago')
    expect(fmt(ago(1 * M + 59 * S))).toBe('1 minute ago')
    expect(fmt(ago(1 * H))).toBe('1 hour ago')
    expect(fmt(ago(1 * D))).toBe('1 day ago')
  })

  it('keeps the plural for every other count', () => {
    expect(fmt(ago(30 * S))).toBe('just now')
    expect(fmt(ago(2 * M))).toBe('2 minutes ago')
    expect(fmt(ago(59 * M))).toBe('59 minutes ago')
    expect(fmt(ago(2 * H))).toBe('2 hours ago')
    expect(fmt(ago(6 * D))).toBe('6 days ago')
  })
})
