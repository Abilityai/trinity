/**
 * Execution trigger buckets — the shared palette behind every stacked activity
 * chart (#1107 Agent Detail Overview, #2161 Workspace agent page).
 *
 * Extracted from `OverviewPanel.vue` when the Workspace agent page became a
 * second consumer: two inline copies of a nine-entry colour map is precisely the
 * shape that drifts, and the *order* below is a contract with the backend
 * (`db/schedules/analytics.py::_BUCKET_ORDER`), not a preference.
 *
 * The rationale for the ramp, carried over verbatim because it is easy to
 * "tidy" into a rainbow otherwise: an *analogous cool* ramp (indigo → violet →
 * blue → sky → cyan → teal → emerald) led by the indigo `action-primary`.
 * Deliberately no warm hues — mixing amber/rose with green/blue reads as a
 * traffic light. Soft 400-level shades keep it calm on the dark theme, so the
 * whole set reads as one family rather than a categorical rainbow. One
 * deliberate exception (#1150): Loops is a fuchsia accent, because it sits
 * stacked between Scheduled (cyan) and Agent-to-agent (teal) and telling loop
 * bursts apart from scheduled work is the entire point of that bucket.
 *
 * Raw hex is unavoidable here and is not a design-system violation: these are
 * bound to `backgroundColor` style properties on generated segments, and a
 * Tailwind class cannot be interpolated into a style binding.
 */

// Keys MUST match the backend `_BUCKET_ORDER`; a bucket missing from this map
// falls back to slate in the chart rather than rendering invisible.
export const BUCKET_COLORS = {
  'Chat/Tasks': '#6366f1',     // indigo-500  (action-primary, anchor)
  'MCP': '#a78bfa',            // violet-400  (accent-purple)
  'Channels': '#60a5fa',       // blue-400
  'Public': '#38bdf8',         // sky-400
  'Scheduled': '#22d3ee',      // cyan-400
  'Loops': '#e879f9',          // fuchsia-400 (deliberate accent, #1150)
  'Agent-to-agent': '#2dd4bf', // teal-400
  'Voice': '#34d399',          // emerald-400
  'Other': '#94a3b8',          // slate-400
}

/**
 * Stack order for a chart, from whatever the payload offers.
 *
 * The backend ships an ordered `buckets` list (`buckets_present`), and that is
 * the answer whenever it is there. The `by_type` fallback exists for one real
 * case — a cached bundle talking to a payload that predates the field — and is
 * equivalent in content, since both are built from `_BUCKET_ORDER` filtered to
 * what occurred.
 *
 * Never derive this from the per-day `by_type` maps inside `timeline`: those are
 * insertion-ordered per day, so the stack order would change shape day to day.
 */
export function bucketsForChart(stats) {
  if (Array.isArray(stats?.buckets) && stats.buckets.length) return stats.buckets
  return (stats?.by_type || [])
    .filter((b) => b && b.bucket)
    .map((b) => b.bucket)
}

/** Does this window contain anything at all? Drives the chart's empty state. */
export function hasChartActivity(stats) {
  return (stats?.timeline || []).some((d) => (d?.total || 0) > 0)
}
