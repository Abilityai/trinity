/**
 * Display helpers shared by the Evaluations tab blocks (trinity-enterprise#938).
 * A rate the API returned as `null` (nothing graded) reads "—", never 0 %.
 */
export const fmtRate = (v) => (typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '—')

/** A signed delta with its unit; `null` reads "—" (no previous window to compare). */
export function fmtDelta(d, unit = 'pts') {
  if (typeof d !== 'number') return '—'
  const n = unit === 'pts' ? d.toFixed(1) : String(d)
  return `${d > 0 ? '+' : d < 0 ? '−' : '±'}${n.replace('-', '')}${unit ? ` ${unit}` : ''}`
}

/** Semantic tone for a rate delta: up is good, down is bad, flat/none is muted. */
export function deltaTone(d) {
  if (typeof d !== 'number' || d === 0) return 'text-gray-500 dark:text-gray-400'
  return d > 0
    ? 'text-status-success-700 dark:text-status-success-300'
    : 'text-status-danger-700 dark:text-status-danger-300'
}
