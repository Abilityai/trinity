// trinity-enterprise#631 — the `@` typeahead's people half, shared by the room
// and the 1:1 composer.
//
// People come from the SERVER, never a client-side directory: `fetch(q)` is the
// conversation's own candidate read (accounts that exist and can already reach
// its agents — the ent#450 shape), asked only once there is a query. The rules
// (tokens, what a send carries, the refusal line) are `portalMentions.js`'s;
// this composable only holds the state and the debounce.
//
// A 403 is a verdict, not a failure: this principal may not tag (an external
// Workspace client). The picker then stays agents-only for the session rather
// than asking again on every keystroke.

import { ref } from 'vue'
import { PEOPLE_DEBOUNCE_MS, PEOPLE_QUERY_MIN, addPicked, tagsInText } from '@/components/portal/portalMentions'

export function usePeoplePicker({ fetch, debounceMs = PEOPLE_DEBOUNCE_MS } = {}) {
  const people = ref([])
  // The query the CURRENT `people` answer. A refusal line is drawn only when
  // the server said "no one" for exactly what is typed — never while a read is
  // in flight, and never after one failed.
  const answeredFor = ref(null)
  const unavailable = ref(false)
  const picked = ref([])
  let timer = null
  let asked = null

  function lookup(query) {
    const q = String(query || '').trim()
    if (timer) { clearTimeout(timer); timer = null }
    if (unavailable.value || q.length < PEOPLE_QUERY_MIN || typeof fetch !== 'function') {
      people.value = []
      answeredFor.value = null
      asked = null
      return
    }
    if (q === answeredFor.value) return
    asked = q
    timer = setTimeout(async () => {
      timer = null
      try {
        const rows = await fetch(q)
        if (asked !== q) return
        people.value = Array.isArray(rows) ? rows : []
        answeredFor.value = q
      } catch (err) {
        if (asked !== q) return
        if (err?.response?.status === 403) unavailable.value = true
        people.value = []
        answeredFor.value = null
      }
    }, debounceMs)
  }

  function pick(person) { picked.value = addPicked(picked.value, person) }
  function tagsFor(text) { return tagsInText(text, picked.value) }
  function clear() {
    if (timer) { clearTimeout(timer); timer = null }
    asked = null
    people.value = []
    answeredFor.value = null
  }
  function reset() { clear(); picked.value = [] }

  return { people, answeredFor, unavailable, picked, lookup, pick, tagsFor, clear, reset }
}
