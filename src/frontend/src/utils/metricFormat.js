/**
 * Metric rendering rules — one formatter for the declared-metric tiles and the
 * `dashboard.yaml` widgets (ent#479 C8).
 *
 * `DashboardPanel.vue` grew its own `formatValue` / `getStatusColors` /
 * `getProgressBarColor` / `getTrendColor` / `getSparklineColor` when the
 * dashboard was the only surface that rendered a number. ent#479 adds a second
 * surface (`DeclaredMetricsTiles.vue`) over the SAME registry types, so the
 * maps are lifted here and both import them: two copies of "what colour is a
 * status widget" is how a bound widget and its tile end up disagreeing about
 * the same point.
 *
 * Everything in here is pure — no Vue, no store, no clock except what the
 * caller passes in — so it is unit-testable without a DOM.
 */

/** The registry's closed type enum (`services/template_metrics.METRIC_TYPES`). */
export const METRIC_TYPES = ['counter', 'gauge', 'percentage', 'status', 'duration', 'bytes']

/** Types whose value is a number the tile may chart, colour and compare. */
const NUMERIC_TYPES = new Set(['counter', 'gauge', 'percentage', 'duration', 'bytes'])

export function isNumericType(type) {
  return NUMERIC_TYPES.has(type)
}

/**
 * A value rendered the way its declared TYPE says it should be read.
 *
 * The dashboard's original `formatValue` only knew "number → locale string",
 * which reads a duration of 5 400 as "5,400" and 1 887 436 800 bytes as a phone
 * number. The registry already carries the type, so use it.
 *
 * Returns the em dash for an absent value — never `0`, never the empty string,
 * both of which are claims about the metric rather than about the reading.
 */
export function formatMetricValue(value, type) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  if (type === 'duration') return formatDuration(value)
  if (type === 'bytes') return formatBytes(value)
  if (type === 'percentage') return `${trim(value)}`
  return value.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

/** The unit suffix to print next to the value, or '' when the type carries it. */
export function metricUnitSuffix(value, type, unit) {
  if (value === null || value === undefined) return ''
  if (type === 'percentage') return '%'
  if (type === 'duration' || type === 'bytes') return ''  // baked into the value
  return unit || ''
}

/** Seconds → `45s` · `12m 30s` · `3h 15m` · `2d 4h`. */
export function formatDuration(seconds) {
  const s = Math.abs(seconds)
  if (s < 60) return `${trim(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`
}

/** Bytes → `940 B` · `1.2 KB` · `17.4 MB` (1024-based, the ops convention). */
export function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let v = bytes
  let i = 0
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${trim(i === 0 ? v : Number(v.toFixed(1)))} ${units[i]}`
}

function trim(n) {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)))
}

/**
 * Status-badge classes for a declared `values[].color` (or a widget's `color`).
 * Lifted verbatim from `DashboardPanel.vue` so the two surfaces cannot drift.
 */
export function statusBadgeClasses(color) {
  const colorMap = {
    green: 'bg-status-success-100 text-status-success-800 dark:bg-status-success-900/50 dark:text-status-success-300',
    red: 'bg-status-danger-100 text-status-danger-800 dark:bg-status-danger-900/50 dark:text-status-danger-300',
    yellow: 'bg-status-warning-100 text-status-warning-800 dark:bg-status-warning-900/50 dark:text-status-warning-300',
    gray: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
    blue: 'bg-status-info-100 text-status-info-800 dark:bg-status-info-900/50 dark:text-status-info-300',
    orange: 'bg-status-urgent-100 text-status-urgent-800 dark:bg-status-urgent-900/50 dark:text-status-urgent-300',
    purple: 'bg-accent-purple-100 text-accent-purple-800 dark:bg-accent-purple-900/50 dark:text-accent-purple-300'
  }
  return colorMap[color] || colorMap.gray
}

/** Progress-bar fill classes for the same palette. */
export function progressBarClasses(color) {
  const colorMap = {
    green: 'bg-status-success-500',
    red: 'bg-status-danger-500',
    yellow: 'bg-status-warning-500',
    blue: 'bg-action-primary-500',
    orange: 'bg-status-urgent-500',
    purple: 'bg-accent-purple-500'
  }
  return colorMap[color] || 'bg-action-primary-500'
}

/**
 * Trend arrow colour, DIRECTION-AWARE (the tile's one real improvement on the
 * panel's version). "Up" is good for revenue and bad for error rate; the
 * registry's `direction` says which, and a metric that never declared one is
 * `neutral` — coloured as information, not as a verdict.
 */
export function trendClasses(trend, direction = 'neutral') {
  if (trend !== 'up' && trend !== 'down') return 'text-gray-500 dark:text-gray-400'
  if (direction === 'neutral' || !direction) return 'text-gray-500 dark:text-gray-400'
  const good = (direction === 'up_good' && trend === 'up')
    || (direction === 'down_good' && trend === 'down')
  return good
    ? 'text-status-success-600 dark:text-status-success-400'
    : 'text-status-danger-600 dark:text-status-danger-400'
}

/**
 * Threshold colouring for the VALUE itself.
 *
 * `warning_threshold` / `critical_threshold` are declared in the registry and
 * are read in the declared direction: for `down_good` a value ABOVE the
 * threshold is bad; for `up_good` a value BELOW it is. `neutral` declines to
 * judge — a colour would be an opinion the author did not express.
 *
 * Returns a token class or `''` (the plain ink).
 */
export function thresholdClasses(definition = {}, value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  const { direction, warning_threshold: warn, critical_threshold: crit } = definition
  if (direction !== 'up_good' && direction !== 'down_good') return ''
  const breached = (t) => {
    if (typeof t !== 'number') return false
    return direction === 'down_good' ? value >= t : value <= t
  }
  if (breached(crit)) return 'text-status-danger-600 dark:text-status-danger-400'
  if (breached(warn)) return 'text-status-warning-600 dark:text-status-warning-400'
  return ''
}

/** Hex stroke for `SparklineChart` (a canvas colour, not a class). */
export function sparklineColor(trend, direction = 'neutral') {
  if ((trend === 'up' || trend === 'down') && (direction === 'up_good' || direction === 'down_good')) {
    const good = (direction === 'up_good' && trend === 'up')
      || (direction === 'down_good' && trend === 'down')
    return good ? '#10b981' : '#ef4444'
  }
  return '#3b82f6'
}

/**
 * The freshness chip a tile shows, derived from the BACKEND's verdict — never
 * recomputed here. The stale rule has exactly one home
 * (`services/metric_read_service.freshness`, ent#479); a second copy in the
 * browser is a second rule the moment one of them is edited.
 *
 * `null` means "say nothing" (a fresh metric needs no chip; the point time
 * beside it already carries the fact).
 */
export function freshnessChip(metric = {}) {
  if (metric.freshness === 'stale' || metric.stale === true) {
    return {
      label: 'Stale',
      tone: 'bg-status-warning-100 text-status-warning-800 dark:bg-status-warning-900/50 dark:text-status-warning-300',
      title: staleTitle(metric),
    }
  }
  if (metric.freshness === 'no_cadence') {
    return {
      label: 'No cadence declared',
      tone: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
      title: 'No `cadence:` in template.yaml, so freshness cannot be judged — this metric is never marked stale.',
    }
  }
  return null
}

function staleTitle(metric) {
  const cadence = metric.cadence || (metric.cadence_seconds ? `${metric.cadence_seconds}s` : null)
  const since = metric.last_point_at ? `No point since ${metric.last_point_at}` : 'No point recorded'
  return cadence ? `${since}; expected every ${cadence}.` : `${since}.`
}

/**
 * The sparkline series for a metric entry: numeric values from `chart.buckets`.
 * `[]` when there is nothing chartable, so the caller's `length > 1` gate is
 * the only place that decides to render.
 *
 * `metric.chart` and NOT `metric.series[0]`: the backend composes one bucket
 * list that describes the same thing as `latest.value` — the cross-series fold
 * for `sum`/`avg`, the newest series for `last` — and `stats` (the trend
 * arrow) is computed from that same list. Reading `series[0]` here drew one
 * dimension's history under a total's number.
 */
export function sparklinePoints(metric = {}) {
  const buckets = Array.isArray(metric.chart?.buckets) ? metric.chart.buckets : []
  return buckets.map((b) => b.value).filter((v) => typeof v === 'number' && Number.isFinite(v))
}

/**
 * What the sparkline is drawing, when that is not every series — `null` when
 * the chart already describes the whole metric and needs no caveat.
 *
 * A cross-series fold is defined for `sum` and `avg` only; for `last` the
 * chart is one series, and a chart that shows one region under a number that
 * names several must SAY so rather than leave the operator to assume.
 */
export function chartBasisNote(metric = {}) {
  const chart = metric.chart
  if (!chart || chart.basis !== 'series' || !(chart.series_count > 1)) return null
  const dims = formatDims(chart.dims)
  return {
    label: dims ? `chart: ${dims}` : 'chart: 1 series',
    title: `A '${chart.aggregation || 'last'}' metric has no cross-series fold, so this `
      + `chart and its trend show the most recently updated of `
      + `${chart.series_count} dimension series, not all of them.`,
  }
}

/**
 * One series' dimensions as `channel=meta, geo=us` (ent#730).
 *
 * Keys in canonical (sorted) order, the order the store's series identity
 * uses (`metric_points_service.canonical_dims`). A value containing
 * whitespace, `,` or `=` is JSON-quoted so the pair stays unambiguous and on
 * one line. Mirrors the backend's `_dims_text`, which writes the same pairs
 * into a `metric_series_not_found` sentence.
 */
export function formatDims(dims) {
  if (!dims || typeof dims !== 'object') return ''
  return Object.keys(dims)
    .sort()
    .map((key) => {
      const value = String(dims[key])
      return `${key}=${/[\s,=]/.test(value) ? JSON.stringify(value) : value}`
    })
    .join(', ')
}

/**
 * The published docs section a `dims:` refusal links to. The published site,
 * not a `github.com/.../blob/main/...` URL (the `HARDENING_DOCS_URL`
 * precedent): `docs/user-docs/advanced/dynamic-dashboards.md` is published as
 * `guides/dynamic-dashboards`.
 */
export const DIMS_DOCS_URL = 'https://docs.ability.ai/guides/dynamic-dashboards#one-series-per-tile-dims'

/** The `binding_error_code`s a `dims:` selector can produce. */
export const DIMS_REFUSAL_CODES = new Set([
  'metric_series_not_found',
  'metric_dimension_undeclared',
  'metric_dimension_invalid',
])

const FOLD_WORDS = { sum: 'sum', avg: 'avg' }
const FOLD_TITLE_WORDS = { sum: 'sum', avg: 'average' }

/**
 * What a bound widget's number IS, from the backend's `bound_series` facts
 * (ent#730) — `{text, dims, tone, title}` or `null` when there is nothing to
 * qualify (one series, no selector).
 *
 * `text` is prose (sans) and `dims` is the machine `k=v` part (mono); the
 * component renders them side by side. The backend sends facts and this
 * writes the copy, so the wording can change without an API change.
 */
export function boundSeriesNote(widget = {}) {
  const bs = widget.bound_series
  if (!widget.metric || widget.bound !== true || !bs || typeof bs !== 'object') return null
  const count = bs.series_count
  const dims = formatDims(bs.dims)
  if (bs.basis === 'selected') {
    return {
      text: '',
      dims,
      tone: 'tertiary',
      title: `Only the ${dims} series of ${widget.metric}, chosen by dims in dashboard.yaml.`,
    }
  }
  if (!(count > 1)) return null
  const dimensions = Array.isArray(bs.dimensions) ? bs.dimensions : []
  const fold = bs.basis === 'folded' ? FOLD_WORDS[bs.aggregation] : undefined
  if (fold) {
    const noun = dimensions.length === 1 ? `${dimensions[0]} values` : 'series'
    const stale = bs.stale_count > 0 ? ` · ${bs.stale_count} stale` : ''
    const each = dimensions.length === 1 ? `${dimensions[0]}s` : 'series'
    return {
      text: `${fold} of ${count} ${noun}${stale}`,
      dims: '',
      tone: 'tertiary',
      title: `The ${FOLD_TITLE_WORDS[bs.aggregation]} of the latest value of each of ${count} ${each}. `
        + 'Add dims to show one.',
    }
  }
  return {
    text: dims ? `newest of ${count}: ` : `newest of ${count} series`,
    dims,
    tone: 'secondary',
    title: `This tile shows the most recently updated of its ${count} series, not all of them. `
      + 'Add dims to pin one.',
  }
}

/**
 * The second line of a `metric_series_not_found` refusal, from the backend's
 * `binding_detail` facts — or `null` without them. Deterministic hints only:
 * a partial selector's real series, a casing mismatch, "no points yet", and
 * otherwise the recent values and the read window. No fuzzy matching.
 */
export function refusalHint(widget = {}) {
  const detail = widget.binding_detail
  if (!detail || typeof detail !== 'object') return null
  const selector = detail.selector || {}
  const recent = Array.isArray(detail.recent_series) ? detail.recent_series : []
  const near = Array.isArray(detail.near) ? detail.near : []
  if (near.length) {
    const missing = Object.keys(near[0]).filter((k) => !(k in selector)).sort()
    return `Exact match also needs ${missing.join(', ')}: ${formatDims(near[0])}`
  }
  const wanted = formatDims(selector).toLowerCase()
  const cased = recent.find((dims) => formatDims(dims).toLowerCase() === wanted)
  if (cased && formatDims(cased) !== formatDims(selector)) {
    return `Did you mean ${formatDims(cased)}?`
  }
  if (!recent.length) return 'This metric has no points yet.'
  const keys = Object.keys(selector)
  const single = keys.length === 1
    && recent.every((dims) => dims && Object.keys(dims).length === 1 && keys[0] in dims)
  const noun = single ? keys[0] : 'series'
  // One declared key: list the values alone ("meta, google"), not "channel=meta".
  const shown = recent.slice(0, 2)
    .map((dims) => (single ? formatDims(dims).slice(keys[0].length + 1) : formatDims(dims)))
  const rest = recent.length - shown.length + (detail.more || 0)
  const list = `${shown.join(single ? ', ' : '; ')}${rest > 0 ? `, +${rest}` : ''}`
  const lead = `Check the selector or confirm this ${noun} reports. Recent ${noun}: ${list}`
  if (detail.series_cap) {
    return `${lead}. Only the ${detail.series_cap} newest series are read, so this one may be outside them.`
  }
  return `${lead} (among the ${detail.window_points} newest points; a ${noun} that reports rarely `
    + 'can fall outside them).'
}

const VERDICTS = {
  critical: { variant: 'danger', label: 'Critical', reserved: false },
  warning: { variant: 'warning', label: 'Warning', reserved: false },
  // `ok` keeps the slot: an invisible placeholder of the widest label, so a
  // background poll that crosses a threshold swaps the badge in place instead
  // of adding one and shifting the layout.
  ok: { variant: 'danger', label: 'Critical', reserved: true },
}

/**
 * The threshold verdict badge on a bound `metric` tile (ent#730), from the
 * backend's typed `threshold_verdict` — `{variant, label, title, reserved}` or
 * `null`.
 *
 * `metric` tiles only: `status` and `progress` tiles already show the verdict
 * through `color`, and a badge there would say it twice. A truthy `metric` and
 * `bound === true` both, because the backend never touches an unbound widget,
 * so an author could type `bound: true` on one. The label is the word alone:
 * a number in it would change the badge's width between Warning and Critical,
 * which defeats the reserved slot; the number is in `title`.
 */
export function verdictBadge(widget = {}) {
  if (widget.type !== 'metric' || !widget.metric || widget.bound !== true) return null
  const level = widget.threshold_verdict?.level
  const base = Object.prototype.hasOwnProperty.call(VERDICTS, level) ? VERDICTS[level] : null
  if (!base) return null
  if (base.reserved) return { ...base }
  const threshold = widget.threshold_verdict.threshold
  const shown = typeof threshold === 'number'
    ? threshold.toLocaleString('en-US', { maximumFractionDigits: 2 })
    : String(threshold)
  const lowerIsBetter = widget.direction === 'down_good'
  return {
    ...base,
    title: `${widget.metric} is at or ${lowerIsBetter ? 'above' : 'below'} its ${level} threshold, `
      + `${shown} (set in template.yaml). ${lowerIsBetter ? 'Lower' : 'Higher'} is better.`,
  }
}

/** A sparkline's y-max: the series peak, never 0 (uPlot draws nothing at 0). */
export function sparklineMax(points, stats) {
  const fromStats = typeof stats?.max === 'number' ? stats.max : null
  const peak = fromStats ?? points.reduce((m, v) => (v > m ? v : m), 0)
  return peak > 0 ? peak : 1
}
