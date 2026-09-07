/**
 * Dashboard Grid browser-storage keys — the single source of truth (#2199).
 *
 * ⚠️ ZERO IMPORTS. KEEP IT THAT WAY. ⚠️
 * This module is imported by `stores/fleetGrid.js` through the Vite `@` alias
 * AND by Playwright specs through a plain relative path
 * (`../src/utils/gridStorageKeys.js`). Playwright reads neither `vite.config.js`
 * nor a tsconfig `paths` map, so a single `import … from '@/…'` added here
 * would fail to resolve at test time and break every grid spec at once.
 * `utils/gridLayout.js` is the established zero-import precedent.
 *
 * Why this file exists: #2042 (ent#325) bumped the layout key v1 → v2 in the
 * store, but `dashboard-grid-view.spec.js` and `grid-org-overlay.spec.js` each
 * carried their own hand-copied `'trinity-grid-layout-v1'` literal. The specs
 * went green→red silently — the read-backs simply resolved `null`. A shared
 * export makes the next bump propagate to every consumer automatically.
 * `tests/unit/gridStorageKeys.spec.js` guards against a new hand-copy.
 *
 * Since trinity-enterprise#413 the record of truth is the SERVER
 * (`user_ui_preferences`, keys `grid_layout` / `grid_widgets` / `grid_org`).
 * localStorage holds two things only:
 *   - a per-USER cache (`userScopedKey(base, principalId)`) that gives the
 *     first paint a layout before the server answers, and keeps the grid
 *     editable when it cannot; and
 *   - the pre-#413 browser-global LEGACY blobs (the bare keys below), read
 *     once to adopt a hand-arranged constellation into the user's server
 *     record and otherwise left alone, so a downgrade is not a data-loss event.
 */

// ent#325: layout v2 admits `widget:*` keys alongside agents. The key is
// bumped rather than reused so a v1 client and a v2 client on the same browser
// cannot fight over one blob — and the migration is a one-time COPY, leaving
// v1 in place, so downgrading is not a data-loss event.
export const LAYOUT_KEY_V1 = 'trinity-grid-layout-v1'
export const LAYOUT_KEY = 'trinity-grid-layout-v2'

// Sparse `{ widgetId: boolean }` OVERRIDE map — see gridWidgets.isWidgetEnabled.
// Its own key: the org overlay (#305) persists Zones/Lines under a key of its
// own and the tile prefs must not be entangled with either, so that "Reset
// tiles" cannot clobber an overlay toggle (ent#325 scope note).
export const WIDGET_PREFS_KEY = 'trinity-grid-widgets-v1'

// Org overlay Zones/Lines toggles (`{ zones, lines }`, #305). Lived in
// `composables/useOrgOverlay.js` until ent#413 moved the toggles' persistence
// into the store beside the other two blobs; it is here now for the same
// reason the others are — the specs need the same literal.
export const ORG_KEY = 'trinity-grid-org-v1'

/**
 * Which identity claimed the legacy browser-global blobs (ent#413). On a
 * shared browser only the FIRST user to sign in after the upgrade adopts the
 * pre-#413 constellation; everyone after starts from the default rather than
 * inheriting someone else's board permanently.
 */
export const LEGACY_ADOPTED_KEY = 'trinity-grid-legacy-adopted-by'

/** Server preference keys (`user_ui_preferences.key`) for the three blobs. */
export const PREF_KEYS = Object.freeze({
  layout: 'grid_layout',
  widgets: 'grid_widgets',
  org: 'grid_org',
})

/**
 * EVERY generation of the LEGACY layout blob, newest first.
 *
 * Load-bearing, not decoration: the store reads v2, then v1, when it has no
 * server record and no per-user cache, so a test cleanup that clears only
 * `LAYOUT_KEY` lets a stale v1 layout be adopted straight back in — the board
 * is not clean and drag/tidy assertions become order-dependent. Clear all of
 * them.
 */
export const ALL_LAYOUT_KEYS = [LAYOUT_KEY, LAYOUT_KEY_V1]

/** Every legacy (browser-global) key plus the adoption marker. */
export const ALL_LEGACY_KEYS = [...ALL_LAYOUT_KEYS, WIDGET_PREFS_KEY, ORG_KEY, LEGACY_ADOPTED_KEY]

/** The per-user cache key for one of the base keys above. */
export function userScopedKey(baseKey, principalId) {
  return `${baseKey}:${principalId}`
}

/** Every per-user cache key for one identity (the three live blobs). */
export function userScopedKeys(principalId) {
  return [LAYOUT_KEY, WIDGET_PREFS_KEY, ORG_KEY].map((k) => userScopedKey(k, principalId))
}

/**
 * Everything a spec must clear for a clean board for one identity: the
 * per-user cache AND every legacy generation (the adopt path reads those).
 */
export function allGridKeysFor(principalId) {
  return [...userScopedKeys(principalId), ...ALL_LEGACY_KEYS]
}
