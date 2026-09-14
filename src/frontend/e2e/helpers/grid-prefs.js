import { allGridKeysFor, PREF_KEYS } from '../../src/utils/gridStorageKeys.js'

/**
 * Grid persistence helpers for the e2e specs (trinity-enterprise#413).
 *
 * Since ent#413 the Dashboard Grid's layout / tile prefs / org toggles live in
 * the USER's server record (`user_ui_preferences`), with a per-user
 * localStorage cache in front of it. "A clean board" therefore means BOTH:
 *   - every browser key for the e2e identity (the per-user cache) plus every
 *     legacy generation (the adopt path reads those), and
 *   - the three server keys — or the next load adopts the previous test's
 *     board straight back in, exactly the #2199 class one layer up.
 *
 * The e2e identity is the admin login from `auth.setup.js`; the JWT `sub` (and
 * so the cache namespace) is the fixed username `admin`.
 */
export const E2E_PRINCIPAL = 'admin'
export const GRID_BROWSER_KEYS = allGridKeysFor(E2E_PRINCIPAL)

/** Register the per-page init script that clears every browser-side key. */
export async function clearBrowserGridState(page) {
  // The argument is SPREAD into one flat array and the callback iterates it —
  // nesting it would call removeItem() with an Array and silently clear
  // nothing, which looks identical to a working cleanup (#2199).
  await page.addInitScript((keys) => {
    keys.forEach((k) => localStorage.removeItem(k))
  }, GRID_BROWSER_KEYS)
}

/**
 * DELETE the three server keys with the app's own JWT. Needs a page already
 * at the app origin (after `page.goto('/')`) — the token lives in its
 * localStorage. Call BEFORE entering Grid mode, which is when the store loads.
 */
export async function resetServerGridPrefs(page) {
  const statuses = await page.evaluate(async (keys) => {
    const out = []
    for (const k of keys) {
      const res = await fetch(`/api/users/me/preferences/${k}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
      })
      out.push(res.status)
    }
    return out
  }, Object.values(PREF_KEYS))
  for (const s of statuses) {
    if (s !== 200) throw new Error(`resetServerGridPrefs: DELETE answered ${s}`)
  }
}

/** The caller's stored record for one key, or null. */
export async function readServerPref(page, key) {
  return page.evaluate(async (k) => {
    const res = await fetch('/api/users/me/preferences', {
      headers: { Authorization: `Bearer ${localStorage.getItem('token')}` },
    })
    if (!res.ok) return null
    const body = await res.json()
    return body.preferences?.[k] || null
  }, key)
}
