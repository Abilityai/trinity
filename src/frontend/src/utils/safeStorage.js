/**
 * The browser's localStorage, or null.
 *
 * Accessing `localStorage` at all can THROW — private mode in some engines,
 * blocked site data, a sandboxed frame — so every per-viewer browser state in
 * the Workspace (rail state, column widths, drafts) asks through here and runs
 * session-only on null. One copy: the rail (`views/Portal.vue`) and the column
 * resize composable each carried their own until trinity-enterprise#657 added
 * a third consumer.
 *
 * `window.localStorage` FIRST, then the bare global: in a browser they are the
 * same object, but the two copies this replaces read different ones, and a
 * caller that fakes only `window` (the #2617 column-resize spec) would
 * otherwise silently get the real store — or nothing.
 */
export function safeStorage() {
  try {
    if (typeof window !== 'undefined' && window.localStorage) return window.localStorage
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}
