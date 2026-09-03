import { describe, it, expect } from 'vitest'
import {
  PHASE_LOADING,
  PHASE_REVEALING,
  PHASE_LOADED,
  REVEAL_FALLBACK_MS,
  initialPhase,
  onLoadingChange,
  onRevealEnd,
} from '@/utils/scanlinePhase'

/**
 * Scanline phase machine (trinity-enterprise#245) — the decision table behind
 * ScanlineReveal.vue. These rules ARE the design-system §6 mandatory list:
 * cache hits skip the animation, background refresh never re-enters loading,
 * error/empty terminals snap, reduced motion skips the reveal, and a stuck
 * reveal can always be forced closed.
 */
describe('scanlinePhase', () => {
  it('is import-pure: module loads in node without touching window', () => {
    // Reaching this test at all proves the import didn't throw in the
    // node environment; assert the surface is what the component consumes.
    expect(typeof initialPhase).toBe('function')
    expect(typeof onLoadingChange).toBe('function')
    expect(typeof onRevealEnd).toBe('function')
    expect(REVEAL_FALLBACK_MS).toBeGreaterThan(550) // must outlive the CSS pass
  })

  describe('initialPhase', () => {
    it('starts loading when there is no data yet', () => {
      expect(initialPhase(true)).toBe(PHASE_LOADING)
    })

    it('cache hit mounts straight into loaded — zero animation', () => {
      // Tiles remount on every grid pan/zoom; a warm-cache remount must not
      // re-flash a loading track (§6: cache hits skip entirely).
      expect(initialPhase(false)).toBe(PHASE_LOADED)
    })
  })

  describe('onLoadingChange', () => {
    it('plays the reveal when loading ends with real data', () => {
      expect(onLoadingChange(PHASE_LOADING, false, { reveal: true })).toBe(PHASE_REVEALING)
    })

    it('reveal defaults to true', () => {
      expect(onLoadingChange(PHASE_LOADING, false)).toBe(PHASE_REVEALING)
    })

    it('snaps to loaded when loading ends without data (error/empty terminal)', () => {
      expect(onLoadingChange(PHASE_LOADING, false, { reveal: false })).toBe(PHASE_LOADED)
    })

    it('skips the reveal under reduced motion', () => {
      expect(
        onLoadingChange(PHASE_LOADING, false, { reveal: true, reducedMotion: true })
      ).toBe(PHASE_LOADED)
    })

    it('re-enters loading from loaded (error-then-retry re-sets store state)', () => {
      expect(onLoadingChange(PHASE_LOADED, true)).toBe(PHASE_LOADING)
    })

    it('restarts loading mid-reveal (rapid refetch)', () => {
      expect(onLoadingChange(PHASE_REVEALING, true)).toBe(PHASE_LOADING)
    })

    it('background refresh is invisible: a falling edge in loaded is a no-op', () => {
      // A surface that already has data is not loading (§6) — the store never
      // raises `loading` on stale-while-revalidate, and even if a consumer's
      // flag churns false again, the phase must not move.
      expect(onLoadingChange(PHASE_LOADED, false, { reveal: true })).toBe(PHASE_LOADED)
    })

    it('a falling edge mid-reveal does not restart the pass', () => {
      expect(onLoadingChange(PHASE_REVEALING, false, { reveal: true })).toBe(PHASE_REVEALING)
    })
  })

  describe('onRevealEnd', () => {
    it('advances revealing to loaded (animationend / fallback timer / onActivated)', () => {
      expect(onRevealEnd(PHASE_REVEALING)).toBe(PHASE_LOADED)
    })

    it('duplicate advance is a no-op (two animations end together and bubble)', () => {
      expect(onRevealEnd(PHASE_LOADED)).toBe(PHASE_LOADED)
    })

    it('a stray end event during loading is a no-op', () => {
      expect(onRevealEnd(PHASE_LOADING)).toBe(PHASE_LOADING)
    })
  })

  describe('full lifecycles', () => {
    it('first load with data: loading → revealing → loaded', () => {
      let phase = initialPhase(true)
      phase = onLoadingChange(phase, false, { reveal: true })
      expect(phase).toBe(PHASE_REVEALING)
      phase = onRevealEnd(phase)
      expect(phase).toBe(PHASE_LOADED)
    })

    it('error then retry then data: loading → loaded → loading → revealing → loaded', () => {
      let phase = initialPhase(true)
      phase = onLoadingChange(phase, false, { reveal: false }) // fetch failed, no data
      expect(phase).toBe(PHASE_LOADED)
      phase = onLoadingChange(phase, true) // hydrate() retries: store back to 'loading'
      expect(phase).toBe(PHASE_LOADING)
      phase = onLoadingChange(phase, false, { reveal: true })
      phase = onRevealEnd(phase)
      expect(phase).toBe(PHASE_LOADED)
    })

    it('a remount after a data-less terminal never plays a late reveal (ent#449)', () => {
      // The Executions info tile is the consumer that relies on this: the
      // chassis replaces the slot with its own message on `empty`/`error`, so
      // the tile's instance is UNMOUNTED there and a fresh one mounts at
      // `ready` with loading already false. A cache-hit mount must stay loaded
      // — a `reveal: true` arriving at a phase that never loaded is not a
      // celebration, it is a stale prop on a new instance.
      let phase = initialPhase(false)
      expect(phase).toBe(PHASE_LOADED)
      phase = onLoadingChange(phase, false, { reveal: true })
      expect(phase).toBe(PHASE_LOADED)
      expect(onRevealEnd(phase)).toBe(PHASE_LOADED)
    })
  })
})
