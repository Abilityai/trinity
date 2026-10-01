/**
 * The priority+ fit rule, as a pure function (#1925).
 *
 * Extracted verbatim from `OverflowTabs.vue::recompute` so a SECOND surface —
 * `NavBar.vue`, whose items are router-links rather than tab buttons and so
 * cannot mount the tab primitive — packs its row by the same arithmetic instead
 * of a second copy that drifts. It lives here and not in the SFC because vitest
 * runs `environment: 'node'` with no mount harness: a rule inside a component is
 * one no test can reach (the ent#392 precedent). The DOM half — measuring the
 * mirror row, observing the container — stays in each component; only the
 * decision is shared.
 *
 * Nothing here reads the DOM, so every branch below is exercised by
 * `tests/unit/overflowFit.spec.js` with plain numbers.
 */

// px tolerance for sub-pixel rounding in the fit decision.
export const FIT_EPSILON = 1

/**
 * How many of `itemWidths` render inline before the rest collapse into the
 * overflow menu.
 *
 * @param {object}   o
 * @param {number}   o.containerWidth  px available to the row
 * @param {number[]} o.itemWidths      px per item, in render order
 * @param {number}   o.moreWidth       px of the worst-case "More" trigger
 * @param {number}   [o.gap]           px of flex `gap` BETWEEN adjacent items.
 *   Zero for a padding-spaced strip (the tab primitive); non-zero for NavBar,
 *   whose links carry real gaps. Counting it is not optional — a six-link row
 *   at `gap-6` hides 120px of width from the sum, which is one whole link, so
 *   a gap-blind rule keeps the last link inline and lets it clip.
 * @param {number}   [o.epsilon]
 * @returns {number} count of leading items to render inline
 */
export function computeInlineCount({ containerWidth, itemWidths, moreWidth, gap = 0, epsilon = FIT_EPSILON }) {
  const widths = Array.isArray(itemWidths) ? itemWidths : []
  // Not yet measured / hidden container → render everything inline. Deliberately
  // the all-inline answer and never zero: a container that has not been measured
  // yet is the first-paint case, and collapsing there is the flicker the mirror
  // row exists to avoid.
  if (!(containerWidth > 0)) return widths.length
  const total = widths.reduce((a, b) => a + b, 0) + gap * Math.max(0, widths.length - 1)
  if (total <= containerWidth + epsilon) return widths.length // fits — no More button

  // Reserve room for the More trigger, then pack from the left.
  // `- gap` is the gap between the last inline item and the More trigger; the
  // per-item `need` below carries the gaps between the items themselves.
  const avail = containerWidth - moreWidth - gap
  let acc = 0
  let count = 0
  for (let i = 0; i < widths.length; i++) {
    const need = widths[i] + (count > 0 ? gap : 0)
    if (acc + need <= avail + epsilon) {
      acc += need
      count++
    } else {
      break
    }
  }
  // If exactly one item overflows and it would fit without reserving the More
  // trigger, keep it inline rather than spend More-width to hide one item.
  if (count === widths.length - 1 && acc + gap + widths[count] <= containerWidth + epsilon) {
    count = widths.length
  }
  // Always keep at least one item inline when even the first one fits.
  if (count === 0 && widths[0] <= containerWidth + epsilon) count = 1
  return count
}
