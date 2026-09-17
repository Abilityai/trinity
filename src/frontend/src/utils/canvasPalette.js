/**
 * Series colours for agent-canvas charts (trinity-enterprise#536).
 *
 * Raw hex is unavoidable here and is not a design-system violation, for the
 * same reason `executionBuckets.js` states: these are handed to uPlot strokes
 * and bound to `backgroundColor` style properties on generated segments, and a
 * Tailwind class cannot be interpolated into either. Every entry mirrors a
 * palette shade the token layer already uses (indigo-500 = action-primary,
 * violet-400 = accent-purple, then the 400 accents the execution chart draws).
 *
 * A series that names no colour, or names one that is not a hex triplet, gets
 * one of these by position — a uPlot series with an undefined stroke draws
 * nothing at all, which reads as "no data", a claim the chart has not earned.
 */
export const CANVAS_PALETTE = Object.freeze([
  '#6366f1', // indigo-500  (action-primary)
  '#22c55e', // green-500   (status-success)
  '#f59e0b', // amber-500   (state-autonomous)
  '#60a5fa', // blue-400
  '#e879f9', // fuchsia-400
  '#2dd4bf', // teal-400
  '#fb7185', // rose-400    (state-locked)
  '#a78bfa', // violet-400  (accent-purple)
])

/** Palette colour for the i-th series, wrapping. */
export function paletteColor(i) {
  const n = CANVAS_PALETTE.length
  return CANVAS_PALETTE[((i % n) + n) % n]
}
