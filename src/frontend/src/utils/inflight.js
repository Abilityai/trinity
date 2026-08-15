// In-flight request de-duplication (#2198).
//
// THE PROBLEM this solves, precisely: two callers that ask for the same thing
// *concurrently* both hit the network. That is not what either existing
// precedent in this repo does, which is why this is a third mechanism rather
// than a reuse:
//
//   • `stores/executions.js:124` is a RESULT cache — it answers from a value
//     that has already arrived, so two simultaneous first-calls still both
//     issue a request. Every duplicate in #2198 is concurrent, so a result
//     cache does not touch them.
//   • `stores/fleetGrid.js:305` is an in-flight SKIP — the second caller
//     returns early with NO value. That is fine for a fire-and-forget tile
//     hydration and wrong here, because `AgentDetail.loadAgent()` needs the
//     agent object back.
//
// What is needed is an in-flight JOIN: the second caller receives the SAME
// promise and therefore the same value. This is that, and only that.
//
// Semantics, each load-bearing:
//
//   • JOIN, not skip — every caller resolves with the winner's value.
//   • Cleared in `finally` — the entry lives only for the flight. This is a
//     dedupe, NOT a cache: two SEQUENTIAL calls must still issue two requests.
//     `AgentDetail.waitForAgentStatus()` polls `fetchAgent` in a loop and would
//     be frozen forever by a stale entry.
//   • Rejection propagates to EVERY joiner and the entry is cleared, so one
//     failure never poisons the next attempt. (`fetchAgent` re-throws and
//     AgentDetail's 404 branch depends on that.)
//
// Scope note: deliberately NOT a global axios interceptor. That would be an
// app-wide behaviour change, would MASK genuine repeat-fetch bugs, must never
// apply to POSTs, and would change what e2e `page.route()` interceptors observe
// (`honest-failed-states.spec.js`, `circuit-breaker-badge.spec.js`).

const inFlight = new Map()

/**
 * Run `fn()` unless an identical call is already in flight, in which case join
 * that one and resolve with its value.
 *
 * @param {string} key  identity of the call — endpoint + its parameters
 * @param {() => Promise<T>} fn
 * @returns {Promise<T>}
 */
export function dedupe(key, fn) {
  const existing = inFlight.get(key)
  if (existing) return existing

  // `fn()` may throw synchronously; Promise.resolve().then keeps every path
  // returning a promise so a joiner can never receive a raw thrown value.
  const flight = Promise.resolve()
    .then(fn)
    .finally(() => {
      // Only clear our own entry. A `finally` that fires after a *newer* flight
      // was registered under the same key would otherwise evict the live one.
      if (inFlight.get(key) === flight) inFlight.delete(key)
    })

  inFlight.set(key, flight)
  return flight
}

// A strictly stronger form: an in-flight join PLUS a resolved-value cache.
//
// `dedupe` only collapses calls that overlap in time. Some duplicates do not:
// `GET /api/settings/feature-flags` is requested by two domain-scoped Pinia
// stores that parse disjoint slices of the same document, and they were
// measured 319 ms apart — far enough apart that the second call starts after
// the first has already resolved.
//
// This is deliberately NOT the default. It must only be used for a document
// that is immutable for the lifetime of a page load and whose refresh is
// explicit. Every agent-scoped endpoint is the opposite — agent state changes
// under you, and `AgentDetail.waitForAgentStatus()` polls `fetchAgent` in a
// loop expecting a fresh answer each time — so those stay on `dedupe`.
//
// A FAILURE is never cached: the stores fall back to safe defaults and a later
// caller must be able to retry.
const settled = new Map()

/**
 * Fetch once per page load (or once per `force`), joining any call already in
 * flight and answering later callers from the resolved value.
 *
 * @param {string} key
 * @param {() => Promise<T>} fn
 * @param {{force?: boolean}} [opts]
 * @returns {Promise<T>}
 */
export function once(key, fn, { force = false } = {}) {
  if (force) settled.delete(key)
  else if (settled.has(key)) return Promise.resolve(settled.get(key))

  return dedupe(key, fn).then((value) => {
    settled.set(key, value)
    return value
  })
}

/** Test/diagnostic helper — the number of calls currently in flight. */
export function inFlightCount() {
  return inFlight.size
}

/** Test helper — drop all entries. Never call this from product code. */
export function resetInFlight() {
  inFlight.clear()
  settled.clear()
}
