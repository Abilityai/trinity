/**
 * Words for the anomaly-detection panel (trinity-enterprise#74). The API sends
 * metric ids and numbers; every label a person reads is here.
 */

export const METRIC_LABELS = {
  duration_ms: 'Duration',
  cost: 'Cost',
  context_used: 'Context used',
  tool_calls: 'Tool calls',
  success_rate: 'Success rate',
}

export function fmtDuration(ms) {
  if (typeof ms !== 'number') return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

export function fmtMetric(metric, v) {
  if (typeof v !== 'number') return '—'
  switch (metric) {
    case 'duration_ms': return fmtDuration(v)
    case 'cost': return `$${v.toFixed(2)}`
    case 'context_used': return v >= 1000 ? `${Math.round(v / 1000)}k` : String(Math.round(v))
    case 'success_rate': return `${Math.round(v * 100)}%`
    default: return String(Math.round(v))
  }
}

export function fmtChange(m) {
  if (m.metric === 'success_rate') {
    return `${m.failures} of ${m.window} failed`
  }
  return typeof m.ratio === 'number' ? `${m.ratio.toFixed(1)}×` : '—'
}

export const TREND_TEXT = { rising: '↗ rising', falling: '↘ falling', flat: '→ flat' }

/** The badge for one schedule's baseline state, or null for not_watched. */
export function stateBadge(s, minSamples) {
  switch (s?.state) {
    case 'on_baseline':
      return { variant: 'success', icon: '✓', label: 'On baseline' }
    case 'off_baseline': {
      const n = s.incident?.occurrences || 1
      return { variant: 'warning', icon: '⚠', label: `Off baseline · ${n} ${n === 1 ? 'run' : 'runs'}` }
    }
    case 'learning':
      return { variant: 'neutral', icon: '◌', label: `Learning · ${s.baseline_runs} of ${minSamples} runs` }
    default:
      return null
  }
}

export function shortDate(iso) {
  const t = iso ? new Date(iso) : null
  return t && !Number.isNaN(t.getTime())
    ? t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : '—'
}
