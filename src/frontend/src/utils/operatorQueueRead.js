/**
 * #3302 — read the operator queue so a count is never a window.
 *
 * `GET /api/operator-queue` is complete within `limit` and says how much more
 * there is: `total` (a COUNT over the page's own WHERE — `null` when the
 * route could not verify it), `has_more`, `next_offset`, and in walk mode
 * `next_cursor` (trinity-enterprise#815). Every operator surface used to read
 * ONE capped page and derive its counts and lists from it, so past the cap
 * the numbers stopped being true and nothing on screen said so. These are the
 * reads the surfaces use instead:
 *
 * - `readPendingQueue` — EVERY pending ask the caller may see (the actionable
 *   set), or a read that says it is partial.
 * - `readQueueSnapshot` — that set, plus the most recently ENDED asks and how
 *   many ended in all (the Operations page, `/m`).
 *
 * Both take `get(params) → Promise<{data}>`, the caller's own client — the
 * store's axios with its auth header, `/m`'s bounded `http`, `api` — so this
 * rule has one home and each surface keeps its transport and its 401 story.
 *
 * The order every read below relies on is the route's offset-mode order
 * (`db/operator_queue.py::list_items`, pinned by
 * `tests/unit/test_3302_queue_list_order.py`): every pending row FIRST — by
 * priority, then newest filed — then every ended row, newest ending first.
 * A cursor walk orders differently (rows that ended in its last five minutes
 * sort with the pending ones), so only `readPendingQueue` — always
 * `status=pending` — ever walks.
 */

/** The route's `limit` ceiling (`le=500`): one page answers a normal queue. */
export const QUEUE_PAGE_MAX = 500
/** Walk pages a read may take (10,000 asks at the ceiling); past it the read says it is partial. */
export const QUEUE_WALK_MAX_PAGES = 20
/** Rows read before the ended section, so asks that ended while the pending walk ran are not skipped. */
export const ENDED_READ_MARGIN = 50

function isPendingRow(item) {
  return item?.status === 'pending'
}

function bodyOf(res) {
  const data = res && res.data
  return data && typeof data === 'object' ? data : {}
}

function rowsOf(body) {
  return Array.isArray(body.items) ? body.items : []
}

/** A count the route verified, or `null` — never `count` (that is the page's length). */
export function pendingCountOf(body) {
  return Number.isFinite(body?.total) ? body.total : null
}

function uniqueById(rows) {
  const seen = new Set()
  return rows.filter((r) => (seen.has(r.id) ? false : (seen.add(r.id), true)))
}

/**
 * Every pending ask, or a read that says it is partial: `{ items, total, complete }`.
 *
 * Page 1 is an offset-mode read — no walk to start, so no Redis write and no
 * snapshot query (a walk keeps its priority snapshot in Redis for an hour) —
 * and a queue that fits it costs one request. Past one page the read restarts
 * as a keyset walk, which neither repeats nor skips an ask while the queue
 * changes; the walk's rows REPLACE page 1, so an ask answered in between is not
 * reported pending. "Complete" is as of the walk's start: an ask raised behind
 * the cursor is missed, and the WebSocket trigger it raised refetches.
 *
 * `total` is the number every header shows. A complete read IS the set, so it
 * is the set's size (a badge and its list can never disagree); a partial read's
 * only true count is the route's `total`, and an unverified one stays `null` —
 * the caller says so, it never shows 0.
 *
 * Partial (`complete: false`) when the walk cannot finish. At `maxPages`, or
 * on a page with more and no cursor (a legacy row's key too long to carry), it
 * returns what it walked. If the walk fails at all (503: no Redis; 410:
 * expired) it returns page 1, which is still true, rather than failing the
 * refresh. A failed page 1 rejects.
 */
export async function readPendingQueue(get, { params = {}, pageLimit = QUEUE_PAGE_MAX, maxPages = QUEUE_WALK_MAX_PAGES } = {}) {
  const base = { ...params, status: 'pending', limit: pageLimit }
  const first = bodyOf(await get(base))
  if (!first.has_more) {
    const items = rowsOf(first)
    return { items, total: items.length, complete: true }
  }
  let page = first
  let walked = []
  try {
    let cursor = 'start'
    for (let pages = 0; cursor && pages < maxPages; pages += 1) {
      page = bodyOf(await get({ ...base, cursor }))
      walked = walked.concat(rowsOf(page))
      cursor = page.has_more ? page.next_cursor : null
    }
  } catch {
    return { items: rowsOf(first), total: pendingCountOf(first), complete: false }
  }
  const items = uniqueById(walked)
  if (!page.has_more) return { items, total: items.length, complete: true }
  return { items, total: pendingCountOf(page), complete: false }
}

/** `a − b`, or `null` when either is unknown; never below `floor`. */
function minus(a, b, floor) {
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null
  return Math.max(floor, a - b)
}

/**
 * The queue as the Operations page and `/m` show it:
 * `{ pending, pendingTotal, pendingComplete, ended, endedTotal, flags }`.
 *
 * Page 1 is exactly the read these surfaces always made (every row, `limit:
 * pageLimit`), so a normal queue still costs one request:
 * - A — no more rows: page 1 is the whole queue.
 * - B — page 1 reached an ended row: every pending ask is in it (pending rows
 *   sort first). The ended window is topped up from the rows after page 1,
 *   which are all ended. A row raised between the two reads shifts them by
 *   one; the merge drops the duplicate.
 * - C — page 1 is all pending: `readPendingQueue` walks them, then the ended
 *   window is read after the pending section, starting `ENDED_READ_MARGIN`
 *   rows early. An ask the later read shows ended is ended (it was answered
 *   while the walk ran) and leaves the pending set.
 *
 * No single `status` filter means "ended", so the ended rows always come from
 * unfiltered reads, filtered here. `endedTotal` is `null` whenever an input
 * total is — never a negative or an invented number. `flags` come from page 1:
 * the route computes them over the caller's agents, whatever the filters.
 */
export async function readQueueSnapshot(get, {
  pageLimit,
  endedLimit,
  walkPageLimit = QUEUE_PAGE_MAX,
  maxPages = QUEUE_WALK_MAX_PAGES,
} = {}) {
  const first = bodyOf(await get({ limit: pageLimit }))
  const rows = rowsOf(first)
  const flags = {
    undelivered: Number(first.undelivered_count) || 0,
    closedByFiler: Number(first.closed_by_filer_count) || 0,
  }
  let pending = rows.filter(isPendingRow)
  let ended = rows.filter((r) => !isPendingRow(r))

  if (!first.has_more) {
    return {
      pending, pendingTotal: pending.length, pendingComplete: true,
      ended: ended.slice(0, endedLimit), endedTotal: ended.length, flags,
    }
  }

  if (ended.length > 0) { // B
    if (ended.length < endedLimit) {
      const more = rowsOf(bodyOf(await get({ limit: endedLimit - ended.length, offset: pageLimit })))
      const pendingIds = new Set(pending.map((r) => r.id))
      ended = uniqueById(ended.concat(more.filter((r) => !isPendingRow(r) && !pendingIds.has(r.id))))
    }
    return {
      pending, pendingTotal: pending.length, pendingComplete: true,
      ended: ended.slice(0, endedLimit),
      endedTotal: minus(pendingCountOf(first), pending.length, Math.min(ended.length, endedLimit)),
      flags,
    }
  }

  // C
  const walk = await readPendingQueue(get, { pageLimit: walkPageLimit, maxPages })
  // A partial walk whose page could not verify `total` does not say where the
  // pending section ends; a one-row read's `total` still does (its single row
  // is rarely the one withheld). The offset is never null: axios drops a null
  // param, which would read from 0 and show an empty Resolved window.
  const counted = walk.complete || walk.total != null
    ? null
    : pendingCountOf(bodyOf(await get({ status: 'pending', limit: 1 })))
  const walkTotal = walk.total ?? counted
  const sqlPending = walkTotal ?? walk.items.length
  // The margin absorbs asks that ended while the walk ran. If more than that
  // ended first (another operator's Clear All), the newest of them sit before
  // the offset and stay in `pending` for this read; the ending's own WebSocket
  // trigger queues the next read, which corrects it.
  const margin = Math.min(sqlPending, ENDED_READ_MARGIN)
  const after = bodyOf(await get({ limit: endedLimit + margin, offset: sqlPending - margin }))
  const endedNow = uniqueById(rowsOf(after).filter((r) => !isPendingRow(r)))
  const endedIds = new Set(endedNow.map((r) => r.id))
  pending = walk.items.filter((r) => !endedIds.has(r.id))
  const moved = walk.items.length - pending.length
  const pendingTotal = walk.complete ? pending.length : minus(walkTotal, moved, pending.length)
  ended = endedNow.slice(0, endedLimit)
  return {
    pending, pendingTotal, pendingComplete: walk.complete,
    ended, endedTotal: minus(pendingCountOf(after), pendingTotal, ended.length), flags,
  }
}

// --------------------------------------------------------------------- copy

const n = (value) => Number(value).toLocaleString()

/** The Operations subtitle's queue part: "3 pending responses", "812 pending responses (500 shown)". */
export function pendingResponsesPhrase({ count, shown, complete = true, exact = true }) {
  const head = exact ? n(count) : `${n(shown)}+`
  const noun = exact && count === 1 ? 'response' : 'responses'
  return complete ? `${head} pending ${noun}` : `${head} pending ${noun} (${n(shown)} shown)`
}

/**
 * The Resolved tab's stated total (design-system principle 28):
 * "340 resolved · latest 200 shown", or `null` when there is nothing to state.
 * `shown` is what is on screen, which an optimistic answer can grow past the
 * fetched total until the refetch lands.
 */
export function resolvedPhrase({ total, shown }) {
  if (!Number.isFinite(total)) return shown > 0 ? `Latest ${n(shown)} resolved` : null
  const all = Math.max(total, shown)
  if (all === 0) return null
  return shown < all ? `${n(all)} resolved · latest ${n(shown)} shown` : `${n(all)} resolved`
}

/** `/m`'s note under a partial pending list. */
export function pendingPartialLine({ shown, total }) {
  return Number.isFinite(total)
    ? `Showing ${n(shown)} of ${n(total)} pending`
    : `Showing the first ${n(shown)} pending — the total could not be read`
}
