// #471 — pure display logic for subscription pressure/headroom.
// The Settings panel, AgentTile chip and AgentListPanel badge all decide
// through these functions; this spec pins the shared predicate so the three
// surfaces cannot drift on what a reading means.
import { describe, it, expect } from 'vitest'
import {
  pressureBadge,
  headroomWindowLabel,
  formatResetTime,
  usageSourceLabel,
  headroomIsFresh,
  failureKindLabel,
  isSubscriptionFunded,
} from '../../src/utils/subscriptionPressure'

describe('pressureBadge', () => {
  it('renders nothing for non-subscription agents regardless of events', () => {
    expect(pressureBadge({ auth_mode: 'api_key', failure_events_24h: 5, rate_limited_now: true })).toBeNull()
    expect(pressureBadge({ auth_mode: 'not_configured' })).toBeNull()
    expect(pressureBadge(undefined)).toBeNull()
  })

  it('renders nothing on a quiet subscription', () => {
    expect(pressureBadge({ auth_mode: 'subscription', failure_events_24h: 0, rate_limited_now: false })).toBeNull()
  })

  it('warn on 24h events without a limited-now verdict', () => {
    const b = pressureBadge({
      auth_mode: 'subscription', subscription_name: 'eugene-max',
      failure_events_24h: 3, rate_limited_now: false,
    })
    expect(b.level).toBe('warn')
    expect(b.text).toBe('sub 429s')
    expect(b.title).toContain('eugene-max')
    expect(b.title).toContain('3 failure events in 24h')
    // The 24h count alone never claims "limited now"
    expect(b.title).not.toContain('rate-limited now')
  })

  it('crit only on the one-gate rate_limited_now', () => {
    const b = pressureBadge({
      auth_mode: 'subscription', subscription_name: 's',
      failure_events_24h: 1, rate_limited_now: true, utilization_5h_pct: 100,
    })
    expect(b.level).toBe('crit')
    expect(b.text).toBe('sub limit')
    expect(b.title).toContain('rate-limited now')
    expect(b.title).toContain('5h window 100% used')
  })

  it('crit with zero 24h events still renders (fresh provider verdict)', () => {
    const b = pressureBadge({ auth_mode: 'subscription', failure_events_24h: 0, rate_limited_now: true })
    expect(b.level).toBe('crit')
  })
})

describe('headroomWindowLabel / formatResetTime', () => {
  it('labels a window with utilization and reset', () => {
    const future = new Date(Date.now() + 60 * 60 * 1000).toISOString()
    const label = headroomWindowLabel({ utilization_pct: 15, resets_at: future }, '5h')
    expect(label).toContain('15% of 5h window')
    expect(label).toContain('resets')
  })

  it('null when the window or utilization is absent', () => {
    expect(headroomWindowLabel(null, '5h')).toBeNull()
    expect(headroomWindowLabel({ resets_at: '2026-08-19T14:00:00Z' }, '5h')).toBeNull()
  })

  it('formatResetTime is null-safe and rejects garbage', () => {
    expect(formatResetTime(null)).toBeNull()
    expect(formatResetTime('not-a-date')).toBeNull()
    expect(formatResetTime(new Date().toISOString())).toBeTruthy()
  })
})

describe('usageSourceLabel / headroomIsFresh', () => {
  it('labels provider readings with age, observed as estimate', () => {
    expect(usageSourceLabel({ source: 'anthropic', headroom: { snapshot_age_seconds: 30 } }))
      .toContain('just now')
    expect(usageSourceLabel({ source: 'anthropic', headroom: { snapshot_age_seconds: 600 } }))
      .toContain('10 min ago')
    expect(usageSourceLabel({ source: 'observed' })).toContain('estimate')
    expect(usageSourceLabel(null)).toBeNull()
  })

  it('freshness requires provider source AND a young snapshot', () => {
    expect(headroomIsFresh({ source: 'anthropic', headroom: { snapshot_age_seconds: 60 } })).toBe(true)
    expect(headroomIsFresh({ source: 'anthropic', headroom: { snapshot_age_seconds: 99999 } })).toBe(false)
    expect(headroomIsFresh({ source: 'observed', headroom: { snapshot_age_seconds: 60 } })).toBe(false)
    expect(headroomIsFresh(null)).toBe(false)
  })
})

describe('failureKindLabel', () => {
  it('names kinds and surfaces unknown (pre-#471 rows) honestly', () => {
    expect(failureKindLabel({ rate_limit: 3, auth: 1 })).toBe('3 rate-limit · 1 auth')
    expect(failureKindLabel({ unknown: 2 })).toBe('2 unclassified')
    expect(failureKindLabel({ rate_limit: 0 })).toBeNull()
    expect(failureKindLabel(null)).toBeNull()
  })
})

describe('isSubscriptionFunded (Tier 0 ≈ predicate)', () => {
  it('accepts an entry or a bare mode string', () => {
    expect(isSubscriptionFunded('subscription')).toBe(true)
    expect(isSubscriptionFunded({ auth_mode: 'subscription' })).toBe(true)
    expect(isSubscriptionFunded({ auth_mode: 'api_key' })).toBe(false)
    expect(isSubscriptionFunded(undefined)).toBe(false)
  })
})
