<template>
  <div class="tr" role="list" :style="{ '--tr-rows': trackCount }">
    <RouterLink
      v-for="row in rows"
      :key="row.key"
      class="tr-row nodrag"
      role="listitem"
      :to="row.to"
      :title="row.title || undefined"
      @pointerdown.stop
    >
      <span class="tr-line">
        <span class="tr-lead">
          <slot name="lead" :row="row">{{ row.lead }}</slot>
        </span>
        <span class="tr-primary">
          <slot name="primary" :row="row">{{ row.primary }}</slot>
        </span>
        <span class="tr-meta">
          <slot name="meta" :row="row">{{ row.meta }}</slot>
        </span>
      </span>
      <span v-if="row.sub" class="tr-sub">{{ row.sub }}</span>
    </RouterLink>
  </div>
</template>

<script setup>
/**
 * TileRowList — a bounded list of linked rows inside a grid info tile
 * (epic ent#94; first consumer ent#100 "Recent failures").
 *
 * ## Why this exists at one consumer
 *
 * Not for the markup — for three CHASSIS rules that are easy to forget per-tile
 * and dashboard-breaking when forgotten. Centralising them is defensive
 * engineering rather than premature abstraction:
 *
 *  1. **`.nodrag` on every interactive child.** `FleetGrid.onTilePointerDown`
 *     does `if (e.target.closest('.nodrag')) return` — without it, clicking a
 *     row starts a TILE DRAG instead of following the link.
 *  2. **No internal scroll, ever.** `FleetGrid.onWheel` calls
 *     `e.preventDefault()` UNCONDITIONALLY and zooms the board, so a nested
 *     `overflow-y: auto` inside a tile is unusable: the wheel zooms instead of
 *     scrolling. Bounded data is bounded by construction (a server-side
 *     `limit`) with the total stated, never by a scrollbar. There is
 *     deliberately no `overflow-y` in this file.
 *  3. **`InfoTile`'s body is `overflow: hidden`.** An overrun does not scroll,
 *     ellipsize or error — the last row SILENTLY VANISHES, and the
 *     node-environment vitest suite structurally cannot see it. So the track
 *     count is fixed (`--tr-rows`) rather than derived from how many rows
 *     happened to arrive, and every line is `nowrap` + ellipsis: row height is
 *     deterministic, and a long value can only ellipsize, never wrap into a
 *     third line and push a row off the bottom. It also means the list keeps
 *     one footprint across loading / ready / empty (contract principle 4/6 —
 *     nothing shifts on arrival).
 *
 * ## API — the shape both queued list-tiles actually share, not a free slot
 *
 * `[lead] [primary] [meta]` on one line, with an optional second `sub` line.
 * A single opaque `#row` slot would make this a wrapper whose only value is a
 * CSS class, and would invite `variant` / `dense` / `align` props until it is
 * an unowned mini design system.
 *
 * **Stop rule, recorded now:** a prop that changes ROW LAYOUT is the signal to
 * stop extending this component and inline the markup in the tile that wants
 * it. `sub` is content, not layout — a row without it renders one line.
 *
 * Row: `{ key, to, title, lead, primary, meta, sub }`. Each of `lead` /
 * `primary` / `meta` also has a scoped slot for a glyph or chip.
 *
 * `to` is a vue-router target. A `RouterLink` naming a route that does not
 * exist THROWS during render, which aborts Vue's update for the whole tree and
 * FREEZES THE DASHBOARD — guarded by `tests/unit/gridTileLinks.spec.js`, which
 * ent#100 extended to see `to:` objects built in `<script setup>` (it
 * previously matched only a literal `link-to="{…}"` attribute, so every row
 * link here was invisible to it).
 */
defineProps({
  /** @type {{key: string, to: object|string, title?: string, lead?: string, primary?: string, meta?: string, sub?: string}[]} */
  rows: { type: Array, default: () => [] },
  /**
   * Fixed number of row tracks. Layout stability, not a variant: the tile's
   * height must not change with how many rows arrived, and the tracks must be
   * sized for the MAXIMUM the tile ever renders so the last row cannot be
   * clipped away by `InfoTile`'s `overflow: hidden`.
   */
  trackCount: { type: Number, default: 4 },
})
</script>

<style scoped>
.tr {
  display: grid;
  grid-template-rows: repeat(var(--tr-rows, 4), minmax(0, 1fr));
  align-content: stretch;
  gap: 4px;
  height: 100%;
  min-height: 0;
  /* NO overflow-y — see rule 2 in the docblock. */
}

.tr-row {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 1px;
  min-width: 0;
  min-height: 0;
  padding: 1px 4px 1px 0;
  border-radius: 6px;
  text-decoration: none;
  color: inherit;
}
.tr-row:hover,
.tr-row:focus-visible {
  background: var(--gv-btn-bg-hover, rgba(0, 0, 0, 0.04));
}
.tr-row:focus-visible {
  outline: 2px solid var(--gv-blue, #2563eb);
  outline-offset: 1px;
}

.tr-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.2;
}

/* Fixed columns. Only `.tr-primary` may consume slack — the identity field is
   what loses by default otherwise: an uppercase EPHEMERAL_EXHAUSTED chip is
   ~37% of a 336px row, which would leave ~13 characters for an agent label
   that is routinely 18 (`canary-fleet-burst`). */
.tr-lead {
  flex: none;
  display: inline-flex;
  align-items: center;
  max-width: 92px;
  overflow: hidden;
}
.tr-primary {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--gv-text, #111827);
  font-weight: 550;
}
.tr-meta {
  flex: none;
  color: var(--gv-muted, #6b7280);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.tr-sub {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  line-height: 1.2;
  color: var(--gv-muted, #6b7280);
}
</style>
