/**
 * Scanline data-loading phase machine (trinity-enterprise#245).
 *
 * Pure decision table for the app's default data-loading motion
 * (docs/memory/design-system.md §6): ScanlineReveal.vue owns all DOM wiring
 * (matchMedia, timers, animation events) and delegates every phase decision
 * here so the rules are unit-testable under the node-env vitest setup.
 *
 * Phases:
 *   loading   — no data yet; beam sweeps the dimmed track
 *   revealing — data just arrived; one final pass wipes the content in
 *   loaded    — content shown; no animation DOM
 *
 * Rules (§6, all mandatory):
 *   - false at init ⇒ loaded (cache hits skip the animation entirely)
 *   - a rising `loading` edge re-enters `loading` from ANY phase
 *     (error-then-retry re-sets store state; a mid-reveal refetch restarts)
 *   - loading → false plays the reveal only for real data (`reveal`) and
 *     never under reduced motion; error/empty terminals snap to loaded
 *   - once loaded, further falling edges are no-ops (background refresh is
 *     invisible)
 *   - a duplicate reveal-end (two animations end together and events bubble)
 *     is a no-op
 *
 * Import-pure: no window/matchMedia/timer access — node imports must not throw.
 */

export const PHASE_LOADING = 'loading'
export const PHASE_REVEALING = 'revealing'
export const PHASE_LOADED = 'loaded'

// Belt for lost animationend events (hidden/throttled tabs, KeepAlive
// deactivation, animation cancel): the component force-advances a stuck
// `revealing` phase after this many ms. Slightly above the 550ms CSS pass.
export const REVEAL_FALLBACK_MS = 700

/** Phase at component setup. Synchronous — a cache hit mounts straight into
 *  `loaded` and never flashes a track (tiles remount on every grid pan/zoom). */
export function initialPhase(loading) {
  return loading ? PHASE_LOADING : PHASE_LOADED
}

/**
 * Phase after the `loading` prop changes.
 * @param {string} phase current phase
 * @param {boolean} loading new prop value
 * @param {{reveal?: boolean, reducedMotion?: boolean}} opts
 *   reveal=false ⇒ the terminal is error/empty — snap, no celebratory pass.
 */
export function onLoadingChange(phase, loading, { reveal = true, reducedMotion = false } = {}) {
  if (loading) return PHASE_LOADING
  if (phase === PHASE_LOADING) {
    return reveal && !reducedMotion ? PHASE_REVEALING : PHASE_LOADED
  }
  return phase
}

/** Phase after the reveal pass ends (animationend/cancel, fallback timer,
 *  or KeepAlive re-activation). Only ever advances `revealing`. */
export function onRevealEnd(phase) {
  return phase === PHASE_REVEALING ? PHASE_LOADED : phase
}
