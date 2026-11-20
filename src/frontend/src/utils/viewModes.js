/**
 * Dashboard view modes — the ONE home of the mode list (#2536).
 *
 * ⚠️ ZERO IMPORTS. KEEP IT THAT WAY. ⚠️
 * Imported by `stores/network.js` and `views/Dashboard.vue` through the Vite
 * `@` alias AND by Playwright specs through a plain relative path
 * (`../src/utils/viewModes.js`) — the same contract as `gridStorageKeys.js`
 * (#2199): Playwright resolves neither the alias nor a tsconfig `paths` map,
 * so a single aliased import here would break every consuming spec at once.
 * `tests/unit/viewModeStructure.spec.js` guards it.
 *
 * ORDER IS LOAD-BEARING: (a) it is the switcher's visual order, (b) it is the
 * `v` cycle order (Timeline → Grid → List → Timeline), (c) index 0 is the
 * default a stale/unknown persisted mode degrades to. Before #2536 the list
 * lived twice — `['grid','timeline','list']` in the store (whitelist only,
 * order never mattered) and `['timeline','grid','list']` in the template —
 * and a cycle built on the store copy would have run against the buttons.
 */
export const VIEW_MODES = Object.freeze(['timeline', 'grid', 'list'])
export const DEFAULT_VIEW_MODE = VIEW_MODES[0]

/**
 * Modes a build shows only when it carries an entitlement
 * (trinity-enterprise#1307): mode id → feature id. Kept OUT of `VIEW_MODES` on
 * purpose — that list is what every build renders, cycles and tests against,
 * and a mode most builds do not have must not be in it.
 */
export const GATED_VIEW_MODES = Object.freeze({ deck: 'flight_deck' })

/** Every mode id any build can hold — the store's whitelist for a saved choice. */
export const ALL_VIEW_MODES = Object.freeze([...VIEW_MODES, ...Object.keys(GATED_VIEW_MODES)])

/** The modes THIS viewer has, in switcher and cycle order. */
export function viewModesFor(features = []) {
  return ALL_VIEW_MODES.filter((m) => !GATED_VIEW_MODES[m] || features.includes(GATED_VIEW_MODES[m]))
}

const LABELS = Object.freeze({ deck: 'Flight Deck' })

/** What the switcher button says. */
export function viewModeLabel(mode) {
  return LABELS[mode] || mode
}

/**
 * The mode after `mode` in cycle order; an unknown mode wraps to the default.
 * `modes` is the viewer's own list (`viewModesFor`), so the cycle never lands
 * on a mode the build does not have.
 */
export function nextViewMode(mode, modes = VIEW_MODES) {
  const i = modes.indexOf(mode) // -1 for unknown → index 0
  return modes[(i + 1) % modes.length]
}
