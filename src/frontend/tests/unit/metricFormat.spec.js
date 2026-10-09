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
  DIMS_DOCS_URL,
  boundSeriesNote,
  chartBasisNote,
  formatBytes,
  formatDims,
  refusalHint,
  verdictBadge,
  VERDICT_WORDS,
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

// ---------------------------------------------------------------------------
// ent#730: which series a bound widget's number is, and why a selector refused
// ---------------------------------------------------------------------------

function bound(boundSeries, extra = {}) {
  return { type: 'metric', metric: 'ad_spend', bound: true, bound_series: boundSeries, ...extra }
}

describe('formatDims spells a series the one way both surfaces read it', () => {
  it('joins k=v pairs in canonical (sorted) key order', () => {
    expect(formatDims({ geo: 'us', channel: 'meta' })).toBe('channel=meta, geo=us')
    expect(formatDims({ channel: 'meta' })).toBe('channel=meta')
    expect(formatDims(null)).toBe('')
    expect(formatDims({})).toBe('')
  })

  it('JSON-quotes a value that would make the pair ambiguous or wrap', () => {
    expect(formatDims({ channel: 'paid social' })).toBe('channel="paid social"')
    expect(formatDims({ channel: 'a,b' })).toBe('channel="a,b"')
    expect(formatDims({ channel: 'a=b' })).toBe('channel="a=b"')
    expect(formatDims({ channel: 'a\nb' })).toBe('channel="a\\nb"')
  })

  it('is what chartBasisNote uses for its label', () => {
    const note = chartBasisNote({
      chart: { basis: 'series', aggregation: 'last', series_count: 3, dims: { region: 'eu', geo: 'us' } },
    })
    expect(note.label).toBe('chart: geo=us, region=eu')
  })
})

describe('boundSeriesNote says what a bound number is', () => {
  it('names the selected series at any series count', () => {
    for (const n of [1, 3]) {
      const note = boundSeriesNote(bound({ basis: 'selected', aggregation: 'sum', series_count: n,
        dims: { channel: 'meta' }, dimensions: ['channel'] }))
      expect(note.text).toBe('')
      expect(note.dims).toBe('channel=meta')
      expect(note.title).toContain('channel=meta')
      expect(note.title).toContain('ad_spend')
    }
  })

  it('says a fold is a fold, naming the one declared dimension', () => {
    const fold = (aggregation, dimensions, stale_count) => boundSeriesNote(bound({
      basis: 'folded', aggregation, series_count: 3, dims: null, dimensions, stale_count }))
    expect(fold('sum', ['channel'], 0).text).toBe('sum of 3 channel values')
    expect(fold('avg', ['channel'], 0).text).toBe('avg of 3 channel values')
    expect(fold('sum', ['channel', 'geo'], 0).text).toBe('sum of 3 series')
    expect(fold('sum', ['channel'], 1).text).toBe('sum of 3 channel values · 1 stale')
    expect(fold('sum', ['channel'], null).text).toBe('sum of 3 channel values')
    expect(fold('sum', ['channel'], 0).dims).toBe('')
  })

  it('says a last tile over several series shows the newest, in secondary ink', () => {
    const note = boundSeriesNote(bound({ basis: 'series', aggregation: 'last', series_count: 3,
      dims: { channel: 'meta' }, dimensions: ['channel'] }))
    expect(note.text).toBe('newest of 3: ')
    expect(note.dims).toBe('channel=meta')
    expect(note.tone).toBe('secondary')
  })

  it('falls to the newest-of copy for an aggregation it does not know', () => {
    const note = boundSeriesNote(bound({ basis: 'folded', aggregation: 'median', series_count: 3,
      dims: null, dimensions: ['channel'] }))
    expect(note.text).toBe('newest of 3 series')
  })

  it('says nothing when there is nothing to qualify', () => {
    expect(boundSeriesNote(bound({ basis: 'series', aggregation: 'last', series_count: 1,
      dims: null, dimensions: [] }))).toBeNull()
    expect(boundSeriesNote(bound(undefined))).toBeNull()
    expect(boundSeriesNote({ ...bound({ basis: 'selected', series_count: 3, dims: { channel: 'meta' } }),
      bound: false })).toBeNull()
    expect(boundSeriesNote({ type: 'metric', value: 3 })).toBeNull()
  })

  it('never puts a backtick in a title', () => {
    const notes = [
      bound({ basis: 'selected', aggregation: 'sum', series_count: 3, dims: { channel: 'meta' }, dimensions: ['channel'] }),
      bound({ basis: 'folded', aggregation: 'sum', series_count: 3, dims: null, dimensions: ['channel'] }),
      bound({ basis: 'series', aggregation: 'last', series_count: 3, dims: { channel: 'meta' }, dimensions: ['channel'] }),
    ].map(boundSeriesNote)
    for (const note of notes) expect(note.title).not.toContain('`')
  })
})

describe('refusalHint writes the second line of a not-found refusal from facts', () => {
  function refused(detail) {
    return { type: 'metric', metric: 'ad_spend', bound: false,
      binding_error_code: 'metric_series_not_found', binding_detail: {
        selector: { channel: 'tiktok' }, recent_series: [], more: 0, lookback_days: 90,
        near: [], ...detail } }
  }

  it('names the dimension a partial selector is missing', () => {
    expect(refusalHint(refused({ selector: { channel: 'google' },
      recent_series: [{ channel: 'google', geo: 'us' }], near: [{ channel: 'google', geo: 'us' }] })))
      .toBe('Exact match also needs geo: channel=google, geo=us')
  })

  it('names a missing dimension even when it is called like an Object property', () => {
    // `constructor` is a valid dimension name; `in` would find it on Object.prototype.
    expect(refusalHint(refused({ selector: { channel: 'meta' },
      recent_series: [{ channel: 'meta', constructor: 'x' }],
      near: [{ channel: 'meta', constructor: 'x' }] })))
      .toBe('Exact match also needs constructor: channel=meta, constructor=x')
  })

  it('does not treat a selector key called like an Object property as present', () => {
    expect(refusalHint(refused({ selector: { constructor: 'x' },
      recent_series: [{ channel: 'meta' }, { channel: 'google' }] })))
      .toBe('Check the selector or confirm this series reports. Recent series: channel=meta; '
        + 'channel=google. This series has no point in the last 90 days.')
  })

  it('suggests a casing fix, and only a casing fix', () => {
    expect(refusalHint(refused({ selector: { channel: 'Google' },
      recent_series: [{ channel: 'meta' }, { channel: 'google' }] })))
      .toBe('Did you mean channel=google?')
    expect(refusalHint(refused({ selector: { channel: 'gogle' },
      recent_series: [{ channel: 'meta' }, { channel: 'google' }] })))
      .not.toContain('Did you mean')
  })

  it('says a metric with no points has none yet', () => {
    expect(refusalHint(refused({ recent_series: [] }))).toBe('This metric has no points yet.')
  })

  it('lists recent values and how far back it looked otherwise', () => {
    expect(refusalHint(refused({ recent_series: [{ channel: 'meta' }, { channel: 'google' },
      { channel: 'linkedin' }], more: 0 })))
      .toBe('Check the selector or confirm this channel reports. Recent channel: meta, google, +1. '
        + 'This channel has no point in the last 90 days.')
  })

  it('never blames a shared window: the selected series is read on its own (#3293)', () => {
    const hint = refusalHint(refused({ recent_series: [{ channel: 'c1' }, { channel: 'c2' }],
      more: 53 }))
    expect(hint).toContain('+53')
    expect(hint).not.toContain('newest')
    expect(hint).not.toContain('outside')
  })

  it('still reads a refusal that carries no lookback', () => {
    expect(refusalHint(refused({ recent_series: [{ channel: 'meta' }], lookback_days: undefined })))
      .toBe('Check the selector or confirm this channel reports. Recent channel: meta.')
  })

  it('says nothing without facts', () => {
    expect(refusalHint({ type: 'metric', metric: 'ad_spend', binding_error_code: 'metric_dimension_invalid' }))
      .toBeNull()
  })

  it('points at the published binding docs', () => {
    expect(DIMS_DOCS_URL).toMatch(/^https:\/\/docs\.ability\.ai\//)
  })
})

describe('verdictBadge: the threshold verdict on a bound metric tile (ent#730)', () => {
  function tile(verdict, extra = {}) {
    return { type: 'metric', metric: 'ad_spend', bound: true, direction: 'down_good',
      threshold_verdict: verdict, ...extra }
  }

  it('maps each level to a variant, a word, and a reserved flag', () => {
    expect(verdictBadge(tile({ level: 'critical', threshold: 500 })))
      .toMatchObject({ variant: 'danger', label: 'Critical', reserved: false })
    expect(verdictBadge(tile({ level: 'warning', threshold: 400 })))
      .toMatchObject({ variant: 'warning', label: 'Warning', reserved: false })
    // `ok` keeps the slot: an invisible badge whose cell already stacks every
    // verdict word, so a poll that crosses a threshold swaps it in place.
    expect(verdictBadge(tile({ level: 'ok', threshold: null })))
      .toMatchObject({ variant: 'danger', label: 'Critical', reserved: true })
  })

  it('spells each verdict word once, and every level shows one of them', () => {
    expect(VERDICT_WORDS).toEqual(['Critical', 'Warning'])
    for (const level of ['critical', 'warning', 'ok']) {
      const badge = verdictBadge(tile({ level, threshold: 1 }))
      expect(VERDICT_WORDS).toContain(badge.label)
    }
  })

  it('titles the threshold in the declared direction, never on ok', () => {
    const down = verdictBadge(tile({ level: 'critical', threshold: 1500.256 }))
    expect(down.title).toContain('at or above')
    expect(down.title).toContain('1,500.26')
    expect(down.title).toContain('Lower is better')
    const up = verdictBadge(tile({ level: 'warning', threshold: 10 }, { direction: 'up_good' }))
    expect(up.title).toContain('at or below')
    expect(up.title).toContain('Higher is better')
    expect(verdictBadge(tile({ level: 'ok', threshold: null })).title).toBeUndefined()
    for (const badge of [down, up]) expect(badge.title).not.toContain('`')
  })

  it('renders only on a bound metric tile with a known level', () => {
    const critical = { level: 'critical', threshold: 500 }
    expect(verdictBadge(tile(critical, { type: 'status' }))).toBeNull()
    expect(verdictBadge(tile(critical, { type: 'progress' }))).toBeNull()
    expect(verdictBadge(tile(critical, { metric: undefined }))).toBeNull()
    expect(verdictBadge(tile(critical, { bound: false }))).toBeNull()
    expect(verdictBadge(tile({ level: 'apocalyptic', threshold: 1 }))).toBeNull()
    expect(verdictBadge(tile(undefined))).toBeNull()
  })
})
