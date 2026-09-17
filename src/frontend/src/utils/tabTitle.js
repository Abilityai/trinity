/**
 * ent#557 — the browser tab says how many replies are waiting.
 *
 * A Workspace tab sitting behind other tabs is exactly the case the unread
 * badge cannot reach: the sidebar is not on screen. `document.title` is, and it
 * is the only channel a background tab has.
 *
 * TWO writers, one string. The router sets a title on every completed
 * navigation (`#1418`), and the unread count changes on its own schedule; if
 * each wrote `document.title` directly the last one to fire would win and the
 * other's half would vanish — a navigation would drop the count, and a count
 * update would drop the page label. So neither writes it. Both call in here,
 * this module holds the two halves, and it renders the whole string.
 *
 * The count is a PREFIX on whatever the router computed, never a replacement.
 * That is what makes it compose with ent#556's branding work: change the label
 * and nothing here needs to know.
 */

// The badge caps where the sidebar's does. A tab is truncated to a few
// characters by every browser once a handful are open, so a four-digit count
// would cost the label its first word for a number nobody acts on differently.
export const TAB_UNREAD_CAP = 99

let baseTitle = ''
let unread = 0

/**
 * Render the tab title. Pure — this is the whole decision, and it is what the
 * tests execute.
 *
 * Zero renders the base title unchanged rather than a `(0)`: an empty state is
 * said by the absence of the marker, and `(0) …` is a badge announcing that
 * there is nothing to announce.
 */
export function formatTabTitle(base, count) {
  const n = Number(count)
  const safe = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
  const label = String(base ?? '')
  if (safe <= 0) return label
  const shown = safe > TAB_UNREAD_CAP ? `${TAB_UNREAD_CAP}+` : String(safe)
  // Leading, not trailing: a browser truncates a tab from the RIGHT, so a
  // suffix is the first thing to disappear on the tab that most needs it.
  return label ? `(${shown}) ${label}` : `(${shown})`
}

function apply() {
  if (typeof document === 'undefined') return
  document.title = formatTabTitle(baseTitle, unread)
}

/** Called by the router on every completed navigation. */
export function setBaseTitle(title) {
  baseTitle = String(title ?? '')
  apply()
}

/**
 * Called by the Workspace when the unread total changes.
 *
 * Deliberately not read from a store: this module is imported by the ROUTER,
 * which must not depend on a portal store — the router runs on every page in
 * the platform, and most of them have no Workspace at all. The Workspace pushes;
 * nothing pulls.
 */
export function setUnreadCount(count) {
  const n = Number(count)
  const next = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
  if (next === unread) return
  unread = next
  apply()
}

/**
 * Leaving the Workspace clears the marker. Without this the count would outlive
 * the surface that can explain it — a tab reading `(3)` on the Settings page,
 * with nothing on screen to click.
 */
export function clearUnreadCount() {
  setUnreadCount(0)
}

// Test seam only — module state is otherwise process-lifetime by design.
export function _resetTabTitleForTest() {
  baseTitle = ''
  unread = 0
}
