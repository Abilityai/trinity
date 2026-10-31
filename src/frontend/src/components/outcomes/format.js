/** Formatting for the Outcomes tab (trinity-enterprise#941). */
export const pct = (v) => (typeof v === 'number' ? `${Math.round(v * 100)}%` : '—')
export const num = (v) => (typeof v === 'number' ? String(v) : '—')
export const hours = (v) => {
  if (typeof v !== 'number') return '—'
  return v >= 48 ? `${Math.round(v / 24)}d` : `${Math.round(v)}h`
}
export const usd = (v) => (typeof v === 'number' ? `$${v.toFixed(2)}` : '—')

export const STATUS_VARIANT = {
  proposed: 'info',
  accepted: 'primary',
  rejected: 'neutral',
  shipped: 'success',
}

/** Before/after as one line: "1 → 0 (moved as expected)". */
export function effectLine(p) {
  const b = p?.before?.value
  const a = p?.after?.value
  if (!p?.after) return `before ${typeof b === 'number' ? b : '—'}`
  const verdict = p.after.moved_as_expected === true ? 'moved as expected'
    : p.after.moved_as_expected === false ? 'did not move as expected' : 'no data after shipping'
  return `${typeof b === 'number' ? b : '—'} → ${typeof a === 'number' ? a : '—'} (${verdict})`
}

/**
 * A week-over-week change as a signed string in the unit of the value:
 * percentages in points, counts and hours as they are, money in dollars.
 * `null` when either side is missing. `lowerIsBetter` picks the tone.
 */
export function weekDelta(now, before, { kind = 'count', lowerIsBetter = false } = {}) {
  if (typeof now !== 'number' || typeof before !== 'number') return null
  const d = now - before
  if (d === 0) return { text: 'no change', tone: 'text-gray-500 dark:text-gray-400' }
  const good = lowerIsBetter ? d < 0 : d > 0
  const tone = good
    ? 'text-status-success-700 dark:text-status-success-300'
    : 'text-status-danger-700 dark:text-status-danger-300'
  const sign = d > 0 ? '+' : '−'
  const mag = Math.abs(d)
  const text = kind === 'pct' ? `${sign}${Math.round(mag * 100)} pts`
    : kind === 'usd' ? `${sign}$${mag.toFixed(2)}`
      : kind === 'hours' ? `${sign}${hours(mag)}`
        : `${sign}${Math.round(mag * 10) / 10}`
  return { text: `${text} vs last week`, tone }
}
