/**
 * `utils/metricFormat.js` — the rendering rules shared by the declared-metric
 * tiles and the bound `dashboard.yaml` widgets (ent#479).
 *
 * The lift out of `DashboardPanel.vue` exists so the two surfaces cannot
 * disagree about the same point, so what is worth asserting here is the
 * behaviour a second copy would have got wrong: type-aware values, DIRECTION-
 * aware verdicts, and the fact that freshness is read from the backend rather
 * than recomputed.
 */
import { describe, expect, it } from 'vitest'
import {
  chartBasisNote,
  formatBytes,
  formatDuration,
  formatMetricValue,
  freshnessChip,
  isNumericType,
  metricUnitSuffix,
  progressBarClasses,
  sparklineColor,
  sparklineMax,
  sparklinePoints,
  statusBadgeClasses,
  thresholdClasses,
  trendClasses,
} from '../../src/utils/metricFormat'

describe('formatMetricValue reads a value the way its declared type says to', () => {
  it('formats a plain number with thousands separators', () => {
    expect(formatMetricValue(1234567, 'counter')).toBe('1,234,567')
    expect(formatMetricValue(12.3456, 'gauge')).toBe('12.35')
  })

  it('formats a duration in time units, not as a count', () => {
    expect(formatMetricValue(45, 'duration')).toBe('45s')
    expect(formatMetricValue(5400, 'duration')).toBe('1h 30m')
    expect(formatDuration(90)).toBe('1m 30s')
    expect(formatDuration(180000)).toBe('2d 2h')
  })

  it('formats bytes in binary units', () => {
    expect(formatMetricValue(940, 'bytes')).toBe('940 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(18_253_611)).toBe('17.4 MB')
  })

  it('leaves a status value as the text the agent recorded', () => {
    expect(formatMetricValue('healthy', 'status')).toBe('healthy')
  })

  it('renders an ABSENT value as an em dash, never as zero', () => {
    // A declared metric with no points must not read as "0" — that is a
    // number the agent never recorded, and the tile would be lying.
    for (const absent of [null, undefined, '']) {
      expect(formatMetricValue(absent, 'gauge')).toBe('—')
    }
    expect(formatMetricValue(0, 'gauge')).toBe('0')
  })

  it('knows which types are chartable', () => {
    expect(isNumericType('gauge')).toBe(true)
    expect(isNumericType('status')).toBe(false)
  })
})

describe('metricUnitSuffix', () => {
  it('prints the declared unit for a plain number', () => {
    expect(metricUnitSuffix(5, 'gauge', 'USD')).toBe('USD')
  })

  it('prints % for a percentage regardless of the declared unit', () => {
    expect(metricUnitSuffix(5, 'percentage', 'pct')).toBe('%')
  })

  it('prints nothing for duration/bytes — the value already carries the unit', () => {
    expect(metricUnitSuffix(5, 'duration', 'seconds')).toBe('')
    expect(metricUnitSuffix(5, 'bytes', 'MB')).toBe('')
  })

  it('prints nothing at all when there is no value to qualify', () => {
    expect(metricUnitSuffix(null, 'gauge', 'USD')).toBe('')
  })
})

describe('trend and threshold colour read the DECLARED direction', () => {
  it('rising revenue is good, rising error rate is bad', () => {
    expect(trendClasses('up', 'up_good')).toContain('status-success')
    expect(trendClasses('up', 'down_good')).toContain('status-danger')
    expect(trendClasses('down', 'down_good')).toContain('status-success')
    expect(trendClasses('down', 'up_good')).toContain('status-danger')
  })

  it('declines to judge a neutral metric — a colour would be an opinion', () => {
    expect(trendClasses('up', 'neutral')).not.toContain('status-')
    expect(trendClasses('up', undefined)).not.toContain('status-')
    expect(trendClasses('stable', 'up_good')).not.toContain('status-')
  })

  it('breaches a down_good threshold from ABOVE and an up_good one from BELOW', () => {
    const errors = { direction: 'down_good', warning_threshold: 5, critical_threshold: 10 }
    expect(thresholdClasses(errors, 2)).toBe('')
    expect(thresholdClasses(errors, 6)).toContain('status-warning')
    expect(thresholdClasses(errors, 11)).toContain('status-danger')

    const uptime = { direction: 'up_good', warning_threshold: 99, critical_threshold: 95 }
    expect(thresholdClasses(uptime, 99.9)).toBe('')
    expect(thresholdClasses(uptime, 98)).toContain('status-warning')
    expect(thresholdClasses(uptime, 90)).toContain('status-danger')
  })

  it('never colours a neutral metric or a non-numeric value', () => {
    expect(thresholdClasses({ direction: 'neutral', critical_threshold: 1 }, 5)).toBe('')
    expect(thresholdClasses({ direction: 'down_good', critical_threshold: 1 }, 'healthy')).toBe('')
  })

  it('gives the sparkline the same verdict as the arrow', () => {
    expect(sparklineColor('up', 'up_good')).toBe('#10b981')
    expect(sparklineColor('up', 'down_good')).toBe('#ef4444')
    expect(sparklineColor('up', 'neutral')).toBe('#3b82f6')
  })
})

describe('freshnessChip reports the BACKEND verdict, it does not recompute it', () => {
  // The stale rule has one home (`metric_read_service.freshness`). The chip is
  // driven by the flags on the payload, so a tile cannot disagree with the
  // health block or a bound widget about the same metric.
  it('marks a stale metric and says what it was late against', () => {
    const chip = freshnessChip({
      freshness: 'stale', stale: true, cadence: '1h',
      last_point_at: '2026-09-20T10:00:00Z',
    })
    expect(chip.label).toBe('Stale')
    expect(chip.tone).toContain('status-warning')
    expect(chip.title).toContain('expected every 1h')
  })

  it('a metric with no declared cadence is chipped, never marked stale', () => {
    const chip = freshnessChip({ freshness: 'no_cadence', stale: null })
    expect(chip.label).toBe('No cadence declared')
    expect(chip.tone).not.toContain('status-warning')
  })

  it('says nothing about a fresh metric — the point time already does', () => {
    expect(freshnessChip({ freshness: 'fresh', stale: false })).toBeNull()
    expect(freshnessChip({ freshness: 'no_points', stale: false })).toBeNull()
  })
})

describe('sparkline inputs', () => {
  const metric = {
    chart: {
      basis: 'series',
      aggregation: 'last',
      series_count: 1,
      dims: null,
      buckets: [{ i: 0, ts: 't1', value: 1 }, { i: 1, ts: 't2', value: null }, { i: 2, ts: 't3', value: 4 }],
    },
  }

  it('takes the chart bucket values and drops the gaps', () => {
    expect(sparklinePoints(metric)).toEqual([1, 4])
  })

  it('reads `chart`, NOT `series[0]` — the chart is what `stats` describes', () => {
    // The backend composes one bucket list that matches `latest.value`: the
    // cross-series fold for `sum`/`avg`. Reading `series[0]` here drew one
    // dimension's history under a total, with the trend arrow — computed
    // from the fold — pointing at something the chart did not show.
    const folded = {
      series: [
        { dims: { region: 'eu' }, buckets: [{ i: 5, ts: 't', value: 10 }] },
        { dims: { region: 'us' }, buckets: [{ i: 5, ts: 't', value: 5 }] },
      ],
      chart: { basis: 'folded', aggregation: 'sum', series_count: 2, dims: null, buckets: [{ i: 5, ts: 't', value: 15 }] },
    }
    expect(sparklinePoints(folded)).toEqual([15])
  })

  it('is empty — not undefined — when there is nothing chartable', () => {
    expect(sparklinePoints({})).toEqual([])
    expect(sparklinePoints({ chart: null })).toEqual([])
    expect(sparklinePoints({ chart: { buckets: [] } })).toEqual([])
  })

  it('never hands uPlot a zero ceiling', () => {
    expect(sparklineMax([0, 0], null)).toBe(1)
    expect(sparklineMax([1, 4], null)).toBe(4)
    expect(sparklineMax([1, 4], { max: 9 })).toBe(9)
  })
})

describe('the chart says when it is NOT showing the whole metric', () => {
  it('captions a one-series chart under a multi-series metric', () => {
    const note = chartBasisNote({
      chart: { basis: 'series', aggregation: 'last', series_count: 3, dims: { region: 'eu' } },
    })
    expect(note.label).toContain('region=eu')
    expect(note.title).toContain('3 dimension series')
  })

  it('says nothing when the chart IS the whole metric', () => {
    // A cross-series fold describes every series, so a caveat would be noise…
    expect(chartBasisNote({
      chart: { basis: 'folded', aggregation: 'sum', series_count: 4, dims: null },
    })).toBeNull()
    // …and so does a metric with only one series to begin with.
    expect(chartBasisNote({
      chart: { basis: 'series', aggregation: 'last', series_count: 1, dims: null },
    })).toBeNull()
    expect(chartBasisNote({})).toBeNull()
  })
})

describe('the colour maps are token-only', () => {
  // The design-system contract forbids raw palette classes, and these maps are
  // the ones a second copy would have re-typed from the old panel (which had
  // `bg-blue-500` in it).
  const RAW = /\b(?:bg|text)-(?:red|green|blue|yellow|orange|purple|amber|emerald)-\d{2,3}\b/
  for (const color of ['green', 'red', 'yellow', 'gray', 'blue', 'orange', 'purple']) {
    it(`statusBadgeClasses('${color}') uses semantic tokens`, () => {
      expect(statusBadgeClasses(color)).not.toMatch(RAW)
    })
    it(`progressBarClasses('${color}') uses semantic tokens`, () => {
      expect(progressBarClasses(color)).not.toMatch(RAW)
    })
  }

  it('falls back to gray for an unknown colour rather than rendering unstyled', () => {
    expect(statusBadgeClasses('chartreuse')).toBe(statusBadgeClasses('gray'))
  })
})
