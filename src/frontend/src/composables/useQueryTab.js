import { ref, watch } from 'vue'

/**
 * The active tab of a strip whose selection lives in a `?tab=` query param.
 *
 *   queryTab    () => the tab id the URL names right now (may be undefined)
 *   validTabIds ref/computed of the ids currently offered
 *   defaultTab  ref/computed of the id to show when the URL names none of them
 *   pushTab     (id) => write the id to the URL as a new history entry
 *
 * The set of offered tabs is not always known at setup: a tab can be gated on
 * a list that is fetched afterwards (#3453). Until it arrives the default is
 * shown, the URL is left alone, and the tab it names is applied when the set
 * that contains it turns up.
 */
export function useQueryTab({ queryTab, validTabIds, defaultTab, pushTab }) {
  const resolve = (id) => (validTabIds.value.includes(id) ? id : defaultTab.value)
  const activeTab = ref(resolve(queryTab()))

  // Set by any click on an offered tab — including the one already shown,
  // which changes neither `activeTab` nor the URL and so leaves no other trace.
  let userChose = false

  // Click handler — a new history entry per change, so back/forward walks the
  // tabs; none for a re-click of the tab already shown.
  function selectTab(id) {
    if (!validTabIds.value.includes(id)) return
    userChose = true
    if (id === activeTab.value) return
    activeTab.value = id
    pushTab(id)
  }

  // The URL changed under us (back/forward, an in-app link).
  watch(queryTab, (id) => {
    activeTab.value = resolve(id)
  })

  // The offered set changed (#3453). Setup resolved `?tab=` against a set that
  // may not have held the tab yet, and nothing else re-reads it: the URL has
  // not changed, so the watcher above never fires. Honour the URL's tab now —
  // unless the user has clicked since, in which case this late write would
  // move them off the tab they chose (the #2130 late-write trap). A tab the
  // URL names that is still not offered leaves the default in place.
  watch(validTabIds, (ids) => {
    if (userChose) return
    const wanted = queryTab()
    if (ids.includes(wanted)) activeTab.value = wanted
  })

  return { activeTab, selectTab }
}
