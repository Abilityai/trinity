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
