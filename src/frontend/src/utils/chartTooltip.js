/**
 * Chart tooltip placement (#3264), shared by `StackedBarChart.vue` and the
 * Workspace band's `ActivityWave.vue` (trinity-enterprise#968).
 *
 * A chart tooltip renders in <body> at fixed coordinates taken from the hovered
 * element, because as an `absolute bottom-full` child it always opened upward
 * and on a 23px chart just under a header its top rows were covered. Out of
 * every parent, nothing can clip or cover it. The placement rule lives here so
 * a second chart cannot drift from the first: above when it fits, else below
 * when that fits, else whichever side has more room, horizontally centred on
 * the anchor and clamped into the viewport.
 */

export const TIP_GAP = 4 // px between the anchor and the tooltip (the old `mb-1`)
export const VIEWPORT_MARGIN = 8 // px the tooltip keeps from every viewport edge

/**
 * @param {{top:number,bottom:number,left:number,width:number}} anchor  the hovered element's rect
 * @param {{width:number,height:number}} tip                             the measured tooltip
 * @param {number} vw  viewport width
 * @param {number} vh  viewport height
 * @returns {{top:number,left:number,side:'above'|'below'}}
 */
export function placeTooltip(anchor, tip, vw, vh) {
  const roomAbove = anchor.top - TIP_GAP - VIEWPORT_MARGIN
  const roomBelow = vh - anchor.bottom - TIP_GAP - VIEWPORT_MARGIN
  const side = tip.height <= roomAbove || (tip.height > roomBelow && roomAbove >= roomBelow)
    ? 'above'
    : 'below'
  const rawTop = side === 'above' ? anchor.top - TIP_GAP - tip.height : anchor.bottom + TIP_GAP
  const top = Math.max(VIEWPORT_MARGIN, Math.min(rawTop, vh - VIEWPORT_MARGIN - tip.height))
  const rawLeft = anchor.left + anchor.width / 2 - tip.width / 2
  const left = Math.max(VIEWPORT_MARGIN, Math.min(rawLeft, vw - VIEWPORT_MARGIN - tip.width))
  return { top, left, side }
}
