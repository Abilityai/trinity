import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from './auth'

/**
 * Per-user UI preferences — the sync engine (trinity-enterprise#413).
 *
 * One home for "this UI state belongs to the user, not the browser": a single
 * `GET /api/users/me/preferences` on load, a trailing-debounced conditional
 * `PUT /api/users/me/preferences/{key}` per key on save, `DELETE` on reset.
 * The Dashboard Grid (`stores/fleetGrid.js`) is the first consumer; every
 * other per-user localStorage key in the app migrates by consuming this store
 * and adding its key to the backend allowlist — never by re-implementing the
 * machinery below.
 *
 * Contract with a consumer:
 *   - `records[key]` is what the SERVER is known to hold (`{ value, updatedAt }`).
 *     It changes on load and on a 409 adoption; `serverGeneration` bumps after
 *     each such change so a consumer can re-apply.
 *   - `save(key, value, { origin })` is local-first by the consumer, then this
 *     store debounces the network write. Nothing leaves before the initial GET
 *     settles: a write needs to state what it believes the server holds
 *     (`base_updated_at` — `null` = insert-only, a string = compare-and-set),
 *     and it cannot know that before the load. There is no unconditional write.
 *   - A 409 is origin-aware. A write born from a user GESTURE re-PUTs once with
 *     the new base (this tab's edit wins — last-write-wins per user, the
 *     issue's accepted contract). A write born from a RECONCILE (roster sync)
 *     adopts the server record and does not retry: that is exactly the stale
 *     tab the base exists to stop from clobbering a newer save. One retry per
 *     write, so there is no loop.
 *   - Every queued write is tagged with the identity it was made under and
 *     discarded on mismatch at send time; an identity change (logout → another
 *     login without a reload — `auth.logout()` does not reset Pinia) cancels
 *     timers and drops the queue SYNCHRONOUSLY, so nothing ever fires under the
 *     next user's token. Never flushed on identity change.
 *   - 401 is not an error here: the global interceptor logs the user out.
 *     Anything else sets `saveError` / `loadError` and keeps the value queued
 *     for the next change, so the grid stays editable and honest.
 */

const DEBOUNCE_MS = 800
const BASE_URL = '/api/users/me/preferences'

export const useUserPreferencesStore = defineStore('userPreferences', () => {
  const authStore = useAuthStore()
  const principalId = computed(() => authStore.principalId || null)

  const records = ref({}) // key → { value, updatedAt } — server-known state
  const loadState = ref('idle') // 'idle' | 'loading' | 'loaded' | 'failed'
  const loadError = ref(null) // string | null (never set on 401)
  const saveError = ref(null) // string | null (never set on 401)
  const serverGeneration = ref(0) // bumps when `records` changed from the server side

  let _loadPromise = null
  let _loadedFor = null
  const _pending = new Map() // key → { value, origin, identity, retried, timer }

  function _describe(e) {
    const status = e?.response?.status
    return status ? `HTTP ${status}` : 'network error'
  }

  function _setRecord(key, rec) {
    const next = { ...records.value }
    if (rec) next[key] = rec
    else delete next[key]
    records.value = next
  }

  /** The base a write must carry for `key` right now. */
  function baseFor(key) {
    return records.value[key]?.updatedAt ?? null
  }

  // ------------------------------------------------------------------ load

  async function load({ force = false } = {}) {
    const pid = principalId.value
    if (!pid) return
    if (!force && _loadedFor === pid && loadState.value === 'loaded') return
    if (_loadPromise) return _loadPromise
    loadState.value = 'loading'
    _loadPromise = (async () => {
      try {
        const res = await axios.get(BASE_URL, { headers: authStore.authHeader })
        if (principalId.value !== pid) return // identity moved mid-flight — discard
        const next = {}
        const prefs = res.data?.preferences || {}
        for (const [key, rec] of Object.entries(prefs)) {
          if (rec && typeof rec.value === 'object' && rec.value !== null) {
            next[key] = { value: rec.value, updatedAt: rec.updated_at }
          }
        }
        records.value = next
        _loadedFor = pid
        loadState.value = 'loaded'
        loadError.value = null
        serverGeneration.value++
        _flushQueued()
      } catch (e) {
        if (principalId.value !== pid) return
        loadState.value = 'failed'
        if (e?.response?.status !== 401) loadError.value = _describe(e)
      } finally {
        _loadPromise = null
      }
    })()
    return _loadPromise
  }

  // ------------------------------------------------------------------ save

  function save(key, value, { origin = 'gesture' } = {}) {
    const pid = principalId.value
    if (!pid) return
    const prev = _pending.get(key)
    if (prev?.timer) clearTimeout(prev.timer)
    const entry = {
      value,
      // A gesture stays a gesture even if a reconcile overwrites the value
      // before the debounce fires: the user DID act in this tab.
      origin: origin === 'gesture' || prev?.origin === 'gesture' ? 'gesture' : 'reconcile',
      identity: pid,
      retried: false,
      timer: null,
    }
    _pending.set(key, entry)
    if (loadState.value === 'loaded') _arm(key)
    // else: waits for load() to settle, then _flushQueued arms it
  }

  function _arm(key) {
    const entry = _pending.get(key)
    if (!entry) return
    entry.timer = setTimeout(() => {
      entry.timer = null
      _push(key)
    }, DEBOUNCE_MS)
  }

  function _flushQueued() {
    for (const key of _pending.keys()) _arm(key)
  }

  async function _push(key) {
    const entry = _pending.get(key)
    if (!entry) return
    _pending.delete(key)
    if (entry.identity !== principalId.value) return // made under another user — drop
    const base = baseFor(key)
    try {
      const res = await axios.put(
        `${BASE_URL}/${key}`,
        { value: entry.value, base_updated_at: base },
        { headers: authStore.authHeader }
      )
      if (entry.identity !== principalId.value) return
      _setRecord(key, { value: entry.value, updatedAt: res.data?.updated_at })
      saveError.value = null
    } catch (e) {
      if (entry.identity !== principalId.value) return
      const status = e?.response?.status
      if (status === 409) {
        const cur = e.response?.data?.detail?.current
        _setRecord(key, cur ? { value: cur.value, updatedAt: cur.updated_at } : null)
        if (entry.origin === 'gesture' && !entry.retried) {
          // This tab's edit wins, once, against the base we now know.
          _pending.set(key, { ...entry, retried: true, timer: null })
          return _push(key)
        }
        serverGeneration.value++ // consumer adopts the server record
        return
      }
      if (status === 401) return // session expired — the interceptor logs out
      saveError.value = _describe(e)
      // Keep the value for the next change / the next flush.
      if (!_pending.has(key)) _pending.set(key, { ...entry, timer: null })
    }
  }

  /** DELETE one key (a "Reset"). Immediate, not debounced. */
  async function remove(key) {
    const pid = principalId.value
    if (!pid) return false
    const prev = _pending.get(key)
    if (prev?.timer) clearTimeout(prev.timer)
    _pending.delete(key)
    try {
      await axios.delete(`${BASE_URL}/${key}`, { headers: authStore.authHeader })
      if (principalId.value !== pid) return false
      _setRecord(key, null)
      saveError.value = null
      return true
    } catch (e) {
      if (principalId.value !== pid) return false
      if (e?.response?.status !== 401) saveError.value = _describe(e)
      return false
    }
  }

  /**
   * Send every debounced write NOW, in a form that survives the page going
   * away. Called from `pagehide` by the consumer. `fetch` with `keepalive`
   * rather than the shared axios instance: an XHR is killed on tab discard
   * and `sendBeacon` cannot carry the bearer header — the same documented
   * exception `streamPortalExecution` makes for streaming (Invariant #7).
   */
  function flushNow({ keepalive = true } = {}) {
    if (loadState.value !== 'loaded') return
    if (!keepalive) {
      // The page is staying (a mode switch unmounted the grid): send through
      // the normal client so the echoed `updated_at` is recorded and the next
      // mount's write carries a current base instead of a guaranteed 409.
      for (const [key, entry] of [..._pending]) {
        if (entry.timer) clearTimeout(entry.timer)
        entry.timer = null
        _push(key)
      }
      return
    }
    if (typeof fetch !== 'function') return
    const pid = principalId.value
    for (const [key, entry] of _pending) {
      if (entry.timer) clearTimeout(entry.timer)
      if (entry.identity !== pid) continue
      try {
        fetch(`${BASE_URL}/${key}`, {
          method: 'PUT',
          keepalive: true,
          headers: { ...authStore.authHeader, 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: entry.value, base_updated_at: baseFor(key) }),
        }).catch(() => {})
      } catch {
        /* page is going away; nothing to surface */
      }
    }
    _pending.clear()
  }

  function hasPending(key) {
    return _pending.has(key)
  }

  // --------------------------------------------------------- identity change

  function _reset() {
    for (const entry of _pending.values()) {
      if (entry.timer) clearTimeout(entry.timer)
    }
    _pending.clear()
    records.value = {}
    loadState.value = 'idle'
    loadError.value = null
    saveError.value = null
    _loadedFor = null
    _loadPromise = null
  }

  // `flush: 'sync'`: the queue must be gone BEFORE anything else reacts to the
  // new identity, not on the next tick — a debounce timer could fire in
  // between and land under the wrong token.
  watch(principalId, () => _reset(), { flush: 'sync' })

  function dismissErrors() {
    loadError.value = null
    saveError.value = null
  }

  return {
    principalId,
    records,
    loadState,
    loadError,
    saveError,
    serverGeneration,
    baseFor,
    load,
    save,
    remove,
    flushNow,
    hasPending,
    dismissErrors,
    // test seams
    _pendingSize: () => _pending.size,
  }
})
