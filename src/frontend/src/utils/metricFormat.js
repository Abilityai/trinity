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
 * The folded sparkline series for a metric entry: the first dimension series'
 * buckets, numeric values only. `[]` when there is nothing chartable, so the
 * caller's `length > 1` gate is the only place that decides to render.
 */
export function sparklinePoints(metric = {}) {
  const series = Array.isArray(metric.series) ? metric.series : []
  if (!series.length) return []
  const buckets = Array.isArray(series[0].buckets) ? series[0].buckets : []
  return buckets.map((b) => b.value).filter((v) => typeof v === 'number' && Number.isFinite(v))
}

/** A sparkline's y-max: the series peak, never 0 (uPlot draws nothing at 0). */
export function sparklineMax(points, stats) {
  const fromStats = typeof stats?.max === 'number' ? stats.max : null
  const peak = fromStats ?? points.reduce((m, v) => (v > m ? v : m), 0)
  return peak > 0 ? peak : 1
}
