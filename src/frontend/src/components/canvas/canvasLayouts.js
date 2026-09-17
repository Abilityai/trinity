/**
 * Starter layouts for the agent canvas (trinity-enterprise#537).
 *
 * A canvas may declare ONE template by name; each names the slots its blocks
 * fill through their `slot` key. The rule this file exists to make decidable:
 * **a layout never hides a block**. A block with no slot, or one naming a slot
 * the layout does not know, renders after the layout in the stacked list; a
 * layout with nothing slotted degrades to the stacked list entirely; and
 * empty regions are not rendered, so there is never blank grid space.
 *
 * The CSS that draws a layout lives in `CanvasKit.vue` (`.ck-layout-<name>`
 * and `.ck-slot-<slot>` rules, collapsing on the kit's own inline size with an
 * `@container` query — the Portal rail is ~300px wide on a desktop viewport,
 * so a viewport media query would never fire there). Those two class families
 * are app-emitted and deliberately NOT in `utils/canvasKit.js::KIT_CLASSES`:
 * an agent cannot fake a region.
 *
 * Keep `LAYOUTS` in step with `CANVAS_LAYOUT_SLOTS` in the backend `models.py`
 * and `canvas.ts`; `test_ent537_canvas_design_kit.py` pins the three.
 */

export const LAYOUTS = Object.freeze({
  dashboard: Object.freeze({
    slots: Object.freeze(['header', 'kpis', 'main', 'side', 'footer']),
    // Slots whose blocks sit side by side in an auto grid rather than stacking.
    gridSlots: Object.freeze(['kpis']),
  }),
  report: Object.freeze({
    slots: Object.freeze(['header', 'summary', 'body', 'figures', 'appendix']),
    gridSlots: Object.freeze(['figures']),
  }),
  brief: Object.freeze({
    slots: Object.freeze(['header', 'key-points', 'body']),
    gridSlots: Object.freeze([]),
  }),
  'status-board': Object.freeze({
    slots: Object.freeze(['header', 'status', 'issues', 'next', 'log']),
    gridSlots: Object.freeze(['status']),
  }),
})

export const CANVAS_TEMPLATES = Object.freeze(Object.keys(LAYOUTS))

/** The slot charset the backend enforces; mirrored so a stored value is never trusted blindly. */
export const SLOT_RE = /^[a-z][a-z0-9-]{0,31}$/

/**
 * Place renderable blocks into a layout.
 *
 * @param {string|null|undefined} template - the canvas's declared template
 * @param {Array<{slot?: string|null}>} blocks - `canvasUtils.renderableBlocks` output
 * @returns {null | {template: string, regions: Array<{slot: string, grid: boolean, blocks: Array}>, unslotted: Array}}
 *   null means "render stacked" — no template, an unknown one, or nothing slotted.
 */
export function placeBlocks(template, blocks) {
  const layout = typeof template === 'string' ? LAYOUTS[template] : null
  const list = Array.isArray(blocks) ? blocks : []
  if (!layout) return null
  const regions = layout.slots
    .map((slot) => ({
      slot,
      grid: layout.gridSlots.includes(slot),
      blocks: list.filter((b) => b && b.slot === slot),
    }))
    .filter((r) => r.blocks.length > 0)
  if (!regions.length) return null
  const unslotted = list.filter((b) => !b || !layout.slots.includes(b.slot))
  return { template, regions, unslotted }
}
