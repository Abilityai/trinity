<template>
  <div
    class="scanline"
    :aria-busy="phase === PHASE_LOADING ? 'true' : undefined"
    :role="announce ? 'status' : undefined"
  >
    <!-- Dimmed track: the loading face. During the reveal it is wiped OUT
         behind the beam with the complementary clip of the content wipe, so
         the background appearing behind the beam is the FINAL one from the
         first frame — when the track unmounts at `loaded` it is already
         fully clipped and nothing snaps. -->
    <div
      v-if="phase !== PHASE_LOADED"
      class="scan-track"
      :class="{ 'wiping-out': phase === PHASE_REVEALING }"
      aria-hidden="true"
    ></div>

    <!-- Content. The clip wipe lives on THIS wrapper (child-owned — never
         :slotted() on consumer DOM). Padding + negative margin extend the
         border box a few px so deliberate chart bleed (the context chart's
         edge dot renders outside its 32px box) isn't truncated mid-wipe and
         doesn't pop back when the clip is removed. clip-path hides only
         visually, so the wrapper is aria-hidden while loading in case a
         consumer renders slot content during that phase. -->
    <div
      class="scan-content"
      :class="[contentClass, { wiping: phase === PHASE_REVEALING }]"
      :aria-hidden="phase === PHASE_LOADING ? 'true' : undefined"
      @animationend="handleAnimationEnd"
      @animationcancel="handleAnimationEnd"
    >
      <slot />
    </div>

    <!-- Beam lane: full-container-width mover so translateX percentages
         resolve against the CONTAINER, not the 18px beam (§6 + ent#245
         review: beam core and wipe edge must share one 0→100% domain —
         %-of-container offsets desync them proportionally to width). -->
    <div
      v-if="phase !== PHASE_LOADED"
      class="scan-beamlane"
      :class="{ pass: phase === PHASE_REVEALING }"
      aria-hidden="true"
    >
      <span class="scan-beam"></span>
    </div>

    <span v-if="announce && phase === PHASE_LOADING" class="sr-only">Loading…</span>
  </div>
</template>

<script setup>
import { ref, watch, onUnmounted, onActivated } from 'vue'
import {
  PHASE_LOADING,
  PHASE_REVEALING,
  PHASE_LOADED,
  REVEAL_FALLBACK_MS,
  initialPhase,
  onLoadingChange,
  onRevealEnd,
} from '../utils/scanlinePhase'

/**
 * ScanlineReveal (trinity-enterprise#245) — the app's default data-loading
 * motion (docs/memory/design-system.md §6). Wrap a data-driven zone; pass
 * one boolean. While `loading`, a beam sweeps a dimmed track; when loading
 * ends with real data, one 550ms pass wipes the slot content in behind the
 * beam. Cache hits (loading=false at mount) render instantly with zero
 * animation; background refreshes (loading never rises) are invisible.
 *
 * Usage contract: ONE persistent instance per zone — swap loading/loaded
 * content inside the SLOT (`<template v-if="data">`), never as sibling
 * v-if branches around the component (a remount re-inits from
 * loading=false and the reveal never plays). The consumer sizes the zone
 * (height/min-height class) so loading and loaded share one footprint;
 * `content-class` sizes the CONTENT wrapper when the loaded content must
 * fill the zone rather than be measured by it (a full-height flex column).
 *
 * Theming: --scan-core / --scan-halo / --scan-glow / --scan-track, with
 * semantic-token defaults; override from the consumer's palette (the grid
 * sets them from --gv-*).
 */
const props = defineProps({
  // "No data yet" — never "fetch in flight". A surface that already has
  // data is not loading (§6).
  loading: { type: Boolean, required: true },
  // False when loading ended without real data (error / empty): snap to
  // loaded instead of playing the celebratory pass.
  reveal: { type: Boolean, default: true },
  // Opt-in live region for solitary surfaces. Off by default: dozens of
  // grid-tile instances must not each announce on a mass reveal.
  //
  // ⚠️ `role="status"` lands on the zone ROOT, which is an implicit
  // aria-live="polite" aria-atomic="true" region — so a zone wrapping
  // CONTENT that keeps changing (a transcript, a composer) would re-announce
  // the whole thing on every update. Pass this only for a zone whose loaded
  // content is settled.
  announce: { type: Boolean, default: false },
  // Classes for the CONTENT wrapper (#2163). `.scan-content` is the
  // primitive's own element and the consumer cannot reach it (`:deep()` is
  // forbidden here — child-owned DOM), so a zone whose loaded content must
  // fill a flex column had no hook at all. Additive: default '' leaves every
  // existing consumer byte-identical.
  contentClass: { type: [String, Array, Object], default: '' },
})

const prefersReducedMotion =
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

const phase = ref(initialPhase(props.loading))
let revealTimer = null

function clearRevealTimer() {
  if (revealTimer) {
    clearTimeout(revealTimer)
    revealTimer = null
  }
}

function setPhase(next) {
  if (next === phase.value) return
  phase.value = next
  clearRevealTimer()
  if (next === PHASE_REVEALING) {
    // Belt for lost end events (hidden tab, KeepAlive detach, cancel):
    // without it a lost event leaves the content permanently clip-hidden.
    revealTimer = setTimeout(() => setPhase(onRevealEnd(phase.value)), REVEAL_FALLBACK_MS)
  }
}

watch(
  () => props.loading,
  (loading) =>
    setPhase(
      onLoadingChange(phase.value, loading, {
        reveal: props.reveal,
        reducedMotion: prefersReducedMotion,
      })
    )
)

function handleAnimationEnd(e) {
  // Only the wipe on this element itself — slot content may run its own
  // animations and those bubble here. Never match e.animationName: Vue
  // scoped styles rename @keyframes at build, so a name comparison ships
  // green and never matches in the bundle.
  if (e.target !== e.currentTarget) return
  setPhase(onRevealEnd(phase.value))
}

// KeepAlive deactivation cancels CSS animations without end events; on
// return, force a stuck reveal closed rather than replaying it.
onActivated(() => {
  if (phase.value === PHASE_REVEALING) setPhase(onRevealEnd(phase.value))
})

onUnmounted(clearRevealTimer)
</script>

<style scoped>
.scanline {
  position: relative;
  border-radius: 4px; /* track + beam lane inherit; consumers reshape via rounded-* */
  /* Token-derived defaults (there is no global :root token var layer —
     theme() resolves at build, hence the explicit .dark overrides below).
     Consumers override these from their own palette. */
  --scan-core: theme('colors.action-primary.500');
  --scan-halo: color-mix(in srgb, var(--scan-core) 45%, transparent);
  --scan-glow: color-mix(in srgb, var(--scan-core) 60%, transparent);
  --scan-track: theme('colors.gray.200');
}
.dark .scanline {
  --scan-core: theme('colors.action-primary.400');
  --scan-track: theme('colors.gray.700');
}

.scan-track {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: var(--scan-track);
  opacity: 0.5;
}

.scan-content {
  position: relative;
  /* Bleed allowance: border box extends 4px past the zone so the wipe clip
     (and its removal at `loaded`) never truncates/pops deliberate chart
     overflow. Net-zero layout impact (padding compensates the margin).
     Deliberately NO height:100% — with global border-box sizing, 100% of the
     zone minus this padding is a SMALLER content box, silently squashing
     percentage-height slot children (the 32px context svg rendered 24px).
     Slot content that needs a definite height brings its own sized wrapper
     (AgentTile nests a .chartbox; Overview's charts are content-sized). */
  margin: -4px;
  padding: 4px;
}
.scan-content.wiping {
  animation: scan-wipe 550ms linear forwards;
}
@keyframes scan-wipe {
  from {
    clip-path: inset(0 100% 0 0);
  }
  to {
    clip-path: inset(0 0 0 0);
  }
}
/* Complement of scan-wipe: the track's visible region shrinks from the left
   exactly as the content's grows, so at every instant each pixel shows either
   the track (not yet revealed) or the final content on the final background —
   never the track UNDER revealed content (which snapped visibly when the
   track unmounted at the end of the pass). */
.scan-track.wiping-out {
  animation: scan-track-out 550ms linear forwards;
}
@keyframes scan-track-out {
  from {
    clip-path: inset(0 0 0 0);
  }
  to {
    clip-path: inset(0 0 0 100%);
  }
}

.scan-beamlane {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  border-radius: inherit;
}
/* The mover is container-width; the visible beam draws at its left edge via
   pseudo-elements, so translateX(0→100%) walks the beam core exactly from
   the zone's left edge to its right — the same spatial domain as the wipe. */
.scan-beam {
  position: absolute;
  inset: 0;
  animation: scan-sweep 1.5s ease-in-out infinite alternate;
}
.scan-beam::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: -9px;
  width: 18px;
  background: linear-gradient(90deg, transparent, var(--scan-halo), transparent);
}
.scan-beam::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: -1px;
  width: 1.5px;
  background: var(--scan-core);
  box-shadow: 0 0 7px var(--scan-glow);
}
@keyframes scan-sweep {
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(100%);
  }
}
.scan-beamlane.pass .scan-beam {
  animation: scan-pass 550ms linear forwards;
}
@keyframes scan-pass {
  0% {
    transform: translateX(0);
    opacity: 1;
  }
  92% {
    opacity: 1;
  }
  100% {
    transform: translateX(100%);
    opacity: 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  /* Static placeholder, instant reveal (§6). The JS matchMedia check keeps
     the machine out of `revealing`; this is the belt. */
  .scan-beam {
    display: none;
  }
  .scan-content.wiping {
    animation: none;
  }
}
</style>
