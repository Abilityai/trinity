<template>
  <article class="it" :class="{ 'is-error': state === 'error' }">
    <!-- Half-inset SQUARE peg. AgentTile's peg is a round avatar; the shape
         is the at-a-glance "this is not an agent" signal, so it must stay
         square even if the icon changes. Kept inside the GAP_X budget (40px)
         so it can never collide with an adjacent department frame's chrome
         (#305 ZONE_CHROME contract, ent#325 interlock C). -->
    <div class="it-peg" aria-hidden="true">
      <slot name="icon">
        <svg viewBox="0 0 24 24" width="18" height="18"><rect x="3" y="3" width="18" height="18" rx="3"></rect></svg>
      </slot>
    </div>

    <header class="it-head">
      <div class="it-titles">
        <span class="it-scope">{{ scope }} ·</span>
        <h3 class="it-title">{{ title }}</h3>
      </div>
      <span v-if="stamp" class="it-stamp" :title="stampTitle || undefined">{{ stamp }}</span>
    </header>

    <div class="it-body">
      <!-- Loading is an honest skeleton, never a spinner over stale numbers:
           a tile that looks live while showing last week's figures is worse
           than one that admits it is loading (ent#325 AC).
           A tile that passes `owns-loading` renders its own loading face in the
           default slot instead (ent#449) — the chassis cannot draw a scanline
           over a zone only the tile knows the shape of. -->
      <div v-if="state === 'loading' && !ownsLoading" class="it-skel" role="status" aria-label="Loading">
        <span class="l l1"></span><span class="l l2"></span><span class="l l3"></span>
      </div>

      <!-- Per-tile failure degrades THIS tile only — the grid and every
           sibling stay live (#47 hydration contract, ent#325 AC). -->
      <div v-else-if="state === 'error'" class="it-msg">
        <b>Couldn't load</b>
        <span>{{ errorText || 'This tile only — the rest of the board is unaffected.' }}</span>
        <button v-if="onRetry" type="button" class="nodrag" @click.stop="onRetry">Retry</button>
      </div>

      <!-- An empty state names the next action rather than showing a void
           (ent#325 AC: "non-dead empty state"). -->
      <div v-else-if="state === 'empty'" class="it-msg">
        <b>{{ emptyTitle || 'Nothing yet' }}</b>
        <span>{{ emptyHint }}</span>
      </div>

      <slot v-else></slot>
    </div>

    <footer v-if="linkTo" class="it-foot">
      <RouterLink class="nodrag" :to="linkTo" @pointerdown.stop>{{ linkLabel }}</RouterLink>
    </footer>
  </article>
</template>

<script setup>
/**
 * InfoTile (trinity-enterprise#325) — the shell every fleet info tile renders
 * inside. The chassis half of epic #94's widget architecture.
 *
 * Deliberately NOT an agent tile:
 *   - no Run / Autonomy toggles. Info tiles summarize; they do not operate.
 *     Giving a fleet-scope tile an operating control would make the blast
 *     radius of a misclick the whole fleet.
 *   - no org-overlay affordances. `AgentTile` gained a bottom-center connect
 *     port for reporting lines (#305); an info tile is not an org node, so it
 *     has no port, cannot be a reporting-line endpoint, and is not a `dept-*`
 *     drop target. Enforced by omission here and by key namespace upstream.
 *
 * Interactive children carry `.nodrag`, which `FleetGrid.onTilePointerDown`
 * checks before starting a drag — otherwise clicking a tile's own link would
 * begin a tile drag instead.
 *
 * `inheritAttrs: false` because a tile is a thin wrapper whose single root IS
 * this component: any prop the chassis binds that the wrapping tile did not
 * declare (`now` on a tile that only declares `agents`, say) would otherwise
 * fall through twice and land on `<article>` as a literal DOM attribute —
 * rewritten on every tick, visible in the inspector, meaningless. One line
 * makes that structurally impossible for every present and future tile; a
 * lint-style guard over `defineProps` would miss both the array form
 * (`defineProps([...])`) and the type-only form (`defineProps<{...}>()`),
 * which are legal and would reintroduce it while passing green.
 */
defineOptions({ inheritAttrs: false })

defineProps({
  /** Scope prefix rendered before the title: "Fleet", "Host", … */
  scope: { type: String, default: 'Fleet' },
  title: { type: String, required: true },
  /** Freshness / window stamp, e.g. "24h" or "updated 2m ago". */
  stamp: { type: String, default: '' },
  stampTitle: { type: String, default: '' },
  /** 'ready' | 'loading' | 'error' | 'empty' */
  state: { type: String, default: 'ready' },
  errorText: { type: String, default: '' },
  emptyTitle: { type: String, default: '' },
  emptyHint: { type: String, default: '' },
  /** vue-router target for the footer deep-link into the owning surface. */
  linkTo: { type: [String, Object], default: null },
  linkLabel: { type: String, default: 'Open' },
  onRetry: { type: Function, default: null },
  /**
   * The tile renders its own loading face inside the default slot — one
   * persistent `ScanlineReveal` around its data zone (ent#449). The chassis
   * then renders the slot for `loading` AND `ready` and never shows `.it-skel`
   * for this tile, so the tile's instance is PATCHED across the loading→ready
   * edge rather than remounted (a remount re-inits the phase machine from
   * `loading = false` and the reveal never plays).
   *
   * A tile owns its loading face ONLY: `error` and `empty` still replace the
   * slot with the chassis message, so a terminal can never end up drawn under
   * a loading track. Default off — every tile that has not adopted keeps the
   * skeleton, and `.it-skel` stays until the last consumer adopts.
   */
  ownsLoading: { type: Boolean, default: false },
})
</script>

<style scoped>
/* Tokens come from the --gv-* cascade FleetGrid establishes, so an info tile
   and an agent tile can never drift apart on colour. */
.it {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  padding: 14px 16px 10px 30px;
  border-radius: var(--gv-radius, 14px);
  background: var(--gv-tile, rgba(255, 255, 255, 0.9));
  border: 1px solid var(--gv-border, #e5e7eb);
  box-shadow: var(--gv-tile-shadow, 0 1px 2px rgba(0, 0, 0, 0.06));
  /* Deliberately NOT `overflow: hidden`: this box is the peg's containing
     block, so clipping here amputates the half-inset peg into a 16px sliver
     jammed against the left border with the glyph sliced down the middle —
     the defect the tiles shipped with. `.it-body` already clips the content
     that actually needs it, and `AgentTile`'s avatar overhangs its own tile
     the same way (`gridLayout.GAP_X` is sized for exactly that). */
}
.it.is-error {
  border-color: var(--gv-danger-border, rgba(220, 38, 38, 0.35));
}

/* Half-inset square peg: 32px wide, 16px of it outside the tile's left edge.
   16 < GAP_X (40), so it stays inside the inter-column gap budget. */
.it-peg {
  position: absolute;
  left: -16px;
  top: 16px;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--gv-peg-bg, #111827);
  color: var(--gv-peg-fg, #fff);
  border: 1px solid var(--gv-border, #e5e7eb);
}
.it-peg :deep(svg) {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.it-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  min-height: 20px;
}
.it-titles {
  display: flex;
  align-items: baseline;
  gap: 5px;
  min-width: 0;
}
.it-scope {
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--gv-muted, #6b7280);
  flex: none;
}
.it-title {
  margin: 0;
  font-size: 13px;
  font-weight: 650;
  color: var(--gv-text, #111827);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.it-stamp {
  font-size: 11px;
  color: var(--gv-muted, #6b7280);
  flex: none;
  font-variant-numeric: tabular-nums;
}

.it-body {
  flex: 1;
  min-height: 0;
  margin-top: 10px;
  overflow: hidden;
}

.it-skel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 4px;
}
.it-skel .l {
  height: 12px;
  border-radius: 6px;
  background: var(--gv-skel, rgba(0, 0, 0, 0.07));
  animation: it-pulse 1.4s ease-in-out infinite;
}
.it-skel .l1 { width: 62%; }
.it-skel .l2 { width: 88%; animation-delay: 0.15s; }
.it-skel .l3 { width: 44%; animation-delay: 0.3s; }
@keyframes it-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
@media (prefers-reduced-motion: reduce) {
  .it-skel .l { animation: none; }
}

.it-msg {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
  font-size: 12px;
  color: var(--gv-muted, #6b7280);
}
.it-msg b {
  font-size: 13px;
  color: var(--gv-text, #111827);
}
.it-msg button {
  margin-top: 6px;
  font: inherit;
  padding: 3px 10px;
  border-radius: 7px;
  cursor: pointer;
  border: 1px solid var(--gv-border, #e5e7eb);
  background: transparent;
  color: inherit;
}

.it-foot {
  margin-top: auto;
  padding-top: 8px;
  font-size: 11px;
}
.it-foot a {
  color: var(--gv-muted, #6b7280);
  text-decoration: none;
}
.it-foot a:hover {
  color: var(--gv-text, #111827);
  text-decoration: underline;
}
</style>
