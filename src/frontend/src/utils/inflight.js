// In-flight request de-duplication (#2198).
//
// THE PROBLEM this solves, precisely: two callers that ask for the same thing
// *concurrently* both hit the network.
//
// Three prior art points in this repo, and why none of them is simply reused:
//
//   • `src/api.js:71` (`deduplicatedGet`, PERF-269) is THE CLOSEST — it is a
//     real in-flight join, keyed on URL+params, cleared in `.finally()`. It is
//     not reused because it only covers callers that go through the `api`
//     instance, and the calls in question use raw `axios` with an explicit
//     `authStore.authHeader` (the widespread Invariant #7 deviation). Moving
//     them onto `api` would have been the smaller diff, and was rejected for a
//     specific reason: `api`'s response interceptor hard-redirects to `/login`
//     on ANY 401, whereas AgentDetail deliberately renders its own error banner
//     and its own 404 not-found panel (#1914). Migrating the store is a real
//     change of failure behaviour on a page whose failure behaviour was
//     deliberately built, and it belongs in an Invariant #7 cleanup, not in a
//     request-count fix.
//   • `stores/executions.js:124` is a RESULT cache — it answers from a value
//     that has already arrived, so two simultaneous first-calls still both
//     issue a request.
//   • `stores/fleetGrid.js:305` is an in-flight SKIP — the second caller
//     returns early with NO value. Fine for a fire-and-forget tile hydration,
//     wrong here, because `AgentDetail.loadAgent()` needs the agent object back.
//
// So: same shape as `api.js`, reachable from a raw-`axios` caller, plus the
// `once()` variant below, which `api.js` has no equivalent of and which the
// staggered duplicates need.
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

// A join that never hands back a STALE answer (#4671).
//
// `dedupe` is right for a read nothing else is changing. The Workspace's lists
// are not that: a turn ends, an ask is answered, a chat is marked read — and
// the refresh that follows exists to show exactly that change. Joining a read
// that LEFT BEFORE the change would resolve the refresh with the old list:
// the unread badge the mark-read just cleared comes back, the reply's thread
// keeps its old place, and nothing corrects it until the next 20 s tick.
//
// So a flight remembers the change epoch it left under. `markChanged()` moves
// the epoch — the Workspace store calls it when a write is sent and again when
// it settles, and when a turn ends. Then:
//
//   • same epoch  → JOIN: nothing the caller could be asking about has
//     happened since the flight left. This is the turn-end burst, the 20 s
//     tick meeting a tab-focus refresh, a title-settle tick meeting either.
//   • older epoch → WAIT for that flight, then read again ONCE — and every
//     caller that arrives meanwhile shares that one read. Waiting rather than
//     racing a second request also keeps the answers in order: the older one
//     can no longer land last and overwrite the newer.
//
// A SCOPE narrows a change to the reads it can affect. Marking a chat read
// moves unread counts and nothing else, and it is sent on every turn end: if
// it moved the one shared epoch, the thread list and the rooms list would be
// re-read behind it for no reason. `markChanged('chat-state')` moves only the
// reads issued under that scope; `markChanged()` moves all of them.
//
// A rejection reaches every joiner and nothing is kept, exactly as `dedupe`.
let changeEpoch = 0
const scopeEpochs = new Map()
const freshFlights = new Map()   // key → { promise, epoch, next, nextEpoch }

const epochOf = (scope) => (scope ? `${changeEpoch}/${scopeEpochs.get(scope) || 0}` : `${changeEpoch}`)

/**
 * Something the Workspace's reads describe has changed (or is changing).
 * @param {string|null} [scope]  only the reads issued under this scope; omit for all
 */
export function markChanged(scope = null) {
  if (scope) scopeEpochs.set(scope, (scopeEpochs.get(scope) || 0) + 1)
  else changeEpoch += 1
}

/**
 * Run `fn()` unless a read under the same key left after the latest change,
 * in which case join it. See the note above.
 *
 * @param {string} key  identity of the read — endpoint + parameters + session
 * @param {() => Promise<T>} fn
 * @param {string|null} [scope]  the scope whose changes also make this read stale
 * @returns {Promise<T>}
 */
export function joinFresh(key, fn, scope = null) {
  const current = freshFlights.get(key)
  if (!current) return startFresh(key, fn, scope)
  const now = epochOf(scope)
  if (current.epoch === now) return current.promise
  if (current.next && current.nextEpoch === now) return current.next
  current.nextEpoch = now
  // The older flight's own outcome is its callers' business, not ours.
  current.next = current.promise.then(() => {}, () => {}).then(() => joinFresh(key, fn, scope))
  return current.next
}

function startFresh(key, fn, scope) {
  const entry = { epoch: epochOf(scope), next: null, nextEpoch: null }
  entry.promise = Promise.resolve()
    .then(fn)
    .finally(() => {
      if (freshFlights.get(key) === entry) freshFlights.delete(key)
    })
  freshFlights.set(key, entry)
  return entry.promise
}

/** Test/diagnostic helper — the number of calls currently in flight. */
export function inFlightCount() {
  return inFlight.size + freshFlights.size
}

/** Test helper — drop all entries. Never call this from product code. */
export function resetInFlight() {
  inFlight.clear()
  settled.clear()
  freshFlights.clear()
  scopeEpochs.clear()
  changeEpoch = 0
}
