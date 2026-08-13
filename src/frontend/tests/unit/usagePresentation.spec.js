import { describe, expect, it } from 'vitest'
import {
  dashboardUsageText,
  shouldShowMeteredCost,
  usageBadgeLabel,
  usageDetail,
} from '@/utils/usagePresentation'

const subscription = (utilization) => ({
  billing_mode: 'subscription',
  provider: 'anthropic',
  subscription: { id: 'sub-1', name: 'studio-max', plan: 'max' },
  utilization,
})

describe('Claude subscription usage presentation', () => {
  it('shows the named subscription and suppresses dollar cost', () => {
    const usage = subscription({
      status: 'unavailable',
      percent: null,
      window: null,
      resets_at: null,
      last_updated_at: null,
      reason: 'provider_signal_unavailable',
    })

    expect(usageBadgeLabel(usage)).toBe('Claude subscription · studio-max')
    expect(dashboardUsageText(usage)).toBe('studio-max · usage unavailable')
    expect(usageDetail(usage)).toBe('Usage unavailable · Last updated: never')
    expect(shouldShowMeteredCost(usage)).toBe(false)
  })

  it('shows percentage, window, reset, and freshness when a signal exists', () => {
    const usage = subscription({
      status: 'available',
      percent: 42.5,
      window: '5-hour rolling window',
      resets_at: '2026-08-13T20:00:00Z',
      last_updated_at: '2026-08-13T18:00:00Z',
    })

    expect(dashboardUsageText(usage)).toBe('studio-max · 42.5% used')
    expect(usageDetail(usage)).toContain('42.5% of 5-hour rolling window')
    expect(usageDetail(usage)).toContain('Resets')
    expect(usageDetail(usage)).toContain('Updated')
    expect(shouldShowMeteredCost(usage)).toBe(false)
  })
})

describe('Anthropic API usage presentation', () => {
  const usage = {
    billing_mode: 'api',
    provider: 'anthropic',
    metering: 'metered',
    cost_currency: 'USD',
  }

  it('labels API usage as metered and preserves cost semantics', () => {
    expect(usageBadgeLabel(usage)).toBe('Anthropic API · metered')
    expect(dashboardUsageText(usage)).toBe('API · metered')
    expect(shouldShowMeteredCost(usage)).toBe(true)
  })
})

describe('other runtimes', () => {
  it('does not infer Anthropic API cost semantics without Claude usage data', () => {
    expect(shouldShowMeteredCost(null)).toBe(false)
    expect(dashboardUsageText(null)).toBe('Usage not configured')
  })
})
