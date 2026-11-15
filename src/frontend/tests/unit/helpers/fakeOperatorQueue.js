/**
 * #3302 — an in-memory `GET /api/operator-queue` for the frontend specs.
 *
 * It answers the way the route does, so a reader that only works against a
 * kinder fake fails here:
 *   - ORDER (`db/operator_queue.py::list_items`): every pending row first, by
 *     priority, then newest filed; then every ended row, newest ENDING first;
 *     `id` last.
 *   - filters `status`, `priority`, `agent_name`; offset paging with
 *     `total` / `has_more` / `next_offset`; a keyset walk (`cursor=start`, then
 *     each `next_cursor`) that resumes after the last key it returned, so a row
 *     raised behind the cursor is missed exactly as the real walk misses it;
 *   - the two flags on every page, whatever the filters.
 *
 * `opts` injects what the route can do to a reader: `totalNull` (the projection
 * belt fired; `totalNullWhen(params)` limits it to the reads it matches), `walkError` (a status the walk answers with — 503 when Redis is
 * down — on `start` or on a later page), `uncarriable` (a page that has more but
 * cannot issue a cursor), `fail(params, n)` (reject this call), and
 * `between(n, rows)` — runs after the n-th call (1-based) so a spec can raise,
 * answer or clear rows between two reads of one fetch.
 */
const RANK = { critical: 0, high: 1, medium: 2, low: 3 }

export function endingTime(row) {
  return row.disposed_at || row.responded_at || row.created_at || ''
}

function sortKey(row) {
  const pending = row.status === 'pending'
  return [
    pending ? 0 : 1,
    pending ? (RANK[row.priority] ?? 4) : 0,
    pending ? (row.created_at || '') : endingTime(row),
    row.id,
  ]
}

function compareKeys(a, b) {
  if (a[0] !== b[0]) return a[0] - b[0]
  if (a[1] !== b[1]) return a[1] - b[1]
  if (a[2] !== b[2]) return a[2] < b[2] ? 1 : -1 // newest first
  if (a[3] === b[3]) return 0
  return a[3] < b[3] ? -1 : 1
}

export function routeOrder(rows) {
  return [...rows].sort((a, b) => compareKeys(sortKey(a), sortKey(b)))
}

function refuse(status) {
  return Promise.reject({ response: { status, data: { detail: `fake: ${status}` } } })
}

export function makeQueue(rows, opts = {}) {
  const state = { rows: rows.map((r) => ({ ...r })), calls: [] }

  state.get = (params = {}) => {
    state.calls.push({ ...params })
    const n = state.calls.length
    const answer = () => {
      if (opts.fail && opts.fail(params, n)) {
        return Promise.reject(new Error(`fake: call ${n} failed`))
      }
      const matching = routeOrder(state.rows.filter((r) =>
        (params.status == null || r.status === params.status)
        && (params.priority == null || r.priority === params.priority)
        && (params.agent_name == null || r.agent_name === params.agent_name)))
      const limit = params.limit ?? 100
      const flags = {
        undelivered_count: opts.undelivered ?? 0,
        closed_by_filer_count: opts.closedByFiler ?? 0,
      }
      const total = opts.totalNull || (opts.totalNullWhen && opts.totalNullWhen(params)) ? null : matching.length
      let page
      let hasMore
      let nextOffset = null
      let nextCursor = null
      if (params.cursor != null) {
        if (params.offset) return refuse(422)
        const starting = params.cursor === 'start'
        if (opts.walkError && (opts.walkError.on === 'any'
            || (opts.walkError.on === 'start') === starting)) {
          return refuse(opts.walkError.status)
        }
        const after = starting ? null : JSON.parse(params.cursor)
        const rest = after ? matching.filter((r) => compareKeys(sortKey(r), after) > 0) : matching
        page = rest.slice(0, limit)
        hasMore = rest.length > limit
        if (hasMore && !opts.uncarriable) nextCursor = JSON.stringify(sortKey(page[page.length - 1]))
      } else {
        const offset = params.offset ?? 0
        page = matching.slice(offset, offset + limit)
        hasMore = matching.length > offset + limit
        if (hasMore) nextOffset = offset + limit
      }
      return Promise.resolve({
        data: {
          items: page.map((r) => ({ ...r })),
          count: page.length,
          total,
          has_more: hasMore,
          next_offset: nextOffset,
          next_cursor: nextCursor,
          ...flags,
        },
      })
    }
    const out = answer()
    if (opts.between) {
      // after this call is answered, before the reader issues the next one
      return out.then(
        (res) => { opts.between(n, state.rows); return res },
        (err) => { opts.between(n, state.rows); throw err },
      )
    }
    return out
  }

  return state
}

/** `count` rows, ids `${prefix}-0001`…, spread across `agents` agents. */
export function rowsOf(count, { prefix = 'p', status = 'pending', priority = 'medium', agents = 1, at = '2026-10-01T00:00:00Z', ...over } = {}) {
  const base = Date.parse(at)
  return Array.from({ length: count }, (_, i) => {
    const stamp = new Date(base + i * 1000).toISOString()
    const row = {
      id: `${prefix}-${String(i + 1).padStart(4, '0')}`,
      agent_name: `agent-${(i % agents) + 1}`,
      type: 'question',
      status,
      priority,
      title: `${prefix} ${i + 1}`,
      question: 'q',
      created_at: stamp,
      ...over,
    }
    if (status !== 'pending') row.disposed_at = row.disposed_at || stamp
    return row
  })
}
