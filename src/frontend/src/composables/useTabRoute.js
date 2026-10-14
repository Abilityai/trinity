import { watch } from 'vue'

/**
 * Two-way binding between a tab ref and the `?tab=` query key (#2900).
 *
 * The ref stays the render source of truth — a click paints immediately and the
 * URL follows — so this adds exactly one new writer to `activeTab`: a route
 * change on the same page whose `?tab=` differs from the tab showing, i.e. a
 * history step (Back / Forward). #2130 was a LATE write to the tab yanking
 * users off one they had clicked, so the two ways this could become one are
 * closed deliberately:
 *
 *   - A write still in flight is compared against, not the stale URL. Click A,
 *     then click back to the original tab while A is held at a slow guard: the
 *     URL still names the original, so a naive "already there" check would
 *     skip the second write, A would land, and the user would be bounced to A.
 *     Instead the second write is issued and supersedes the first.
 *   - Arriving from another page is owned by the caller's lifecycle hooks (the
 *     view is KeepAlive-cached and applies its deep link in onActivated).
 *
 * History: a tab CHANGE made through the ref or `selectTab` is a `push`, so
 * Back steps through tabs. `selectTab(…, { replace: true })` and `syncUrl()`
 * are the normalising writes (a fallback off a tab the viewer cannot see, a
 * bare URL catching up with a remembered tab) and never add an entry.
 *
 * An absent or unknown `?tab=` means `defaultTab`, so a plain link to the page
 * is never rewritten just to say so.
 */
export function useTabRoute({ activeTab, route, router, routeName, defaultTab, resolveTab }) {
  // Writes issued here that the router has not settled yet, and the tab the
  // newest of them asked for (route.query is stale until it lands).
  let inflight = 0
  let intended = null

  const onRoute = () => route.name === routeName
  const urlTab = () => resolveTab(route.query.tab) || defaultTab
  const target = () => (inflight ? intended : urlTab())

  function write(tab, { replace = false, query = {} } = {}) {
    const location = { query: { ...route.query, ...query, tab } }
    const push = !replace && tab !== target()
    intended = tab
    inflight++
    const settle = () => { inflight-- }
    Promise.resolve(push ? router.push(location) : router.replace(location)).then(settle, settle)
  }

  // Tab → URL. `sync` so the write reads route.query at the moment of the click
  // and `target()` already reflects it when the next statement runs.
  watch(activeTab, (tab) => {
    if (!onRoute() || tab === target()) return
    write(tab)
  }, { flush: 'sync' })

  // URL → tab: a history step on this page. Our own write landing arrives here
  // too, already equal to the ref, and falls through as a no-op.
  watch(
    () => [route.name, route.params.name, route.query.tab],
    ([name, owner], [prevName, prevOwner]) => {
      if (name !== routeName || name !== prevName) return
      if (owner !== prevOwner && !resolveTab(route.query.tab)) {
        // Same page, different agent, no tab named: the remembered tab stands
        // and the bare URL catches up with it.
        syncUrl()
        return
      }
      const tab = urlTab()
      if (activeTab.value !== tab) activeTab.value = tab
    },
    { flush: 'sync' }
  )

  /** Select a tab in ONE navigation, optionally carrying extra query keys. */
  function selectTab(tab, options = {}) {
    if (onRoute() && (tab !== target() || options.query)) write(tab, options)
    activeTab.value = tab
  }

  /** Make the URL name the tab that is showing, without a history entry. */
  function syncUrl() {
    if (onRoute() && activeTab.value !== target()) write(activeTab.value, { replace: true })
  }

  return { selectTab, syncUrl }
}
