/**
 * ent#190 — the fleet-benchmark card's decisions, all pure.
 *
 * The card (`components/settings/FleetBenchmarkCard.vue`) renders exactly what
 * these functions return. The backend's response contract is the enterprise
 * `FleetBenchmark` model; the fixtures below are that shape, plus the legacy
 * pre-bump shape (`pending_hosted_service` + `installation_id`) which must keep
 * rendering through the `message` branch while the public PR's checks run
 * against an older backend.
 */
import { describe, it, expect } from 'vitest'
import {
  METRIC_ROWS,
  benchmarkBranch,
  benchmarkTone,
  ordinal,
  formatMetricValue,
  formatPercentile,
  metricRows,
  participantsLine,
  basedOnLine,
  reasonDetail,
} from '../../src/components/settings/benchmarkFormat'

const READY = {
  sharing_enabled: true, hosted_ready: true, status: 'ready', reason: null,
  message: 'Your fleet benchmarks, against 7 instances that shared in the last 45 days.',
  receiver_status: 'ready', participants: 7, min_participants: 5, window_days: 45,
  computed_at: '2026-09-07T13:15:45.268Z',
  based_on: { shared_at: '2026-09-06T10:00:00.000Z', window_days: 1, backfill: false },
  metrics: {
    execution_success_rate: { value: 0.95, percentile: 71.4, fleet_p25: 0.8, fleet_median: 0.91, fleet_p75: 0.97, n: 7 },
    executions_per_day: { value: 12.5, percentile: 50, fleet_p25: 3, fleet_median: 9, fleet_p75: 20, n: 7 },
    agents: { value: 3, percentile: 35.7, fleet_p25: 2, fleet_median: 4, fleet_p75: 8, n: 7 },
  },
}
const LEGACY = {
  installation_id: '8e64e3be-ecd1-477d-8d53-02187708c006', sharing_enabled: false, hosted_ready: false,
  status: 'not_sharing', message: 'Fleet benchmarks are computed only for instances that share anonymous usage.',
  benchmark_url: null, percentiles: null,
}
const LEGACY_PENDING = { ...LEGACY, sharing_enabled: true, status: 'pending_hosted_service', message: "You're sharing — benchmarks will appear here once the hosted benchmark service is live." }

describe('benchmarkBranch', () => {
  it('routes each contract status to one branch and everything else to the message', () => {
    expect(benchmarkBranch(READY)).toBe('ready')
    expect(benchmarkBranch({ ...READY, status: 'not_enough_data', metrics: null })).toBe('not_enough_data')
    expect(benchmarkBranch({ ...READY, status: 'pending', metrics: null })).toBe('message')
    expect(benchmarkBranch({ ...READY, status: 'unavailable', reason: 'http_503', metrics: null })).toBe('message')
    expect(benchmarkBranch(LEGACY)).toBe('message')
    expect(benchmarkBranch(LEGACY_PENDING)).toBe('message')
  })
  it('never takes the ready branch without a metrics object', () => {
    expect(benchmarkBranch({ ...READY, metrics: null })).toBe('message')
    expect(benchmarkBranch({ ...READY, metrics: [] })).toBe('message')
    expect(benchmarkBranch({ ...READY, metrics: 'x' })).toBe('message')
  })
  it('is "none" for no document at all', () => {
    expect(benchmarkBranch(null)).toBe('none')
    expect(benchmarkBranch(undefined)).toBe('none')
    expect(benchmarkBranch('ready')).toBe('none')
    expect(benchmarkBranch([])).toBe('none')
  })
})

describe('benchmarkTone', () => {
  it('follows the effective sharing_enabled flag, not the status', () => {
    expect(benchmarkTone(READY)).toBe('sharing')
    expect(benchmarkTone({ ...READY, status: 'unavailable' })).toBe('sharing')
    expect(benchmarkTone(LEGACY)).toBe('off')
    expect(benchmarkTone({ status: 'ready', sharing_enabled: 'yes' })).toBe('off')
    expect(benchmarkTone(null)).toBe('off')
  })
})

describe('ordinal + formatPercentile', () => {
  it('spells English ordinals including the teens', () => {
    expect([0, 1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 71.4, 100, 111, 112].map(ordinal)).toEqual(
      ['0th', '1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '71st', '100th', '111th', '112th'],
    )
  })
  it('formats a percentile and dashes anything that is not a finite number', () => {
    expect(formatPercentile(71.4)).toBe('71st percentile')
    expect(formatPercentile(0)).toBe('0th percentile')
    expect(formatPercentile(140)).toBe('100th percentile')
    expect(formatPercentile(-5)).toBe('0th percentile')
    expect(formatPercentile(null)).toBe('—')
    expect(formatPercentile('71')).toBe('—')
    expect(formatPercentile(NaN)).toBe('—')
  })
})

describe('formatMetricValue', () => {
  it('formats by kind', () => {
    expect(formatMetricValue('percent', 0.95)).toBe('95%')
    expect(formatMetricValue('percent', 0.9567)).toBe('96%')
    expect(formatMetricValue('rate', 12.5)).toBe('12.5')
    expect(formatMetricValue('rate', 9)).toBe('9.0')
    expect(formatMetricValue('count', 3)).toBe('3')
    expect(formatMetricValue('count', 2.5)).toBe('3')
  })
  it('dashes null, strings, booleans and non-finite numbers', () => {
    for (const bad of [null, undefined, '0.95', true, NaN, Infinity]) {
      expect(formatMetricValue('percent', bad)).toBe('—')
    }
  })
})

describe('metricRows', () => {
  it('returns all three documented rows, formatted', () => {
    const rows = metricRows(READY.metrics)
    expect(rows.map((r) => r.key)).toEqual(METRIC_ROWS.map((r) => r.key))
    expect(rows[0]).toEqual({ key: 'execution_success_rate', label: 'Execution success rate', you: '95%', fleetMedian: '91%', percentile: '71st percentile', n: '7' })
    expect(rows[1]).toEqual({ key: 'executions_per_day', label: 'Executions per day', you: '12.5', fleetMedian: '9.0', percentile: '50th percentile', n: '7' })
    expect(rows[2]).toEqual({ key: 'agents', label: 'Agents', you: '3', fleetMedian: '4', percentile: '36th percentile', n: '7' })
  })
  it('keeps the footprint when a metric is null, missing or malformed', () => {
    const rows = metricRows({ execution_success_rate: null, executions_per_day: 'x', agents: { value: null, percentile: null, n: 0 } })
    expect(rows).toHaveLength(3)
    expect(rows[0]).toMatchObject({ you: '—', fleetMedian: '—', percentile: '—', n: '—' })
    expect(rows[1]).toMatchObject({ you: '—', fleetMedian: '—', percentile: '—', n: '—' })
    expect(rows[2]).toMatchObject({ you: '—', fleetMedian: '—', percentile: '—', n: '0' })
    expect(metricRows(null)).toHaveLength(3)
    expect(metricRows(undefined)).toHaveLength(3)
  })
})

describe('participantsLine', () => {
  it('reads the receiver numbers and pluralises', () => {
    expect(participantsLine(READY)).toBe('Compared against 7 instances that shared in the last 45 days')
    expect(participantsLine({ participants: 1, window_days: 45 })).toBe('Compared against 1 instance that shared in the last 45 days')
  })
  it('is null when a number is missing or not a number', () => {
    expect(participantsLine({ participants: null, window_days: 45 })).toBeNull()
    expect(participantsLine({ participants: 7, window_days: '45' })).toBeNull()
    expect(participantsLine(LEGACY)).toBeNull()
    expect(participantsLine(null)).toBeNull()
  })
})

describe('basedOnLine', () => {
  const rel = (ts) => `relative(${ts})`
  it('names the share the percentiles were computed from', () => {
    expect(basedOnLine(READY.based_on, rel)).toBe('Based on your share from relative(2026-09-06T10:00:00.000Z) (1-day window, heartbeat)')
    expect(basedOnLine({ shared_at: '2026-09-06T10:00:00.000Z', window_days: 30, backfill: true }, rel)).toBe('Based on your share from relative(2026-09-06T10:00:00.000Z) (30-day window, backfill)')
  })
  it('drops the parenthetical when the details are null, and falls back to the raw timestamp without a formatter', () => {
    expect(basedOnLine({ shared_at: '2026-09-06T10:00:00.000Z', window_days: null, backfill: null }, rel)).toBe('Based on your share from relative(2026-09-06T10:00:00.000Z)')
    expect(basedOnLine({ shared_at: '2026-09-06T10:00:00.000Z' })).toBe('Based on your share from 2026-09-06T10:00:00.000Z')
  })
  it('is null without a shared_at', () => {
    expect(basedOnLine(null, rel)).toBeNull()
    expect(basedOnLine({ shared_at: '' }, rel)).toBeNull()
    expect(basedOnLine({ shared_at: 5 }, rel)).toBeNull()
    expect(basedOnLine('2026-09-06', rel)).toBeNull()
  })
})

describe('reasonDetail', () => {
  it('shows the class only for unavailable', () => {
    expect(reasonDetail({ status: 'unavailable', reason: 'http_503' })).toBe('http_503')
    expect(reasonDetail({ status: 'unavailable', reason: null })).toBe('')
    expect(reasonDetail({ status: 'pending', reason: 'first_share_pending' })).toBe('')
    expect(reasonDetail(READY)).toBe('')
    expect(reasonDetail(null)).toBe('')
  })
})

describe('the legacy shape keeps rendering through the pre-bump window', () => {
  it('routes to the message branch, off tint, no numbers, no detail', () => {
    for (const doc of [LEGACY, LEGACY_PENDING]) {
      expect(benchmarkBranch(doc)).toBe('message')
      expect(participantsLine(doc)).toBeNull()
      expect(reasonDetail(doc)).toBe('')
      expect(typeof doc.message).toBe('string')
    }
    expect(benchmarkTone(LEGACY)).toBe('off')
    expect(benchmarkTone(LEGACY_PENDING)).toBe('sharing')
  })
})
