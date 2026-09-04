/**
 * A bounded wrapper over the GLOBAL axios, for surfaces that deliberately do
 * not use `api.js` (#2446).
 *
 * `/m` has its own auth and 401 story — `main.js` excludes it from the global
 * interceptor's redirect — so it calls the raw `axios` global, which carries
 * **no timeout**. Only the `api.js` instance sets one (30 s). A request that
 * never settles therefore never reaches its `catch`/`finally`, which is not
 * merely a stuck spinner: `/m` holds per-control in-flight guards
 * (`respondingItems[id]`, `togglingAgents[name]`, `actionLoading`, …), and PR
 * #2378 additionally exempts an in-flight operator-queue id from
 * `pruneQueueItemState`. One hung POST leaves that card's Send disabled and its
 * state unprunable for the life of the tab.
 *
 * Why not `axios.create()`: `stores/auth.js` authenticates by mutating
 * `axios.defaults.headers.common.Authorization` at login and deleting it at
 * logout. `create()` snapshots defaults at construction, so a module-level
 * instance would miss a later sign-in and keep a stale header after sign-out.
 * These wrappers call the global per request, so the header, the base config
 * and the response interceptor all still resolve exactly as before — the only
 * thing added is a bound.
 *
 * Why not `axios.defaults.timeout`: that is a process-wide mutation reaching
 * every other surface, including ones with legitimately long requests.
 *
 * A caller-supplied `timeout` wins (the spread is after the default), so a
 * genuinely long request stays expressible.
 */
import axios from 'axios'

// Matches `api.js`'s instance timeout. Kept equal on purpose: two operator
// surfaces answering "how long before we give up" differently is a difference
// nobody can explain at 3am.
export const REQUEST_TIMEOUT_MS = 30000

function bounded(config) {
  return { timeout: REQUEST_TIMEOUT_MS, ...(config || {}) }
}

export const http = {
  get: (url, config) => axios.get(url, bounded(config)),
  post: (url, data, config) => axios.post(url, data, bounded(config)),
  put: (url, data, config) => axios.put(url, data, bounded(config)),
  patch: (url, data, config) => axios.patch(url, data, bounded(config)),
  delete: (url, config) => axios.delete(url, bounded(config)),
}

export default http
