/**
 * Dashboard Grid localStorage keys — the single source of truth (#2199).
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
 */

// ent#325: layout v2 admits `widget:*` keys alongside agents. The key is
// bumped rather than reused so a v1 client and a v2 client on the same browser
// cannot fight over one blob — and the migration is a one-time COPY, leaving
// v1 in place, so downgrading is not a data-loss event.
export const LAYOUT_KEY_V1 = 'trinity-grid-layout-v1'
export const LAYOUT_KEY = 'trinity-grid-layout-v2'

// Sparse `{ widgetId: boolean }` OVERRIDE map — see gridWidgets.isWidgetEnabled.
// Its own key: the org overlay (#305) persists Zones/Lines under keys of its
// own and the tile prefs must not be entangled with either, so that "Reset
// tiles" cannot clobber an overlay toggle (ent#325 scope note).
export const WIDGET_PREFS_KEY = 'trinity-grid-widgets-v1'

/**
 * EVERY generation of the layout blob, newest first.
 *
 * Load-bearing, not decoration: `fleetGrid._loadSavedRaw` migrates a v1 blob
 * into v2 when v2 is absent, so a test cleanup that clears only `LAYOUT_KEY`
 * lets a stale v1 layout be migrated straight back in — the board is not
 * clean and drag/tidy assertions become order-dependent. Clear all of them.
 */
export const ALL_LAYOUT_KEYS = [LAYOUT_KEY, LAYOUT_KEY_V1]

// NOT here by design: `ORG_KEY = 'trinity-grid-org-v1'` stays in
// `composables/useOrgOverlay.js`. It has never been bumped and the spec copy
// matches, so there is no desync to fix and no reason to edit a composable.
