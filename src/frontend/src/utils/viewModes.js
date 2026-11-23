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
 * The view a person who has never chosen one opens in (trinity-enterprise#1357).
 * Someone new has one agent or none, and Grid shows what exists. This is NOT
 * the degrade default above: a stored value that is no longer a mode (e.g.
 * the retired 'graph') was a choice, and keeps landing on Timeline as before.
 */
export const FIRST_VIEW_MODE = 'grid'

/**
 * The view the dashboard opens in, from the stored choice (`localStorage`
 * returns `null` when there is none). A stored known mode always wins; nothing
 * stored → `FIRST_VIEW_MODE`; anything else → `DEFAULT_VIEW_MODE`.
 */
export function initialViewMode(saved) {
  if (saved === null || saved === undefined) return FIRST_VIEW_MODE
  return VIEW_MODES.includes(saved) ? saved : DEFAULT_VIEW_MODE
}

/** The mode after `mode` in cycle order; an unknown mode wraps to the default. */
export function nextViewMode(mode) {
  const i = VIEW_MODES.indexOf(mode) // -1 for unknown → (-1 + 1) % 3 = 0
  return VIEW_MODES[(i + 1) % VIEW_MODES.length]
}
