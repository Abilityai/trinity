/**
 * Pure decisions for the fleet-benchmark card (ent#12 → ent#190).
 *
 * The backend's `GET /api/enterprise/telemetry/benchmark` answers one of five
 * statuses — `not_sharing | pending | not_enough_data | ready | unavailable` —
 * with `message` as the single home of the operator prose, `reason` naming the
 * class, and (for `ready`) a `metrics` block projected from the hosted
 * benchmark service: three documented metrics × {value, percentile, fleet_p25,
 * fleet_median, fleet_p75, n}, any of which may be null.
 *
 * Everything the card decides lives here so it is unit-testable under the
 * node-only vitest config (`vitest.config.js` pins `environment: 'node'`, no
 * component mounting). The card renders what these functions return and adds
 * nothing of its own; the two lines that feed it are eyeballed on the local
 * stack. A backend older than the gitlink bump still answers the legacy
 * `pending_hosted_service` status with an `installation_id` — that shape
 * lands in the `message` branch, so the card keeps rendering during the
 * pre-bump window.
 */

const EM_DASH = '—'

export const METRIC_ROWS = [
  { key: 'execution_success_rate', label: 'Execution success rate', kind: 'percent' },
  { key: 'executions_per_day', label: 'Executions per day', kind: 'rate' },
  { key: 'agents', label: 'Agents', kind: 'count' },
]

function isObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * Which rendering branch the card takes. `none` = no benchmark document at all
 * (the card is not rendered); `ready` = the metrics table; `not_enough_data` =
 * the floor copy; `message` = everything else, including every legacy status.
 */
export function benchmarkBranch(benchmark) {
  if (!isObject(benchmark)) return 'none'
  if (benchmark.status === 'ready' && isObject(benchmark.metrics)) return 'ready'
  if (benchmark.status === 'not_enough_data') return 'not_enough_data'
  return 'message'
}

/** The card's tint: sharing (primary tint) or off (chrome). Coloured by the
 *  EFFECTIVE `sharing_enabled` the backend reports, never by status alone. */
export function benchmarkTone(benchmark) {
  return isObject(benchmark) && benchmark.sharing_enabled === true ? 'sharing' : 'off'
}

/** Ordinal for a 0..100 percentile: 71.4 → "71st". */
export function ordinal(n) {
  const v = Math.round(n)
  const mod100 = v % 100
  if (mod100 >= 11 && mod100 <= 13) return `${v}th`
  switch (v % 10) {
    case 1: return `${v}st`
    case 2: return `${v}nd`
    case 3: return `${v}rd`
    default: return `${v}th`
  }
}

export function formatMetricValue(kind, value) {
  if (!isFiniteNumber(value)) return EM_DASH
  if (kind === 'percent') return `${Math.round(value * 100)}%`
  if (kind === 'rate') return value.toFixed(1)
  return String(Math.round(value))
}

export function formatPercentile(p) {
  if (!isFiniteNumber(p)) return EM_DASH
  return `${ordinal(Math.min(Math.max(p, 0), 100))} percentile`
}

/**
 * The three table rows, always all three, each cell already a string. A
 * missing or malformed metric renders as dashes, never as a missing row
 * (principle 4: one footprint whatever the data).
 */
export function metricRows(metrics) {
  const src = isObject(metrics) ? metrics : {}
  return METRIC_ROWS.map(({ key, label, kind }) => {
    const m = isObject(src[key]) ? src[key] : {}
    return {
      key,
      label,
      you: formatMetricValue(kind, m.value),
      fleetMedian: formatMetricValue(kind, m.fleet_median),
      percentile: formatPercentile(m.percentile),
      n: isFiniteNumber(m.n) ? String(Math.round(m.n)) : EM_DASH,
    }
  })
}

/** "Compared against 7 instances that shared in the last 45 days", or null. */
export function participantsLine(benchmark) {
  if (!isObject(benchmark)) return null
  const { participants, window_days: window } = benchmark
  if (!isFiniteNumber(participants) || !isFiniteNumber(window)) return null
  const noun = participants === 1 ? 'instance' : 'instances'
  return `Compared against ${participants} ${noun} that shared in the last ${window} days`
}

/**
 * "Based on your share from 3 hours ago (1-day window, heartbeat)". The
 * relative formatter is injected so the decision stays clock-free in tests;
 * the card passes `formatRelativeTime` and puts the absolute timestamp in the
 * element's title (principle 22).
 */
export function basedOnLine(basedOn, formatRelative) {
  if (!isObject(basedOn) || typeof basedOn.shared_at !== 'string' || !basedOn.shared_at) return null
  const when = typeof formatRelative === 'function' ? formatRelative(basedOn.shared_at) : basedOn.shared_at
  const parts = []
  if (isFiniteNumber(basedOn.window_days)) {
    parts.push(`${basedOn.window_days}-day window`)
  }
  if (typeof basedOn.backfill === 'boolean') {
    parts.push(basedOn.backfill ? 'backfill' : 'heartbeat')
  }
  const detail = parts.length ? ` (${parts.join(', ')})` : ''
  return `Based on your share from ${when}${detail}`
}

/** The technical class shown in tertiary mono ink under an `unavailable`
 *  message; empty for every other status (the message already says it all). */
export function reasonDetail(benchmark) {
  if (!isObject(benchmark) || benchmark.status !== 'unavailable') return ''
  return typeof benchmark.reason === 'string' ? benchmark.reason : ''
}
